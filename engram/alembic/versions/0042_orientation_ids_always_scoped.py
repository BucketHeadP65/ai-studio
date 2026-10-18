"""Orientation page ids are always collection-scoped: `meta.about`/`meta.authoring` retire the bare form.

Revision ID: 0042
Revises: 0041
Create Date: 2026-09-02 00:00:00.000000

`engram.compile.handlers.orientation_page_id` now returns `f"{base_id}@{collection}"` for every
collection — the bare-literal exception for `'research'` (the one collection 0037's `api.about` /
`api.authoring_rules` / `api.collection_about` / `api.collection_authoring_rules` matched with no
suffix) is gone. This revision renames the existing bare `meta.about` / `meta.authoring` rows to
the suffixed form and repoints every dependent to match, two kinds:

* **Foreign-key-constrained** (`compile/orm.py`'s `ForeignKeyConstraint`s are this list's
  authority): `memory.pages.superseded_by`, `sections.page_id`, `claims.page_id`/`subject_id`,
  `claim_qualifiers.target_id`, `comparison_cells.comparison_id`/`row_entity`,
  `link_refs.src_page_id`.
* **Foreign-key-free, but a real page reference nonetheless**: `unit_summaries.unit_id` (the
  disclosure summary the envelope reads falls back to, `unit_kind` carrying no FK) and both
  `memory.edges` endpoint pairs, `src_id`/`dst_id` (a resolved graph neighbour). Left stale, a read
  in the window between this migration and the collection's next compile would serve a summary or
  a graph neighbour under an id no page carries anymore. `raw_chunks` and `page_type_contracts`
  carry no page reference at all; `lint_store.page_id` is also FK-free, but every one of its rows
  is wholesale-replaced by that collection's next compile regardless (`LintStoreRepository.replace_all`
  / `replace_for_pages`), so a stale row there self-heals and needs no repointing here.

Then recreates the two views and two functions against the unconditional suffixed id, keeping
0037's `api.visible_collection` guard exactly.

Every FK here is `NOT DEFERRABLE`, checked at the end of each statement — a plain `id` rename on
`memory.pages` would fail immediately were any dependent still pointing at the old value. The rename
instead clones each orientation row under its new id (so both ids briefly coexist, `ON CONFLICT
(collection, id) DO NOTHING` so a database that somehow already carries both forms still upgrades),
repoints every dependent onto the clone, then drops the old row once nothing references it — correct
whether or not a dependent actually exists, so it never depends on an unproven claim about live data.

Downgrade only has one bare id to give back: it is well-defined exactly when every orientation page
in the database belongs to the collection named `'research'` (0037's own hardcoded literal) — zero
orientation pages downgrades cleanly too, with nothing to rename. Any other single collection, or
more than one, refuses: 0037's bare-id form cannot represent either case.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.engine import Connection

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_LEGACY_COLLECTION = "research"
_ORIENTATION_BASE_IDS: tuple[str, ...] = ("meta.about", "meta.authoring")

# (table, id column, its collection column) for every reference onto an orientation page's id,
# FK-constrained or not (see the module docstring for which is which and why `lint_store` is
# excluded) — containment pairs reuse the referencing row's own `collection`, citation pairs carry
# their own target-collection column.
_DEPENDENTS: tuple[tuple[str, str, str], ...] = (
    ("memory.pages", "superseded_by", "collection"),
    ("memory.sections", "page_id", "collection"),
    ("memory.claims", "page_id", "collection"),
    ("memory.claims", "subject_id", "subject_collection"),
    ("memory.claim_qualifiers", "target_id", "target_collection"),
    ("memory.comparison_cells", "comparison_id", "collection"),
    ("memory.comparison_cells", "row_entity", "row_entity_collection"),
    ("memory.link_refs", "src_page_id", "collection"),
    ("memory.unit_summaries", "unit_id", "collection"),
    ("memory.edges", "src_id", "src_collection"),
    ("memory.edges", "dst_id", "dst_collection"),
)

# Every `memory.pages` column except `id`, in DDL order — the clone step's shared column list.
_PAGE_COLUMNS: tuple[str, ...] = (
    "collection",
    "kind",
    "slug",
    "title",
    "aliases",
    "summary",
    "status",
    "superseded_by",
    "source_id",
    "source_hash",
    "facets",
    "frontmatter",
    "card_text",
    "body_hash",
    "created",
    "updated",
    "embedding",
)

_ABOUT = """
CREATE OR REPLACE VIEW api.about AS
SELECT p.collection, p.card_text AS body
FROM memory.pages p
WHERE p.id = 'meta.about@' || p.collection AND api.visible_collection(p.collection)
"""

_AUTHORING_RULES = """
CREATE OR REPLACE VIEW api.authoring_rules AS
SELECT p.collection, p.card_text AS body
FROM memory.pages p
WHERE p.id = 'meta.authoring@' || p.collection AND api.visible_collection(p.collection)
"""

_COLLECTION_ABOUT = """
CREATE OR REPLACE FUNCTION api.collection_about(p_collection text)
  RETURNS TABLE(body text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT p.card_text AS body
    FROM memory.pages p
    WHERE p.id = 'meta.about@' || p_collection
      AND p.collection = p_collection AND api.visible_collection(p_collection)
$$
"""

_COLLECTION_AUTHORING_RULES = """
CREATE OR REPLACE FUNCTION api.collection_authoring_rules(p_collection text)
  RETURNS TABLE(body text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT p.card_text AS body
    FROM memory.pages p
    WHERE p.id = 'meta.authoring@' || p_collection
      AND p.collection = p_collection AND api.visible_collection(p_collection)
$$
"""

_ABOUT_OLD = """
CREATE OR REPLACE VIEW api.about AS
SELECT p.collection, p.card_text AS body
FROM memory.pages p
WHERE p.id = (CASE WHEN p.collection = 'research' THEN 'meta.about' ELSE 'meta.about@' || p.collection END) AND api.visible_collection(p.collection)
"""

_AUTHORING_RULES_OLD = """
CREATE OR REPLACE VIEW api.authoring_rules AS
SELECT p.collection, p.card_text AS body
FROM memory.pages p
WHERE p.id = (CASE WHEN p.collection = 'research' THEN 'meta.authoring' ELSE 'meta.authoring@' || p.collection END) AND api.visible_collection(p.collection)
"""

_COLLECTION_ABOUT_OLD = """
CREATE OR REPLACE FUNCTION api.collection_about(p_collection text)
  RETURNS TABLE(body text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT p.card_text AS body
    FROM memory.pages p
    WHERE p.id = (CASE WHEN p_collection = 'research' THEN 'meta.about' ELSE 'meta.about@' || p_collection END)
      AND p.collection = p_collection AND api.visible_collection(p_collection)
$$
"""

_COLLECTION_AUTHORING_RULES_OLD = """
CREATE OR REPLACE FUNCTION api.collection_authoring_rules(p_collection text)
  RETURNS TABLE(body text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT p.card_text AS body
    FROM memory.pages p
    WHERE p.id = (CASE WHEN p_collection = 'research' THEN 'meta.authoring' ELSE 'meta.authoring@' || p_collection END)
      AND p.collection = p_collection AND api.visible_collection(p_collection)
$$
"""


def _sql_list(values: Sequence[str]) -> str:
    """`values` as a parenthesis-free, comma-joined list of single-quoted SQL literals."""
    return ", ".join(f"'{value}'" for value in values)


def _clone_and_repoint(conn: Connection, *, old_ids: Sequence[str], suffix: bool) -> None:
    """Rename every `memory.pages` row whose id is in `old_ids`, and repoint every `_DEPENDENTS` column to match.

    Clones each matching row under its new id (`ON CONFLICT (collection, id) DO NOTHING`, so a
    database that already carries a row at the new id too — bare and scoped both present — still
    upgrades instead of failing on the clone), repoints every `_DEPENDENTS` column that still names
    the old id, then deletes the old row — safe regardless of whether a dependent exists, and safe
    against the `ON DELETE CASCADE` on `sections`/`claims`/`link_refs`, since every one of those is
    already repointed off the old id by the time the old row is dropped.

    Args:
        conn: The migration's bound connection.
        old_ids: The exact ids to rename away from (bare for the upgrade direction, `@research`
            -suffixed for the downgrade direction).
        suffix: `True` appends `'@' || collection` (upgrade); `False` strips a trailing `@...`
            back off via `split_part(..., '@', 1)` (downgrade).
    """
    id_expr = "id || '@' || collection" if suffix else "split_part(id, '@', 1)"
    id_list = _sql_list(old_ids)
    other_columns = ", ".join(_PAGE_COLUMNS)

    conn.exec_driver_sql(
        f"INSERT INTO memory.pages (id, {other_columns}) "
        f"SELECT {id_expr}, {other_columns} FROM memory.pages WHERE id IN ({id_list}) "
        "ON CONFLICT (collection, id) DO NOTHING"
    )
    for table, column, scope_column in _DEPENDENTS:
        new_id_expr = f"{column} || '@' || {scope_column}" if suffix else f"split_part({column}, '@', 1)"
        conn.exec_driver_sql(f"UPDATE {table} SET {column} = {new_id_expr} WHERE {column} IN ({id_list})")
    conn.exec_driver_sql(f"DELETE FROM memory.pages WHERE id IN ({id_list})")


def _orientation_collections(conn: Connection) -> set[str]:
    """Every distinct collection currently carrying a suffixed orientation page (`meta.about@<c>` / `meta.authoring@<c>`)."""
    result = conn.exec_driver_sql("SELECT DISTINCT collection FROM memory.pages WHERE id LIKE 'meta.about@%' OR id LIKE 'meta.authoring@%'")
    return {str(row[0]) for row in result}


def _assert_downgrade_safe(conn: Connection) -> None:
    """Downgrade guard: 0037's bare-id form can serve only `research`, never more than one collection.

    Raises:
        RuntimeError: An orientation page belongs to some collection other than `research` — 0037's
            hardcoded literal has no way to represent it, and downgrading would leave that
            collection's `api.about` / `api.authoring_rules` reading empty.
    """
    others = _orientation_collections(conn) - {_LEGACY_COLLECTION}
    if others:
        msg = (
            f"orientation pages exist outside {_LEGACY_COLLECTION!r} ({sorted(others)}); 0037's bare-id form "
            "serves only one collection, named 'research' -- resolve before downgrading"
        )
        raise RuntimeError(msg)


def upgrade() -> None:
    """Scope every collection's orientation ids, repoint FK dependents, then the guarded views/functions."""
    conn = op.get_bind()
    _clone_and_repoint(conn, old_ids=_ORIENTATION_BASE_IDS, suffix=True)

    op.execute(_ABOUT)
    op.execute(_AUTHORING_RULES)
    op.execute(_COLLECTION_ABOUT)
    op.execute(_COLLECTION_AUTHORING_RULES)


def downgrade() -> None:
    """Restore 0037's bodies, and `research`'s bare ids when it is the only collection carrying one."""
    conn = op.get_bind()
    _assert_downgrade_safe(conn)

    if _LEGACY_COLLECTION in _orientation_collections(conn):
        suffixed_ids = tuple(f"{base}@{_LEGACY_COLLECTION}" for base in _ORIENTATION_BASE_IDS)
        _clone_and_repoint(conn, old_ids=suffixed_ids, suffix=False)

    op.execute(_ABOUT_OLD)
    op.execute(_AUTHORING_RULES_OLD)
    op.execute(_COLLECTION_ABOUT_OLD)
    op.execute(_COLLECTION_AUTHORING_RULES_OLD)
