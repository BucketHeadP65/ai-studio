"""The public surface of the running slice: what code outside the slice imports.

The launch functions, the agent tools and the gate decider load on first use, so `harness.session`
can import this module while those modules import `harness.session`.
"""

from typing import TYPE_CHECKING

from core.lazy_imports import lazy_module_attributes

from harness.features.running.api_models import AutomodeRequest, Decision, ResumeRequest
from harness.features.running.conversations import StepConversation, StepConversations, conversation_key
from harness.features.running.domain.progress import GateVerdict, LiveProgress, PendingReview
from harness.features.running.exceptions import (
    RunEngineUnavailableError,
    RunGraphRefusedError,
    RunStateRefusedError,
    WorkflowNotRunnableError,
)
from harness.features.running.infrastructure.durable import DurableTurns
from harness.features.running.internal_contracts import GateAsker, RunArchive, RunArchiveQueries, SpendLog, SpendLogQueries
from harness.features.running.records import RunCollaborators, RunEndObserver, RunRecords, SpendLimit, spend_limits
from harness.features.running.spend import SCHEDULE_RUNNER_PREFIX, rows_for, schedule_runner, schedule_spend, workflow_spend
from harness.features.running.spend_report import DEFAULT_SINCE_HOURS, Scope, SpendReporter
from harness.features.running.step_stream import StepSteerDelivery, run_relayed

if TYPE_CHECKING:
    from harness.features.running.feedback import orchestrator_decider
    from harness.features.running.service import (
        decide_session_gate,
        install_engine_reachable,
        launch,
        pause_session_run,
        resume_session_run,
        run_phase,
        set_session_automode,
        start,
        stop_session_run,
    )
    from harness.features.running.tools import check_runs, message_worker, recent_runs, run_values, run_workflow, spend_report, stop_run

__all__ = [
    "DEFAULT_SINCE_HOURS",
    "SCHEDULE_RUNNER_PREFIX",
    "AutomodeRequest",
    "Decision",
    "DurableTurns",
    "GateAsker",
    "GateVerdict",
    "LiveProgress",
    "PendingReview",
    "ResumeRequest",
    "RunArchive",
    "RunArchiveQueries",
    "RunCollaborators",
    "RunEndObserver",
    "RunEngineUnavailableError",
    "RunGraphRefusedError",
    "RunRecords",
    "RunStateRefusedError",
    "Scope",
    "SpendLimit",
    "SpendLog",
    "SpendLogQueries",
    "SpendReporter",
    "StepConversation",
    "StepConversations",
    "StepSteerDelivery",
    "WorkflowNotRunnableError",
    "check_runs",
    "conversation_key",
    "decide_session_gate",
    "install_engine_reachable",
    "launch",
    "message_worker",
    "orchestrator_decider",
    "pause_session_run",
    "recent_runs",
    "resume_session_run",
    "rows_for",
    "run_phase",
    "run_relayed",
    "run_values",
    "run_workflow",
    "schedule_runner",
    "schedule_spend",
    "set_session_automode",
    "spend_limits",
    "spend_report",
    "start",
    "stop_run",
    "stop_session_run",
    "workflow_spend",
]

_LAZY_IMPORTS = {
    "check_runs": "harness.features.running.tools",
    "decide_session_gate": "harness.features.running.service",
    "install_engine_reachable": "harness.features.running.service",
    "launch": "harness.features.running.service",
    "pause_session_run": "harness.features.running.service",
    "message_worker": "harness.features.running.tools",
    "orchestrator_decider": "harness.features.running.feedback",
    "recent_runs": "harness.features.running.tools",
    "resume_session_run": "harness.features.running.service",
    "run_phase": "harness.features.running.service",
    "run_values": "harness.features.running.tools",
    "run_workflow": "harness.features.running.tools",
    "set_session_automode": "harness.features.running.service",
    "spend_report": "harness.features.running.tools",
    "start": "harness.features.running.service",
    "stop_run": "harness.features.running.tools",
    "stop_session_run": "harness.features.running.service",
}


__getattr__, __dir__ = lazy_module_attributes(__name__, _LAZY_IMPORTS, __all__)
