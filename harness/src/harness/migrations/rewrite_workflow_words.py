"""Rewrite the state written under the old workflow words into the new ones.

Four places carry them: the library's saved workflows and reusable steps (`spec.tasks`, `fingerprint.world`, `packed_by`
and a lineage's `*_task` op names), the evidence file, the run-log files, and any workflow file still keyed `tasks:`.
Renaming `tasks` to `inline` moves a workflow's content address, so every evidence record filed under the old address is
re-keyed in the same run.
Run it with `uv run python -m harness.migrations.rewrite_workflow_words`.
`harness serve` runs it at every boot and rewrites the library only when the probe finds a page in the old shape.
A second run finds nothing and says so.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.ai.workflows.contracts import Lineage, Workflow, WorkflowFingerprint, spec_identity
from pydantic import ValidationError

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
from harness.settings import CHECKOUT, HarnessSettings, StateSettings

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.workflows.contracts import Ingest, Rows

OP_NAMES = {"add_task": "add_inline_step", "replace_task": "replace_inline_step", "remove_task": "remove_inline_step"}
"""The adaptation op names a saved lineage may still carry, and what each one is called now."""

STALE_SQL = """
select count(*) as total
from api.cards c
where c.src_collection = $1
  and (c.frontmatter -> 'spec' ? 'tasks' or c.frontmatter -> 'fingerprint' ? 'world' or c.frontmatter ? 'packed_by'
       or exists (select 1 from jsonb_array_elements(c.frontmatter -> 'lineage' -> 'ops') o
                  where o ->> 'op' in ('add_task', 'replace_task', 'remove_task')))
"""
"""How many pages still carry an old key. A starting process runs this probe first."""


@dataclass
class Report(LibraryReport):
    """What one run of the migration did, in the order it did it."""

    workflow_files: list[str] = field(default_factory=list)
    """The path of each workflow file rewritten from `tasks:` to `inline:`."""
    lineages: int = 0
    """How many rewritten pages carry a lineage."""

    @property
    def touched(self) -> bool:
        """Whether anything at all was rewritten."""
        return bool(self.pages or self.files or self.workflow_files or self.evidence_rekeyed)

    def lines(self) -> list[str]:
        """One line per thing rewritten, or the single line that says there was nothing to do.

        Returns:
            The lines to print, in order.
        """
        if not self.touched:
            return ["nothing to migrate: every page, file and workflow file already reads the new keys"]
        out = [f"rewrote {len(self.pages)} library page(s):", *(f"  {page}" for page in self.pages)]
        out.append(f"rewrote {self.lineages} lineage(s) carrying an old adaptation op name")
        out.append(f"re-keyed {self.evidence_rekeyed} evidence record(s) onto the new content address")
        if self.files:
            out.extend(["renamed:", *(f"  {name}" for name in self.files)])
        if self.workflow_files:
            out.extend(["rewrote `tasks:` to `inline:` in:", *(f"  {name}" for name in self.workflow_files)])
        if self.unlinked:
            out.extend(["left alone, superseded with no link to restore:", *(f"  {page}" for page in self.unlinked)])
        if self.refused:
            out.extend(["left alone, the rewrite would not validate:", *(f"  {why}" for why in self.refused)])
        return out


def address_before(spec: Mapping[str, Any]) -> str:
    """The content address this workflow had while its inline steps were spelled `tasks`.

    The address is rebuilt through the models, never read off the row, and reproduces the address evidence was filed
    under at save time. A workflow saved again since a run gives a different address, and `rekey_evidence` leaves that
    run's records alone.

    Args:
        spec: A saved workflow still keyed `tasks`, as the library stores it.

    Returns:
        The workflow's old content address.

    Raises:
        ValidationError: The workflow does not validate.
    """
    document = dict(spec)
    document["inline"] = document.pop("tasks")
    canonical = Workflow.model_validate(document).model_dump(mode="json")
    canonical["tasks"] = canonical.pop("inline")
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class MigratedPage:
    """One page rewritten into the new keys, and the content address the rewrite moved it from."""

    document: dict[str, Any]
    """The page's long tail in the new keys."""
    was: str | None = None
    """The workflow's content address before the rewrite, or `None` when the rewrite did not move it."""
    now: str | None = None
    """Its content address after the rewrite, never equal to `was`. `None`, like `was`, when the rewrite did not move the address."""


def migrated_document(frontmatter: Mapping[str, object]) -> MigratedPage | None:
    """One page's long tail in the new keys, or `None` when it already reads that way.

    Four rewrites: a workflow's `spec.tasks` becomes `spec.inline`, a fingerprint's `world` becomes `environment` without
    the orchestrator model id, `packed_by` becomes `saved_by`, and a lineage's `add_task`, `replace_task` and
    `remove_task` ops take their new names. The result is validated against the models before it is offered, so a page
    that would come back unreadable is never written.

    Args:
        frontmatter: The page's long tail as the library stores it. It is never changed.

    Returns:
        The rewritten page with the content addresses the rewrite moved it between, or `None` when no key needed a rewrite.

    Raises:
        ValueError: The rewritten workflow, fingerprint or lineage does not validate.
    """
    document: dict[str, Any] = json.loads(json.dumps(dict(frontmatter)))
    changed = False
    before: dict[str, Any] | None = None
    spec = document.get("spec")
    if isinstance(spec, dict) and "tasks" in spec:
        before = json.loads(json.dumps(spec))
        spec["inline"] = spec.pop("tasks")
        changed = True
    stamp = document.get("fingerprint")
    if isinstance(stamp, dict) and "world" in stamp:
        environment = dict(stamp.pop("world"))
        environment.pop("model", None)
        stamp["environment"] = environment
        changed = True
    if "packed_by" in document:
        document["saved_by"] = document.pop("packed_by")
        changed = True
    lineage = document.get("lineage")
    if isinstance(lineage, dict) and _renamed_ops(lineage):
        changed = True
    if not changed:
        return None
    return _checked(document, moved_from=before, named=str(document.get("id", "")) or "this page")


def _checked(document: dict[str, Any], *, moved_from: dict[str, Any] | None, named: str) -> MigratedPage:
    """`document` validated against the models, paired with the two addresses a moved workflow has."""
    spec, stamp, lineage = document.get("spec"), document.get("fingerprint"), document.get("lineage")
    try:
        # `spec_identity` over the parsed workflow, so the new address is exactly what every later
        # lookup computes, not a second spelling of the same intent.
        now = spec_identity(Workflow.model_validate(spec)) if isinstance(spec, dict) else None
        was = address_before(moved_from) if moved_from is not None else None
        if isinstance(stamp, dict):
            WorkflowFingerprint.model_validate(stamp)
        if isinstance(lineage, dict):
            Lineage.model_validate(lineage)
    except ValidationError as error:
        msg = f"{named}: the rewritten page does not validate: {error}"
        raise ValueError(msg) from error
    return MigratedPage(document=document, was=was, now=now if was is not None else None)


def _renamed_ops(lineage: dict[str, Any]) -> bool:
    """Give every `*_task` op in `lineage` its new name in place, and say whether any moved."""
    ops = lineage.get("ops")
    if not isinstance(ops, list):
        return False
    moved = False
    for op in ops:
        if isinstance(op, dict) and op.get("op") in OP_NAMES:
            op["op"] = OP_NAMES[str(op["op"])]
            moved = True
    return moved


def addresses_of(frontmatter: Mapping[str, object]) -> tuple[str, str] | None:
    """One workflow page's content address before and after the rewrite, or `None` for anything else.

    It answers for every page carrying a workflow, whether or not this run rewrote it, so a re-run re-keys the evidence a
    crashed run left behind.

    Args:
        frontmatter: The page's long tail, in the old keys or the new.

    Returns:
        The old and the new content address, or `None` for a page with no workflow or one whose workflow does not parse.
    """
    spec = frontmatter.get("spec")
    if not isinstance(spec, dict):
        return None
    old_shape = dict(spec)
    if "inline" in old_shape:
        old_shape["tasks"] = old_shape.pop("inline")
    if "tasks" not in old_shape:
        return None
    new_shape = dict(old_shape)
    new_shape["inline"] = new_shape.pop("tasks")
    try:
        return address_before(old_shape), spec_identity(Workflow.model_validate(new_shape))
    except ValidationError:
        return None


async def migrate_library(rows: Rows, ingest: Ingest, *, collection: str = COLLECTION, report: Report) -> dict[str, str]:
    """Rewrite every page of `collection` that still carries an old key, through engram's own ingest.

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
        pair = addresses_of(document)
        if pair is not None and pair[0] != pair[1]:
            moved[pair[0]] = pair[1]
        try:
            migrated = migrated_document(document)
        except ValueError as refusal:
            report.refused.append(f"{page_id}: {refusal}")
            continue
        if migrated is None:
            continue
        superseded_by = links.get(page_id)
        if superseded_by is None and bool(page["superseded"]):
            report.unlinked.append(page_id)
            continue
        await ingest(
            payload_for(page, migrated.document, str(page["applicability"] or ""), superseded_by=superseded_by), collection=collection
        )
        report.pages.append(page_id)
        if isinstance(migrated.document.get("lineage"), dict):
            report.lineages += 1
    return moved


def migrate_state_files(state: StateSettings, *, report: Report) -> None:
    """Rename the evidence file and the run-log files under `state`'s home to the names the code reads.

    Args:
        state: The state settings whose home holds the files.
        report: The report this run adds to.
    """
    home = state.home
    for old, new in ((home / "attestations.jsonl", state.evidence_log), (home / "run-log.json", state.waiting_run(project=False))):
        if old.exists() and not new.exists():
            old.rename(new)
            report.files.append(f"{old.name} -> {new.name}")
    for old in sorted(home.rglob("*.parked-run.json")):
        new = old.with_name(old.name.replace(".parked-run.json", ".waiting-run.json"))
        if not new.exists():
            old.rename(new)
            report.files.append(f"{old.name} -> {new.name}")


def migrate_workflow_files(root: Path, *, report: Report) -> None:
    """Rewrite the YAML key `tasks:` to `inline:` in every workflow file under `root/workflows`.

    Args:
        root: The folder whose `workflows` folder holds the workflow files.
        report: The report this run adds to.
    """
    for file in sorted((root / "workflows").glob("*.md")):
        text = file.read_text(encoding="utf-8")
        if "\n  tasks:\n" not in text:
            continue
        file.write_text(text.replace("\n  tasks:\n", "\n  inline:\n"), encoding="utf-8")
        report.workflow_files.append(str(file))


async def migrate(state: StateSettings) -> Report:
    """Run every part of the migration against this deployment's own library and the state under `state`'s home.

    Args:
        state: The state settings whose home holds the evidence file and the run-log files.

    Returns:
        What the run did.
    """
    from harness.adapters.contracts import (  # noqa: PLC0415 (the write path stays off this module's import path)
        store_connection,
        store_ingest,
    )

    report = Report()
    migrate_state_files(state, report=report)
    migrate_workflow_files(CHECKOUT / "workflows", report=report)
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
