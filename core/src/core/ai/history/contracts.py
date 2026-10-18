"""The public surface of the record: its ports, the continuation types and refusals, its reads and its Postgres stores."""

from core.ai.history.domain import ContinuationAttempt, ContinuationCommit, ContinuationRef
from core.ai.history.exceptions import (
    ContinuationConflictError,
    ContinuationInFlightError,
    ContinuationPayloadTooLargeError,
    ContinuationUnsafeReplayError,
)
from core.ai.history.infrastructure import reads
from core.ai.history.infrastructure.checkpoints import PostgresContinuationStore
from core.ai.history.infrastructure.database import PoolDatabase
from core.ai.history.infrastructure.postgres_step_store import PostgresStepStore
from core.ai.history.infrastructure.rows import PositionRow, row_as, rows_as
from core.ai.history.infrastructure.schema import create_record_schema
from core.ai.history.internal_contracts import Connection, ContinuationCheckpointStore, Database, RequestParametersSink
from core.ai.history.message_key import message_key
from core.ai.history.read_models import EventAtPositionRow, Jsonb, RecordedStep, RunRow
from core.ai.history.recording import recording_capabilities
from core.ai.history.step_usage import StepUsage, StepUsageTap, to_chunk

__all__ = [
    "Connection",
    "ContinuationAttempt",
    "ContinuationCheckpointStore",
    "ContinuationCommit",
    "ContinuationConflictError",
    "ContinuationInFlightError",
    "ContinuationPayloadTooLargeError",
    "ContinuationRef",
    "ContinuationUnsafeReplayError",
    "Database",
    "EventAtPositionRow",
    "Jsonb",
    "PoolDatabase",
    "PositionRow",
    "PostgresContinuationStore",
    "PostgresStepStore",
    "RecordedStep",
    "RequestParametersSink",
    "RunRow",
    "StepUsage",
    "StepUsageTap",
    "create_record_schema",
    "message_key",
    "reads",
    "recording_capabilities",
    "row_as",
    "rows_as",
    "to_chunk",
]
