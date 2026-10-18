"""Rewrite the sources state written under the old words into the ones the code now reads.

The old names sit in each registered knowledge base's `_meta/`: the source queue `intake.yaml`, whose rows keyed the source type as `kind`,
the watch list `feeds.yaml`, whose rows keyed the scanner as `kind`, and the source queue's fetch cache `intake-cache`.
They also sit in its schema file, which keyed the connectors as `sources.providers`, the queue policy as `sources.intake`
with its `kinds` list and privacy as `retrieval.library: false`,
and in the settings file, which keyed each registered knowledge base's privacy as `library: false`.
Privacy inverts, and a missing key means not private in both shapes, so a file or a row without the key gains nothing.
Run it with `uv run python -m studio.migrations.wave4 [--skip NAME]`, or let `studio serve-http` run it at boot while any old name remains.
A folder that cannot be read or written is reported and passed over, and a second run finds nothing and says so.
"""

from __future__ import annotations

import argparse
import json

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from studio.knowledge_base.settings import default_settings_path
from studio.migrations.wave3 import registered_paths

if TYPE_CHECKING:
    from pydantic import JsonValue

OLD_QUEUE_FILE = "intake.yaml"
"""The file name the queue of requested sources was written under."""

QUEUE_FILE = "source-queue.yaml"
"""What that file is called now."""

OLD_QUEUE_CACHE = "intake-cache"
"""The folder name the queue's fetched documents were cached under."""

QUEUE_CACHE = "source-queue-cache"
"""What that folder is called now."""

OLD_WATCH_LIST_FILE = "feeds.yaml"
"""The file name the watch list was written under."""

WATCH_LIST_FILE = "watch-list.yaml"
"""What that file is called now."""

QUEUE_ROW_KEY = ("kind", "source_type")
"""A queued row's own key for what the URL turned out to be, old then new."""

WATCH_ROW_KEY = ("kind", "scanner")
"""A watch-list row's own key for what reads it, old then new."""

OLD_CONNECTORS_KEY = "providers"
"""The `sources` key that listed the connectors a knowledge base declares."""

CONNECTORS_KEY = "connectors"
"""What that key is called now."""

OLD_QUEUE_POLICY_KEY = "intake"
"""The `sources` key that held the queue's admission policy."""

QUEUE_POLICY_KEY = "source_queue"
"""What that key is called now."""

OLD_SOURCE_TYPES_KEY = "kinds"
"""The queue policy's own key for the source types it admits."""

SOURCE_TYPES_KEY = "source_types"
"""What that key is called now."""

OLD_PRIVACY_KEY = "library"
"""The key that said whether a host may read this knowledge base, `false` meaning it may not."""

PRIVACY_KEY = "private"
"""What that key is called now, with the sense inverted: `true` means no host may read it."""


@dataclass
class Report:
    """What one run of the migration did, in the order it did it."""

    files: list[str] = field(default_factory=list)
    """One line per `_meta/` file or folder renamed, with its knowledge base folder."""
    rows: list[str] = field(default_factory=list)
    """One line per YAML file whose rows were re-keyed, with how many moved."""
    schemas: list[str] = field(default_factory=list)
    """The path of each schema file rewritten."""
    settings: list[str] = field(default_factory=list)
    """One line per settings file rewritten, with the registered knowledge bases whose privacy key moved."""
    skipped: list[str] = field(default_factory=list)
    """One line per registered knowledge base left out by name, with its folder."""
    refused: list[str] = field(default_factory=list)
    """One line per file or folder that could not be read or rewritten, with the reason."""

    @property
    def touched(self) -> bool:
        """Whether anything at all was rewritten."""
        return bool(self.files or self.rows or self.schemas or self.settings)

    @property
    def clean(self) -> bool:
        """Whether every knowledge base this run met came through. A refusal is reported, never raised."""
        return not self.refused

    def lines(self) -> list[str]:
        """One line per thing rewritten, or the single line that says there was nothing to do.

        Returns:
            The lines to print, in order.
        """
        out: list[str] = []
        if not self.touched:
            out.append("nothing to migrate: every _meta file, schema file and settings row already reads the new words")
        for name, rows in (("renamed", self.files), ("re-keyed rows in", self.rows), ("rewrote schema file", self.schemas)):
            if rows:
                out.extend([f"{name} ({len(rows)}):", *(f"  {row}" for row in rows)])
        if self.settings:
            out.extend(["rewrote the settings file:", *(f"  {row}" for row in self.settings)])
        if self.skipped:
            out.extend(["left alone, skipped by name:", *(f"  {row}" for row in self.skipped)])
        if self.refused:
            out.extend(["left alone, could not be read or rewritten:", *(f"  {row}" for row in self.refused)])
        return out


def migrated_schema(document: dict[str, JsonValue]) -> dict[str, JsonValue] | None:
    """`document` in the new keys, or `None` when it already reads that way.

    Four rewrites: `sources.providers` becomes `sources.connectors`, `sources.intake` becomes `sources.source_queue`
    and its `kinds` list becomes `source_types`, and `retrieval.library: false` becomes `retrieval.private: true`.
    The connector names inside the list are data and stay exactly as they are.

    Args:
        document: A schema file's parsed JSON object. It is never changed.

    Returns:
        A rewritten copy of `document`, or `None` when no key needed a rewrite.
    """
    out: dict[str, JsonValue] = json.loads(json.dumps(document))
    sources = out.get("sources")
    changed = False
    if isinstance(sources, dict):
        if OLD_CONNECTORS_KEY in sources:
            sources[CONNECTORS_KEY] = sources.pop(OLD_CONNECTORS_KEY)
            changed = True
        if OLD_QUEUE_POLICY_KEY in sources:
            sources[QUEUE_POLICY_KEY] = sources.pop(OLD_QUEUE_POLICY_KEY)
            changed = True
        policy = sources.get(QUEUE_POLICY_KEY)
        if isinstance(policy, dict) and OLD_SOURCE_TYPES_KEY in policy:
            policy[SOURCE_TYPES_KEY] = policy.pop(OLD_SOURCE_TYPES_KEY)
            changed = True
    retrieval = out.get("retrieval")
    if isinstance(retrieval, dict) and OLD_PRIVACY_KEY in retrieval:
        was_shared = retrieval.pop(OLD_PRIVACY_KEY) is not False
        if not was_shared:
            retrieval[PRIVACY_KEY] = True
        changed = True
    return out if changed else None


def migrate_schema_file(root: Path, *, report: Report) -> None:
    """Rewrite `root`'s schema file in the new keys, leaving one that already reads them alone.

    A failure goes to `report.refused`.

    Args:
        root: The knowledge base folder.
        report: The run's report, which gains the file's path or a line for the failure.
    """
    path = root / "knowledge-base-schema.json"
    if not path.is_file():
        return
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        report.refused.append(f"{path}: {error}")
        return
    if not isinstance(document, dict):
        report.refused.append(f"{path}: the schema file is not a JSON object")
        return
    migrated = migrated_schema(document)
    if migrated is None:
        return
    try:
        path.write_text(json.dumps(migrated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as error:
        report.refused.append(f"{path}: {error}")
        return
    report.schemas.append(str(path))


def _rekeyed_rows(rows: list[JsonValue], old: str, new: str) -> int:
    """Give every mapping row in `rows` its new key in place, and answer how many moved."""
    moved = 0
    for row in rows:
        if isinstance(row, dict) and old in row and new not in row:
            row[new] = row.pop(old)
            moved += 1
    return moved


def migrate_yaml_file(root: Path, old_name: str, new_name: str, row_key: tuple[str, str], *, report: Report) -> None:
    """Rename one `_meta/` YAML list and re-key its rows.

    With both names present, the old file is left alone and reported as refused.
    The rows are re-keyed whether the file sits under its old name or its new one.

    Args:
        root: The knowledge base folder.
        old_name: The file's old name under `_meta/`.
        new_name: The file's new name under `_meta/`.
        row_key: A row's old key, then its new one.
        report: The run's report, which gains a line for the rows re-keyed, the file renamed or the failure.
    """
    old_path, new_path = root / "_meta" / old_name, root / "_meta" / new_name
    if new_path.exists() and old_path.is_file():
        report.refused.append(f"{old_path}: {new_path.name} is already there, so this file was left alone")
        return
    path = new_path if new_path.is_file() else old_path
    if not path.is_file():
        return
    try:
        rows = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        report.refused.append(f"{path}: {error}")
        return
    old_key, new_key = row_key
    moved = _rekeyed_rows(rows, old_key, new_key) if isinstance(rows, list) else 0
    if moved:
        try:
            path.write_text(yaml.safe_dump(rows, sort_keys=False, allow_unicode=True), encoding="utf-8")
        except OSError as error:
            report.refused.append(f"{path}: {error}")
            return
        report.rows.append(f"{moved} row(s) {old_key} -> {new_key} in {path}")
    if path == old_path:
        try:
            old_path.rename(new_path)
        except OSError as error:
            report.refused.append(f"{old_path}: {error}")
            return
        report.files.append(f"_meta/{old_name} -> _meta/{new_name} in {root}")


def migrate_queue_cache(root: Path, *, report: Report) -> None:
    """Rename `root/_meta/intake-cache` to `root/_meta/source-queue-cache`, leaving an already-renamed folder alone.

    With both folders present, the old folder is left alone and reported as refused.

    Args:
        root: The knowledge base folder.
        report: The run's report, which gains a line for the folder renamed or for the failure.
    """
    old_path, new_path = root / "_meta" / OLD_QUEUE_CACHE, root / "_meta" / QUEUE_CACHE
    if new_path.exists():
        if old_path.is_dir():
            report.refused.append(f"{old_path}: {new_path.name} is already there, so this folder was left alone")
        return
    if not old_path.is_dir():
        return
    try:
        old_path.rename(new_path)
    except OSError as error:
        report.refused.append(f"{old_path}: {error}")
        return
    report.files.append(f"_meta/{OLD_QUEUE_CACHE} -> _meta/{QUEUE_CACHE} in {root}")


def migrate_registered_knowledge_base(row: dict[str, JsonValue]) -> bool:
    """Give one registered knowledge base's settings row its new privacy key in place.

    `library: false` becomes `private: true`, and `library: true` is removed.
    A row without either key is not private.

    Args:
        row: The registered knowledge base's row in the settings file.

    Returns:
        Whether the row changed.
    """
    if OLD_PRIVACY_KEY not in row:
        return False
    was_shared = row.pop(OLD_PRIVACY_KEY) is not False
    if not was_shared:
        row[PRIVACY_KEY] = True
    return True


def migrate_settings(settings_path: Path, *, skip: frozenset[str] = frozenset(), report: Report) -> None:
    """Give every registered knowledge base in the settings file its new privacy key, leaving `skip` and every other key untouched.

    A failure goes to `report.refused`.

    Args:
        settings_path: The settings file to rewrite.
        skip: The names of the registered knowledge bases to leave untouched.
        report: The run's report, which gains a line for the privacy keys moved or for the failure.
    """
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        report.refused.append(f"{settings_path}: {error}")
        return
    rows = settings.get("knowledge_bases") if isinstance(settings, dict) else None
    if not isinstance(rows, dict):
        return
    moved = [
        str(name)
        for name, row in rows.items()
        if str(name) not in skip and isinstance(row, dict) and migrate_registered_knowledge_base(row)
    ]
    if not moved:
        return
    try:
        settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as error:
        report.refused.append(f"{settings_path}: {error}")
        return
    report.settings.append(f"{OLD_PRIVACY_KEY} -> {PRIVACY_KEY} for {', '.join(sorted(moved))} in {settings_path}")


def _schema_needs_migration(root: Path) -> bool:
    """Whether `root`'s schema file still carries one of the old keys."""
    try:
        document = json.loads((root / "knowledge-base-schema.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(document, dict) and migrated_schema(document) is not None


def needs_migration(settings_path: Path | None = None) -> bool:
    """Whether any registered knowledge base or the settings file still reads one of the old names.

    It answers on the old names alone, so once every one of them is gone it answers False.

    Args:
        settings_path: The settings file to read, or None for the application settings file.

    Returns:
        True while an old name remains, and False otherwise or when the settings file cannot be read.
    """
    path = settings_path if settings_path is not None else default_settings_path()
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    rows = settings.get("knowledge_bases") if isinstance(settings, dict) else None
    if isinstance(rows, dict) and any(isinstance(row, dict) and OLD_PRIVACY_KEY in row for row in rows.values()):
        return True
    for root in registered_paths(path).values():
        meta = root / "_meta"
        if (meta / OLD_QUEUE_FILE).exists() or (meta / OLD_WATCH_LIST_FILE).exists() or (meta / OLD_QUEUE_CACHE).exists():
            return True
        if _schema_needs_migration(root):
            return True
    return False


def migrate(settings_path: Path | None = None, *, skip: frozenset[str] = frozenset()) -> Report:
    """Rewrite every registered knowledge base and the settings file, skipping the names in `skip`.

    A folder that cannot be read or written is reported and passed over, and the others still run.
    The settings file is rewritten last, after every folder it names has had its turn.

    Args:
        settings_path: The settings file to read and rewrite, or None for the application settings file.
        skip: The names of the registered knowledge bases to leave untouched.

    Returns:
        What the run rewrote, skipped and refused.
    """
    path = settings_path if settings_path is not None else default_settings_path()
    report = Report()
    for name, root in registered_paths(path).items():
        if name in skip:
            report.skipped.append(f"{name} at {root}")
            continue
        if not root.is_dir():
            report.refused.append(f"{name}: no folder at {root}")
            continue
        migrate_yaml_file(root, OLD_QUEUE_FILE, QUEUE_FILE, QUEUE_ROW_KEY, report=report)
        migrate_yaml_file(root, OLD_WATCH_LIST_FILE, WATCH_LIST_FILE, WATCH_ROW_KEY, report=report)
        migrate_queue_cache(root, report=report)
        migrate_schema_file(root, report=report)
    migrate_settings(path, skip=skip, report=report)
    return report


def main() -> None:
    """Run the migration and print what it did."""
    parser = argparse.ArgumentParser(description="Rewrite the sources state into the vocabulary the code now reads.")
    parser.add_argument("--skip", action="append", default=[], metavar="NAME", help="a registered knowledge base to leave untouched")
    parser.add_argument("--settings", type=Path, default=None, help="the settings file to read and rewrite")
    args = parser.parse_args()
    report = migrate(args.settings, skip=frozenset(args.skip))
    for line in report.lines():
        print(line)  # noqa: T201 (the command's whole output is this listing)


if __name__ == "__main__":
    main()
