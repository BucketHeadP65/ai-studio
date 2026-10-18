"""The public surface of the workflow engine: what code outside the slice imports.

The Temporal names load on first use, so importing this module never imports `temporalio`.
"""

from typing import TYPE_CHECKING

from core.ai.artifacts import ArtifactRef
from core.ai.workflows.authoring import build_type
from core.ai.workflows.birth import check_birth, shapes_of
from core.ai.workflows.contract_port import WorkflowContract
from core.ai.workflows.domain.document import SpecDocument, StepDocument, load_spec_document, load_step_document
from core.ai.workflows.domain.evidence import Approval, Grade, RunEvidence, approved_gates, orchestrated_gates, trust_level
from core.ai.workflows.domain.fingerprint import WorkflowFingerprint, drift, fingerprint, spec_identity
from core.ai.workflows.domain.identity import (
    CURRENT_STEP,
    ReviewedArtifacts,
    StepIdentity,
    TurnFacts,
    canonical_reviewed_value,
    performing_step,
    reviewed_value_digest,
)
from core.ai.workflows.domain.intent import WorkflowIntent, intent_issues
from core.ai.workflows.domain.lineage import (
    Lineage,
    Prediction,
    PredictionOutcome,
    adapt,
    carried_trust,
    grade,
    moved_nodes,
    prediction_record,
)
from core.ai.workflows.domain.outcome import GateReview, MapBatch, Outcome, OutcomeStatus, WorkItemFact, observed_outcome
from core.ai.workflows.domain.parameter_values import WorkflowArguments, normalize_arguments
from core.ai.workflows.domain.presentation import (
    KNOWLEDGE_BASE_CONTROL,
    ArtifactActionSlot,
    InputPresentation,
    ResultFieldPresentation,
    WorkflowPresentation,
    verify_presentation,
)
from core.ai.workflows.domain.run_events import (
    DecisionSource,
    GateDecided,
    GateDecisionFact,
    GateReached,
    RecordedRunEvent,
    RetryFeedback,
    ReviewedArtifactRef,
    RunEvent,
    RunFinished,
    RunLaunched,
    RunPaused,
    RunResumed,
    RunStopped,
    decode_run_event,
    encode_run_event,
)
from core.ai.workflows.domain.task import TaskContract, contract_of
from core.ai.workflows.domain.validator import Prepared, WiringIssue, prepare, unsatisfied_parameters, validate
from core.ai.workflows.domain.vocabulary import (
    AgentStep,
    Briefing,
    FromNode,
    FromParameter,
    InlineStep,
    JudgeGate,
    MapNode,
    Node,
    OrchestratorGate,
    ParameterSpec,
    PersonGate,
    Program,
    ResultArtifactSpec,
    RunNode,
    Shape,
    Tier,
    Wire,
    Workflow,
    WorkflowResultSpec,
    referenced_steps,
)
from core.ai.workflows.exceptions import (
    AdaptationRefusedError,
    ArgumentRefusedError,
    DocumentError,
    LibraryError,
    ParameterSchemaError,
    PresentationRefusedError,
    RegistrationError,
    RetryExhaustedError,
    SaveRefusedError,
    WorkflowUnknownError,
)
from core.ai.workflows.graph_builder import GateDecision, WorkflowState, build_graph, run_workflow
from core.ai.workflows.infrastructure.evidence_log import FileEvidenceLog
from core.ai.workflows.infrastructure.library_collection import LibraryCollection, step_page_id
from core.ai.workflows.internal_contracts import (
    AgentResolver,
    CodeRunner,
    EvidenceLog,
    Ingest,
    OrchestratorDecider,
    PerformStep,
    Rows,
    RunObserver,
    SaveGuard,
    WorkflowLibrary,
)
from core.ai.workflows.read_models import SavedWorkflow
from core.ai.workflows.registry import StepRegistry
from core.ai.workflows.service import save, verify_savable
from core.ai.workflows.voice import process_instructions
from core.lazy_imports import lazy_module_attributes

if TYPE_CHECKING:
    from core.ai.workflows.infrastructure.temporal import (
        DEFAULT_TASK_QUEUE,
        DurableWorkflowRun,
        GateVerdict,
        RunRequest,
        StepConversationContinuation,
        connect,
    )

__all__ = [
    "CURRENT_STEP",
    "DEFAULT_TASK_QUEUE",
    "KNOWLEDGE_BASE_CONTROL",
    "AdaptationRefusedError",
    "AgentResolver",
    "AgentStep",
    "Approval",
    "ArgumentRefusedError",
    "ArtifactActionSlot",
    "ArtifactRef",
    "Briefing",
    "CodeRunner",
    "DecisionSource",
    "DocumentError",
    "DurableWorkflowRun",
    "EvidenceLog",
    "FileEvidenceLog",
    "FromNode",
    "FromParameter",
    "GateDecided",
    "GateDecision",
    "GateDecisionFact",
    "GateReached",
    "GateReview",
    "GateVerdict",
    "Grade",
    "Ingest",
    "InlineStep",
    "InputPresentation",
    "JudgeGate",
    "LibraryCollection",
    "LibraryError",
    "Lineage",
    "MapBatch",
    "MapNode",
    "Node",
    "OrchestratorDecider",
    "OrchestratorGate",
    "Outcome",
    "OutcomeStatus",
    "ParameterSchemaError",
    "ParameterSpec",
    "PerformStep",
    "PersonGate",
    "Prediction",
    "PredictionOutcome",
    "Prepared",
    "PresentationRefusedError",
    "Program",
    "RecordedRunEvent",
    "RegistrationError",
    "ResultArtifactSpec",
    "ResultFieldPresentation",
    "RetryExhaustedError",
    "RetryFeedback",
    "ReviewedArtifactRef",
    "ReviewedArtifacts",
    "Rows",
    "RunEvent",
    "RunEvidence",
    "RunFinished",
    "RunLaunched",
    "RunNode",
    "RunObserver",
    "RunPaused",
    "RunRequest",
    "RunResumed",
    "RunStopped",
    "SaveGuard",
    "SaveRefusedError",
    "SavedWorkflow",
    "Shape",
    "SpecDocument",
    "StepConversationContinuation",
    "StepDocument",
    "StepIdentity",
    "StepRegistry",
    "TaskContract",
    "Tier",
    "TurnFacts",
    "Wire",
    "WiringIssue",
    "WorkItemFact",
    "Workflow",
    "WorkflowArguments",
    "WorkflowContract",
    "WorkflowFingerprint",
    "WorkflowIntent",
    "WorkflowLibrary",
    "WorkflowPresentation",
    "WorkflowResultSpec",
    "WorkflowState",
    "WorkflowUnknownError",
    "adapt",
    "approved_gates",
    "build_graph",
    "build_type",
    "canonical_reviewed_value",
    "carried_trust",
    "check_birth",
    "connect",
    "contract_of",
    "decode_run_event",
    "drift",
    "encode_run_event",
    "fingerprint",
    "grade",
    "intent_issues",
    "load_spec_document",
    "load_step_document",
    "moved_nodes",
    "normalize_arguments",
    "observed_outcome",
    "orchestrated_gates",
    "performing_step",
    "prediction_record",
    "prepare",
    "process_instructions",
    "referenced_steps",
    "reviewed_value_digest",
    "run_workflow",
    "save",
    "shapes_of",
    "spec_identity",
    "step_page_id",
    "trust_level",
    "unsatisfied_parameters",
    "validate",
    "verify_presentation",
    "verify_savable",
]

_LAZY_IMPORTS = {
    "DEFAULT_TASK_QUEUE": "core.ai.workflows.infrastructure.temporal",
    "DurableWorkflowRun": "core.ai.workflows.infrastructure.temporal",
    "GateVerdict": "core.ai.workflows.infrastructure.temporal",
    "RunRequest": "core.ai.workflows.infrastructure.temporal",
    "StepConversationContinuation": "core.ai.workflows.infrastructure.temporal",
    "connect": "core.ai.workflows.infrastructure.temporal",
}


__getattr__, __dir__ = lazy_module_attributes(__name__, _LAZY_IMPORTS, __all__)
