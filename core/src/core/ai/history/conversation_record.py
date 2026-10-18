"""A conversation's history stored once, with one small row per step saying what the model was given.

The record receives full snapshots and keeps them without the duplication. A message's content is stored as forms under
its key: form 0 the first time it is seen, a new form each time a step carries it in a shape not stored yet. A message's
stamps, the fields pydantic-ai rewrites or adds when it sends a message again, are versions beside the forms, and instructions
are content-addressed. A step is the ordered list of entries the model was given.

`ConversationRecord` is the in-memory shape of the Postgres store: the store reads the digests a conversation holds,
records the snapshot here, and writes back the content of what became new.
"""

from __future__ import annotations

import json

from dataclasses import dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING, Final

from pydantic import JsonValue
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelResponse
from pydantic_core import to_jsonable_python

from core.ai.history.domain import SnapshotAdditions, StepEntry, StoredStep
from core.ai.history.message_key import message_key

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from pydantic_ai_harness.step_persistence import ContinuableSnapshot

type MessageJson = dict[str, JsonValue]
"""One message in its JSON form, as `to_jsonable_python` renders it and jsonb stores it."""

STAMP_FIELDS: Final = ("timestamp", "run_id", "conversation_id", "instructions", "workspace_ref")
"""The top-level fields pydantic-ai may rewrite or add when it sends a message again. Stored as versions, never inside the content."""


@dataclass
class ConversationRecord:
    """The history of one conversation, whatever the number of runs it spans, known by digest.

    The digests say what the conversation already holds. The contents are the ones the recorded snapshots added, which
    is what there is to write.
    """

    forms: dict[str, list[str]] = field(default_factory=dict)
    """The digest of every shape each message was ever given in, form 0 first. Insertion order is the message's ordinal."""
    stamps: dict[str, list[str]] = field(default_factory=dict)
    """The digest of every set of stamps each message was ever sent with, version 0 first."""
    new_forms: dict[tuple[str, int], MessageJson] = field(default_factory=dict)
    """The content of every form the recorded snapshots added, by message key and form."""
    new_stamps: dict[tuple[str, int], MessageJson] = field(default_factory=dict)
    """Every set of stamps the recorded snapshots added, instructions as a digest, by message key and version."""
    new_instructions: dict[str, str] = field(default_factory=dict)
    """The instructions text the recorded snapshots carried, by digest."""
    steps: list[StoredStep] = field(default_factory=list)

    def record(self, snapshot: ContinuableSnapshot, *, seq: int) -> SnapshotAdditions:
        """Store one snapshot as a step, keeping only the forms, stamps and instructions the record does not hold yet.

        Args:
            snapshot: The messages the model was given at one step, with the run and state they belong to.
            seq: The store's own number for the step, unique and increasing within the run.

        Returns:
            How many messages, forms and instructions the snapshot added.

        Raises:
            ValueError: when the run already has a step with this `seq`, a key repeats inside the snapshot, or a message
                does not come back from the content and the stamps it is split into.
        """
        run_id = snapshot.run_id
        if any(step.run_id == run_id and step.seq == seq for step in self.steps):
            msg = f"run {run_id!r} already has a step with seq {seq}"
            raise ValueError(msg)
        given = [_message_json(message) for message in snapshot.messages]
        keys = [message_key(message) for message in snapshot.messages]
        if len(set(keys)) != len(keys):
            msg = f"run {run_id!r} seq {seq}: two messages share a key"
            raise ValueError(msg)
        known_forms, known_texts = set(self.new_forms), set(self.new_instructions)
        entries = tuple(self._place(key, message) for key, message in zip(keys, given, strict=True))
        self.steps.append(
            StoredStep(run_id=run_id, seq=seq, step_index=snapshot.step_index, at=snapshot.timestamp, state=snapshot.state, entries=entries)
        )
        self._refuse_a_lossy_split(given, run_id=run_id, seq=seq)
        return self._added_since(known_forms, known_texts)

    def messages_json_at(self, *, run_id: str, seq: int) -> list[MessageJson]:
        """Read the snapshot at one step back as JSON messages, forms and stamps applied.

        Only the pieces this record holds the content of can be read, the ones a recorded snapshot added.

        Args:
            run_id: The run the step belongs to.
            seq: The step's number in the run.

        Returns:
            The messages, in the order the model was given them.

        Raises:
            KeyError: when no such step was recorded, or a piece of it was stored before this record was read.
        """
        step = self._step(run_id, seq)
        return [self.message_json(entry) for entry in step.entries]

    def messages_at(self, *, run_id: str, seq: int) -> list[ModelMessage]:
        """Read the snapshot at one step back as pydantic-ai messages.

        Args:
            run_id: The run the step belongs to.
            seq: The step's number in the run.

        Returns:
            The messages, in the order the model was given them.

        Raises:
            KeyError: when no such step was recorded, or a piece of it was stored before this record was read.
            ValidationError: when what was stored is not a message pydantic-ai knows.
        """
        return messages_of(self.messages_json_at(run_id=run_id, seq=seq))

    def latest_step(self, *, run_id: str, include_interrupted: bool = False) -> StoredStep | None:
        """The newest step of a run, complete ones only unless asked otherwise.

        Args:
            run_id: The run.
            include_interrupted: Whether a step saved interrupted counts.

        Returns:
            The step, or None when the run has none that counts.
        """
        for step in reversed(self.steps):
            if step.run_id == run_id and (include_interrupted or step.state == "complete"):
                return step
        return None

    def message_json(self, entry: StepEntry) -> MessageJson:
        """One message as one entry saw it: its form with its stamps, instructions text restored.

        Only the pieces this record holds the content of can be read, the ones a recorded snapshot added.

        Args:
            entry: Which message, form and stamp version to read.

        Returns:
            The message as JSON.

        Raises:
            KeyError: when the form or the stamp version the entry names was stored before this record was read.
        """
        return message_from(self.new_forms[entry.key, entry.form], self.new_stamps[entry.key, entry.stamp], self.new_instructions)

    def _place(self, key: str, message: MessageJson) -> StepEntry:
        """Where one message sits: its form and its stamp version, each kept when the record knows neither digest."""
        content, stamps = self._split(message)
        text = stamps.get("instructions")
        if isinstance(text, str):
            stamps["instructions"] = {"digest": self._keep_instructions(text)}
        return StepEntry(key=key, form=self._form_of(key, content), stamp=self._version_of(key, stamps))

    def _form_of(self, key: str, content: MessageJson) -> int:
        """The form `content` takes under `key`: the last one with its digest, or a new one whose content is kept."""
        digest = digest_of(content)
        digests = self.forms.setdefault(key, [])
        form = _last_index(digests, digest)
        if form is None:
            digests.append(digest)
            form = len(digests) - 1
            self.new_forms[key, form] = content
        return form

    def _version_of(self, key: str, stamps: MessageJson) -> int:
        """The version `stamps` takes under `key`: the last one with its digest, or a new one which is kept."""
        digest = digest_of(stamps)
        digests = self.stamps.setdefault(key, [])
        version = _last_index(digests, digest)
        if version is None:
            digests.append(digest)
            version = len(digests) - 1
            self.new_stamps[key, version] = stamps
        return version

    def _keep_instructions(self, text: str) -> str:
        """The digest of an instructions text, the text kept under it."""
        digest = instructions_digest(text)
        self.new_instructions.setdefault(digest, text)
        return digest

    def _split(self, message: MessageJson) -> tuple[MessageJson, MessageJson]:
        """One message as the two pieces it is stored in: its content, and the stamps it was sent with."""
        stamps = {name: message[name] for name in STAMP_FIELDS if name in message}
        content = {name: value for name, value in message.items() if name not in stamps}
        return content, stamps

    def _refuse_a_lossy_split(self, messages: list[MessageJson], *, run_id: str, seq: int) -> None:
        """Raise `ValueError` unless every message comes back from the two pieces it is stored in."""
        for message in messages:
            content, stamps = self._split(message)
            if {**content, **stamps} != message:
                msg = f"run {run_id!r} seq {seq}: the stored form does not reconstruct the snapshot"
                raise ValueError(msg)

    def _added_since(self, known_forms: set[tuple[str, int]], known_texts: set[str]) -> SnapshotAdditions:
        """What the record gained since those forms and texts: a form 0 is a message of its own, a later form an edit."""
        added = [form for form in self.new_forms if form not in known_forms]
        return SnapshotAdditions(
            new_messages=sum(1 for _, form in added if form == 0),
            new_forms=sum(1 for _, form in added if form > 0),
            new_instructions=len(self.new_instructions) - len(known_texts),
        )

    def _step(self, run_id: str, seq: int) -> StoredStep:
        for step in self.steps:
            if step.run_id == run_id and step.seq == seq:
                return step
        msg = f"no seq {seq} in run {run_id!r}"
        raise KeyError(msg)


def instructions_digest(text: str) -> str:
    """The content address of an instructions text: the SHA-256 of its UTF-8 bytes.

    Args:
        text: The instructions text.

    Returns:
        The 64-character lowercase hex digest.
    """
    return sha256(text.encode()).hexdigest()


def instructions_digest_of(stamps: MessageJson) -> str | None:
    """The digest a set of stamps points its instructions at.

    Args:
        stamps: One set of stamps as it is stored, instructions as a digest.

    Returns:
        The digest, or None when the stamps carry no instructions.
    """
    reference = stamps.get("instructions")
    return str(reference["digest"]) if isinstance(reference, dict) else None


def instructions_digests(stamps: Iterable[MessageJson]) -> set[str]:
    """Every instructions digest some sets of stamps point at.

    Args:
        stamps: Sets of stamps as they are stored, instructions as a digest.

    Returns:
        The digests, each once.
    """
    digests = (instructions_digest_of(stamp) for stamp in stamps)
    return {digest for digest in digests if digest is not None}


def message_from(content: MessageJson, stamps: MessageJson, texts: Mapping[str, str]) -> MessageJson:
    """One message back from the two pieces it is stored in: its form under its stamps, instructions text restored.

    Args:
        content: The message's form.
        stamps: The stamps it was sent with, instructions as a digest.
        texts: Instructions texts by digest.

    Returns:
        The message as JSON.

    Raises:
        KeyError: when the stamps point at an instructions text `texts` does not carry.
    """
    stamped = dict(stamps)
    digest = instructions_digest_of(stamped)
    if digest is not None:
        stamped["instructions"] = texts[digest]
    return {**content, **stamped}


def message_kind(content: MessageJson) -> str:
    """Which kind of message a stored form is, as pydantic-ai names it.

    Args:
        content: The message's form.

    Returns:
        The value of its `kind` field.
    """
    return str(content["kind"])


def messages_of(rendered: list[MessageJson]) -> list[ModelMessage]:
    """Stored messages as pydantic-ai reads them, with no workspace ref on any response.

    Args:
        rendered: The messages as JSON.

    Returns:
        The messages, in the same order.

    Raises:
        ValidationError: when what was stored is not a message pydantic-ai knows.
    """
    messages = ModelMessagesTypeAdapter.validate_python(rendered)
    for message in messages:
        if isinstance(message, ModelResponse):
            # A ref naming a directory that later moved makes the run's workspace refuse it, and the next turn fails.
            message.workspace_ref = None
    return messages


def canonical_json(value: dict[str, JsonValue]) -> str:
    """One string per JSON object, whatever the key order it was built in.

    Keys are sorted, there are no separator spaces and text outside ASCII is written as escapes. This
    spelling must not change: every stored form, stamp version and request parameters set is addressed by it.

    Args:
        value: The JSON object.

    Returns:
        Its canonical text.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest_of(value: dict[str, JsonValue]) -> str:
    """The content address of a JSON object, the one the record diffs by and the store keeps beside it.

    Args:
        value: The JSON object.

    Returns:
        The SHA-256 of its `canonical_json` text, as 64 lowercase hex characters.
    """
    return sha256(canonical_json(value).encode()).hexdigest()


def _last_index(digests: list[str], digest: str) -> int | None:
    for index in range(len(digests) - 1, -1, -1):
        if digests[index] == digest:
            return index
    return None


def _message_json(message: ModelMessage) -> MessageJson:
    rendered = to_jsonable_python(message)
    if not isinstance(rendered, dict):
        msg = f"a message serializes to a JSON object, got {type(rendered).__name__}"
        raise TypeError(msg)
    return rendered
