"""References to values that cannot change after they are minted, and the port that resolves them.

An `ArtifactRef` is an address plus the digest of what lives there. It carries no URL and no
transport. Whoever holds one asks a resolver, and the resolver answers with the exact bytes the
digest names or refuses.

One value has exactly one canonical spelling here: sorted keys, no spaces, no escaping and no
nonfinite numbers. A digest therefore depends only on the value.

This module does not depend on the workflow engine.
"""

from __future__ import annotations

import hashlib
import json

from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, JsonValue, StringConstraints, model_validator

MAX_CANONICAL_BYTES = 1_000_000
"""The largest canonical value in bytes a reference may address. A resolver refuses anything past it."""

ArtifactMediaType = Literal["text/markdown", "application/json", "text/plain"]
"""How the value is meant to be read."""

ArtifactText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
"""One identifier inside a reference: trimmed, never blank, and bounded so an address stays an address."""

ArtifactDigest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
"""SHA-256 of a value's canonical bytes, lowercase hex and nothing else."""


def canonical_bytes(value: JsonValue) -> bytes:
    """`value` as the one byte string that stands for it.

    Keys are sorted, there are no separator spaces and text is written as UTF-8, not escaped.

    Args:
        value: The JSON value to encode.

    Returns:
        The canonical UTF-8 bytes.

    Raises:
        ValueError: `value` holds a nonfinite number, which has no JSON spelling.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def artifact_digest(value: JsonValue) -> str:
    """The SHA-256 of `value`'s canonical bytes, lowercase hex.

    Args:
        value: The JSON value to digest.

    Returns:
        The 64-character hex digest.
    """
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def text_media(media_type: ArtifactMediaType) -> bool:
    """Whether this media type promises a string, so a caller can render it without parsing.

    Args:
        media_type: The media type to test.

    Returns:
        True for the text media types.
    """
    return media_type in ("text/markdown", "text/plain")


class ArtifactRef(BaseModel):
    """The address of one immutable value, and the digest that says which value it is.

    Mint one only once the value it names is fixed somewhere a reader can reach it again.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: ArtifactText
    """Which declared artifact of the run this is."""
    label: ArtifactText
    """What to call it in front of a person."""
    run_id: ArtifactText
    node_id: ArtifactText
    """Which node produced the value, or `parameter:<name>` for a value wired from a parameter."""
    turn: ArtifactText | None = None
    """Which turn of a map produced it, when the wire named one."""
    value_digest: ArtifactDigest
    media_type: ArtifactMediaType


class ArtifactDocument(BaseModel):
    """One resolved reference: the address asked for, and the value it actually addresses.

    The value is checked against its reference on construction. A document that exists has a digest,
    media type and size that agree with its reference.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: ArtifactRef
    value: JsonValue

    @model_validator(mode="after")
    def _value_is_what_the_reference_addresses(self) -> ArtifactDocument:
        """Refuse a value whose bytes, type or size disagree with the reference carrying it."""
        if text_media(self.ref.media_type) and not isinstance(self.value, str):
            msg = f"media type {self.ref.media_type!r} carries a string, and this value is {type(self.value).__name__}"
            raise ValueError(msg)
        payload = canonical_bytes(self.value)
        if len(payload) > MAX_CANONICAL_BYTES:
            msg = f"the canonical value is {len(payload)} bytes, past the {MAX_CANONICAL_BYTES} a reference may address"
            raise ValueError(msg)
        if hashlib.sha256(payload).hexdigest() != self.ref.value_digest:
            msg = "the value does not match the digest its reference addresses"
            raise ValueError(msg)
        return self


class ArtifactUnavailableError(LookupError):
    """The reference addresses nothing this resolver can answer with, and why."""


class ArtifactResolver(Protocol):
    """Turns a reference into the value it names, or refuses."""

    async def resolve(self, ref: ArtifactRef) -> ArtifactDocument:
        """The document `ref` addresses.

        Args:
            ref: The reference to resolve.

        Returns:
            The document, already checked against `ref`.

        Raises:
            ArtifactUnavailableError: nothing here answers for that address, or what answers
                disagrees with the reference.
        """
        ...
