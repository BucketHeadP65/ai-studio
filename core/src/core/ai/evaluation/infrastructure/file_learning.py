"""Learning records as validated local JSON files, one per learning key, merged under a shared lock."""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from core.ai.evaluation.domain.learning import LearningRecord
from core.ai.evaluation.domain.outcomes import TaskExecution
from core.files import atomic_write, exclusive_lock

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.evaluation.domain.learning import LearningKey


class JsonLearningStore[ExecutionT: TaskExecution]:
    """Validated local JSON records with atomic append-merge writes across processes.

    The async methods move file I/O and cooperative locking off the event loop. A writer using this
    store cannot delete history with a stale snapshot, and a conflicting historical id fails.
    """

    def __init__(self, directory: Path, *, record_type: type[LearningRecord[ExecutionT]] = LearningRecord[TaskExecution]) -> None:
        """Keep records in one folder.

        Args:
            directory: The folder holding one JSON file per learning key. It is created on the first save.
            record_type: The record type a stored record's trials carry. It defaults to the general
                kernel's own `TaskExecution`, so a caller with no adapter fields names nothing extra.
        """
        self.directory = directory
        self.record_type = record_type

    async def load(self, key: LearningKey) -> LearningRecord[ExecutionT] | None:
        """Load and validate the full record for `key`. A corrupt or mismatched file raises.

        Args:
            key: The learning key to load.

        Returns:
            The record, or `None` when nothing is stored under the key.

        Raises:
            ValueError: The stored file is not a valid record, or names a different key.
        """
        return await asyncio.to_thread(self._load, key)

    async def save(self, record: LearningRecord[ExecutionT]) -> LearningRecord[ExecutionT]:
        """Merge additions with persisted history and return the complete saved record.

        Args:
            record: The record whose evidence and attempts are merged in.

        Returns:
            The complete record as saved.

        Raises:
            ValueError: An evidence id or an attempt id already names something different.
        """
        return await asyncio.to_thread(self._save, record)

    def _load(self, key: LearningKey) -> LearningRecord[ExecutionT] | None:
        path = self.directory / f"{key.fingerprint}.json"
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        record = self.record_type.model_validate_json(content)
        if record.key != key:
            msg = "stored learning key does not match its file identity"
            raise ValueError(msg)
        return record

    def _save(self, incoming: LearningRecord[ExecutionT]) -> LearningRecord[ExecutionT]:
        validated = self.record_type.model_validate_json(incoming.model_dump_json())
        self.directory.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(self.directory / ".lock"):
            record = self._load(validated.key) or self.record_type(key=validated.key)
            for evidence in validated.evidence:
                record = record.add_evidence(evidence)
            for attempt in validated.attempts:
                record = record.add_attempt(attempt)
            self._write(record)
            return record

    def _write(self, record: LearningRecord[ExecutionT]) -> None:
        atomic_write(self.directory / f"{record.key.fingerprint}.json", record.model_dump_json(indent=2))
