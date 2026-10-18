"""What the record keeps of a step, and the attempts, commits and checkpoints of a durable turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue, NonNegativeInt, PositiveInt

from core.ai.artifacts import artifact_digest, canonical_bytes
from core.ai.history.exceptions import ContinuationPayloadTooLargeError

if TYPE_CHECKING:
    from datetime import datetime

    from pydantic_ai_harness.step_persistence import SnapshotState

CONTINUATION_OUTPUT_BYTES: Final[int] = 1_000_000
"""How many UTF-8 bytes one committed output may canonicalize to. Past it, the commit is refused."""

CONTINUATION_REF_BYTES: Final[int] = 512
"""How many UTF-8 bytes a serialized `ContinuationRef` may take. Past it, the ref is refused rather than returned."""


@dataclass(frozen=True)
class StepEntry:
    """Which message one step gave the model, in which of its forms and which of its stamp versions."""

    key: str
    """The message's key, which survives edits to its content."""
    form: int
    """Which of the message's forms the step carried, 0 for the first."""
    stamp: int
    """Which of the message's stamp versions the step carried, 0 for the first."""


@dataclass(frozen=True)
class SnapshotAdditions:
    """What one snapshot added to the record: messages, forms and instructions not stored before."""

    new_messages: int
    """How many messages the snapshot stored for the first time."""
    new_forms: int
    """How many new forms it stored for messages already held, each one an edit."""
    new_instructions: int
    """How many instructions texts it stored for the first time."""


@dataclass(frozen=True)
class StoredStep:
    """One snapshot of a run, in order, with the state it was saved in."""

    run_id: str
    seq: int
    """The store's own number for the step, unique and increasing within the run."""
    step_index: int
    """The step number the snapshot carries."""
    at: datetime
    """When the snapshot was taken."""
    state: SnapshotState
    entries: tuple[StepEntry, ...]
    """Every message the model was given at this step, in order."""


class ContinuationRef(BaseModel):
    """Where a conversation stands: the revision reached, and the exact step the messages are read from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    conversation_id: UUID
    revision: PositiveInt
    """The revision the conversation reached, 1 after its first commit."""
    run_id: str
    """The run that wrote the step."""
    snapshot_seq: NonNegativeInt
    """The step's sequence in that run."""
    checkpoint_digest: str
    """The digest of the messages the step holds."""


class ContinuationAttempt(BaseModel):
    """One turn's claim on a conversation: who it is, which revision it believes is current, and the run it will write.

    A commit joins the registered run and refuses a mismatch.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    conversation_id: UUID
    attempt_id: str
    """The attempt's own id. A turn delivered twice carries the same one."""
    expected_revision: NonNegativeInt
    """The revision the attempt believes is current, 0 before the conversation's first commit."""
    run_id: str
    """The run the attempt registers in the transcript once it starts."""


class ContinuationCommit(BaseModel):
    """What a settled attempt holds: the revision it reached, and the output it committed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: ContinuationRef
    """Where the conversation stands once the attempt settled."""
    output: JsonValue
    """What the turn produced, as JSON."""


class ContinuationCheckpoint(BaseModel):
    """The step a commit points at: its sequence in its run, and the digest of the messages it holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_seq: NonNegativeInt
    """The step's sequence in its run."""
    checkpoint_digest: str
    """The digest of the messages the step holds."""


def canonical_output(output: JsonValue) -> str:
    """One committed output as the text it is stored and addressed by.

    Keys are sorted, there are no separator spaces and text is written as UTF-8, not escaped.

    Args:
        output: What the turn produced, as JSON.

    Returns:
        The canonical text.

    Raises:
        ContinuationPayloadTooLargeError: the text takes more than `CONTINUATION_OUTPUT_BYTES` in UTF-8.
        ValueError: the output holds a nonfinite number, which has no JSON spelling.
    """
    document = canonical_bytes(output).decode()
    size = len(document.encode())
    if size > CONTINUATION_OUTPUT_BYTES:
        msg = f"this output canonicalizes to {size} bytes, past the {CONTINUATION_OUTPUT_BYTES} a continuation may carry"
        raise ContinuationPayloadTooLargeError(msg)
    return document


def output_digest(output: JsonValue) -> str:
    """The content address of one committed output: the SHA-256 of its canonical text.

    Args:
        output: What the turn produced, as JSON.

    Returns:
        The 64-character lowercase hex digest.

    Raises:
        ContinuationPayloadTooLargeError: the text takes more than `CONTINUATION_OUTPUT_BYTES` in UTF-8.
        ValueError: the output holds a nonfinite number, which has no JSON spelling.
    """
    canonical_output(output)
    return artifact_digest(output)
