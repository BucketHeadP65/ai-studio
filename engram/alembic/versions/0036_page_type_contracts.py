"""`memory.page_type_contracts` + `api.page_types`: the corpusspec's page-type contract, served by the DB.

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-01 00:00:00.000000

A corpusspec declares each page type's contract (`status_values`, `id_prefix`, `folder`,
`canonical_sections`, `frontmatter_fields`, `claims_enabled`, `disclosed`) but nothing surfaced it
through the `api` SQL doors before this: `api.lookup_value` covers only facets/entity_type,
`api.authoring_rules` is hand-written prose. The DB stored only `spec_hash`
(`memory.compile_fingerprint`), never the contract itself.

`memory.page_type_contracts` gains one row per `(collection, kind)` every compile writes, the
page type's spec node serialized whole into `contract` jsonb. It stays ungranted, exactly like its
compile-state siblings. `api.page_types` is the one reader door onto it: one row per declared page
type, the common fields extracted as typed columns (`status_values`, `canonical_sections`, and
`edge_fields` as `text[]`; `claims_enabled`/`disclosed` as `boolean`; `frontmatter_fields` left as
jsonb) plus `contract` itself as the escape hatch for anything the typed columns leave out. Every
array extraction tolerates an absent key (an empty array, never an error) since a hand-written or
partial contract row should degrade gracefully rather than break the view.
"""

from __future__ import annotations

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "corpus_reader"

_TABLE_COMMENT = (
    "COMMENT ON TABLE memory.page_type_contracts IS "
    "'One row per collection and page-type kind, the compiled contract from that corpus''s spec. "
    "Ungranted, served to readers only through api.page_types, never by a direct grant on this table.'"
)

_VIEW_BODY = """
CREATE VIEW api.page_types AS
SELECT
  t.collection,
  t.kind,
  COALESCE(t.contract ->> 'id_prefix', '') AS id_prefix,
  t.contract ->> 'folder' AS folder,
  ARRAY(SELECT jsonb_array_elements_text(t.contract -> 'status_values')) AS status_values,
  ARRAY(
    SELECT CASE WHEN jsonb_typeof(elem) = 'object' THEN elem ->> 'slug' ELSE elem #>> '{}' END
    FROM jsonb_array_elements(t.contract -> 'canonical_sections') AS elem
  ) AS canonical_sections,
  ARRAY(SELECT jsonb_array_elements_text(t.contract -> 'edge_fields')) AS edge_fields,
  COALESCE((t.contract ->> 'claims_enabled')::boolean, false) AS claims_enabled,
  COALESCE((t.contract ->> 'disclosed')::boolean, true) AS disclosed,
  COALESCE(t.contract -> 'frontmatter_fields', '[]'::jsonb) AS frontmatter_fields,
  t.contract
FROM memory.page_type_contracts t
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


def upgrade() -> None:
    """Create `memory.page_type_contracts`, `api.page_types`, and grant SELECT on the view to the reader role."""
    op.execute(
        """
        CREATE TABLE memory.page_type_contracts (
          collection text NOT NULL,
          kind text NOT NULL,
          contract jsonb NOT NULL,
          PRIMARY KEY (collection, kind)
        )
        """
    )
    op.execute(_TABLE_COMMENT)
    op.execute(_VIEW_BODY)
    op.execute(f"GRANT SELECT ON api.page_types TO {_READER_ROLE}")
    op.execute(_VIEW_COMMENT)
    for statement in _COLUMN_COMMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Drop `api.page_types`, then `memory.page_type_contracts`."""
    op.execute("DROP VIEW IF EXISTS api.page_types")
    op.execute("DROP TABLE IF EXISTS memory.page_type_contracts")
