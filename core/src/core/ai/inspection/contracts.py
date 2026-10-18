"""The public surface of the inspection: what code outside the slice imports."""

from core.ai.inspection.attribute_lead import AttributeLeadHandler
from core.ai.inspection.dispatch import Dispatch
from core.ai.inspection.domain.evaluation import (
    ComparisonReport,
    DiagnosticDataset,
    DiagnosticPrediction,
    DiagnosticReport,
    DiagnosticResponse,
    TaskExecution,
    evaluate_diagnostics,
)
from core.ai.inspection.domain.facts import ToolFacts
from core.ai.inspection.domain.leads import Lead
from core.ai.inspection.domain.learning import LearningAttempt, LearningRecord
from core.ai.inspection.domain.taxonomy import TAXONOMY, Finding, Taxonomy
from core.ai.inspection.domain.watches import Watch
from core.ai.inspection.infrastructure.postgres import InspectionStore
from core.ai.inspection.infrastructure.record_events import RecordEventsOverDatabase
from core.ai.inspection.infrastructure.schema import create_inspection_schema
from core.ai.inspection.inspect_run import InspectRun, InspectRunHandler
from core.ai.inspection.inspector import (
    INSPECTOR_INSTRUCTIONS,
    InspectorDeps,
    NoIssue,
    TierAgent,
    Undecided,
    ask,
    inspector_agent,
    inspector_chat_agent,
    model_name,
    resolve,
)
from core.ai.inspection.internal_contracts import InspectionQueries, InspectRuns, RecordEvents
from core.ai.inspection.lanes import (
    ATTRIBUTE_LEAD_KIND,
    ATTRIBUTE_LEAD_POLICY,
    INSPECT_RUN_KIND,
    INSPECT_RUN_POLICY,
    INSPECTOR_AGENT_NAME,
    SCAN_RUN_KIND,
    SCAN_RUN_POLICY,
    AttributeLead,
    JobLanes,
    ScanRun,
    inspect_run_key,
    scan_run_key,
)
from core.ai.inspection.read_models import LeadResolution, StoredBlame, StoredFinding, StoredLead, StoredQuestion
from core.ai.inspection.scan_run import (
    SCAN_INSTRUCTIONS,
    SCAN_PROMPT,
    NothingSeen,
    ScanRunHandler,
    ScanUndecided,
    candidates,
    resolve_scan,
    scan_agent,
)
from core.ai.inspection.sweep import Sweep, SweepPass
from core.ai.inspection.views import label_of, outline, outline_steps, plain_whys, shown, step_at, text_of, turns_for, view_for

__all__ = [
    "ATTRIBUTE_LEAD_KIND",
    "ATTRIBUTE_LEAD_POLICY",
    "INSPECTOR_AGENT_NAME",
    "INSPECTOR_INSTRUCTIONS",
    "INSPECT_RUN_KIND",
    "INSPECT_RUN_POLICY",
    "SCAN_INSTRUCTIONS",
    "SCAN_PROMPT",
    "SCAN_RUN_KIND",
    "SCAN_RUN_POLICY",
    "TAXONOMY",
    "AttributeLead",
    "AttributeLeadHandler",
    "ComparisonReport",
    "DiagnosticDataset",
    "DiagnosticPrediction",
    "DiagnosticReport",
    "DiagnosticResponse",
    "Dispatch",
    "Finding",
    "InspectRun",
    "InspectRunHandler",
    "InspectRuns",
    "InspectionQueries",
    "InspectionStore",
    "InspectorDeps",
    "JobLanes",
    "Lead",
    "LeadResolution",
    "LearningAttempt",
    "LearningRecord",
    "NoIssue",
    "NothingSeen",
    "RecordEvents",
    "RecordEventsOverDatabase",
    "ScanRun",
    "ScanRunHandler",
    "ScanUndecided",
    "StoredBlame",
    "StoredFinding",
    "StoredLead",
    "StoredQuestion",
    "Sweep",
    "SweepPass",
    "TaskExecution",
    "Taxonomy",
    "TierAgent",
    "ToolFacts",
    "Undecided",
    "Watch",
    "ask",
    "candidates",
    "create_inspection_schema",
    "evaluate_diagnostics",
    "inspect_run_key",
    "inspector_agent",
    "inspector_chat_agent",
    "label_of",
    "model_name",
    "outline",
    "outline_steps",
    "plain_whys",
    "resolve",
    "resolve_scan",
    "scan_agent",
    "scan_run_key",
    "shown",
    "step_at",
    "text_of",
    "turns_for",
    "view_for",
]
