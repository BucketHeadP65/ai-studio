"""Rewrite knowledge base state written under the old words into the new ones.

It rewrites three places: every registered knowledge base's schema file (`corpusspec.json`, whose page type key was `kind`
and whose type lists were `*_kinds`), its `_meta/registry/` folder, and the settings key `corpora`.
The same pass re-marks the managed region of `_meta/dashboard.md`,
so the next `studio index` replaces the block in place instead of appending a second one.
Run it with `uv run python -m studio.migrations.wave3 [--skip NAME]`,
or let `studio serve-http` run it at boot, with no `--skip`, only while the settings file still carries `corpora`.
So once the key has moved, a folder skipped by name is migrated only by running this module by hand.
A second run finds nothing and says so.
"""

from __future__ import annotations

import argparse
import json
import re

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from studio.knowledge_base.settings import default_settings_path

if TYPE_CHECKING:
    from pydantic import JsonValue

OLD_SCHEMA_FILE = "corpusspec.json"
"""The old schema file name, renamed to `SCHEMA_FILE`."""

SCHEMA_FILE = "knowledge-base-schema.json"
"""What a knowledge base's schema file is called now."""

OLD_VOCABULARY_DIR = "registry"
"""The old name of the vocabulary folder under `_meta/`, renamed to `VOCABULARY_DIR`."""

VOCABULARY_DIR = "vocabulary"
"""What that folder is called now."""

OLD_SETTINGS_KEY = "corpora"
"""The old settings key that lists the registered knowledge bases, renamed to `SETTINGS_KEY`."""

SETTINGS_KEY = "knowledge_bases"
"""What that key is called now."""

TYPE_LISTS = {"claim_kinds": "claim_types", "digest_kinds": "digest_types", "quote_required_kinds": "quote_required_types"}
"""The schema's type vocabularies, by their old key and their new one.

`sources.intake.kinds` is not in the map, because it lists source types, not page types."""

OLD_DASHBOARD_MARKERS = (("<!-- COMPILE-LINT:BEGIN", "<!-- INDEX-VALIDATION:BEGIN"), ("<!-- COMPILE-LINT:END", "<!-- INDEX-VALIDATION:END"))
"""The managed-region markers of `_meta/dashboard.md`, old prefix to new one."""

_DASHBOARD_MANAGER = re.compile(r"(<!-- INDEX-VALIDATION:BEGIN \(managed by `)[^`]*(`)")
"""The manager label inside a re-marked BEGIN marker, whatever an older indexer wrote there."""

DASHBOARD_MANAGER = "studio index"
"""The command that owns the managed region of `_meta/dashboard.md`, written into its BEGIN marker."""


@dataclass
class Report:
    """What one run of the migration did, in the order it did it."""

    schemas: list[str] = field(default_factory=list)
    """One line per schema file renamed and rewritten, with its knowledge base folder."""
    folders: list[str] = field(default_factory=list)
    """One line per vocabulary folder renamed, with its knowledge base folder."""
    dashboards: list[str] = field(default_factory=list)
    """The path of each `_meta/dashboard.md` whose managed region was re-marked."""
    settings: list[str] = field(default_factory=list)
    """One line per settings key moved, with the settings file."""
    skipped: list[str] = field(default_factory=list)
    """One line per registered knowledge base left out by name, with its folder."""
    refused: list[str] = field(default_factory=list)
    """One line per file or folder that could not be read or rewritten, with the reason."""

    @property
    def touched(self) -> bool:
        """Whether anything at all was rewritten."""
        return bool(self.schemas or self.folders or self.dashboards or self.settings)

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
            out.append("nothing to migrate: every schema file, vocabulary folder and settings key already reads the new words")
        for name, rows in (("rewrote schema file", self.schemas), ("renamed folder", self.folders), ("re-marked dashboard", self.dashboards)):
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

    The rewrites: a page type entry's `kind` becomes `page_type`, a compounding entry's `from.kinds` becomes `from.page_types`,
    the three type vocabularies take their `*_types` names, `path_root: "corpus"` becomes `path_root: "knowledge_base"`,
    and `retrieval.group_by: "kind"` becomes `retrieval.group_by: "page_type"`.

    Args:
        document: A schema file's parsed JSON object. It is never changed.

    Returns:
        A rewritten copy of `document`, or `None` when no key or value needed a rewrite.
    """
    out: dict[str, JsonValue] = json.loads(json.dumps(document))
    changed = False
    page_types = out.get("page_types")
    if isinstance(page_types, list):
        for entry in page_types:
            if isinstance(entry, dict) and "kind" in entry:
                entry["page_type"] = entry.pop("kind")
                changed = True
    changed = _migrated_compounding(out) or changed
    changed = _migrated_type_lists(out) or changed
    if out.get("path_root") == "corpus":
        out["path_root"] = "knowledge_base"
        changed = True
    retrieval = out.get("retrieval")
    if isinstance(retrieval, dict) and retrieval.get("group_by") == "kind":
        retrieval["group_by"] = "page_type"
        changed = True
    return out if changed else None


def _migrated_compounding(document: dict[str, JsonValue]) -> bool:
    """Give every compounding entry's pages selector its new key in place, and say whether any moved."""
    compounding = document.get("compounding")
    entries = compounding.get("entries") if isinstance(compounding, dict) else None
    if not isinstance(entries, list):
        return False
    moved = False
    for entry in entries:
        source = entry.get("from") if isinstance(entry, dict) else None
        if isinstance(source, dict) and "kinds" in source:
            source["page_types"] = source.pop("kinds")
            moved = True
    return moved


def _migrated_type_lists(document: dict[str, JsonValue]) -> bool:
    """Give the three type vocabularies their new keys in place, and say whether any moved."""
    moved = False
    for old, new in TYPE_LISTS.items():
        for holder in (document, document.get("claims"), document.get("sources")):
            if isinstance(holder, dict) and old in holder:
                holder[new] = holder.pop(old)
                moved = True
    return moved


def migrate_schema_file(root: Path, *, report: Report) -> None:
    """Write `root`'s schema under its new name in the new keys, and drop the old file.

    A folder that already has the new file is left alone, even with the old one beside it.
    A failure goes to `report.refused`.

    Args:
        root: The knowledge base folder.
        report: The run's report, which gains a line for the file rewritten or for the failure.
    """
    old, new = root / OLD_SCHEMA_FILE, root / SCHEMA_FILE
    if new.exists() or not old.is_file():
        return
    try:
        document = json.loads(old.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        report.refused.append(f"{old}: {error}")
        return
    if not isinstance(document, dict):
        report.refused.append(f"{old}: the schema file is not a JSON object")
        return
    migrated = migrated_schema(document) or document
    try:
        new.write_text(json.dumps(migrated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        old.unlink()
    except OSError as error:
        report.refused.append(f"{old}: {error}")
        return
    report.schemas.append(f"{old.name} -> {new.name} in {root}")


def migrate_vocabulary_folder(root: Path, *, report: Report) -> None:
    """Rename `root/_meta/registry` to `root/_meta/vocabulary`.

    A folder that already has `_meta/vocabulary` is left alone, even with the old folder beside it.
    A failure goes to `report.refused`.

    Args:
        root: The knowledge base folder.
        report: The run's report, which gains a line for the folder renamed or for the failure.
    """
    old, new = root / "_meta" / OLD_VOCABULARY_DIR, root / "_meta" / VOCABULARY_DIR
    if new.exists() or not old.is_dir():
        return
    try:
        old.rename(new)
    except OSError as error:
        report.refused.append(f"{old}: {error}")
        return
    report.folders.append(f"_meta/{OLD_VOCABULARY_DIR} -> _meta/{VOCABULARY_DIR} in {root}")


def migrate_dashboard(root: Path, *, report: Report) -> None:
    """Re-mark the managed region of `root/_meta/dashboard.md` so the next `studio index` replaces it in place.

    A page whose markers already read the new way is left as it is.
    A failure goes to `report.refused`.

    Args:
        root: The knowledge base folder.
        report: The run's report, which gains the page's path or a line for the failure.
    """
    path = root / "_meta" / "dashboard.md"
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        report.refused.append(f"{path}: {error}")
        return
    rewritten = text
    for old, new in OLD_DASHBOARD_MARKERS:
        rewritten = rewritten.replace(old, new)
    rewritten = _DASHBOARD_MANAGER.sub(rf"\g<1>{DASHBOARD_MANAGER}\g<2>", rewritten)
    if rewritten == text:
        return
    try:
        path.write_text(rewritten, encoding="utf-8")
    except OSError as error:
        report.refused.append(f"{path}: {error}")
        return
    report.dashboards.append(str(path))


def registered_paths(settings_path: Path) -> dict[str, Path]:
    """Every registered knowledge base's name and folder, under whichever key the settings file uses.

    Args:
        settings_path: The settings file to read.

    Returns:
        Each registered knowledge base's folder by its name, or an empty map when the settings file cannot be read or lists none.
    """
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(settings, dict):
        return {}
    rows = settings.get(SETTINGS_KEY) or settings.get(OLD_SETTINGS_KEY) or {}
    if not isinstance(rows, dict):
        return {}
    out: dict[str, Path] = {}
    for name, entry in rows.items():
        path = entry.get("path") if isinstance(entry, dict) else None
        if isinstance(path, str):
            out[str(name)] = Path(path).expanduser()
    return out


def migrate_settings(settings_path: Path, *, report: Report) -> None:
    """Give the settings file's registry of knowledge bases its new key, leaving every other key untouched.

    A file without the old key is left as it is.
    A failure goes to `report.refused`.

    Args:
        settings_path: The settings file to rewrite.
        report: The run's report, which gains a line for the key moved or for the failure.
    """
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        report.refused.append(f"{settings_path}: {error}")
        return
    if not isinstance(settings, dict) or OLD_SETTINGS_KEY not in settings:
        return
    rewritten = {(SETTINGS_KEY if key == OLD_SETTINGS_KEY else key): value for key, value in settings.items()}
    try:
        settings_path.write_text(json.dumps(rewritten, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as error:
        report.refused.append(f"{settings_path}: {error}")
        return
    report.settings.append(f"{OLD_SETTINGS_KEY} -> {SETTINGS_KEY} in {settings_path}")


def needs_migration(settings_path: Path | None = None) -> bool:
    """Whether the settings file still carries the old key `corpora`.

    It answers on the settings key alone.
    Once the file reads `knowledge_bases` it answers False, even for a folder still in the old shape.

    Args:
        settings_path: The settings file to read, or None for the application settings file.

    Returns:
        True while the settings file carries `corpora`, and False otherwise or when the file cannot be read.
    """
    path = settings_path if settings_path is not None else default_settings_path()
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(settings, dict) and OLD_SETTINGS_KEY in settings


def migrate(settings_path: Path | None = None, *, skip: frozenset[str] = frozenset()) -> Report:
    """Rewrite every registered knowledge base and the settings file, skipping the names in `skip`.

    A folder that cannot be read or written is reported and passed over, and the others still run.
    The settings key moves last, after every folder it names has had its turn.

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
        migrate_schema_file(root, report=report)
        migrate_vocabulary_folder(root, report=report)
        migrate_dashboard(root, report=report)
    migrate_settings(path, report=report)
    return report


def main() -> None:
    """Run the migration and print what it did."""
    parser = argparse.ArgumentParser(description="Rewrite the knowledge base state into the vocabulary the code now reads.")
    parser.add_argument("--skip", action="append", default=[], metavar="NAME", help="a registered knowledge base to leave untouched")
    parser.add_argument("--settings", type=Path, default=None, help="the settings file to read and rewrite")
    args = parser.parse_args()
    report = migrate(args.settings, skip=frozenset(args.skip))
    for line in report.lines():
        print(line)  # noqa: T201 (the command's whole output is this listing)


if __name__ == "__main__":
    main()
