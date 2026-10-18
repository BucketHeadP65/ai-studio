"""Delivered evidence: the exact bytes a trial's tools handed the model, kept immutable.

A grader judges what execution actually saw, not what a source says now. Every tool result is
serialized once, hashed over those exact UTF-8 bytes, and stored under that hash before the value
reaches the model. A stored payload is never rewritten: appending the same bytes under the same
identity returns the same reference, and different bytes under that identity are refused.

Two size bounds keep one trial's record readable and bounded. One payload is at most
`DELIVERED_PAYLOAD_BYTES` and one trial at most `TRIAL_DELIVERED_EVIDENCE_BYTES`. Crossing either
one invalidates the trial. Nothing is truncated, because a grader reading a shortened payload would
be grading evidence the model never saw.

`DeliveredSource` records what a payload was about: the canonical reference of each source in the
payload, plus that source's content hash and version when the result itself carries them. Missing
integrity metadata stays `None`. It is never derived from a snippet, a fetched body, or a later read
of the live source, so a grade that depends on source integrity reads `unknown` instead of a guess.
"""

from __future__ import annotations

from pydantic import Field

from core.ai.evaluation.domain.models import EvaluationModel, Fingerprint, Nonempty

DELIVERED_PAYLOAD_BYTES = 262_144
"""The largest single delivered payload, in UTF-8 bytes."""

TRIAL_DELIVERED_EVIDENCE_BYTES = 1_048_576
"""The largest total delivered payload bytes one trial may accumulate."""


class DeliveredSource(EvaluationModel):
    """One source a delivered payload carried, and how much of its integrity was observable.

    `source_content_sha256` and `source_version` stay `None` unless the result itself supplied
    them. They are facts about the source at delivery time, never recomputed from a snippet or from
    a later read.
    """

    source_ref: Nonempty
    source_content_sha256: Fingerprint | None = None
    source_version: Nonempty | None = None


class DeliveredEvidenceRef(EvaluationModel):
    """The immutable handle to one stored payload: where it sits and exactly what it must be.

    A reader resolves the payload through `evidence_id`, then refuses anything whose byte count or
    hash differs from what this reference names.
    """

    evidence_id: Nonempty
    ordinal: int = Field(ge=0)
    payload_sha256: Fingerprint
    payload_bytes: int = Field(ge=1, le=DELIVERED_PAYLOAD_BYTES)
    sources: tuple[DeliveredSource, ...] = ()


class DeliveredEvidence(EvaluationModel):
    """One stored payload beside the reference that names it, as it was handed to the model."""

    ref: DeliveredEvidenceRef
    payload_json: Nonempty
