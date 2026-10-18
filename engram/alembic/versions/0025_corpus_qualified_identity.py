"""Corpus-qualified identity: every corpus-owned table's key becomes `(collection, id)`.

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-20 00:00:00.000000

`memory.pages.id` (and its analogues on `sections`/`claims`/`unit_summaries`/`vocab`/`sources_raw`)
is today the sole primary key shared by every collection compiled into the same database —
`collection` rides beside it as an ordinary column. Two collections claiming the same id is an
upsert, not an error: the later writer wins, and the row's `collection` never changes. This
revision closes that hole structurally by making `collection` part of the key at every level a unit
is stored, referenced, or served, so a same-id write from another corpus becomes a second row
instead of a silent theft.

Every reference between rows is one of two kinds (fixed by the ORM `0024`'s composite mappers
already declare, `compile/orm.py` is this migration's authority when prose disagrees):

* **Containment** — the row belongs to an owner in the same corpus (a section to its page, a claim
  to its page, a raw chunk to its source mirror). A containment reference shares the row's own
  `collection` column and takes a composite FK `(collection, owner_id)`, `ON DELETE CASCADE` kept
  where it had one.
* **Citation** — the row points at a unit that may live in another corpus (a claim's subject, a
  comparison cell's row entity, a resolved edge's endpoint, a dangling link's target). A citation
  carries its own target-collection column, takes a composite FK on the pair (no cascade), and gets
  a `CHECK ((x_id IS NULL) = (x_collection IS NULL))` so a half-qualified reference cannot exist.
  Every citation is backfilled to the row's own collection below — the resolver cannot yet produce a
  genuine cross-corpus reference, so every existing citation already targets its own corpus.

Five phases, each transactional:

1. Add every new identity column, nullable (`_add_new_columns`).
2. Backfill: containment columns from the parent page, citation columns to the row's own
   collection, edge endpoint collections via each endpoint's owning page — then two loud guards,
   aborting the whole migration on either failure: every edge endpoint must resolve to a page, and
   no id/key may already be claimed by more than one collection at any unit level
   (`_backfill_columns`, `_assert_no_unresolved_edges`, `_assert_no_cross_collection_duplicates`).
   `memory.vocab` is the one table with no owning row to backfill from — its pre-existing rows are a
   single, undifferentiated registry contributed by every corpus that ever compiled against it
   (`compile/repository.py`'s `VocabRepository` docstring), so they backfill to `'research'`, the
   same founding collection the old `server_default` silently filed unlabeled rows under. A fresh
   compile of any collection writes its own scoped rows going forward.
3. `NOT NULL` on every new column, plus the null-pairing `CHECK`s (`_finalize_not_null_and_checks`);
   then drop every touched view (`_drop_touched_views`) — one of them (`api.scoreboard`, see below)
   has a `pg_depend` edge onto a primary key phase 4 is about to drop, so the views must come down
   before that, not after.
4. Drop the old single-column primary keys and every foreign key that depended on one, in
   dependency order (children before the parents they reference); create the composite primary
   keys, `sources_raw`'s `UNIQUE (collection, provider, provider_key)` (one native item mirrored
   at most once per collection), the composite foreign keys mirroring
   `compile/orm.py`'s `__table_args__` exactly, and two supporting indexes for the `page_id`-keyed
   child tables (`_drop_old_keys`, `_create_composite_keys_and_indexes`).
5. Re-create every `api` function and every view `_drop_touched_views` dropped, over the widened
   key shape:
   `api.fetch`, `api.lookup_value`, `api.envelope_page`, `api.envelope_section` (all gain a leading
   `p_collection` parameter), `api.graph_expand`/`api.graph_paths` (built from the same
   `core.ai.retrieval` traversal builders 0010 uses, now with `partition_col="src_collection"`), and
   the views `cards`/`sections`/`claims`/`edges`/`raw_chunks` (pair joins, new collection output
   columns) plus `api.scoreboard` (a research projection, forced in: its `GROUP BY p.id, p.title`
   selects other `p.*` columns via PostgreSQL's group-by-primary-key functional dependency, which
   records a hard `pg_depend` edge onto `pages_pkey` — the PK swap cannot proceed while it exists,
   so it is dropped, pair-joined, and recreated alongside the spine views rather than left for a
   later slice) — `api.facets` already carries `collection` and joins nothing, so it is untouched
   (`_recreate_api_surface_new`).

Apache AGE's projected property graph carries no partition tag, so it cannot express a
collection-scoped traversal at all (`core.ai.retrieval.graph_builder.AgeGraphTraversalBuilder`
refuses a partitioned `GraphSchema` with `ValueError`, by design, pending the AGE projection fork).
`_graph_bodies` re-raises that refusal as this migration's own `RuntimeError`, naming the limitation
and the portable recursive-CTE alternative, rather than letting a bare `ValueError` reach the
operator — a database configured for the Apache AGE graph backend cannot apply this revision until
that fork lands.

Downgrade reverses all five phases, in reverse order, and is honest about the one thing it cannot
reverse: if two collections have taken the same id or key while the composite key was in force,
collapsing back to a global one would silently drop one of them. `_assert_no_cross_collection_duplicates`
runs FIRST, before any DDL, and aborts loudly rather than let that happen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import context, op
from core.ai.retrieval.contracts import AgeGraphTraversalBuilder, CteGraphTraversalBuilder, GraphSchema

from engram.retrieval.graph_backend.backend import EXPAND_COMMENT, PATHS_COMMENT

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "corpus_reader"

# The founding collection pre-existing, undifferentiated `memory.vocab` rows backfill to — see the
# module docstring's phase-2 note.
_VOCAB_BACKFILL_COLLECTION = "research"

# ~8k tokens ≈ 32k chars (4 chars/token heuristic); byte-identical to 0002/0010's constant.
_FETCH_CHAR_BUDGET = 32000
_FETCH_TRUNCATION_MARKER = "[truncated — fetch sections instead]"

# A page's whole text rides its envelope inline below this section-body character sum, byte-identical
# to 0022's constant.
_INLINE_CAP_CHARS = 6_000

_OLD_FETCH_SIGNATURE = "api.fetch(text)"
_NEW_FETCH_SIGNATURE = "api.fetch(text, text)"
_OLD_LOOKUP_VALUE_SIGNATURE = "api.lookup_value(text, text)"
_NEW_LOOKUP_VALUE_SIGNATURE = "api.lookup_value(text, text, text)"
_OLD_ENVELOPE_PAGE_SIGNATURE = "api.envelope_page(text)"
_NEW_ENVELOPE_PAGE_SIGNATURE = "api.envelope_page(text, text)"
_OLD_ENVELOPE_SECTION_SIGNATURE = "api.envelope_section(text)"
_NEW_ENVELOPE_SECTION_SIGNATURE = "api.envelope_section(text, text)"
_OLD_EXPAND_SIGNATURE = "api.graph_expand(text[], integer, text[], integer)"
_NEW_EXPAND_SIGNATURE = "api.graph_expand(text, text[], integer, text[], integer)"
_OLD_PATHS_SIGNATURE = "api.graph_paths(text, text, integer, text[], integer)"
_NEW_PATHS_SIGNATURE = "api.graph_paths(text, text, text, integer, text[], integer)"

# The touched views' names: dropped in `_drop_touched_views` and recreated in
# `_recreate_api_surface_new` (upgrade), or dropped there and recreated in `_recreate_views_old`
# (downgrade). `facets` is not among them: it already carries `collection` and joins nothing else,
# so 0025 leaves it untouched. `scoreboard` is forced in
# despite being a research projection: PostgreSQL's GROUP-BY-primary-key functional
# dependency (its `GROUP BY p.id, p.title` selects other `p.*` columns without aggregating them,
# legal only because `p.id` was the whole key) records a hard `pg_depend` edge onto `pages_pkey`
# — `ALTER TABLE memory.pages DROP CONSTRAINT pages_pkey` fails outright while that view exists,
# so it must be dropped before the swap regardless of scope, and while it's open its own
# `subject_id`/`page_id` joins get the same pair-join fix as the spine views (a merged entity's
# claim counts must not cross collections either).
_TOUCHED_VIEWS: tuple[str, ...] = ("cards", "sections", "claims", "edges", "raw_chunks", "scoreboard")


def _dialect_year_cast(alias: str) -> str:
    """A `frontmatter->>'year'` read, cast to `int`, NULL instead of raising on a non-numeric value.

    Byte-identical to 0024's `_guarded_year_cast`, migration-local because a hand-authored view body
    cannot import across revision files.
    """
    return f"CASE WHEN {alias}.frontmatter->>'year' ~ '^-?[0-9]+$' THEN ({alias}.frontmatter->>'year')::int END"


# -- guards -------------------------------------------------------------------


def _assert_no_unresolved_edges(conn: Connection) -> None:
    """Abort if any edge endpoint failed to resolve to an owning page during backfill.

    `memory.edges` is resolved-only (dangling targets live in `link_refs`, never here), so every
    `src_id` / `dst_id` must extract a page id (`split_part(x_id, '#', 1)`) that exists in
    `memory.pages` — an edge whose endpoint does not resolve violates that invariant and must be
    repaired before its collection can be assigned.
    """
    unresolved = conn.exec_driver_sql("SELECT count(*) FROM memory.edges WHERE src_collection IS NULL OR dst_collection IS NULL").scalar()
    if unresolved:
        msg = f"{unresolved} edge endpoints resolve to no page; repair the resolved-only invariant before migrating"
        raise RuntimeError(msg)


def _cross_collection_duplicate_tables(conn: Connection) -> list[str]:
    """Return the `table (key)` labels of every unit level where one id/key is claimed by more than one collection.

    Checked at every level the identity migration widens to a composite key: `pages` / `sections` /
    `claims` / `unit_summaries` / `raw_chunks` by their bare id, `vocab` by `(vocab, value)`,
    `sources_raw` by `source_id`. Empty when the database has no such collision.
    """
    offending: list[str] = []
    for table, key_col in (
        ("memory.pages", "id"),
        ("memory.sections", "id"),
        ("memory.claims", "id"),
        ("memory.unit_summaries", "unit_id"),
        ("memory.raw_chunks", "id"),
    ):
        count = conn.exec_driver_sql(
            f"SELECT count(*) FROM (SELECT 1 FROM {table} GROUP BY {key_col} HAVING count(DISTINCT collection) > 1) d"
        ).scalar()
        if count:
            offending.append(f"{table} ({key_col})")

    vocab_count = conn.exec_driver_sql(
        "SELECT count(*) FROM (SELECT 1 FROM memory.vocab GROUP BY vocab, value HAVING count(DISTINCT collection) > 1) d"
    ).scalar()
    if vocab_count:
        offending.append("memory.vocab (vocab, value)")

    source_count = conn.exec_driver_sql(
        "SELECT count(*) FROM (SELECT 1 FROM memory.sources_raw GROUP BY source_id HAVING count(DISTINCT collection) > 1) d"
    ).scalar()
    if source_count:
        offending.append("memory.sources_raw (source_id)")

    return offending


def _assert_no_cross_collection_duplicates(conn: Connection) -> None:
    """Upgrade guard: abort if any unit id/key is already claimed by more than one collection."""
    offending = _cross_collection_duplicate_tables(conn)
    if offending:
        msg = f"cross-collection id duplicates found in {', '.join(offending)}; resolve them before migrating"
        raise RuntimeError(msg)


def _assert_downgrade_safe(conn: Connection) -> None:
    """Downgrade guard: collapsing to global ids would drop a row wherever two collections share one."""
    if _cross_collection_duplicate_tables(conn):
        msg = "cross-collection id duplicates exist; downgrading to global ids would drop rows — resolve them first"
        raise RuntimeError(msg)


# -- phase 1: new columns, nullable -------------------------------------------


def _add_new_columns() -> None:
    op.execute("ALTER TABLE memory.sections ADD COLUMN collection text")
    op.execute("ALTER TABLE memory.claims ADD COLUMN collection text")
    op.execute("ALTER TABLE memory.claims ADD COLUMN subject_collection text")
    op.execute("ALTER TABLE memory.comparison_cells ADD COLUMN collection text")
    op.execute("ALTER TABLE memory.comparison_cells ADD COLUMN row_entity_collection text")
    op.execute("ALTER TABLE memory.edges ADD COLUMN src_collection text")
    op.execute("ALTER TABLE memory.edges ADD COLUMN dst_collection text")
    op.execute("ALTER TABLE memory.link_refs ADD COLUMN dst_collection text")
    op.execute("ALTER TABLE memory.vocab ADD COLUMN collection text")
    op.execute("ALTER TABLE memory.raw_chunks ADD COLUMN collection text")


# -- phase 2: backfill + guards ------------------------------------------------


def _backfill_columns() -> None:
    """Containment from the parent page, citations to the row's own collection, edges via each endpoint's page."""
    op.execute("UPDATE memory.sections s SET collection = p.collection FROM memory.pages p WHERE s.page_id = p.id")
    op.execute("UPDATE memory.claims c SET collection = p.collection FROM memory.pages p WHERE c.page_id = p.id")
    op.execute(
        "UPDATE memory.comparison_cells cc SET collection = p.collection FROM memory.pages p WHERE cc.comparison_id = p.id"
    )
    op.execute("UPDATE memory.raw_chunks rc SET collection = sr.collection FROM memory.sources_raw sr WHERE rc.source_id = sr.source_id")

    # Citations: the resolver cannot yet produce a cross-corpus target, so every existing citation
    # already points within its own row's (now-populated) collection.
    op.execute("UPDATE memory.claims SET subject_collection = collection WHERE subject_id IS NOT NULL")
    op.execute("UPDATE memory.comparison_cells SET row_entity_collection = collection")
    op.execute("UPDATE memory.link_refs SET dst_collection = collection WHERE dst_id IS NOT NULL")

    # vocab: no owning row exists to backfill from (see the module docstring's phase-2 note).
    op.execute(f"UPDATE memory.vocab SET collection = '{_VOCAB_BACKFILL_COLLECTION}' WHERE collection IS NULL")

    # edges: both endpoints via their owning page (a claim/section edge's id is `<page_id>#<anchor>`).
    op.execute("UPDATE memory.edges e SET src_collection = p.collection FROM memory.pages p WHERE split_part(e.src_id, '#', 1) = p.id")
    op.execute("UPDATE memory.edges e SET dst_collection = p.collection FROM memory.pages p WHERE split_part(e.dst_id, '#', 1) = p.id")


# -- phase 3: NOT NULL + null-pairing CHECKs -----------------------------------


def _finalize_not_null_and_checks() -> None:
    op.execute("ALTER TABLE memory.sections ALTER COLUMN collection SET NOT NULL")
    op.execute("ALTER TABLE memory.claims ALTER COLUMN collection SET NOT NULL")
    op.execute("ALTER TABLE memory.claims ADD CONSTRAINT claims_subject_pairing_check CHECK ((subject_id IS NULL) = (subject_collection IS NULL))")
    op.execute("ALTER TABLE memory.comparison_cells ALTER COLUMN collection SET NOT NULL")
    op.execute("ALTER TABLE memory.comparison_cells ALTER COLUMN row_entity_collection SET NOT NULL")
    op.execute("ALTER TABLE memory.edges ALTER COLUMN src_collection SET NOT NULL")
    op.execute("ALTER TABLE memory.edges ALTER COLUMN dst_collection SET NOT NULL")
    op.execute(
        "ALTER TABLE memory.link_refs ADD CONSTRAINT link_refs_dst_pairing_check CHECK ((dst_id IS NULL) = (dst_collection IS NULL))"
    )
    op.execute("ALTER TABLE memory.vocab ALTER COLUMN collection SET NOT NULL")
    op.execute("ALTER TABLE memory.raw_chunks ALTER COLUMN collection SET NOT NULL")


def _drop_not_null_and_checks() -> None:
    """Downgrade mirror of `_finalize_not_null_and_checks` — drops the CHECKs (NOT NULL drops with the columns)."""
    op.execute("ALTER TABLE memory.claims DROP CONSTRAINT claims_subject_pairing_check")
    op.execute("ALTER TABLE memory.link_refs DROP CONSTRAINT link_refs_dst_pairing_check")


# -- phase 4: key swap + indexes -----------------------------------------------


def _drop_old_keys() -> None:
    """Drop every FK depending on a single-column PK, then the six single-column PKs themselves.

    Order: FKs referencing `pages(id)` first, then `sources_raw(source_id)`, then `claims(id)` —
    each PK can only be dropped once nothing still depends on it.
    """
    # FKs into pages(id):
    op.execute("ALTER TABLE memory.pages DROP CONSTRAINT pages_superseded_by_fkey")
    op.execute("ALTER TABLE memory.sections DROP CONSTRAINT sections_page_id_fkey")
    op.execute("ALTER TABLE memory.claims DROP CONSTRAINT claims_page_id_fkey")
    op.execute("ALTER TABLE memory.claims DROP CONSTRAINT claims_subject_id_fkey")
    op.execute("ALTER TABLE memory.comparison_cells DROP CONSTRAINT comparison_cells_comparison_id_fkey")
    op.execute("ALTER TABLE memory.comparison_cells DROP CONSTRAINT comparison_cells_row_entity_fkey")
    op.execute("ALTER TABLE memory.claim_qualifiers DROP CONSTRAINT claim_qualifiers_target_id_fkey")
    op.execute("ALTER TABLE memory.link_refs DROP CONSTRAINT link_refs_src_page_id_fkey")

    # FKs into sources_raw(source_id):
    op.execute("ALTER TABLE memory.pages DROP CONSTRAINT pages_source_id_fkey")
    op.execute("ALTER TABLE memory.raw_chunks DROP CONSTRAINT raw_chunks_source_id_fkey")

    # FKs into claims(id):
    op.execute("ALTER TABLE memory.claim_qualifiers DROP CONSTRAINT claim_qualifiers_claim_id_fkey")

    # Now safe: the six single-column primary keys.
    op.execute("ALTER TABLE memory.pages DROP CONSTRAINT pages_pkey")
    op.execute("ALTER TABLE memory.sections DROP CONSTRAINT sections_pkey")
    op.execute("ALTER TABLE memory.claims DROP CONSTRAINT claims_pkey")
    op.execute("ALTER TABLE memory.comparison_cells DROP CONSTRAINT comparison_cells_pkey")
    op.execute("ALTER TABLE memory.vocab DROP CONSTRAINT vocab_pkey")
    op.execute("ALTER TABLE memory.sources_raw DROP CONSTRAINT sources_raw_pkey")
    op.execute("ALTER TABLE memory.unit_summaries DROP CONSTRAINT unit_summaries_pkey")
    op.execute("ALTER TABLE memory.raw_chunks DROP CONSTRAINT raw_chunks_pkey")


def _create_composite_keys_and_indexes() -> None:
    """Composite primary keys, composite foreign keys mirroring `compile/orm.py`, and the two child indexes."""
    op.execute("ALTER TABLE memory.pages ADD PRIMARY KEY (collection, id)")
    op.execute("ALTER TABLE memory.sections ADD PRIMARY KEY (collection, id)")
    op.execute("ALTER TABLE memory.claims ADD PRIMARY KEY (collection, id)")
    op.execute("ALTER TABLE memory.comparison_cells ADD PRIMARY KEY (collection, comparison_id, row_entity, dimension)")
    op.execute("ALTER TABLE memory.vocab ADD PRIMARY KEY (collection, vocab, value)")
    op.execute("ALTER TABLE memory.sources_raw ADD PRIMARY KEY (collection, source_id)")
    op.execute(
        "ALTER TABLE memory.sources_raw ADD CONSTRAINT sources_raw_collection_provider_provider_key_key"
        " UNIQUE (collection, provider, provider_key)"
    )
    op.execute("ALTER TABLE memory.unit_summaries ADD PRIMARY KEY (collection, unit_id)")
    op.execute("ALTER TABLE memory.raw_chunks ADD PRIMARY KEY (collection, id)")

    op.execute(
        "ALTER TABLE memory.pages ADD CONSTRAINT pages_collection_superseded_by_fkey"
        " FOREIGN KEY (collection, superseded_by) REFERENCES memory.pages (collection, id)"
    )
    op.execute(
        "ALTER TABLE memory.pages ADD CONSTRAINT pages_collection_source_id_fkey"
        " FOREIGN KEY (collection, source_id) REFERENCES memory.sources_raw (collection, source_id)"
    )
    op.execute(
        "ALTER TABLE memory.sections ADD CONSTRAINT sections_collection_page_id_fkey"
        " FOREIGN KEY (collection, page_id) REFERENCES memory.pages (collection, id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE memory.claims ADD CONSTRAINT claims_collection_page_id_fkey"
        " FOREIGN KEY (collection, page_id) REFERENCES memory.pages (collection, id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE memory.claims ADD CONSTRAINT claims_subject_collection_subject_id_fkey"
        " FOREIGN KEY (subject_collection, subject_id) REFERENCES memory.pages (collection, id)"
    )
    op.execute(
        "ALTER TABLE memory.comparison_cells ADD CONSTRAINT comparison_cells_collection_comparison_id_fkey"
        " FOREIGN KEY (collection, comparison_id) REFERENCES memory.pages (collection, id)"
    )
    op.execute(
        "ALTER TABLE memory.comparison_cells ADD CONSTRAINT comparison_cells_row_entity_collection_row_entity_fkey"
        " FOREIGN KEY (row_entity_collection, row_entity) REFERENCES memory.pages (collection, id)"
    )
    op.execute(
        "ALTER TABLE memory.claim_qualifiers ADD CONSTRAINT claim_qualifiers_collection_claim_id_fkey"
        " FOREIGN KEY (collection, claim_id) REFERENCES memory.claims (collection, id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE memory.claim_qualifiers ADD CONSTRAINT claim_qualifiers_target_collection_target_id_fkey"
        " FOREIGN KEY (target_collection, target_id) REFERENCES memory.pages (collection, id)"
    )
    op.execute(
        "ALTER TABLE memory.link_refs ADD CONSTRAINT link_refs_collection_src_page_id_fkey"
        " FOREIGN KEY (collection, src_page_id) REFERENCES memory.pages (collection, id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE memory.raw_chunks ADD CONSTRAINT raw_chunks_collection_source_id_fkey"
        " FOREIGN KEY (collection, source_id) REFERENCES memory.sources_raw (collection, source_id) ON DELETE CASCADE"
    )

    op.execute("CREATE INDEX ix_sections_collection_page_id ON memory.sections (collection, page_id)")
    op.execute("CREATE INDEX ix_claims_collection_page_id ON memory.claims (collection, page_id)")


def _drop_composite_keys_and_indexes() -> None:
    """Downgrade mirror of `_create_composite_keys_and_indexes`: drop the composite FKs, indexes, then PKs."""
    op.execute("DROP INDEX memory.ix_sections_collection_page_id")
    op.execute("DROP INDEX memory.ix_claims_collection_page_id")

    op.execute("ALTER TABLE memory.pages DROP CONSTRAINT pages_collection_superseded_by_fkey")
    op.execute("ALTER TABLE memory.pages DROP CONSTRAINT pages_collection_source_id_fkey")
    op.execute("ALTER TABLE memory.sections DROP CONSTRAINT sections_collection_page_id_fkey")
    op.execute("ALTER TABLE memory.claims DROP CONSTRAINT claims_collection_page_id_fkey")
    op.execute("ALTER TABLE memory.claims DROP CONSTRAINT claims_subject_collection_subject_id_fkey")
    op.execute("ALTER TABLE memory.comparison_cells DROP CONSTRAINT comparison_cells_collection_comparison_id_fkey")
    op.execute("ALTER TABLE memory.comparison_cells DROP CONSTRAINT comparison_cells_row_entity_collection_row_entity_fkey")
    op.execute("ALTER TABLE memory.claim_qualifiers DROP CONSTRAINT claim_qualifiers_collection_claim_id_fkey")
    op.execute("ALTER TABLE memory.claim_qualifiers DROP CONSTRAINT claim_qualifiers_target_collection_target_id_fkey")
    op.execute("ALTER TABLE memory.link_refs DROP CONSTRAINT link_refs_collection_src_page_id_fkey")
    op.execute("ALTER TABLE memory.raw_chunks DROP CONSTRAINT raw_chunks_collection_source_id_fkey")

    op.execute("ALTER TABLE memory.pages DROP CONSTRAINT pages_pkey")
    op.execute("ALTER TABLE memory.sections DROP CONSTRAINT sections_pkey")
    op.execute("ALTER TABLE memory.claims DROP CONSTRAINT claims_pkey")
    op.execute("ALTER TABLE memory.comparison_cells DROP CONSTRAINT comparison_cells_pkey")
    op.execute("ALTER TABLE memory.vocab DROP CONSTRAINT vocab_pkey")
    op.execute("ALTER TABLE memory.sources_raw DROP CONSTRAINT sources_raw_collection_provider_provider_key_key")
    op.execute("ALTER TABLE memory.sources_raw DROP CONSTRAINT sources_raw_pkey")
    op.execute("ALTER TABLE memory.unit_summaries DROP CONSTRAINT unit_summaries_pkey")
    op.execute("ALTER TABLE memory.raw_chunks DROP CONSTRAINT raw_chunks_pkey")


def _restore_old_keys() -> None:
    """Restore the six single-column primary keys, then every FK that depended on one."""
    op.execute("ALTER TABLE memory.pages ADD PRIMARY KEY (id)")
    op.execute("ALTER TABLE memory.sections ADD PRIMARY KEY (id)")
    op.execute("ALTER TABLE memory.claims ADD PRIMARY KEY (id)")
    op.execute("ALTER TABLE memory.comparison_cells ADD PRIMARY KEY (comparison_id, row_entity, dimension)")
    op.execute("ALTER TABLE memory.vocab ADD PRIMARY KEY (vocab, value)")
    op.execute("ALTER TABLE memory.sources_raw ADD PRIMARY KEY (source_id)")
    op.execute("ALTER TABLE memory.unit_summaries ADD PRIMARY KEY (unit_id)")
    op.execute("ALTER TABLE memory.raw_chunks ADD PRIMARY KEY (id)")

    op.execute("ALTER TABLE memory.pages ADD CONSTRAINT pages_superseded_by_fkey FOREIGN KEY (superseded_by) REFERENCES memory.pages (id)")
    op.execute(
        "ALTER TABLE memory.pages ADD CONSTRAINT pages_source_id_fkey FOREIGN KEY (source_id) REFERENCES memory.sources_raw (source_id)"
    )
    op.execute(
        "ALTER TABLE memory.sections ADD CONSTRAINT sections_page_id_fkey"
        " FOREIGN KEY (page_id) REFERENCES memory.pages (id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE memory.claims ADD CONSTRAINT claims_page_id_fkey FOREIGN KEY (page_id) REFERENCES memory.pages (id) ON DELETE CASCADE"
    )
    op.execute("ALTER TABLE memory.claims ADD CONSTRAINT claims_subject_id_fkey FOREIGN KEY (subject_id) REFERENCES memory.pages (id)")
    op.execute(
        "ALTER TABLE memory.comparison_cells ADD CONSTRAINT comparison_cells_comparison_id_fkey"
        " FOREIGN KEY (comparison_id) REFERENCES memory.pages (id)"
    )
    op.execute(
        "ALTER TABLE memory.comparison_cells ADD CONSTRAINT comparison_cells_row_entity_fkey"
        " FOREIGN KEY (row_entity) REFERENCES memory.pages (id)"
    )
    op.execute(
        "ALTER TABLE memory.claim_qualifiers ADD CONSTRAINT claim_qualifiers_claim_id_fkey"
        " FOREIGN KEY (claim_id) REFERENCES memory.claims (id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE memory.claim_qualifiers ADD CONSTRAINT claim_qualifiers_target_id_fkey"
        " FOREIGN KEY (target_id) REFERENCES memory.pages (id)"
    )
    op.execute(
        "ALTER TABLE memory.raw_chunks ADD CONSTRAINT raw_chunks_source_id_fkey"
        " FOREIGN KEY (source_id) REFERENCES memory.sources_raw (source_id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE memory.link_refs ADD CONSTRAINT link_refs_src_page_id_fkey"
        " FOREIGN KEY (src_page_id) REFERENCES memory.pages (id) ON DELETE CASCADE"
    )


# -- phase 5: api surface ------------------------------------------------------


def _graph_schema(*, partitioned: bool) -> GraphSchema:
    """The link-graph shape `api.graph_expand`/`api.graph_paths` build from, byte-identical to 0010's `_graph_schema` otherwise."""
    return GraphSchema(
        edge_table="memory.edges",
        src_col="src_id",
        dst_col="dst_id",
        rel_col="rel",
        edge_payload_cols=("provenance_claim",),
        node_view="api.cards",
        node_cols=("id", "type", "title", "status", "superseded"),
        node_col_types=("text", "text", "text", "text", "boolean"),
        partition_col="src_collection" if partitioned else None,
    )


def _graph_bodies(schema: GraphSchema) -> tuple[str, str]:
    """Return `(expand_body, paths_body)` CREATE FUNCTION text for the target DB's configured backend.

    Built directly from the `core.ai.retrieval` traversal builders, not `engram.retrieval.graph_backend`
    (whose module-level schema constant must stay pointed at `wiki.edges` for migration 0004's sake,
    mirroring 0010's own `_graph_statements`). A partitioned `schema` on the Apache AGE backend raises
    `ValueError` inside the builder (its projected property graph carries no partition tag); re-raised
    here as a migration-scoped `RuntimeError` naming the limitation and the CTE-backend alternative,
    rather than letting the bare `ValueError` reach the operator as an unexplained stack trace.
    """
    backend = context.config.attributes.get("graph_backend")
    backend_name = getattr(backend, "name", None)
    builder: AgeGraphTraversalBuilder | CteGraphTraversalBuilder
    if backend_name == "age":
        builder = AgeGraphTraversalBuilder("wiki_kg")
    elif backend_name == "cte":
        builder = CteGraphTraversalBuilder()
    else:
        msg = f"graph_backend not configured (or unrecognised name {backend_name!r}): alembic/env.py must stash a GraphBackendSql"
        raise RuntimeError(msg)
    try:
        return builder.expand_body(schema), builder.paths_body(schema)
    except ValueError as exc:
        msg = (
            "revision 0025 requires api.graph_expand/api.graph_paths to scope every traversal to one "
            "collection (every unit's identity is now (collection, id)), but the Apache AGE backend "
            "cannot build a partitioned traversal: its projected property graph carries no partition "
            f"column ({exc}). Configure retrieval.graph_backend = 'cte' (the portable recursive-CTE "
            "backend, which supports partitioned traversal) for a database migrating to corpus-qualified "
            "identity; Apache AGE regains support once its projection carries the collection column."
        )
        raise RuntimeError(msg) from exc


_NEW_EXPAND_COMMENT = (
    "COMMENT ON FUNCTION api.graph_expand(text, text[], int, text[], int) IS "
    "'Typed-relation neighbourhood expansion, scoped to one collection: from one or more seed ids in "
    "p_collection, walk the link graph out to N hops (default 2), optionally filtered to rel_types, "
    "returning one row per reached node — its id/type/title/status/superseded plus the edge that "
    "reached it (src_id, dst_id, rel, provenance_claim; NULL on a seed). k caps how many rows come "
    "back (max 50). Example: SELECT * FROM api.graph_expand(''research'', ARRAY[''ent.graphrag''], hops => 2);'"
)
_NEW_PATHS_COMMENT = (
    "COMMENT ON FUNCTION api.graph_paths(text, text, text, int, text[], int) IS "
    "'Shortest typed-relation paths between two nodes, scoped to one collection: enumerate the paths "
    "from from_id to to_id within p_collection within max_hops (default 4), optionally filtered to "
    "rel_types, returning one row per node along each path — its id/type/title/status/superseded plus "
    "the edge into it (src_id, dst_id, rel, provenance_claim; NULL on the start node), the path_rank "
    "group and the path score. k caps the number of paths. Example: "
    "SELECT * FROM api.graph_paths(''research'', ''ent.graphrag'', ''ent.lightrag'');'"
)

_NEW_FETCH_BODY = f"""
CREATE FUNCTION api.fetch(p_collection text, unit_id text)
  RETURNS TABLE (id text, level text, title text, body text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT p.id, 'page'::text, p.title,
           CASE WHEN length(p.card_text) > {_FETCH_CHAR_BUDGET}
                THEN left(p.card_text, {_FETCH_CHAR_BUDGET}) || ' {_FETCH_TRUNCATION_MARKER}'
                ELSE p.card_text END
    FROM memory.pages p WHERE p.collection = p_collection AND p.id = unit_id
    UNION ALL
    SELECT s.id, 'section', s.heading, s.body
    FROM memory.sections s WHERE s.collection = p_collection AND s.id = unit_id
    UNION ALL
    SELECT c.id, 'claim', c.kind, c.text
    FROM memory.claims c WHERE c.collection = p_collection AND c.id = unit_id
    UNION ALL
    SELECT r.id, 'raw', r.heading, r.body
    FROM memory.raw_chunks r WHERE r.collection = p_collection AND r.id = unit_id
$$
"""

_OLD_FETCH_BODY = f"""
CREATE FUNCTION api.fetch(unit_id text)
  RETURNS TABLE (id text, level text, title text, body text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT p.id, 'page'::text, p.title,
           CASE WHEN length(p.card_text) > {_FETCH_CHAR_BUDGET}
                THEN left(p.card_text, {_FETCH_CHAR_BUDGET}) || ' {_FETCH_TRUNCATION_MARKER}'
                ELSE p.card_text END
    FROM memory.pages p WHERE p.id = unit_id
    UNION ALL
    SELECT s.id, 'section', s.heading, s.body
    FROM memory.sections s WHERE s.id = unit_id
    UNION ALL
    SELECT c.id, 'claim', c.kind, c.text
    FROM memory.claims c WHERE c.id = unit_id
    UNION ALL
    SELECT r.id, 'raw', r.heading, r.body
    FROM memory.raw_chunks r WHERE r.id = unit_id
$$
"""

_FETCH_COMMENT_STATEMENTS = (
    "COMMENT ON FUNCTION api.fetch(text, text) IS "
    "'Fetch one unit''s full text by (collection, id) — a page, section, claim, or raw chunk. A page''s "
    "card_text is truncated past ~8k tokens (fetch its sections instead). Example: "
    "SELECT * FROM api.fetch(''research'', ''mu.x'');'",
)

_NEW_LOOKUP_VALUE_BODY = """
CREATE FUNCTION api.lookup_value(p_collection text, facet text, term text DEFAULT NULL)
  RETURNS TABLE (value text, definition text, n_pages int)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT v.value, v.definition,
           (SELECT count(*)::int FROM memory.pages p
            WHERE p.collection = p_collection
              AND ((v.vocab = 'entity_type' AND p.frontmatter ->> 'entity_type' = v.value)
               OR (v.vocab LIKE 'facet:%' AND (p.facets -> split_part(v.vocab, ':', 2)) ? v.value))) AS n_pages
    FROM memory.vocab v
    WHERE v.collection = p_collection
      AND (v.vocab = facet OR v.vocab = 'facet:' || facet)
      AND (term IS NULL OR v.value ILIKE '%' || term || '%')
    ORDER BY v.value
$$
"""

_OLD_LOOKUP_VALUE_BODY = """
CREATE FUNCTION api.lookup_value(facet text, term text DEFAULT NULL)
  RETURNS TABLE (value text, definition text, n_pages int)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT v.value, v.definition,
           (SELECT count(*)::int FROM memory.pages p
            WHERE (v.vocab = 'entity_type' AND p.frontmatter ->> 'entity_type' = v.value)
               OR (v.vocab LIKE 'facet:%' AND (p.facets -> split_part(v.vocab, ':', 2)) ? v.value)) AS n_pages
    FROM memory.vocab v
    WHERE (v.vocab = facet OR v.vocab = 'facet:' || facet)
      AND (term IS NULL OR v.value ILIKE '%' || term || '%')
    ORDER BY v.value
$$
"""

_LOOKUP_VALUE_COMMENT_STATEMENTS = (
    "COMMENT ON FUNCTION api.lookup_value(text, text, text) IS "
    "'Resolve a facet value''s definition + usage count within one collection (vocab is corpus-scoped). "
    "facet is a declared facet name or ''entity_type''; term filters by substring. Example: "
    "SELECT * FROM api.lookup_value(''research'', ''facet:tasks'', ''retrieval'');'",
)

_NEW_ENVELOPE_PAGE_BODY = f"""
CREATE FUNCTION api.envelope_page(p_collection text, p_page_id text)
  RETURNS TABLE (row_kind text, id text, kind text, title text, ord int, summary text, body text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    WITH page_size AS (
      SELECT COALESCE(sum(length(sec.body)), 0) AS chars
      FROM memory.sections sec
      WHERE sec.collection = p_collection AND sec.page_id = p_page_id
    )
    SELECT 'page'::text, p.id, p.kind, p.title, -1 AS ord,
           COALESCE(s.summary, p.summary, '') AS summary,
           NULL::text AS body
    FROM memory.pages p
    LEFT JOIN memory.unit_summaries s ON s.collection = p.collection AND s.unit_id = p.id AND s.status = 'active'
    WHERE p.collection = p_collection AND p.id = p_page_id
    UNION ALL
    SELECT 'section'::text, sec.id, 'section'::text, sec.heading, sec.ord AS ord,
           COALESCE(t.summary, sec.summary, '') AS summary,
           CASE WHEN page_size.chars <= {_INLINE_CAP_CHARS} THEN sec.body END AS body
    FROM memory.sections sec
    CROSS JOIN page_size
    LEFT JOIN memory.unit_summaries t ON t.collection = sec.collection AND t.unit_id = sec.id AND t.status = 'active'
    WHERE sec.collection = p_collection AND sec.page_id = p_page_id
    ORDER BY ord
  $$
"""

_OLD_ENVELOPE_PAGE_BODY = f"""
CREATE FUNCTION api.envelope_page(p_page_id text)
  RETURNS TABLE (row_kind text, id text, kind text, title text, ord int, summary text, body text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    WITH page_size AS (
      SELECT COALESCE(sum(length(sec.body)), 0) AS chars
      FROM memory.sections sec
      WHERE sec.page_id = p_page_id
    )
    SELECT 'page'::text, p.id, p.kind, p.title, -1 AS ord,
           COALESCE(s.summary, p.summary, '') AS summary,
           NULL::text AS body
    FROM memory.pages p
    LEFT JOIN memory.unit_summaries s ON s.unit_id = p.id AND s.status = 'active'
    WHERE p.id = p_page_id
    UNION ALL
    SELECT 'section'::text, sec.id, 'section'::text, sec.heading, sec.ord AS ord,
           COALESCE(t.summary, sec.summary, '') AS summary,
           CASE WHEN page_size.chars <= {_INLINE_CAP_CHARS} THEN sec.body END AS body
    FROM memory.sections sec
    CROSS JOIN page_size
    LEFT JOIN memory.unit_summaries t ON t.unit_id = sec.id AND t.status = 'active'
    WHERE sec.page_id = p_page_id
    ORDER BY ord
  $$
"""

_ENVELOPE_PAGE_COMMENT_STATEMENTS = (
    f"COMMENT ON FUNCTION api.envelope_page(text, text) IS "
    "'Progressive disclosure, page envelope: one page row (row_kind=''page'') plus its section "
    "children (row_kind=''section''), ordered by ord, in a single call. When the page''s whole "
    f"text is at most {_INLINE_CAP_CHARS} characters, each section row also carries its body "
    "(a small page reads whole in one call); larger pages return body NULL. A section''s "
    "summary falls back to its own compiled memory.sections.summary when it has no active "
    "unit_summaries row, exactly as a page falls back to its frontmatter summary. Example: "
    "SELECT * FROM api.envelope_page(''research'', ''mu.x'');'",
)

_NEW_ENVELOPE_SECTION_BODY = """
CREATE FUNCTION api.envelope_section(p_collection text, p_section_id text)
  RETURNS TABLE (id text, title text, body text, summary text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT sec.id, sec.heading, sec.body,
           COALESCE(s.summary, '') AS summary
    FROM memory.sections sec
    LEFT JOIN memory.unit_summaries s ON s.collection = sec.collection AND s.unit_id = sec.id AND s.status = 'active'
    WHERE sec.collection = p_collection AND sec.id = p_section_id
$$
"""

_OLD_ENVELOPE_SECTION_BODY = """
CREATE FUNCTION api.envelope_section(p_section_id text)
  RETURNS TABLE (id text, title text, body text, summary text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT sec.id, sec.heading, sec.body,
           COALESCE(s.summary, '') AS summary
    FROM memory.sections sec
    LEFT JOIN memory.unit_summaries s ON s.unit_id = sec.id AND s.status = 'active'
    WHERE sec.id = p_section_id
$$
"""

_ENVELOPE_SECTION_COMMENT_STATEMENTS = (
    "COMMENT ON FUNCTION api.envelope_section(text, text) IS "
    "'Progressive disclosure, section envelope: one section''s id/title/body/summary, a disclosure "
    "leaf (no children). Example: SELECT * FROM api.envelope_section(''research'', ''mu.x#s1'');'",
)

# -- views ----------------------------------------------------------------------

_NEW_CARDS_VIEW = f"""
CREATE VIEW api.cards AS
SELECT
  p.id,
  p.collection                        AS src_collection,
  p.kind                              AS type,
  p.title,
  p.summary,
  p.status,
  (p.superseded_by IS NOT NULL)       AS superseded,
  p.frontmatter->>'entity_type'       AS entity_type,
  p.frontmatter->>'paper_type'        AS paper_type,
  p.frontmatter->>'maturity'          AS maturity,
  {_dialect_year_cast("p")}           AS year,
  COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets->'tasks')), ARRAY[]::text[])   AS tasks,
  COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets->'domains')), ARRAY[]::text[]) AS domains,
  COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets->'stages')), ARRAY[]::text[])  AS stages,
  (SELECT count(*) FROM memory.claims c WHERE c.collection = p.collection AND c.page_id = p.id) AS n_claims,
  (SELECT count(*) FROM memory.edges e WHERE e.src_collection = p.collection AND e.src_id = p.id) AS out_degree,
  (SELECT count(*) FROM memory.edges e WHERE e.dst_collection = p.collection AND e.dst_id = p.id) AS in_degree,
  p.updated,
  p.frontmatter
FROM memory.pages p
"""

_OLD_CARDS_VIEW = f"""
CREATE VIEW api.cards AS
SELECT
  p.id,
  p.kind                              AS type,
  p.title,
  p.summary,
  p.status,
  (p.superseded_by IS NOT NULL)       AS superseded,
  p.frontmatter->>'entity_type'       AS entity_type,
  p.frontmatter->>'paper_type'        AS paper_type,
  p.frontmatter->>'maturity'          AS maturity,
  {_dialect_year_cast("p")}           AS year,
  COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets->'tasks')), ARRAY[]::text[])   AS tasks,
  COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets->'domains')), ARRAY[]::text[]) AS domains,
  COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets->'stages')), ARRAY[]::text[])  AS stages,
  (SELECT count(*) FROM memory.claims c WHERE c.page_id = p.id)        AS n_claims,
  (SELECT count(*) FROM memory.edges e WHERE e.src_id = p.id)          AS out_degree,
  (SELECT count(*) FROM memory.edges e WHERE e.dst_id = p.id)          AS in_degree,
  p.updated,
  p.frontmatter
FROM memory.pages p
"""

_SHARED_CARDS_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.cards IS 'L1 card scan (~50-150 tokens/row): one compact row per page across all 8 types. Filter by type + facet arrays, exclude stubs/superseded, order by updated. Example: SELECT id, title, summary FROM api.cards WHERE type = ''playbook'' AND ''long-doc-qa'' = ANY(tasks) AND NOT superseded LIMIT 20;'",
    "COMMENT ON COLUMN api.cards.type IS 'Page type: paper|entity|concept|comparison|playbook|question|moc|meta.'",
    "COMMENT ON COLUMN api.cards.title IS 'Human-readable page title.'",
    "COMMENT ON COLUMN api.cards.summary IS 'One-paragraph summary (<= 280 chars), or NULL for stubs.'",
    "COMMENT ON COLUMN api.cards.status IS 'Lifecycle: stub|draft|synthesized|verified.'",
    "COMMENT ON COLUMN api.cards.superseded IS 'boolean — true when a newer page supersedes this one.'",
    "COMMENT ON COLUMN api.cards.entity_type IS 'For entities: the registry entity_type. Example value: ''technique''.'",
    "COMMENT ON COLUMN api.cards.paper_type IS 'For papers: e.g. ''empirical''|''survey''|''system''.'",
    "COMMENT ON COLUMN api.cards.maturity IS 'For playbooks: e.g. ''experimental''|''established''.'",
    "COMMENT ON COLUMN api.cards.year IS 'Publication year (papers). Example value: 2025.'",
    "COMMENT ON COLUMN api.cards.tasks IS 'text[] facet — task tags. Example value: {long-doc-qa,multi-hop-qa}.'",
    "COMMENT ON COLUMN api.cards.domains IS 'text[] facet — domain tags. Example value: {biomedical}.'",
    "COMMENT ON COLUMN api.cards.stages IS 'text[] facet — pipeline stage tags. Example value: {retrieval,indexing}.'",
    "COMMENT ON COLUMN api.cards.n_claims IS 'int — number of claim blocks on this page.'",
    "COMMENT ON COLUMN api.cards.out_degree IS 'int — outgoing typed edges from this page.'",
    "COMMENT ON COLUMN api.cards.in_degree IS 'int — incoming typed edges to this page.'",
    "COMMENT ON COLUMN api.cards.updated IS 'date — last edit date of the source note.'",
    "COMMENT ON COLUMN api.cards.frontmatter IS 'jsonb: the page''s long-tail frontmatter as a document (the same keys api.facets unpivots, plus values deeper than a scalar). Example: SELECT frontmatter->''priority'' FROM api.cards WHERE id = ''ent.lightrag'';'",
)

_NEW_CARDS_COMMENTS: tuple[str, ...] = (
    "COMMENT ON COLUMN api.cards.id IS 'Page id — unique only paired with src_collection. Example value: ''paper.lightrag-2025''.'",
    "COMMENT ON COLUMN api.cards.src_collection IS 'This page''s own collection. Named to match memory.edges.src_collection — api.graph_expand/api.graph_paths join this view on that pair. Example value: ''research''.'",
    *_SHARED_CARDS_COMMENTS,
)

_OLD_CARDS_COMMENTS: tuple[str, ...] = (
    "COMMENT ON COLUMN api.cards.id IS 'Page id (primary key everywhere). Example value: ''paper.lightrag-2025''.'",
    *_SHARED_CARDS_COMMENTS,
)

_NEW_SECTIONS_VIEW = """
CREATE VIEW api.sections AS
SELECT
  s.id           AS section_id,
  s.collection,
  s.page_id,
  p.title        AS page_title,
  p.kind         AS page_kind,
  s.canon,
  s.path::text   AS heading_path,
  s.body,
  s.tokens
FROM memory.sections s
JOIN memory.pages p ON p.collection = s.collection AND p.id = s.page_id
"""

_OLD_SECTIONS_VIEW = """
CREATE VIEW api.sections AS
SELECT
  s.id           AS section_id,
  s.page_id,
  p.title        AS page_title,
  p.kind         AS page_kind,
  s.canon,
  s.path::text   AS heading_path,
  s.body,
  s.tokens
FROM memory.sections s
JOIN memory.pages p ON p.id = s.page_id
"""

_SECTIONS_VIEW_COMMENT = (
    "COMMENT ON VIEW api.sections IS 'L2 read unit: one row per H2/H3 leaf section of a page, with its container "
    "page named. Example: SELECT page_title, body FROM api.sections WHERE canon = ''limitations'' LIMIT 10;'"
)

_SHARED_SECTIONS_COLUMN_COMMENTS: tuple[str, ...] = (
    "COMMENT ON COLUMN api.sections.page_id IS 'Owning page id.'",
    "COMMENT ON COLUMN api.sections.page_title IS 'Owning page title (the breadcrumb).'",
    "COMMENT ON COLUMN api.sections.page_kind IS 'Owning page type: paper|entity|…'",
    "COMMENT ON COLUMN api.sections.canon IS 'Canonical H2 slug when the heading matches the type''s canon set. Example value: ''limitations''.'",
    "COMMENT ON COLUMN api.sections.heading_path IS 'ltree path as text. Example value: ''approach.graph_indexing''.'",
    "COMMENT ON COLUMN api.sections.body IS 'Section markdown body.'",
    "COMMENT ON COLUMN api.sections.tokens IS 'int — approximate token count of the body.'",
)

_NEW_SECTIONS_COMMENTS: tuple[str, ...] = (
    _SECTIONS_VIEW_COMMENT,
    "COMMENT ON COLUMN api.sections.section_id IS 'Section id ''<page_id>#<heading-slug>'' — unique only paired with collection. Example value: ''paper.lightrag-2025#findings''.'",
    "COMMENT ON COLUMN api.sections.collection IS 'This section''s own collection (its owning page''s collection). Example value: ''research''.'",
    *_SHARED_SECTIONS_COLUMN_COMMENTS,
)

_OLD_SECTIONS_COMMENTS: tuple[str, ...] = (
    _SECTIONS_VIEW_COMMENT,
    "COMMENT ON COLUMN api.sections.section_id IS 'Section id ''<page_id>#<heading-slug>''. Example value: ''paper.lightrag-2025#findings''.'",
    *_SHARED_SECTIONS_COLUMN_COMMENTS,
)

_NEW_CLAIMS_VIEW = f"""
CREATE VIEW api.claims AS
SELECT
  c.id           AS claim_id,
  c.collection,
  c.page_id      AS paper_id,
  p.title        AS paper_title,
  {_dialect_year_cast("p")} AS paper_year,
  subj.title     AS subject_name,
  ds.title       AS dataset_name,
  tk.title       AS task_name,
  mt.title       AS metric_name,
  c.text,
  c.quote,
  c.loc,
  c.kind,
  c.condition,
  c.magnitude,
  c.eval_setting,
  c.status
FROM memory.claims c
JOIN memory.pages p ON p.collection = c.collection AND p.id = c.page_id
LEFT JOIN memory.pages subj ON subj.collection = c.subject_collection AND subj.id = c.subject_id
LEFT JOIN memory.claim_qualifiers qds ON qds.collection = c.collection AND qds.claim_id = c.id AND qds.qualifier = 'on_dataset' AND qds.ord = 0
LEFT JOIN memory.pages ds   ON ds.collection = qds.target_collection AND ds.id = qds.target_id
LEFT JOIN memory.claim_qualifiers qtk ON qtk.collection = c.collection AND qtk.claim_id = c.id AND qtk.qualifier = 'on_task' AND qtk.ord = 0
LEFT JOIN memory.pages tk   ON tk.collection = qtk.target_collection AND tk.id = qtk.target_id
LEFT JOIN memory.claim_qualifiers qmt ON qmt.collection = c.collection AND qmt.claim_id = c.id AND qmt.qualifier = 'metric' AND qmt.ord = 0
LEFT JOIN memory.pages mt   ON mt.collection = qmt.target_collection AND mt.id = qmt.target_id
"""

_OLD_CLAIMS_VIEW = f"""
CREATE VIEW api.claims AS
SELECT
  c.id           AS claim_id,
  c.page_id      AS paper_id,
  p.title        AS paper_title,
  {_dialect_year_cast("p")} AS paper_year,
  subj.title     AS subject_name,
  ds.title       AS dataset_name,
  tk.title       AS task_name,
  mt.title       AS metric_name,
  c.text,
  c.quote,
  c.loc,
  c.kind,
  c.condition,
  c.magnitude,
  c.eval_setting,
  c.status
FROM memory.claims c
JOIN memory.pages p ON p.id = c.page_id
LEFT JOIN memory.pages subj ON subj.id = c.subject_id
LEFT JOIN memory.claim_qualifiers qds ON qds.claim_id = c.id AND qds.qualifier = 'on_dataset' AND qds.ord = 0
LEFT JOIN memory.pages ds   ON ds.id   = qds.target_id
LEFT JOIN memory.claim_qualifiers qtk ON qtk.claim_id = c.id AND qtk.qualifier = 'on_task' AND qtk.ord = 0
LEFT JOIN memory.pages tk   ON tk.id   = qtk.target_id
LEFT JOIN memory.claim_qualifiers qmt ON qmt.claim_id = c.id AND qmt.qualifier = 'metric' AND qmt.ord = 0
LEFT JOIN memory.pages mt   ON mt.id   = qmt.target_id
"""

_SHARED_CLAIMS_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.claims IS 'Joined, human-readable claims: paper title+year and subject/dataset/task/metric NAMES (never bare ids), plus quote+loc for L4 verification. Example: SELECT paper_title, text, quote, loc FROM api.claims WHERE claim_id = ''paper.lightrag-2025#c1'';'",
    "COMMENT ON COLUMN api.claims.paper_id IS 'Owning paper page id.'",
    "COMMENT ON COLUMN api.claims.paper_title IS 'Owning paper title.'",
    "COMMENT ON COLUMN api.claims.paper_year IS 'Owning paper year. Example value: 2025.'",
    "COMMENT ON COLUMN api.claims.subject_name IS 'Name of the entity the claim is about. Example value: ''LightRAG''.'",
    "COMMENT ON COLUMN api.claims.dataset_name IS 'Dataset name the result is on. Example value: ''HotpotQA''.'",
    "COMMENT ON COLUMN api.claims.task_name IS 'Task name. Example value: ''multi-hop-qa''.'",
    "COMMENT ON COLUMN api.claims.metric_name IS 'Metric name. Example value: ''F1''.'",
    "COMMENT ON COLUMN api.claims.text IS 'The atomic claim sentence.'",
    "COMMENT ON COLUMN api.claims.quote IS 'Verbatim source snippet supporting the claim (may be NULL).'",
    "COMMENT ON COLUMN api.claims.loc IS 'Raw locator into the source. Example value: ''§5.1, Table 2''.'",
    "COMMENT ON COLUMN api.claims.kind IS 'Claim kind (registry). Example value: ''benchmark-result''.'",
    "COMMENT ON COLUMN api.claims.condition IS 'Qualifying condition. Example value: ''zero-shot''.'",
    "COMMENT ON COLUMN api.claims.magnitude IS 'Effect size text. Example value: ''+4.2 F1''.'",
    "COMMENT ON COLUMN api.claims.eval_setting IS 'Evaluation setting. Example value: ''closed-book''.'",
    "COMMENT ON COLUMN api.claims.status IS 'Claim status: active|disputed|superseded|retracted-source.'",
)

_NEW_CLAIMS_COMMENTS: tuple[str, ...] = (
    "COMMENT ON COLUMN api.claims.claim_id IS 'Claim id ''<page_id>#cN'' — unique only paired with collection. Example value: ''paper.lightrag-2025#c1''.'",
    "COMMENT ON COLUMN api.claims.collection IS 'This claim''s own collection (its owning page''s collection). Example value: ''research''.'",
    *_SHARED_CLAIMS_COMMENTS,
)

_OLD_CLAIMS_COMMENTS: tuple[str, ...] = (
    "COMMENT ON COLUMN api.claims.claim_id IS 'Claim id ''<page_id>#cN''. Example value: ''paper.lightrag-2025#c1''.'",
    *_SHARED_CLAIMS_COMMENTS,
)

_NEW_EDGES_VIEW = """
CREATE VIEW api.edges AS
SELECT
  e.src_collection,
  e.src_id,
  src.title AS src_title,
  e.rel,
  e.dst_collection,
  e.dst_id,
  dst.title AS dst_title,
  e.qualifiers,
  e.provenance_claim
FROM memory.edges e
LEFT JOIN memory.pages src ON src.collection = e.src_collection AND src.id = e.src_id
LEFT JOIN memory.pages dst ON dst.collection = e.dst_collection AND dst.id = e.dst_id
"""

_OLD_EDGES_VIEW = """
CREATE VIEW api.edges AS
SELECT
  e.src_id,
  src.title AS src_title,
  e.rel,
  e.dst_id,
  dst.title AS dst_title,
  e.qualifiers,
  e.provenance_claim
FROM memory.edges e
LEFT JOIN memory.pages src ON src.id = e.src_id
LEFT JOIN memory.pages dst ON dst.id = e.dst_id
"""

_SHARED_EDGES_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.edges IS 'Typed relations between pages/claims, both endpoints named. Example: SELECT src_title, rel, dst_title FROM api.edges WHERE rel = ''evaluates'' AND dst_id = ''ent.graphrag'' LIMIT 20;'",
    "COMMENT ON COLUMN api.edges.src_title IS 'Source title.'",
    "COMMENT ON COLUMN api.edges.rel IS 'Registry-validated relation. Example value: ''outperforms''.'",
    "COMMENT ON COLUMN api.edges.dst_title IS 'Destination title.'",
    "COMMENT ON COLUMN api.edges.qualifiers IS 'jsonb qualifiers copied from the claim block for claim-level edges.'",
    "COMMENT ON COLUMN api.edges.provenance_claim IS 'Claim id that produced this edge (claim-level edges only).'",
)

_NEW_EDGES_COMMENTS: tuple[str, ...] = (
    *_SHARED_EDGES_COMMENTS,
    "COMMENT ON COLUMN api.edges.src_collection IS 'Source endpoint''s collection. Example value: ''research''.'",
    "COMMENT ON COLUMN api.edges.src_id IS 'Source id (page or claim) — unique only paired with src_collection.'",
    "COMMENT ON COLUMN api.edges.dst_collection IS 'Destination endpoint''s collection. Example value: ''research''.'",
    "COMMENT ON COLUMN api.edges.dst_id IS 'Destination id — unique only paired with dst_collection.'",
)

_OLD_EDGES_COMMENTS: tuple[str, ...] = (
    *_SHARED_EDGES_COMMENTS,
    "COMMENT ON COLUMN api.edges.src_id IS 'Source id (page or claim).'",
    "COMMENT ON COLUMN api.edges.dst_id IS 'Destination id.'",
)

_NEW_RAW_CHUNKS_VIEW = """
CREATE VIEW api.raw_chunks AS
SELECT
  r.collection,
  r.source_id,
  r.section_path::text AS section_path,
  r.heading,
  r.body
FROM memory.raw_chunks r
"""

_OLD_RAW_CHUNKS_VIEW = """
CREATE VIEW api.raw_chunks AS
SELECT
  r.source_id,
  r.section_path::text AS section_path,
  r.heading,
  r.body
FROM memory.raw_chunks r
"""

_SHARED_RAW_CHUNKS_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.raw_chunks IS 'L4 verification spans: raw mirror fulltext chunked by numbered headings. Example: SELECT heading, body FROM api.raw_chunks WHERE source_id = ''ABCD1234'' ORDER BY section_path;'",
    "COMMENT ON COLUMN api.raw_chunks.section_path IS 'ltree path as text. Example value: ''s4.s1''.'",
    "COMMENT ON COLUMN api.raw_chunks.heading IS 'The numbered heading of the chunk. Example value: ''4.1 Experiments''.'",
    "COMMENT ON COLUMN api.raw_chunks.body IS 'Raw verbatim source text of the chunk.'",
)

_NEW_RAW_CHUNKS_COMMENTS: tuple[str, ...] = (
    "COMMENT ON COLUMN api.raw_chunks.collection IS 'This chunk''s own collection (its owning source mirror''s collection). Example value: ''research''.'",
    "COMMENT ON COLUMN api.raw_chunks.source_id IS 'Vendor-neutral source id of the mirror — unique only paired with collection. Example value: ''ABCD1234''.'",
    *_SHARED_RAW_CHUNKS_COMMENTS,
)

_OLD_RAW_CHUNKS_COMMENTS: tuple[str, ...] = (
    "COMMENT ON COLUMN api.raw_chunks.source_id IS 'Vendor-neutral source id of the mirror (memory.sources_raw.source_id). Example value: ''ABCD1234''.'",
    *_SHARED_RAW_CHUNKS_COMMENTS,
)

_NEW_SCOREBOARD_VIEW = f"""
CREATE VIEW api.scoreboard AS
SELECT
  p.id     AS entity,
  p.collection AS entity_collection,
  p.title  AS entity_title,
  COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets->'stages')), ARRAY[]::text[]) AS stages,
  count(*) FILTER (WHERE c.kind = 'support')      AS n_supports,
  count(*) FILTER (WHERE c.kind = 'refute')       AS n_refutes,
  count(*) FILTER (WHERE c.kind = 'benchmark-result' AND c.status = 'active') AS n_wins,
  count(*) FILTER (WHERE c.kind = 'ablation')     AS n_losses,
  count(*) FILTER (WHERE c.kind = 'null-result')  AS n_null_results,
  count(DISTINCT c.page_id)                       AS n_papers,
  count(DISTINCT ds.target_id) FILTER (WHERE ds.target_id IS NOT NULL) AS datasets_covered,
  max({_dialect_year_cast("pp")})                 AS last_evidence_year
FROM memory.pages p
LEFT JOIN memory.claims c ON c.subject_collection = p.collection AND c.subject_id = p.id
LEFT JOIN memory.pages  pp ON pp.collection = c.collection AND pp.id = c.page_id
LEFT JOIN memory.claim_qualifiers ds ON ds.collection = c.collection AND ds.claim_id = c.id AND ds.qualifier = 'on_dataset'
WHERE p.kind = 'entity'
GROUP BY p.collection, p.id, p.title
"""

_OLD_SCOREBOARD_VIEW = f"""
CREATE VIEW api.scoreboard AS
SELECT
  p.id     AS entity,
  p.title  AS entity_title,
  COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets->'stages')), ARRAY[]::text[]) AS stages,
  count(*) FILTER (WHERE c.kind = 'support')      AS n_supports,
  count(*) FILTER (WHERE c.kind = 'refute')       AS n_refutes,
  count(*) FILTER (WHERE c.kind = 'benchmark-result' AND c.status = 'active') AS n_wins,
  count(*) FILTER (WHERE c.kind = 'ablation')     AS n_losses,
  count(*) FILTER (WHERE c.kind = 'null-result')  AS n_null_results,
  count(DISTINCT c.page_id)                       AS n_papers,
  count(DISTINCT ds.target_id) FILTER (WHERE ds.target_id IS NOT NULL) AS datasets_covered,
  max({_dialect_year_cast("pp")})                 AS last_evidence_year
FROM memory.pages p
LEFT JOIN memory.claims c ON c.subject_id = p.id
LEFT JOIN memory.pages  pp ON pp.id = c.page_id
LEFT JOIN memory.claim_qualifiers ds ON ds.claim_id = c.id AND ds.qualifier = 'on_dataset'
WHERE p.kind = 'entity'
GROUP BY p.id, p.title
"""

_SHARED_SCOREBOARD_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.scoreboard IS 'Computed trust per entity: support/refute/win/null-result counts, datasets covered, latest evidence year. Example: SELECT entity, n_supports, n_wins, last_evidence_year FROM api.scoreboard WHERE ''retrieval'' = ANY(stages) ORDER BY n_wins + n_supports DESC LIMIT 15;'",
    "COMMENT ON COLUMN api.scoreboard.entity_title IS 'Entity name.'",
    "COMMENT ON COLUMN api.scoreboard.stages IS 'text[] pipeline-stage facet of the entity.'",
    "COMMENT ON COLUMN api.scoreboard.n_supports IS 'int — supporting claims.'",
    "COMMENT ON COLUMN api.scoreboard.n_refutes IS 'int — refuting claims.'",
    "COMMENT ON COLUMN api.scoreboard.n_wins IS 'int — active benchmark-result wins.'",
    "COMMENT ON COLUMN api.scoreboard.n_losses IS 'int — ablation/loss claims.'",
    "COMMENT ON COLUMN api.scoreboard.n_null_results IS 'int — null-result claims.'",
    "COMMENT ON COLUMN api.scoreboard.n_papers IS 'int — distinct papers contributing evidence.'",
    "COMMENT ON COLUMN api.scoreboard.datasets_covered IS 'int — distinct datasets in the evidence.'",
    "COMMENT ON COLUMN api.scoreboard.last_evidence_year IS 'int — most recent evidence year. Example value: 2025.'",
)

_NEW_SCOREBOARD_COMMENTS: tuple[str, ...] = (
    *_SHARED_SCOREBOARD_COMMENTS,
    "COMMENT ON COLUMN api.scoreboard.entity IS 'Entity page id — unique only paired with entity_collection.'",
    "COMMENT ON COLUMN api.scoreboard.entity_collection IS 'The entity''s own collection. Example value: ''research''.'",
)

_OLD_SCOREBOARD_COMMENTS: tuple[str, ...] = (
    *_SHARED_SCOREBOARD_COMMENTS,
    "COMMENT ON COLUMN api.scoreboard.entity IS 'Entity page id.'",
)


def _drop_touched_views() -> None:
    """Drop every touched view before the key swap.

    `api.scoreboard`'s `pg_depend` edge onto `pages_pkey` (see `_TOUCHED_VIEWS`'s docstring) means
    this must run before `_drop_old_keys`, not after: `_recreate_api_surface_new`'s view section
    runs too late in the upgrade order to unblock the PK drop, so the views come down here, on
    their own, right after the backfill guards pass.
    """
    for view_name in _TOUCHED_VIEWS:
        op.execute(f"DROP VIEW api.{view_name}")


def _recreate_api_surface_new() -> None:
    """Phase 5 (upgrade): every changed `api` function and view, over the new composite-key shape."""
    for view_sql, view_name, comments in (
        (_NEW_CARDS_VIEW, "cards", _NEW_CARDS_COMMENTS),
        (_NEW_SECTIONS_VIEW, "sections", _NEW_SECTIONS_COMMENTS),
        (_NEW_CLAIMS_VIEW, "claims", _NEW_CLAIMS_COMMENTS),
        (_NEW_EDGES_VIEW, "edges", _NEW_EDGES_COMMENTS),
        (_NEW_RAW_CHUNKS_VIEW, "raw_chunks", _NEW_RAW_CHUNKS_COMMENTS),
        (_NEW_SCOREBOARD_VIEW, "scoreboard", _NEW_SCOREBOARD_COMMENTS),
    ):
        op.execute(view_sql)
        op.execute(f"GRANT SELECT ON api.{view_name} TO {_READER_ROLE}")
        for statement in comments:
            op.execute(statement)

    op.execute(f"DROP FUNCTION {_OLD_FETCH_SIGNATURE}")
    op.execute(_NEW_FETCH_BODY)
    op.execute(f"GRANT EXECUTE ON FUNCTION {_NEW_FETCH_SIGNATURE} TO {_READER_ROLE}")
    for statement in _FETCH_COMMENT_STATEMENTS:
        op.execute(statement)

    op.execute(f"DROP FUNCTION {_OLD_LOOKUP_VALUE_SIGNATURE}")
    op.execute(_NEW_LOOKUP_VALUE_BODY)
    op.execute(f"GRANT EXECUTE ON FUNCTION {_NEW_LOOKUP_VALUE_SIGNATURE} TO {_READER_ROLE}")
    for statement in _LOOKUP_VALUE_COMMENT_STATEMENTS:
        op.execute(statement)

    op.execute(f"DROP FUNCTION {_OLD_ENVELOPE_PAGE_SIGNATURE}")
    op.execute(_NEW_ENVELOPE_PAGE_BODY)
    op.execute(f"GRANT EXECUTE ON FUNCTION {_NEW_ENVELOPE_PAGE_SIGNATURE} TO {_READER_ROLE}")
    for statement in _ENVELOPE_PAGE_COMMENT_STATEMENTS:
        op.execute(statement)

    op.execute(f"DROP FUNCTION {_OLD_ENVELOPE_SECTION_SIGNATURE}")
    op.execute(_NEW_ENVELOPE_SECTION_BODY)
    op.execute(f"GRANT EXECUTE ON FUNCTION {_NEW_ENVELOPE_SECTION_SIGNATURE} TO {_READER_ROLE}")
    for statement in _ENVELOPE_SECTION_COMMENT_STATEMENTS:
        op.execute(statement)

    expand_body, paths_body = _graph_bodies(_graph_schema(partitioned=True))
    op.execute(f"DROP FUNCTION {_OLD_EXPAND_SIGNATURE}")
    op.execute(f"DROP FUNCTION {_OLD_PATHS_SIGNATURE}")
    op.execute(expand_body)
    op.execute(paths_body)
    op.execute(f"GRANT EXECUTE ON FUNCTION {_NEW_EXPAND_SIGNATURE} TO {_READER_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_NEW_PATHS_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_NEW_EXPAND_COMMENT)
    op.execute(_NEW_PATHS_COMMENT)


def _recreate_api_functions_old() -> None:
    """Downgrade: restore every pre-0025 function body (order-independent — function bodies resolve lazily)."""
    expand_body, paths_body = _graph_bodies(_graph_schema(partitioned=False))
    op.execute(f"DROP FUNCTION {_NEW_EXPAND_SIGNATURE}")
    op.execute(f"DROP FUNCTION {_NEW_PATHS_SIGNATURE}")
    op.execute(expand_body)
    op.execute(paths_body)
    op.execute(f"GRANT EXECUTE ON FUNCTION {_OLD_EXPAND_SIGNATURE} TO {_READER_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_OLD_PATHS_SIGNATURE} TO {_READER_ROLE}")
    op.execute(EXPAND_COMMENT)
    op.execute(PATHS_COMMENT)

    op.execute(f"DROP FUNCTION {_NEW_ENVELOPE_SECTION_SIGNATURE}")
    op.execute(_OLD_ENVELOPE_SECTION_BODY)
    op.execute(f"GRANT EXECUTE ON FUNCTION {_OLD_ENVELOPE_SECTION_SIGNATURE} TO {_READER_ROLE}")
    op.execute(
        "COMMENT ON FUNCTION api.envelope_section(text) IS "
        "'Progressive disclosure, section envelope: one section''s id/title/body/summary, a disclosure "
        "leaf (no children). Example: SELECT * FROM api.envelope_section(''mu.x#s1'');'"
    )

    op.execute(f"DROP FUNCTION {_NEW_ENVELOPE_PAGE_SIGNATURE}")
    op.execute(_OLD_ENVELOPE_PAGE_BODY)
    op.execute(f"GRANT EXECUTE ON FUNCTION {_OLD_ENVELOPE_PAGE_SIGNATURE} TO {_READER_ROLE}")
    op.execute(
        "COMMENT ON FUNCTION api.envelope_page(text) IS "
        "'Progressive disclosure, page envelope: one page row (row_kind=''page'') plus its section "
        "children (row_kind=''section''), ordered by ord, in a single call. When the page''s whole "
        f"text is at most {_INLINE_CAP_CHARS} characters, each section row also carries its body "
        "(a small page reads whole in one call); larger pages return body NULL. A section''s "
        "summary falls back to its own compiled memory.sections.summary when it has no active "
        "unit_summaries row, exactly as a page falls back to its frontmatter summary. Example: "
        "SELECT * FROM api.envelope_page(''mu.x'');'"
    )

    op.execute(f"DROP FUNCTION {_NEW_LOOKUP_VALUE_SIGNATURE}")
    op.execute(_OLD_LOOKUP_VALUE_BODY)
    op.execute(f"GRANT EXECUTE ON FUNCTION {_OLD_LOOKUP_VALUE_SIGNATURE} TO {_READER_ROLE}")

    op.execute(f"DROP FUNCTION {_NEW_FETCH_SIGNATURE}")
    op.execute(_OLD_FETCH_BODY)
    op.execute(f"GRANT EXECUTE ON FUNCTION {_OLD_FETCH_SIGNATURE} TO {_READER_ROLE}")


def _recreate_views_old() -> None:
    """Downgrade: restore every pre-0025 view body.

    Must run AFTER `_restore_old_keys` — `_OLD_SCOREBOARD_VIEW`'s `GROUP BY p.id, p.title` selects
    other `p.*` columns without aggregating them, legal only when `p.id` is `pages`' WHOLE primary
    key (PostgreSQL's group-by-primary-key functional dependency): while the composite `(collection,
    id)` key from this revision's upgrade is still in place, `CREATE VIEW` itself would reject the
    body outright ("column p.facets must appear in the GROUP BY clause"), not merely warn.
    """
    for view_sql, view_name, comments in (
        (_OLD_CARDS_VIEW, "cards", _OLD_CARDS_COMMENTS),
        (_OLD_SECTIONS_VIEW, "sections", _OLD_SECTIONS_COMMENTS),
        (_OLD_CLAIMS_VIEW, "claims", _OLD_CLAIMS_COMMENTS),
        (_OLD_EDGES_VIEW, "edges", _OLD_EDGES_COMMENTS),
        (_OLD_RAW_CHUNKS_VIEW, "raw_chunks", _OLD_RAW_CHUNKS_COMMENTS),
        (_OLD_SCOREBOARD_VIEW, "scoreboard", _OLD_SCOREBOARD_COMMENTS),
    ):
        op.execute(view_sql)
        op.execute(f"GRANT SELECT ON api.{view_name} TO {_READER_ROLE}")
        for statement in comments:
            op.execute(statement)


# -- upgrade / downgrade -------------------------------------------------------


def upgrade() -> None:
    """Swap every corpus-owned table's key to `(collection, id)`, loud guards, then the api surface."""
    conn = op.get_bind()

    _add_new_columns()
    _backfill_columns()
    _assert_no_unresolved_edges(conn)
    _assert_no_cross_collection_duplicates(conn)
    _finalize_not_null_and_checks()
    _drop_touched_views()
    _drop_old_keys()
    _create_composite_keys_and_indexes()
    _recreate_api_surface_new()


def downgrade() -> None:
    """Collapse back to a single global id per table — loudly refuses when that would drop a row."""
    conn = op.get_bind()
    _assert_downgrade_safe(conn)

    _recreate_api_functions_old()
    _drop_touched_views()
    _drop_composite_keys_and_indexes()
    _restore_old_keys()
    _recreate_views_old()
    _drop_not_null_and_checks()

    op.execute("ALTER TABLE memory.sections DROP COLUMN collection")
    op.execute("ALTER TABLE memory.claims DROP COLUMN collection")
    op.execute("ALTER TABLE memory.claims DROP COLUMN subject_collection")
    op.execute("ALTER TABLE memory.comparison_cells DROP COLUMN collection")
    op.execute("ALTER TABLE memory.comparison_cells DROP COLUMN row_entity_collection")
    op.execute("ALTER TABLE memory.edges DROP COLUMN src_collection")
    op.execute("ALTER TABLE memory.edges DROP COLUMN dst_collection")
    op.execute("ALTER TABLE memory.link_refs DROP COLUMN dst_collection")
    op.execute("ALTER TABLE memory.vocab DROP COLUMN collection")
    op.execute("ALTER TABLE memory.raw_chunks DROP COLUMN collection")
