"""What both rewrites over the library share: its queries, the payload a page is written back with, and the evidence re-key."""

from __future__ import annotations

import json

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from core.ai.workflows.contracts import Rows

    from harness.migrations.report import LibraryReport

COLLECTION = "workflows"
"""The one collection the library keeps its workflows and reusable steps in."""

PAGES_SQL = """
select c.id, c.type, c.title, c.summary, c.status, c.superseded, c.frontmatter,
       coalesce((select s.body from api.sections s
                 where s.page_id = c.id and s.canon = 'applicability'), '') as applicability
from api.cards c
where c.src_collection = $1
order by c.id
"""
"""Every page of the library, in the parts a page comes back in: the columns plus the long tail."""

SUPERSEDED_BY_SQL = """
select e.src_id, e.dst_id
from api.edges e
where e.rel = 'superseded_by' and e.src_collection = $1
"""
"""Which page each retired version was linked forward to.

`api.cards` says whether a page is superseded but never by which page."""


def payload_for(page: Mapping[str, Any], document: Mapping[str, Any], applicability: str, *, superseded_by: str | None) -> dict[str, Any]:
    """The authoring payload that writes `page` back with `document` as its long tail.

    The long tail is spread first, so the modelled keys always win. A page with a successor gets its `superseded_by` link
    back.

    Args:
        page: The page's row, as `PAGES_SQL` answers it.
        document: The long tail to write back.
        applicability: The applicability prose, written as the page's one section.
        superseded_by: The id of the page that replaced this one, or `None` when none did.

    Returns:
        The payload engram's ingest takes.
    """
    frontmatter: dict[str, Any] = {
        **document,
        "id": str(page["id"]),
        "type": str(page["type"]),
        "title": str(page["title"]),
        "summary": str(page["summary"] or ""),
        "status": str(page["status"]),
    }
    if superseded_by is not None:
        frontmatter["superseded_by"] = f"[[{superseded_by}]]"
    return {"frontmatter": frontmatter, "sections": [{"heading": "Applicability", "body": applicability}]}


async def needs_library_migration(rows: Rows, stale_sql: str, *, collection: str = COLLECTION) -> bool:
    """Whether any page of `collection` is still in the old shape `stale_sql` probes for.

    Args:
        rows: The library's read side.
        stale_sql: The rewrite's probe. It takes the collection as `$1` and answers one row with a `total` column.
        collection: The collection to probe.

    Returns:
        True when the probe counts at least one page in the old shape.
    """
    found = await rows.fetch(stale_sql, collection)
    return bool(found) and int(found[0]["total"]) > 0


async def moved_addresses(
    rows: Rows, addresses_of: Callable[[Mapping[str, object]], tuple[str, str] | None], *, collection: str = COLLECTION
) -> dict[str, str]:
    """Old content address to new one for every workflow page, without rewriting anything.

    Args:
        rows: The library's read side.
        addresses_of: The rewrite's own reading of one page's long tail into its old and new content address.
        collection: The collection to read.

    Returns:
        Each moved workflow's old content address, mapped to its new one.
    """
    moved: dict[str, str] = {}
    for page in await rows.fetch(PAGES_SQL, collection):
        stored = page["frontmatter"]
        pair = addresses_of(stored if isinstance(stored, Mapping) else json.loads(str(stored or "{}")))
        if pair is not None and pair[0] != pair[1]:
            moved[pair[0]] = pair[1]
    return moved


def rekey_evidence(path: Path, moved: Mapping[str, str], *, report: LibraryReport) -> None:
    """Point every evidence record filed under a moved content address at the new one.

    A record's other keys are untouched, so a workflow's trust level reads what it read before the rewrite. Idempotent:
    a second run finds no old address and rewrites nothing. A line that is not JSON is copied through.

    Args:
        path: The evidence file.
        moved: Each old content address, mapped to its new one.
        report: The report this run adds to.
    """
    if not moved or not path.exists():
        return
    out: list[str] = []
    rekeyed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            out.append(line)
            continue
        if isinstance(record, dict) and record.get("spec_hash") in moved:
            record["spec_hash"] = moved[str(record["spec_hash"])]
            rekeyed += 1
            out.append(json.dumps(record))
            continue
        out.append(line)
    if rekeyed:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    report.evidence_rekeyed += rekeyed
