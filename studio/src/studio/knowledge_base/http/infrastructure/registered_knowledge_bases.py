"""The registered knowledge bases, read from and written to the settings file.

Every write reads the file again, changes one registered knowledge base, and replaces the file in one atomic write.
Every other settings key, and every registered knowledge base that fails validation, is written back as it was read.
"""

from __future__ import annotations

import json
import secrets

from pathlib import Path
from typing import TYPE_CHECKING

from core.files import atomic_write
from pydantic import ValidationError

from studio.knowledge_base.http.exceptions import KnowledgeBaseRefusal, KnowledgeBaseRefusedError, unknown_knowledge_base_error
from studio.knowledge_base.settings import ApplicationSettings, KnowledgeBaseEntry, Schedule, is_valid_registry_row, read_registry

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import JsonValue


def refuse_unloadable_merge(settings_path: Path, data: dict[str, object]) -> None:
    """Refuse `data` when the registered knowledge bases in it that pass validation would not load as `ApplicationSettings`.

    Only the valid entries are checked, so a broken one already in the file never blocks writing another.
    The check writes a separate file beside `settings_path` and always removes it.

    Args:
        settings_path: The settings file `data` is about to replace.
        data: The whole settings document as it would be written.

    Raises:
        KnowledgeBaseRefusedError: `REGISTRY_WRITE_REFUSED` when the settings would not load.
    """
    knowledge_bases_raw = data.get("knowledge_bases", {})
    valid_only = {name: raw for name, raw in knowledge_bases_raw.items() if is_valid_registry_row(raw)} if isinstance(knowledge_bases_raw, dict) else {}
    belt_data = dict(data)
    belt_data["knowledge_bases"] = valid_only
    belt_tmp = settings_path.with_name(f"{settings_path.name}.{secrets.token_hex(8)}.beltcheck.tmp")
    belt_tmp.write_text(json.dumps(belt_data), encoding="utf-8")
    try:
        ApplicationSettings(json_file=belt_tmp)
    except ValidationError as exc:
        msg = f"the updated settings would not reload: {exc}"
        raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.REGISTRY_WRITE_REFUSED, msg) from exc
    finally:
        belt_tmp.unlink(missing_ok=True)


class SettingsFileRegisteredKnowledgeBases:
    """The registered knowledge bases in one settings file, read again on every call."""

    def __init__(self, settings_path: Path) -> None:
        """Read and write the registry of knowledge bases in `settings_path`.

        Args:
            settings_path: The settings file that holds the registry of knowledge bases.
        """
        self._settings_path = settings_path

    def registered(self) -> dict[str, KnowledgeBaseEntry]:
        """Read the registered knowledge bases that pass validation from the settings file.

        Returns:
            Each valid entry by its registered name.
        """
        valid, _broken = read_registry(self._settings_path)
        return valid

    def registered_and_broken(self) -> tuple[dict[str, KnowledgeBaseEntry], dict[str, str | None]]:
        """Read every registered knowledge base from the settings file, split by whether it passes validation.

        Returns:
            The valid entries by name, and each failing name with its unvalidated folder, `None` when it names none.
        """
        return read_registry(self._settings_path)

    def register(self, name: str, entry: KnowledgeBaseEntry) -> None:
        """Write `entry` under `name` in the settings file.

        Args:
            name: The registered name.
            entry: The folder, description, schedule and privacy to write.

        Raises:
            KnowledgeBaseRefusedError: `REGISTRY_WRITE_REFUSED` when the settings would not load after the write.
        """
        row: dict[str, JsonValue] = {"path": str(entry.path), "description": entry.description}
        if entry.schedule is not None:
            row["schedule"] = entry.schedule.model_dump(exclude_none=True)
        if entry.private:
            row["private"] = True

        def add(data: dict[str, JsonValue]) -> None:
            knowledge_bases = data.setdefault("knowledge_bases", {})
            if not isinstance(knowledge_bases, dict):
                msg = "the updated settings would not reload: knowledge_bases is not a JSON object"
                raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.REGISTRY_WRITE_REFUSED, msg)
            knowledge_bases[name] = row

        self._rewrite(add)

    def unregister(self, name: str) -> tuple[Path, str]:
        """Remove `name` from the settings file.

        Args:
            name: The registered name.

        Returns:
            The removed entry's folder and description, `Path()` and `""` when the entry held none.

        Raises:
            KnowledgeBaseRefusedError: `UNKNOWN_KNOWLEDGE_BASE` when nothing is registered under `name`,
                and `REGISTRY_WRITE_REFUSED` when the settings would not load after the write.
        """

        def remove(data: dict[str, JsonValue]) -> JsonValue:
            knowledge_bases_raw = data.get("knowledge_bases", {})
            if not isinstance(knowledge_bases_raw, dict) or name not in knowledge_bases_raw:
                raise unknown_knowledge_base_error(name)
            return knowledge_bases_raw.pop(name)

        removed = self._rewrite(remove)
        removed_path = removed.get("path") if isinstance(removed, dict) else None
        removed_description = removed.get("description") if isinstance(removed, dict) else None
        return (
            Path(removed_path) if isinstance(removed_path, str) else Path(),
            removed_description if isinstance(removed_description, str) else "",
        )

    def write_schedule(self, name: str, schedule: Schedule | None) -> None:
        """Set or clear the `schedule` key of `name` in the settings file.

        Args:
            name: The registered name.
            schedule: The schedule to set, or `None` to clear it.

        Raises:
            KnowledgeBaseRefusedError: `UNKNOWN_KNOWLEDGE_BASE` when nothing is registered under `name`,
                and `REGISTRY_WRITE_REFUSED` when the settings would not load after the write.
        """

        def change_schedule(data: dict[str, JsonValue]) -> None:
            knowledge_bases_raw = data.get("knowledge_bases", {})
            raw = knowledge_bases_raw.get(name) if isinstance(knowledge_bases_raw, dict) else None
            if not isinstance(raw, dict):
                raise unknown_knowledge_base_error(name)
            if schedule is None:
                raw.pop("schedule", None)
            else:
                raw["schedule"] = schedule.model_dump(exclude_none=True)

        self._rewrite(change_schedule)

    def _rewrite[T](self, change: Callable[[dict[str, JsonValue]], T]) -> T:
        """Read the settings file, apply `change` to it, refuse a result that would not load, and replace the file."""
        data = json.loads(self._settings_path.read_text(encoding="utf-8"))
        answer = change(data)
        refuse_unloadable_merge(self._settings_path, data)
        atomic_write(self._settings_path, json.dumps(data, indent=2) + "\n")
        return answer
