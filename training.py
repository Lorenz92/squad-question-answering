import collections
import time
import os

import numpy as np
import sklearn
import torch
import transformers
from transformers.trainer_pt_utils import nested_detach
from transformers.trainer_utils import (
    EvalPrediction,
    PREFIX_CHECKPOINT_DIR,
    PredictionOutput,
    speed_metrics,
)
from transformers.integrations import WandbCallback
from transformers.file_utils import ENV_VARS_TRUE_VALUES, is_torch_tpu_available

import utils


class SquadTrainer(transformers.Trainer):
    """
    Custom HuggingFace Trainer class
    """

    def __init__(self, *args, **kwargs):
        kwargs["compute_metrics"] = self.compute_metrics
        super(SquadTrainer, self).__init__(*args, **kwargs)
        self.remove_callback(WandbCallback)
        self.add_callback(CustomWandbCallback)

    def compute_loss(self, model, inputs):
        """
        Override loss computation to calculate and log metrics
        during training
        """
        outputs = model(**inputs)

        # Custom logging steps (to log training metrics)
        if (self.state.global_step == 1 and self.args.logging_first_step) or (
            self.args.logging_steps > 0
            and self.state.global_step > 0
            and self.state.global_step % self.args.logging_steps == 0
        ):
            labels = None
            has_labels = all(inputs.get(k) is not None for k in self.label_names)
            if has_labels:
                labels = nested_detach(
                    tuple(inputs.get(name) for name in self.label_names)
                )
                if len(labels) == 1:
                    labels = labels[0]

            # Compute and log metrics only if labels are available
            if labels is not None:
                metrics = self.compute_metrics(
                    EvalPrediction(
                        predictions=outputs["token_outputs"], label_ids=labels
                    )
                )
                self.log(metrics)

        # Save past state if it exists
        # TODO: this needs to be fixed and made cleaner later.
        if self.args.past_index >= 0:
            self._past = outputs[self.args.past_index]
        # We don't use .loss here since the model may return tuples instead of ModelOutput.
        return outputs["loss"] if isinstance(outputs, dict) else outputs[0]

    def compute_metrics(self, eval_prediction):
        """
        Custom function that computes task-specific
        training and evaluation metrics
        """
        # Labels are stored as a single tensor
        # (concatenation of answer start and answer end)
        labels = eval_prediction.label_ids
        preds = eval_prediction.predictions
        if isinstance(preds, tuple):
            preds = preds[0]
        labels = utils.get_nearest_answers(labels, preds, device=self.args.device)

        # Ensure to work with numpy arrays
        if isinstance(labels, torch.Tensor):
            labels = labels.cpu().numpy()
        if isinstance(preds, torch.Tensor):
            preds = preds.cpu().numpy()
        f_labels, f_preds = labels.flatten(), preds.flatten()

        # Return a dictionary of metrics, as required by the Trainer
        return {
            "f1": sklearn.metrics.f1_score(f_labels, f_preds, average="macro"),
            "accuracy": sklearn.metrics.accuracy_score(f_labels, f_preds),
            "em": self.exact_match(labels, preds),
        }

    def exact_match(self, labels, preds):
        """
        Compute the EM score of the SQuAD competition,
        measuring the number of exactly matched answers
        """
        assert labels.shape == preds.shape
        total = labels.shape[0]
        matches = np.count_nonzero((labels == preds).all(axis=1))
        return matches / total

    def predict(self, test_dataset, ignore_keys=None, metric_key_prefix="test"):
        """
        Run prediction and returns predictions and potential metrics
        """
        if test_dataset is not None and not isinstance(
            test_dataset, collections.abc.Sized
        ):
            raise ValueError("test_dataset must implement __len__")

        # Test the model with the given dataloader and gather outputs
        test_dataloader = self.get_test_dataloader(test_dataset)
        start_time = time.time()
        output = self.prediction_loop(
            test_dataloader,
            description="Test",
            ignore_keys=ignore_keys,
            metric_key_prefix=metric_key_prefix,
        )

        # Compute answers (taking spans from original contexts)
        answers_dict = dict()
        for span, index in zip(
            output.predictions[1].tolist(), output.predictions[2].tolist()
        ):
            answers_dict[
                test_dataset.df.loc[index, "question_id"]
            ] = test_dataset.df.loc[index, "context"][span[0] : span[1] + 1]

        # Update metrics and patch the predictions attribute
        # with the computed answers
        output.metrics.update(
            speed_metrics(metric_key_prefix, start_time, len(test_dataset))
        )
        return PredictionOutput(
            predictions=output.predictions + (answers_dict,),
            label_ids=output.label_ids,
            metrics=output.metrics,
        )


class CustomWandbCallback(WandbCallback):
    def __init__(self):
        super().__init__()

    def setup(self, args, state, model, reinit, **kwargs):

        if self._wandb is None:
            return
        self._initialized = True
        if state.is_world_process_zero:
            combined_dict = {**args.to_sanitized_dict()}

            if hasattr(model, "config") and model.config is not None:
                model_config = model.config.to_dict()
                combined_dict = {**model_config, **combined_dict}
            trial_name = state.trial_name
            init_args = {}
            if trial_name is not None:
                run_name = trial_name
                init_args["group"] = args.run_name
            else:
                run_name = args.run_name

            self._wandb.init(
                project=os.getenv("WANDB_PROJECT", "squad-qa"),
                group=os.getenv("WANDB_RUN_GROUP", None),
                config=combined_dict,
                name=run_name,
                reinit=True,
                **init_args,
            )

            # keep track of model topology and gradients, unsupported on TPU
            if not is_torch_tpu_available() and os.getenv("WANDB_WATCH") != "false":
                self._wandb.watch(
                    model,
                    log=os.getenv("WANDB_WATCH", "gradients"),
                    log_freq=max(100, args.logging_steps),
                )

            # log outputs
            self._log_model = os.getenv(
                "WANDB_LOG_MODEL", "FALSE"
            ).upper() in ENV_VARS_TRUE_VALUES.union({"TRUE"})

    def on_epoch_begin(self, args, state, control, **kwargs):
        self._save_checkpoint(args.output_dir, state.global_step)

    def on_train_end(self, args, state, control, **kwargs):
        self._save_checkpoint(args.output_dir, state.global_step)

    def _save_checkpoint(self, output_dir, step):
        checkpoint_path = f"{output_dir}/checkpoint-{step}"
        if os.path.exists(checkpoint_path):
            self._wandb.save(f"{checkpoint_path}/*")
