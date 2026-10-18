"""The step identity every step call runs under, so a step can read who it is being performed for.

The workflow engine publishes it on `CURRENT_STEP` for exactly one step call, and a step only reads it.
A background task a step spawns sees the identity published when that task's context was captured,
never a sibling call's identity.

A call that owes a repair carries the rejections it owes it to, and a reader for the exact values
those rejections were about.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import JsonValue, TypeAdapter
from pydantic_core import to_jsonable_python

from core.ai.artifacts import canonical_bytes
from core.ai.workflows.domain.run_events import RetryFeedback, ReviewedArtifactRef
from core.fingerprints import content_fingerprint

if TYPE_CHECKING:
    from collections.abc import Generator

    from core.ai.workflows.domain.vocabulary import Tier
    from core.ai.workflows.internal_contracts import ReviewedArtifactQueries


_JSON_VALUE = TypeAdapter(JsonValue)


def canonical_reviewed_value(value: object) -> str:
    """`value` as canonical JSON text: sorted keys, no spaces, no NaN.

    One value always gets one spelling, however it was built.

    Args:
        value: The value a gate reviews.

    Returns:
        The canonical JSON text.

    Raises:
        PydanticSerializationError: `value` holds something no JSON type covers.
        ValueError: `value` holds a NaN or an infinite float.
    """
    return canonical_bytes(_JSON_VALUE.validate_python(to_jsonable_python(value))).decode("utf-8")


def reviewed_value_digest(canonical: str) -> str:
    """The SHA-256 of a canonical reviewed value's UTF-8 bytes.

    Args:
        canonical: The value as `canonical_reviewed_value` spells it.

    Returns:
        The digest as lowercase hex.
    """
    return content_fingerprint(canonical)


@dataclass
class ReviewedArtifacts:
    """One run's reviewed values, held by the full reference each was issued under.

    Written once per reviewed value, before anyone is asked to decide, and never overwritten: the
    repair that follows a rejection reads exactly the bytes the decider saw, whatever the run has
    produced since.
    """

    run_id: str = ""
    values: dict[ReviewedArtifactRef, str] = field(default_factory=dict)

    def persist(self, node_id: str, turn: str | None, value: object) -> ReviewedArtifactRef:
        """Capture a value before a gate decides on it, and answer with its reference.

        A value already captured under the same reference keeps its first bytes.

        Args:
            node_id: The node that produced the value, or `parameter:<name>` for a value wired from a parameter.
            turn: The turn of a map that produced it, or `None` when the wire named none.
            value: The value the gate reviews.

        Returns:
            The reference the value is held under.

        Raises:
            PydanticSerializationError: `value` holds something no JSON type covers.
            ValueError: `value` holds a NaN or an infinite float.
        """
        canonical = canonical_reviewed_value(value)
        ref = ReviewedArtifactRef(run_id=self.run_id, node_id=node_id, turn=turn, value_digest=reviewed_value_digest(canonical))
        self.values.setdefault(ref, canonical)
        return ref

    def read(self, ref: ReviewedArtifactRef) -> JsonValue:
        """The value `ref` addresses.

        Args:
            ref: The reference `persist` answered with.

        Returns:
            The value, read back from the bytes the decider saw.

        Raises:
            ValueError: `ref` names another run, names nothing this run captured, or addresses
                bytes whose digest no longer matches.
        """
        if ref.run_id != self.run_id:
            msg = "The reviewed value belongs to another run."
            raise ValueError(msg)
        canonical = self.values.get(ref)
        if canonical is None:
            msg = "The reviewed value was not recorded for this run and node."
            raise ValueError(msg)
        if reviewed_value_digest(canonical) != ref.value_digest:
            msg = "The reviewed value no longer matches its digest."
            raise ValueError(msg)
        return _JSON_VALUE.validate_json(canonical)


@dataclass(frozen=True)
class TurnFacts:
    """What a step performing one turn of a multi-turn map needs to know about the sequence."""

    work: str
    index: int
    turns: tuple[str, ...]
    each: str
    tools: tuple[str, ...]
    """The union of every turn's declared tools, for a host to mount once at turn one."""
    outputs: tuple[type, ...]
    """Every turn's output type in turn order, for a host to hold one output surface for the whole
    conversation."""
    skills: tuple[str, ...] = ()
    """The union of every turn's declared skills, sorted, for a host to mount once at turn one."""


@dataclass(frozen=True)
class StepIdentity:
    """Which node, item and turn a step is being performed for, published on `CURRENT_STEP`."""

    node_id: str
    item_key: str | None = None
    turn: TurnFacts | None = None
    tier: Tier | None = None
    retry_feedback: tuple[RetryFeedback, ...] = ()
    """Every rejection this call owes a repair to, oldest first. Empty for an ordinary call."""
    parameters_json: str = "{}"
    """The run's own parameters as canonical JSON, so a repair can restate the original request.
    `{}` when no repair is owed."""
    reviewed_artifacts: ReviewedArtifactQueries | None = None
    """Where the values named by `retry_feedback` are read back from."""


CURRENT_STEP: ContextVar[StepIdentity | None] = ContextVar("core_ai_workflows_current_step", default=None)


@contextmanager
def performing_step(identity: StepIdentity) -> Generator[None]:
    """Publish `identity` on `CURRENT_STEP` for the duration of one step call.

    A step reads `CURRENT_STEP` and never calls this.

    Args:
        identity: The node, item and turn the call is performed for.

    Yields:
        Nothing. `identity` stays published until the block exits.
    """
    token = CURRENT_STEP.set(identity)
    try:
        yield
    finally:
        CURRENT_STEP.reset(token)
