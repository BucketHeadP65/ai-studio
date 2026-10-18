"""One-time repair: every stored workflow page's map nodes, moved from `work`/`then` to `turns`.

Written and unit-tested here only. Never run this against the live store from this checkout --
the store's own cutover runs it once, by hand, at the owner's chosen moment:

    cd harness && uv run python scripts/migrate_map_turns.py

Reads every workflow page in the store's `workflows` collection, live and archived alike, with
the same raw `PAGE_SQL` read `LibraryCollection.set_archived` uses (the validated `fetch_workflow`
path would refuse an unmigrated document outright). `migrate_spec` rewrites each page's spec in
place; only a page that actually changed is re-ingested. A final scan over the same pages proves
no map node still names an old field.

A page's spec identity is a hash of its canonical JSON (`spec_identity` in
`core.ai.workflows.domain.fingerprint`). Moving a map's `work` (and `then`, `then_each`, `then_inputs`)
to `turns` changes that JSON, so a migrated page gets a new content address. Every approval and
attestation filed under its old address stays in the ledger untouched, but
`FileEvidenceLog.history_for(spec_hash=...)` no longer finds it there. That lookup backs the
trust line a workflow card shows, wherever one is rendered, and the approve endpoint's own history
refresh after it records a new approval. A spec with no map node is untouched by the migration and
keeps its old address.
"""

from __future__ import annotations

import asyncio
import json

from collections.abc import Mapping, Sequence
from typing import Protocol

from core.ai.workflows.contracts import Ingest, LibraryCollection
from core.ai.workflows.infrastructure.library_collection import PAGE_SQL

from harness.adapters.knowledge_base import store_ingest
from harness.adapters.store import store_connection
from harness.features.authoring.turns_migration import MigrationConflict, migrate_spec

COLLECTION = "workflows"


class RowFetcher(Protocol):
    """The one pool capability this script's page reads need: raw rows for one query.

    Narrow on purpose so a test can stand in for the pool with a hand-written fake rather than a
    live connection. `asyncpg.Pool.fetch` has this call shape, and its rows are `asyncpg.Record`,
    not mappings: this script only ever subscripts a row by column name, the one operation a
    `Record` and a plain dict share, so a fake answering dicts stands in faithfully. Iterating a
    row, `dict(row)`, or `key in row` would type-check here and behave differently on a `Record`.
    """

    async def fetch(self, query: str, /, *args: object) -> Sequence[Mapping[str, object]]:
        """Run `query` against `args`, answering every row -- empty when nothing matched."""
        ...


_EVERY_PAGE = 100_000
"""The `catalog`/`archived` page cap: high enough that no real collection is ever cut short."""

_OLD_MAP_FIELDS = ("work", "then", "then_each", "then_inputs")


async def _every_workflow_id(store: LibraryCollection) -> list[str]:
    """Every workflow page's id in this collection, live and archived, newest archive last."""
    live = await store.catalog(limit=_EVERY_PAGE)
    archived = await store.archived(limit=_EVERY_PAGE)
    return [card.id for card in (*live, *archived)]


async def _raw_page(pool: RowFetcher, workflow_id: str) -> tuple[Mapping[str, object], dict[str, object]] | None:
    """The page row and its frontmatter's long tail, read raw, or `None` when the page has vanished since it was listed.

    Raw because the model this store validates against refuses an unmigrated page, and vanishing
    is real: the listing and this read are two round trips, and nothing holds the store still
    between them.
    """
    found = await pool.fetch(PAGE_SQL, workflow_id)
    if not found:
        return None
    page = found[0]
    stored = page["frontmatter"]
    long_tail = dict(stored if isinstance(stored, Mapping) else json.loads(stored or "{}"))
    return page, long_tail


async def _reingest(ingest: Ingest, page: Mapping[str, object], long_tail: Mapping[str, object]) -> None:
    """Rebuild the page the way `LibraryCollection.set_archived` rebuilds one, with the migrated long tail."""
    await ingest(
        {
            "frontmatter": {
                **long_tail,
                "id": str(page["id"]),
                "type": str(page["type"]),
                "title": str(page["title"]),
                "summary": str(page["summary"] or ""),
                "status": str(page["status"]),
            },
            "sections": [{"heading": "Applicability", "body": str(page["applicability"] or "")}],
        },
        collection=COLLECTION,
    )


async def _migrate_one(pool: RowFetcher, ingest: Ingest, workflow_id: str) -> str:
    """Migrate one page's spec and re-ingest it if anything changed. One report line, always."""
    raw = await _raw_page(pool, workflow_id)
    if raw is None:
        return f"{workflow_id}: vanished since it was listed, skipped"
    page, long_tail = raw
    spec = long_tail.get("spec")
    if not isinstance(spec, Mapping):
        return f"{workflow_id}: no spec on this page, left alone"
    try:
        migrated_spec, changed = migrate_spec(spec)
    except MigrationConflict as exc:
        return f"{workflow_id}: refused -- {exc}"
    if not changed:
        return f"{workflow_id}: already turns"
    long_tail["spec"] = migrated_spec
    await _reingest(ingest, page, long_tail)
    return f"{workflow_id}: migrated"


def _old_fields_on_map_nodes(spec: Mapping[str, object]) -> list[str]:
    """Every map node in `spec` still naming a retired chain field, as `"node_id: field, field"` lines."""
    nodes = spec.get("nodes")
    if not isinstance(nodes, list):
        return []
    lines = []
    for node in nodes:
        if not isinstance(node, Mapping) or node.get("kind") != "map":
            continue
        carried = [field for field in _OLD_MAP_FIELDS if field in node]
        if carried:
            lines.append(f"{node.get('id', '?')}: {', '.join(carried)}")
    return lines


async def _scan_for_leftovers(pool: RowFetcher, workflow_ids: list[str]) -> list[str]:
    """Every page that, read fresh after migration, still names an old field on a map node.

    A page vanished since it was listed names nothing here either -- `_migrate_one` already
    reported it skipped, and there is no spec left to scan.
    """
    leftovers = []
    for workflow_id in workflow_ids:
        raw = await _raw_page(pool, workflow_id)
        if raw is None:
            continue
        _, long_tail = raw
        spec = long_tail.get("spec")
        if isinstance(spec, Mapping):
            leftovers.extend(f"{workflow_id} {line}" for line in _old_fields_on_map_nodes(spec))
    return leftovers


async def main() -> None:
    """Migrate every page in the workflow store, then prove none of them still speak the old grammar."""
    pool = await store_connection()
    try:
        async with store_ingest() as ingest:
            store = LibraryCollection(pool, ingest, collection=COLLECTION)
            workflow_ids = await _every_workflow_id(store)
            for workflow_id in workflow_ids:
                print(await _migrate_one(pool, ingest, workflow_id))
            leftovers = await _scan_for_leftovers(pool, workflow_ids)
            if leftovers:
                print("still naming an old field on a map node:")
                for line in leftovers:
                    print(f"  {line}")
            else:
                print("scan: no page names an old field on a map node")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
