"""`api.page_types` gains a typed `decision` column: a kind's open status and outcomes, off the compiled contract.

Revision ID: 0039
Revises: 0038
Create Date: 2026-09-01 00:00:00.000000

`PageTypeSpec.decision` (engram's `CorpusSpec`) already rides the whole node `contract` jsonb
carries, but a reader had no typed path to it before this, only the untyped `contract -> 'decision'`.
`CREATE OR REPLACE VIEW` appends the column at the end of the projection, which preserves the
view's existing grant and every current reader query untouched -- including the `WHERE
api.visible_collection(t.collection)` guard 0037_library_privacy.py put on this view: that guard
is reproduced verbatim in both the upgrade body and the downgrade's restore body below, since the
live view this migration replaces is 0037's guarded one, not 0036's bare one (0038 left it alone).
A kind with no declared `decision` still carries the key in its compiled `contract`, with a JSON
null value, not an absent key -- `t.contract -> 'decision'` alone would read back as the jsonb
value `'null'::jsonb`, not SQL `NULL`. `NULLIF(t.contract -> 'decision', 'null'::jsonb)` folds that
JSON null into a real SQL `NULL`, matching the column comment's own contract.
"""

from __future__ import annotations

from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "corpus_reader"

# The view body as 0037_library_privacy.py left it (its `_PAGE_TYPES` guarded form) -- unchanged by
# 0038, which touched only `api.cards.frontmatter`'s comment. Byte-identical to that migration's
# rendering, restored on downgrade.
_VIEW_BODY_PRE_0039 = """
CREATE OR REPLACE VIEW api.page_types AS
SELECT t.collection,
       t.kind,
       COALESCE(t.contract ->> 'id_prefix', '') AS id_prefix,
       t.contract ->> 'folder' AS folder,
       ARRAY(SELECT jsonb_array_elements_text(t.contract -> 'status_values')) AS status_values,
       ARRAY(
         SELECT CASE WHEN jsonb_typeof(elem.value) = 'object' THEN elem.value ->> 'slug' ELSE elem.value #>> '{}' END
         FROM jsonb_array_elements(t.contract -> 'canonical_sections') elem(value)
       ) AS canonical_sections,
       ARRAY(SELECT jsonb_array_elements_text(t.contract -> 'edge_fields')) AS edge_fields,
       COALESCE((t.contract ->> 'claims_enabled')::boolean, false) AS claims_enabled,
       COALESCE((t.contract ->> 'disclosed')::boolean, true) AS disclosed,
       COALESCE(t.contract -> 'frontmatter_fields', '[]'::jsonb) AS frontmatter_fields,
       t.contract
FROM memory.page_type_contracts t
WHERE api.visible_collection(t.collection)
"""

_VIEW_COMMENT = (
    "COMMENT ON VIEW api.page_types IS "
    "'The writing contract per page type: which statuses, sections, and frontmatter a kind accepts. "
    "One row per collection and kind. Example: "
    "SELECT status_values FROM api.page_types WHERE collection = ''research'' AND kind = ''flag'';'"
)

_COLUMN_COMMENTS: tuple[str, ...] = (
    "COMMENT ON COLUMN api.page_types.collection IS 'The corpus collection this page type belongs to.'",
    "COMMENT ON COLUMN api.page_types.kind IS 'The page type slug, e.g. flag or paper.'",
    "COMMENT ON COLUMN api.page_types.id_prefix IS "
    "'The id prefix pages of this kind must carry, or an empty string when ids are free form.'",
    "COMMENT ON COLUMN api.page_types.folder IS 'The corpus subfolder this kind is authored under, or null when it declares none.'",
    "COMMENT ON COLUMN api.page_types.status_values IS "
    "'The closed set of status values this kind accepts, or an empty array when the grammar default applies.'",
    "COMMENT ON COLUMN api.page_types.canonical_sections IS 'The ordered H2 section slugs a page of this kind pre-seeds.'",
    "COMMENT ON COLUMN api.page_types.edge_fields IS 'The frontmatter fields on this kind that are typed relations (graph edges).'",
    "COMMENT ON COLUMN api.page_types.claims_enabled IS 'Whether pages of this kind carry claim bullets.'",
    "COMMENT ON COLUMN api.page_types.disclosed IS 'Whether units of this kind are listed as envelope children.'",
    "COMMENT ON COLUMN api.page_types.frontmatter_fields IS "
    "'The full field declarations (name, type, required, choices, default) for this kind.'",
    "COMMENT ON COLUMN api.page_types.contract IS "
    "'The whole page-type spec node as compiled, the escape hatch for anything the typed columns leave out.'",
)

# The guarded body plus the new `decision` column, appended at the end so `CREATE OR REPLACE VIEW`
# preserves the view's grant and every existing reader query.
_VIEW_BODY = """
CREATE OR REPLACE VIEW api.page_types AS
SELECT t.collection,
       t.kind,
       COALESCE(t.contract ->> 'id_prefix', '') AS id_prefix,
       t.contract ->> 'folder' AS folder,
       ARRAY(SELECT jsonb_array_elements_text(t.contract -> 'status_values')) AS status_values,
       ARRAY(
         SELECT CASE WHEN jsonb_typeof(elem.value) = 'object' THEN elem.value ->> 'slug' ELSE elem.value #>> '{}' END
         FROM jsonb_array_elements(t.contract -> 'canonical_sections') elem(value)
       ) AS canonical_sections,
       ARRAY(SELECT jsonb_array_elements_text(t.contract -> 'edge_fields')) AS edge_fields,
       COALESCE((t.contract ->> 'claims_enabled')::boolean, false) AS claims_enabled,
       COALESCE((t.contract ->> 'disclosed')::boolean, true) AS disclosed,
       COALESCE(t.contract -> 'frontmatter_fields', '[]'::jsonb) AS frontmatter_fields,
       t.contract,
       NULLIF(t.contract -> 'decision', 'null'::jsonb) AS decision
FROM memory.page_type_contracts t
WHERE api.visible_collection(t.collection)
"""

_DECISION_COLUMN_COMMENT = (
    "COMMENT ON COLUMN api.page_types.decision IS "
    "'jsonb: the kind''s decision block, null for a kind with no declared decision. An object with open (the status a "
    "decision opens in) and outcomes (an array of objects, each with status and an optional link naming the edge_fields "
    "entry that outcome asks the owner to write). Example: SELECT decision FROM api.page_types WHERE "
    "collection = ''research'' AND kind = ''flag'';'"
)


def upgrade() -> None:
    """Append a typed `decision` column to `api.page_types`, preserving the view's existing grant and visibility guard."""
    op.execute(_VIEW_BODY)
    op.execute(_DECISION_COLUMN_COMMENT)


def downgrade() -> None:
    """Drop and recreate `api.page_types` without the `decision` column, restoring its pre-0039 grant, guard and comments."""
    op.execute("DROP VIEW IF EXISTS api.page_types")
    op.execute(_VIEW_BODY_PRE_0039)
    op.execute(f"GRANT SELECT ON api.page_types TO {_READER_ROLE}")
    op.execute(_VIEW_COMMENT)
    for statement in _COLUMN_COMMENTS:
        op.execute(statement)
