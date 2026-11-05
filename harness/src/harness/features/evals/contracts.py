"""The public surface of the evals slice: what code outside the slice imports."""

from harness.features.evals.domain.benchmark import (
    BenchmarkCases,
    BenchmarkInputs,
    BenchmarkRequest,
    DevelopmentReplay,
    OutcomeObjective,
    OutcomeStateReport,
    SealedComparison,
)
from harness.features.evals.domain.pilot import PilotParticipant
from harness.features.evals.domain.step_evaluation import bind_collection, case_result, report_failed
from harness.features.evals.exceptions import CaseRunRefusedError
from harness.features.evals.internal_contracts import BenchmarkArms, BenchmarkRecord, CaseReports, ComposedBenchmark
from harness.features.evals.service.benchmark import BenchmarkMount, budgeted, run_outcome_benchmark
from harness.features.evals.service.case_runs import perform_step_cases
from harness.features.evals.service.pilot import WorkflowAutomationTaskRunner
from harness.features.evals.service.step_evaluation import resolve_step, run_step_evals

__all__ = [
    "BenchmarkArms",
    "BenchmarkCases",
    "BenchmarkInputs",
    "BenchmarkMount",
    "BenchmarkRecord",
    "BenchmarkRequest",
    "CaseReports",
    "CaseRunRefusedError",
    "ComposedBenchmark",
    "DevelopmentReplay",
    "OutcomeObjective",
    "OutcomeStateReport",
    "PilotParticipant",
    "SealedComparison",
    "WorkflowAutomationTaskRunner",
    "bind_collection",
    "budgeted",
    "case_result",
    "perform_step_cases",
    "report_failed",
    "resolve_step",
    "run_outcome_benchmark",
    "run_step_evals",
]
