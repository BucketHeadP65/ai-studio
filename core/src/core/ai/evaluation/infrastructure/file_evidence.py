"""Delivered evidence as immutable files under one POSIX state root.

Each trial owns a directory named by the SHA-256 of its id, holding one `<payload_sha256>.json`
file per payload, a `manifest.json` of the references filed so far with their running byte total,
and a `.lock` file. A payload file is created with an exclusive create, so the bytes under a hash
are written once and never replaced. The manifest is swapped in whole through a temporary file, so
a reader sees one complete manifest or the previous one, never a half-written one.

Every append holds the trial's cooperative lock across reloading the manifest, checking both size
bounds, creating the payload and swapping the manifest back. Two processes appending to one trial
therefore accumulate against one byte total rather than each against its own stale copy. Trials
never share a lock, so concurrent trials under one state root stay independent.

A payload's bytes are written and flushed to a temporary file inside the trial directory and linked
into place only once every byte is on disk, so a crash mid-write leaves a stray temporary rather
than a short file sitting at the content address. Linking fails when the address is already taken,
which keeps the write-once property.

A read resolves the payload's path from the reference alone, then refuses unless the file exists,
the manifest still names that exact reference, and the bytes match both the recorded length and the
recorded hash. It never returns partial content and never falls back to a live source.

Every failure surfaces as a `DeliveredEvidenceError` subclass. A record that cannot be parsed reads
as unreadable and an IO failure reads as a persistence failure, so a caller folding one trial's
evidence to unknown never has to catch a filesystem or validation error to do it.
"""

from __future__ import annotations

import asyncio
import hashlib

from typing import TYPE_CHECKING, Self

from pydantic import Field, model_validator

from core.ai.evaluation.domain.evidence import (
    DELIVERED_PAYLOAD_BYTES,
    TRIAL_DELIVERED_EVIDENCE_BYTES,
    DeliveredEvidence,
    DeliveredEvidenceRef,
    DeliveredSource,
)
from core.ai.evaluation.domain.models import EvaluationModel, Nonempty, require_unique
from core.ai.evaluation.exceptions import (
    DeliveredEvidenceBoundError,
    DeliveredEvidenceConflictError,
    DeliveredEvidencePersistenceError,
    DeliveredEvidenceUnreadableError,
)
from core.files import atomic_create, atomic_write, exclusive_lock
from core.fingerprints import content_fingerprint

if TYPE_CHECKING:
    from pathlib import Path

_EVIDENCE_DIRECTORY = "delivered-evidence"
_MANIFEST_NAME = "manifest.json"
_LOCK_NAME = ".lock"
_PAYLOAD_SUFFIX = ".json"

# An evidence id names its trial's directory and the position within that trial, so a reference
# alone resolves to exactly one file without consulting anything else first.
_EVIDENCE_ID_SEPARATOR = ":"


class TrialEvidenceManifest(EvaluationModel):
    """One trial's filed references and the payload bytes they have already admitted."""

    trial_id: Nonempty
    total_bytes: int = Field(default=0, ge=0, le=TRIAL_DELIVERED_EVIDENCE_BYTES)
    entries: tuple[DeliveredEvidenceRef, ...] = ()

    @model_validator(mode="after")
    def one_entry_per_position(self) -> Self:
        """Refuse a manifest naming one position twice, which would hide one of the payloads.

        Returns:
            The manifest, unchanged.

        Raises:
            ValueError: An evidence id appears twice.
        """
        require_unique((entry.evidence_id for entry in self.entries), "evidence ids")
        return self


def evidence_id_for(trial_id: str, ordinal: int) -> str:
    """The identity of one trial's `ordinal`-th delivered payload.

    Args:
        trial_id: The trial that delivered the payload.
        ordinal: The payload's position in the trial.

    Returns:
        The evidence id.
    """
    return f"{content_fingerprint(trial_id)}{_EVIDENCE_ID_SEPARATOR}{ordinal}"


class FileDeliveredEvidenceStore:
    """`DeliveredEvidenceStore` over one state root shared by co-located processes.

    The root holds one directory per trial. Nothing outside the root is read or written, and no
    entry is ever removed, so a trial graded after a restart reads the same bytes execution saw.
    """

    def __init__(self, state_root: Path) -> None:
        """Keep every trial's evidence under one state root.

        Args:
            state_root: The root every participating process shares. Evidence goes under its
                `delivered-evidence` folder.
        """
        self.state_root = state_root

    async def append(
        self, trial_id: str, ordinal: int, payload_json: str, *, sources: tuple[DeliveredSource, ...]
    ) -> DeliveredEvidenceRef:
        """Store `payload_json` as the trial's `ordinal`-th delivered payload and name it.

        Args:
            trial_id: The trial that delivered the payload.
            ordinal: The payload's position in the trial.
            payload_json: The exact serialized payload handed to the model.
            sources: The sources the payload carried.

        Returns:
            The reference that names the stored payload.

        Raises:
            DeliveredEvidenceBoundError: The payload is empty or crosses a size bound.
            DeliveredEvidenceConflictError: This trial and ordinal already name different bytes.
            DeliveredEvidenceUnreadableError: The trial's own record is no longer readable.
            DeliveredEvidencePersistenceError: The payload could not be written.
        """
        return await asyncio.to_thread(self._append, trial_id, ordinal, payload_json, sources)

    async def read(self, ref: DeliveredEvidenceRef) -> DeliveredEvidence:
        """The payload `ref` names, byte-identical to what was stored.

        Args:
            ref: The reference to resolve.

        Returns:
            The stored payload beside its reference.

        Raises:
            DeliveredEvidenceUnreadableError: The payload or its manifest entry is missing or differs from `ref`.
            DeliveredEvidencePersistenceError: The stored payload could not be read at all.
        """
        return await asyncio.to_thread(self._read, ref)

    def _trial_directory(self, trial_digest: str) -> Path:
        return self.state_root / _EVIDENCE_DIRECTORY / trial_digest

    def _load_manifest(self, directory: Path, trial_id: str) -> TrialEvidenceManifest:
        try:
            content = (directory / _MANIFEST_NAME).read_text(encoding="utf-8")
        except FileNotFoundError:
            return TrialEvidenceManifest(trial_id=trial_id)
        return _parsed_manifest(content, trial_id)

    def _append(self, trial_id: str, ordinal: int, payload_json: str, sources: tuple[DeliveredSource, ...]) -> DeliveredEvidenceRef:
        payload = payload_json.encode("utf-8")
        directory = self._trial_directory(content_fingerprint(trial_id))
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with exclusive_lock(directory / _LOCK_NAME):
                return self._append_locked(directory, trial_id, ordinal, payload, sources)
        except OSError as error:
            msg = f"delivered evidence for trial {trial_id!r} could not be written: {error}"
            raise DeliveredEvidencePersistenceError(msg) from error

    def _append_locked(
        self, directory: Path, trial_id: str, ordinal: int, payload: bytes, sources: tuple[DeliveredSource, ...]
    ) -> DeliveredEvidenceRef:
        """Admit these bytes against the trial's own manifest, or refuse without writing anything."""
        manifest = self._load_manifest(directory, trial_id)
        ref = _reference(trial_id, ordinal, payload, sources)
        existing = next((entry for entry in manifest.entries if entry.evidence_id == ref.evidence_id), None)
        if existing is not None:
            if existing != ref:
                msg = f"delivered evidence {ref.evidence_id!r} already names {_conflict_reason(existing, ref)}"
                raise DeliveredEvidenceConflictError(msg)
            return existing
        if manifest.total_bytes + ref.payload_bytes > TRIAL_DELIVERED_EVIDENCE_BYTES:
            msg = (
                f"trial {trial_id!r} would hold {manifest.total_bytes + ref.payload_bytes} bytes of delivered evidence, "
                f"over the {TRIAL_DELIVERED_EVIDENCE_BYTES} byte trial bound"
            )
            raise DeliveredEvidenceBoundError(msg)
        self._create_payload(directory / f"{ref.payload_sha256}{_PAYLOAD_SUFFIX}", payload)
        self._replace_manifest(
            directory,
            TrialEvidenceManifest(
                trial_id=trial_id, total_bytes=manifest.total_bytes + ref.payload_bytes, entries=(*manifest.entries, ref)
            ),
        )
        return ref

    def _create_payload(self, path: Path, payload: bytes) -> None:
        """Put exactly `payload` at `path`, accepting a file that already holds those exact bytes."""
        try:
            atomic_create(path, payload)
        except FileExistsError:
            if path.read_bytes() != payload:
                msg = f"stored payload {path.name!r} already holds different content"
                raise DeliveredEvidenceConflictError(msg) from None

    def _replace_manifest(self, directory: Path, manifest: TrialEvidenceManifest) -> None:
        atomic_write(directory / _MANIFEST_NAME, manifest.model_dump_json(indent=2))

    def _read(self, ref: DeliveredEvidenceRef) -> DeliveredEvidence:
        trial_digest, separator, position = ref.evidence_id.rpartition(_EVIDENCE_ID_SEPARATOR)
        if not separator or position != str(ref.ordinal):
            msg = f"evidence id {ref.evidence_id!r} does not name position {ref.ordinal}"
            raise DeliveredEvidenceUnreadableError(msg)
        directory = self._trial_directory(trial_digest)
        payload = self._stored_bytes(directory / f"{ref.payload_sha256}{_PAYLOAD_SUFFIX}", ref)
        self._require_filed(directory, ref)
        try:
            return DeliveredEvidence(ref=ref, payload_json=payload.decode("utf-8"))
        except UnicodeDecodeError as error:
            msg = f"stored payload for {ref.evidence_id!r} is not the UTF-8 text it was stored as"
            raise DeliveredEvidenceUnreadableError(msg) from error

    def _stored_bytes(self, path: Path, ref: DeliveredEvidenceRef) -> bytes:
        try:
            payload = path.read_bytes()
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError) as error:
            msg = f"no stored payload for {ref.evidence_id!r}"
            raise DeliveredEvidenceUnreadableError(msg) from error
        except OSError as error:
            msg = f"the stored payload for {ref.evidence_id!r} could not be read: {error}"
            raise DeliveredEvidencePersistenceError(msg) from error
        if len(payload) != ref.payload_bytes:
            msg = f"stored payload for {ref.evidence_id!r} is {len(payload)} bytes, not the {ref.payload_bytes} recorded"
            raise DeliveredEvidenceUnreadableError(msg)
        if hashlib.sha256(payload).hexdigest() != ref.payload_sha256:
            msg = f"stored payload for {ref.evidence_id!r} does not hash to {ref.payload_sha256!r}"
            raise DeliveredEvidenceUnreadableError(msg)
        return payload

    def _require_filed(self, directory: Path, ref: DeliveredEvidenceRef) -> None:
        """Refuse a reference the trial's own manifest does not name exactly."""
        try:
            content = (directory / _MANIFEST_NAME).read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError) as error:
            msg = f"trial holding {ref.evidence_id!r} has no manifest"
            raise DeliveredEvidenceUnreadableError(msg) from error
        except OSError as error:
            msg = f"the manifest holding {ref.evidence_id!r} could not be read: {error}"
            raise DeliveredEvidencePersistenceError(msg) from error
        manifest = _parsed_manifest(content, ref.evidence_id)
        if ref not in manifest.entries:
            msg = f"trial manifest does not file {ref.evidence_id!r} with this hash, size and sources"
            raise DeliveredEvidenceUnreadableError(msg)


def _parsed_manifest(content: str, named: str) -> TrialEvidenceManifest:
    """The manifest `content` holds, refusing anything that is no longer a readable evidence record."""
    try:
        return TrialEvidenceManifest.model_validate_json(content)
    except ValueError as error:
        msg = f"the trial manifest holding {named!r} is not a readable evidence record"
        raise DeliveredEvidenceUnreadableError(msg) from error


def _conflict_reason(existing: DeliveredEvidenceRef, incoming: DeliveredEvidenceRef) -> str:
    """What differs between a filed reference and one offered under the same identity."""
    if existing.payload_sha256 != incoming.payload_sha256:
        return f"different bytes, hashing {existing.payload_sha256} rather than {incoming.payload_sha256}"
    if existing.payload_bytes != incoming.payload_bytes:
        return f"a different byte count, {existing.payload_bytes} rather than {incoming.payload_bytes}"
    return "the same bytes with different sources"


def _reference(trial_id: str, ordinal: int, payload: bytes, sources: tuple[DeliveredSource, ...]) -> DeliveredEvidenceRef:
    """The reference these exact bytes earn, refusing a payload over the per-payload bound."""
    evidence_id = evidence_id_for(trial_id, ordinal)
    if not payload:
        msg = f"delivered evidence {evidence_id!r} carries no bytes"
        raise DeliveredEvidenceBoundError(msg)
    if len(payload) > DELIVERED_PAYLOAD_BYTES:
        msg = f"delivered payload {evidence_id!r} is {len(payload)} bytes, over the {DELIVERED_PAYLOAD_BYTES} byte payload bound"
        raise DeliveredEvidenceBoundError(msg)
    return DeliveredEvidenceRef(
        evidence_id=evidence_id,
        ordinal=ordinal,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        payload_bytes=len(payload),
        sources=sources,
    )
