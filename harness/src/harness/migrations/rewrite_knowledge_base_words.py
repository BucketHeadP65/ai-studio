"""Rewrite the saved workflows, reusable steps and workflow knowledge base into the new words.

Three places carry the old ones: a library page's document (a program's source and a briefing's instructions that name
the read and authoring tools' `corpus` parameter, and a fingerprint still keyed `leaves`), the workflow knowledge base's
own schema file and vocabulary folder, and the `leaves:` key in the workflow files beside them.
A program's source and a briefing's instructions are part of a workflow's content address, so every evidence record
filed under the old address is re-keyed in the same run.
Run it with `uv run python -m harness.migrations.rewrite_knowledge_base_words`.
`harness serve` runs it at every boot and rewrites the library only when the probe finds a page in the old shape.
A second run finds nothing and says so.
"""

from __future__ import annotations

import asyncio
import json
import re

from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from core.ai.workflows.contracts import Workflow, spec_identity
from pydantic import JsonValue, ValidationError

from harness.migrations.library import (
    COLLECTION,
    PAGES_SQL,
    SUPERSEDED_BY_SQL,
    moved_addresses,
    needs_library_migration,
    payload_for,
    rekey_evidence,
)
from harness.migrations.report import LibraryReport
from harness.settings import WORKFLOW_KNOWLEDGE_BASE, HarnessSettings, StateSettings

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.workflows.contracts import Ingest, Rows

TEXT_RENAMES = (
    ("corpus_profile", "knowledge_base_profile"),
    ("corpus=", "knowledge_base="),
    ("`corpus` set to", "`knowledge_base` set to"),
    ('"corpus"', '"knowledge_base"'),
    ("'corpus'", "'knowledge_base'"),
)
"""How a saved program or briefing names the knowledge base now, old spelling first.

`corpus_profile` comes first, so the tool name is rewritten whole."""

PROSE_RENAMES = ((r"\bcorpora\b", "knowledge bases"), (r"\bcorpus\b", "knowledge base"))
"""How prose a worker reads names the knowledge base now, as whole words.

Only prose takes these: a briefing's instructions and a page's applicability. A program's `source` never does, and
`TEXT_RENAMES` alone rewrites it."""

FINGERPRINT_KEY = ("leaves", "steps")
"""The fingerprint key that held the workflow's registered steps, old name then new."""

SCHEMA_FILE = ("corpusspec.json", "knowledge-base-schema.json")
"""The workflow knowledge base's schema file, old name then new."""

VOCABULARY_DIR = ("registry", "vocabulary")
"""The folder under `_meta/` holding the vocabulary pages, old name then new."""

TYPE_LISTS = {"claim_kinds": "claim_types", "digest_kinds": "digest_types", "quote_required_kinds": "quote_required_types"}
"""The schema's type vocabularies, by their old key and their new one."""

STALE_SQL = r"""
select count(*) as total
from api.cards c
where c.src_collection = $1
  and (
    c.frontmatter -> 'fingerprint' ? 'leaves'
    or c.frontmatter::text like '%corpus_profile%'
    or c.frontmatter::text like '%corpus=%'
    or c.frontmatter::text like '%`corpus` set to%'
    or c.frontmatter::text like '%"corpus"%'
    or c.frontmatter::text like '%''corpus''%'
    or jsonb_path_exists(c.frontmatter, '$.**.charge ? (@ like_regex "\\ycorpus\\y|\\ycorpora\\y")')
    or jsonb_path_exists(c.frontmatter, '$.**.description ? (@ like_regex "\\ycorpus\\y|\\ycorpora\\y")')
    or jsonb_path_exists(c.frontmatter, '$.**.instructions ? (@ like_regex "\\ycorpus\\y|\\ycorpora\\y")')
  )
"""
"""How many pages still carry an old key or one of the exact old spellings. A starting process runs this probe first.

Only `charge`, `description` and `instructions`, wherever nested, are checked word by word, so a program's `source`
holding a bare local named `corpus` never counts."""


@dataclass
class Report(LibraryReport):
    """What one run of the migration did, in the order it did it."""

    @property
    def touched(self) -> bool:
        """Whether anything at all was rewritten."""
        return bool(self.pages or self.files or self.evidence_rekeyed)

    def lines(self) -> list[str]:
        """One line per thing rewritten, or the single line that says there was nothing to do.

        Returns:
            The lines to print, in order.
        """
        if not self.touched:
            return ["nothing to migrate: every page, schema file and workflow file already reads the new words"]
        out = [f"rewrote {len(self.pages)} library page(s):", *(f"  {page}" for page in self.pages)]
        out.append(f"re-keyed {self.evidence_rekeyed} evidence record(s) onto the new content address")
        if self.files:
            out.extend(["rewrote or renamed:", *(f"  {name}" for name in self.files)])
        if self.unlinked:
            out.extend(["left alone, superseded with no link to restore:", *(f"  {page}" for page in self.unlinked)])
        if self.refused:
            out.extend(["left alone, the rewrite would not validate:", *(f"  {why}" for why in self.refused)])
        return out


def renamed(value: JsonValue) -> JsonValue:
    """`value` with every old spelling replaced, in every string anywhere inside it.

    Args:
        value: A JSON value. It is never changed.

    Returns:
        A rewritten copy of `value`.
    """
    if isinstance(value, str):
        for old, new in TEXT_RENAMES:
            value = value.replace(old, new)
        return value
    if isinstance(value, dict):
        return {key: renamed(item) for key, item in value.items()}
    if isinstance(value, list):
        return [renamed(item) for item in value]
    return value


def renamed_prose(text: str) -> str:
    """`text` with the old words replaced, the whole-word spellings prose uses included.

    Args:
        text: Prose a person or a worker reads.

    Returns:
        The rewritten text.
    """
    out = cast("str", renamed(text))
    for pattern, word in PROSE_RENAMES:
        out = re.sub(pattern, word, out)
    return out


PROSE_FIELDS = ("charge", "description", "instructions")
"""The keys of a saved workflow or step that hold prose a person or a worker reads.

Only these take the whole-word spellings. A program's `source` never does."""


def _rewrite_prose_fields(holder: dict[str, Any]) -> None:
    """Give every prose field in `holder` and in anything nested under it the whole-word spellings."""
    for key, value in holder.items():
        if key in PROSE_FIELDS and isinstance(value, str):
            holder[key] = renamed_prose(value)
        elif isinstance(value, dict):
            _rewrite_prose_fields(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _rewrite_prose_fields(item)


@dataclass(frozen=True)
class MigratedPage:
    """One page rewritten into the new words, and the content address the rewrite moved it from."""

    document: dict[str, Any]
    """The page's long tail in the new words."""
    applicability: str
    """The page's applicability prose in the new words."""
    was: str | None = None
    """The workflow's content address before the rewrite, or `None` when the rewrite did not move it."""
    now: str | None = None
    """Its content address after the rewrite, never equal to `was`. `None`, like `was`, when the rewrite did not move the address."""


def migrated_document(frontmatter: Mapping[str, object], applicability: str) -> MigratedPage | None:
    """One page's long tail and applicability prose in the new words, or `None` when they already read that way.

    Three rewrites: every string in the document takes the new spellings (a program's source, a briefing's
    instructions, a step's tool list), every prose field takes the whole-word ones as well, and a fingerprint's
    `leaves` becomes `steps`. The result is validated against the models before it is offered, so a page that would come
    back unreadable is never written.

    Args:
        frontmatter: The page's long tail as the library stores it. It is never changed.
        applicability: The page's applicability prose.

    Returns:
        The rewritten page with the content addresses the rewrite moved it between, or `None` when nothing needed a
        rewrite.

    Raises:
        ValueError: The rewritten workflow does not validate.
    """
    document: dict[str, Any] = json.loads(json.dumps(dict(frontmatter)))
    before = json.loads(json.dumps(document.get("spec"))) if isinstance(document.get("spec"), dict) else None
    rewritten = cast("dict[str, Any]", renamed(document))
    for holder in (rewritten.get("spec"), rewritten.get("step")):
        if isinstance(holder, dict):
            _rewrite_prose_fields(holder)
    stamp = rewritten.get("fingerprint")
    if isinstance(stamp, dict) and FINGERPRINT_KEY[0] in stamp:
        stamp[FINGERPRINT_KEY[1]] = stamp.pop(FINGERPRINT_KEY[0])
    prose = renamed_prose(applicability)
    if rewritten == document and prose == applicability:
        return None
    named = str(document.get("id", "")) or "this page"
    return _checked(rewritten, prose, moved_from=before, named=named)


def _checked(document: dict[str, Any], applicability: str, *, moved_from: dict[str, Any] | None, named: str) -> MigratedPage:
    """`document` validated against the models, paired with the two addresses a moved workflow has."""
    spec = document.get("spec")
    try:
        now = spec_identity(Workflow.model_validate(spec)) if isinstance(spec, dict) else None
        was = spec_identity(Workflow.model_validate(moved_from)) if moved_from is not None else None
    except ValidationError as error:
        msg = f"{named}: the rewritten page does not validate: {error}"
        raise ValueError(msg) from error
    moved = was is not None and now is not None and was != now
    return MigratedPage(document=document, applicability=applicability, was=was if moved else None, now=now if moved else None)


def addresses_of(frontmatter: Mapping[str, object]) -> tuple[str, str] | None:
    """One workflow page's content address before and after the rewrite, or `None` for anything else.

    It answers for every page carrying a workflow, whether or not this run rewrote it, so a re-run re-keys the evidence a
    crashed run left behind.

    Args:
        frontmatter: The page's long tail, in the old words or the new.

    Returns:
        The old and the new content address, or `None` for a page with no workflow or one whose workflow does not parse.
    """
    spec = frontmatter.get("spec")
    if not isinstance(spec, dict):
        return None
    after = cast("dict[str, Any]", renamed(spec))
    _rewrite_prose_fields(after)
    try:
        return spec_identity(Workflow.model_validate(spec)), spec_identity(Workflow.model_validate(after))
    except ValidationError:
        return None


async def migrate_library(rows: Rows, ingest: Ingest, *, collection: str = COLLECTION, report: Report) -> dict[str, str]:
    """Rewrite every page of `collection` that still carries an old word, through engram's own ingest.

    A page superseded by another is written back with its forward link restored. A superseded page whose successor
    cannot be named goes to `report.unlinked` and is left alone. A page whose rewrite does not validate goes to
    `report.refused`, and the others are still rewritten.

    Args:
        rows: The library's read side.
        ingest: Engram's write path.
        collection: The collection to rewrite.
        report: The report this run adds to.

    Returns:
        Old content address to new one, for every workflow page whose address the rewrite moves, rewritten or not.
    """
    links = {str(edge["src_id"]): str(edge["dst_id"]) for edge in await rows.fetch(SUPERSEDED_BY_SQL, collection)}
    moved: dict[str, str] = {}
    for page in await rows.fetch(PAGES_SQL, collection):
        stored = page["frontmatter"]
        document = stored if isinstance(stored, Mapping) else json.loads(str(stored or "{}"))
        page_id = str(page["id"])
        applicability = str(page["applicability"] or "")
        pair = addresses_of(document)
        if pair is not None and pair[0] != pair[1]:
            moved[pair[0]] = pair[1]
        try:
            migrated = migrated_document(document, applicability)
        except ValueError as refusal:
            report.refused.append(f"{page_id}: {refusal}")
            continue
        if migrated is None:
            continue
        superseded_by = links.get(page_id)
        if superseded_by is None and bool(page["superseded"]):
            report.unlinked.append(page_id)
            continue
        await ingest(payload_for(page, migrated.document, migrated.applicability, superseded_by=superseded_by), collection=collection)
        report.pages.append(page_id)
    return moved


def migrated_schema(document: dict[str, Any]) -> dict[str, Any] | None:
    """`document` in the new keys, or `None` when it already reads that way.

    A page type entry's `kind` becomes `page_type`, a compounding entry's `from.kinds` becomes `from.page_types`, the
    three type vocabularies take their `*_types` names, and `path_root: "corpus"` becomes `path_root: "knowledge_base"`.

    Args:
        document: A schema file's parsed JSON object. It is never changed.

    Returns:
        A rewritten copy of `document`, or `None` when no key or value needed a rewrite.
    """
    out: dict[str, Any] = json.loads(json.dumps(document))
    changed = False
    page_types = out.get("page_types")
    if isinstance(page_types, list):
        for entry in page_types:
            if isinstance(entry, dict) and "kind" in entry:
                entry["page_type"] = entry.pop("kind")
                changed = True
    entries = out.get("compounding", {}).get("entries") if isinstance(out.get("compounding"), dict) else None
    for entry in entries if isinstance(entries, list) else []:
        source = entry.get("from") if isinstance(entry, dict) else None
        if isinstance(source, dict) and "kinds" in source:
            source["page_types"] = source.pop("kinds")
            changed = True
    for old, new in TYPE_LISTS.items():
        for holder in (out, out.get("claims"), out.get("sources")):
            if isinstance(holder, dict) and old in holder:
                holder[new] = holder.pop(old)
                changed = True
    if out.get("path_root") == "corpus":
        out["path_root"] = "knowledge_base"
        changed = True
    return out if changed else None


def migrate_knowledge_base(root: Path, *, report: Report) -> None:
    """Give the workflow knowledge base its schema file, its vocabulary folder and its files' new names.

    Args:
        root: The workflow knowledge base's folder.
        report: The report this run adds to.
    """
    old_schema, new_schema = root / SCHEMA_FILE[0], root / SCHEMA_FILE[1]
    if old_schema.is_file() and not new_schema.exists():
        try:
            document = json.loads(old_schema.read_text(encoding="utf-8"))
            migrated = migrated_schema(document) if isinstance(document, dict) else None
            new_schema.write_text(json.dumps(migrated or document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            old_schema.unlink()
        except (OSError, json.JSONDecodeError) as error:
            report.refused.append(f"{old_schema}: {error}")
        else:
            report.files.append(f"{SCHEMA_FILE[0]} -> {SCHEMA_FILE[1]} in {root}")
    old_dir, new_dir = root / "_meta" / VOCABULARY_DIR[0], root / "_meta" / VOCABULARY_DIR[1]
    if old_dir.is_dir() and not new_dir.exists():
        try:
            old_dir.rename(new_dir)
        except OSError as error:
            report.refused.append(f"{old_dir}: {error}")
        else:
            report.files.append(f"_meta/{VOCABULARY_DIR[0]} -> _meta/{VOCABULARY_DIR[1]} in {root}")
    migrate_workflow_files(root, report=report)


def migrate_workflow_files(root: Path, *, report: Report) -> None:
    """Rewrite the fingerprint key `leaves:` and the old words in every markdown file under `root`.

    Args:
        root: The folder searched, with every folder below it, for markdown files.
        report: The report this run adds to.
    """
    for file in sorted(root.rglob("*.md")):
        try:
            text = file.read_text(encoding="utf-8")
        except OSError as error:
            report.refused.append(f"{file}: {error}")
            continue
        rewritten = text.replace("\n  leaves:", "\n  steps:").replace("\nleaves:", "\nsteps:")
        for old, new in TEXT_RENAMES:
            rewritten = rewritten.replace(old, new)
        if rewritten == text:
            continue
        try:
            file.write_text(rewritten, encoding="utf-8")
        except OSError as error:
            report.refused.append(f"{file}: {error}")
            continue
        report.files.append(str(file))


async def migrate(state: StateSettings) -> Report:
    """Run every part of the migration against this deployment's own library and workflow knowledge base.

    Args:
        state: The state settings whose home holds the evidence file.

    Returns:
        What the run did.
    """
    from harness.adapters.contracts import store_connection, store_ingest  # noqa: PLC0415 (the write path stays off this module's import path)

    report = Report()
    migrate_knowledge_base(WORKFLOW_KNOWLEDGE_BASE, report=report)
    connection = await store_connection()
    try:
        if await needs_library_migration(connection, STALE_SQL):
            async with AsyncExitStack() as stack:
                ingest = await stack.enter_async_context(store_ingest())
                moved = await migrate_library(connection, ingest, report=report)
        else:
            # No page to rewrite still leaves a re-key an earlier run may not have reached, so the
            # addresses are read either way and `rekey_evidence` decides whether there is work.
            moved = await moved_addresses(connection, addresses_of)
        rekey_evidence(state.evidence_log, moved, report=report)
    finally:
        await connection.close()
    return report


def main() -> None:
    """Run the migration and print what it did."""
    report = asyncio.run(migrate(HarnessSettings().state))
    for line in report.lines():
        print(line)  # noqa: T201 (the command's whole output is this listing)


if __name__ == "__main__":
    main()
