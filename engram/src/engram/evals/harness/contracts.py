"""The public surface of the retrieval evaluation harness."""

from engram.evals.harness.agent import build_agent, make_task
from engram.evals.harness.connection_failures import connection_failure_message
from engram.evals.harness.domain.evaluators import AnswerAccuracy, RetrievalMetrics
from engram.evals.harness.domain.experiments import ExperimentAxes, ExperimentAxis, ExperimentRun, ExperimentVariant, expand_variants
from engram.evals.harness.infrastructure.instrumentation import (
    configure_logfire,
    enable_local_span_recording,
    experiment_span,
    resolve_logfire_settings,
)
from engram.evals.harness.infrastructure.policy_files import ApplyReport, apply_policy
from engram.evals.harness.internal_contracts import VariantEvaluator
from engram.evals.harness.service import (
    EvaluationRunConfig,
    EvaluationRunReport,
    ResultsFileNotResumableError,
    TracedEvaluationRun,
    average_metrics,
    run_eval,
    run_eval_on_dataset,
    run_experiment,
    run_traced_eval_on_dataset,
)

__all__ = [
    "AnswerAccuracy",
    "ApplyReport",
    "EvaluationRunConfig",
    "EvaluationRunReport",
    "ExperimentAxes",
    "ExperimentAxis",
    "ExperimentRun",
    "ExperimentVariant",
    "ResultsFileNotResumableError",
    "RetrievalMetrics",
    "TracedEvaluationRun",
    "VariantEvaluator",
    "apply_policy",
    "average_metrics",
    "build_agent",
    "configure_logfire",
    "connection_failure_message",
    "enable_local_span_recording",
    "expand_variants",
    "experiment_span",
    "make_task",
    "resolve_logfire_settings",
    "run_eval",
    "run_eval_on_dataset",
    "run_experiment",
    "run_traced_eval_on_dataset",
]
