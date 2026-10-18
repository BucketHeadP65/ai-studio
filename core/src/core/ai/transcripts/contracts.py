"""The public surface of transcripts: the shape a detector reads, the loaders by scheme, the run ports and the record's adapters."""

from core.ai.transcripts.exceptions import NOTHING_TO_READ, UNREADABLE, RunEmptyError, SchemeUnknownError, unreadable_reason
from core.ai.transcripts.infrastructure.record import RecordRunsOfConversation, RecordTranscriptLoader, record_source
from core.ai.transcripts.internal_contracts import RunFacts, RunsOfConversation
from core.ai.transcripts.loaders import TranscriptLoaders
from core.ai.transcripts.models import Call, Span, Step, Transcript
from core.ai.transcripts.read_models import RunSummary

__all__ = [
    "NOTHING_TO_READ",
    "UNREADABLE",
    "Call",
    "RecordRunsOfConversation",
    "RecordTranscriptLoader",
    "RunEmptyError",
    "RunFacts",
    "RunSummary",
    "RunsOfConversation",
    "SchemeUnknownError",
    "Span",
    "Step",
    "Transcript",
    "TranscriptLoaders",
    "record_source",
    "unreadable_reason",
]
