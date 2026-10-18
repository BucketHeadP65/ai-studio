"""api.facets view + slot-complete lookup_value: per-vault facets become queryable.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-16 00:00:00.000000

Two gaps made a vault's own facets invisible to the read surface — a facet declared in
the vaultspec with ``slot: null`` reached neither:

* ``api.lookup_value`` counted pages only over the three research-era array slots
  (``tasks`` / ``domains`` / ``stages``) plus ``entity_type`` — a facet mapped to
  ``modalities`` / ``scale_regimes``, or a slotless facet living in the
  ``frontmatter`` jsonb, always reported ``n_pages = 0``. Replaced here with a body
  that checks all five array slots AND the frontmatter jsonb key named by the
  ``facet:<key>`` vocab entry (string or array shaped).
* No surface exposed the frontmatter long tail at all, so a question like
  "which stories are MVP" was unanswerable through ``api``. The new ``api.facets``
  view unpivots every page's facet assignments — the five array slots plus every
  scalar/array key of the ``frontmatter`` jsonb (slotless facets, official ids such
  as ``us_id``, ``doc_type``, …) — into ``(page_id, collection, facet, value)`` rows.

This surface reads the ``frontmatter`` column ``_RowBuilder._page_row`` projects the
note's ``model_extra`` long tail into, so a vault compiled before that projection must
recompile once to populate it.
"""

from __future__ import annotations

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Create ``api.facets`` and replace ``api.lookup_value`` with the slot-complete body."""
    op.execute(
        """
        CREATE VIEW api.facets AS
        SELECT p.id AS page_id, p.collection, f.facet, f.value
        FROM wiki.pages p
        CROSS JOIN LATERAL (
          SELECT 'tasks'::text AS facet, t.v AS value FROM unnest(p.tasks) t(v)
          UNION ALL SELECT 'domains', d.v FROM unnest(p.domains) d(v)
          UNION ALL SELECT 'stages', s.v FROM unnest(p.stages) s(v)
          UNION ALL SELECT 'modalities', m.v FROM unnest(p.modalities) m(v)
          UNION ALL SELECT 'scale_regimes', r.v FROM unnest(p.scale_regimes) r(v)
          UNION ALL
          SELECT kv.key, kv.value
          FROM jsonb_each_text(p.frontmatter) kv(key, value)
          WHERE jsonb_typeof(p.frontmatter -> kv.key) IN ('string', 'number', 'boolean')
          UNION ALL
          SELECT kv.key, el.v
          FROM jsonb_each(p.frontmatter) kv(key, value)
          CROSS JOIN LATERAL jsonb_array_elements_text(kv.value) el(v)
          WHERE jsonb_typeof(kv.value) = 'array'
        ) f
        WHERE f.value IS NOT NULL
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION api.lookup_value(facet text, term text DEFAULT NULL)
          RETURNS TABLE (value text, definition text, n_pages int)
          LANGUAGE sql STABLE SECURITY DEFINER SET search_path = wiki, public, pg_catalog AS $$
            SELECT v.value, v.definition,
                   (SELECT count(*)::int FROM wiki.pages p
                    WHERE v.value = ANY(p.tasks) OR v.value = ANY(p.domains)
                       OR v.value = ANY(p.stages) OR v.value = ANY(p.modalities)
                       OR v.value = ANY(p.scale_regimes) OR p.entity_type = v.value
                       OR (v.vocab LIKE 'facet:%' AND (
                             p.frontmatter ->> split_part(v.vocab, ':', 2) = v.value
                             OR (jsonb_typeof(p.frontmatter -> split_part(v.vocab, ':', 2)) = 'array'
                                 AND p.frontmatter -> split_part(v.vocab, ':', 2) ? v.value)))) AS n_pages
            FROM wiki.vocab v
            WHERE (v.vocab = facet OR v.vocab = 'facet:' || facet)
              AND (term IS NULL OR v.value ILIKE '%' || term || '%')
            ORDER BY v.value
        $$
        """
    )

    for statement in _COMMENT_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Drop ``api.facets`` and restore the 0002 three-slot ``lookup_value`` body."""
    op.execute("DROP VIEW IF EXISTS api.facets")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION api.lookup_value(facet text, term text DEFAULT NULL)
          RETURNS TABLE (value text, definition text, n_pages int)
          LANGUAGE sql STABLE SECURITY DEFINER SET search_path = wiki, public, pg_catalog AS $$
            SELECT v.value, v.definition,
                   (SELECT count(*)::int FROM wiki.pages p
                    WHERE v.value = ANY(p.tasks) OR v.value = ANY(p.domains)
                       OR v.value = ANY(p.stages) OR p.entity_type = v.value) AS n_pages
            FROM wiki.vocab v
            WHERE (v.vocab = facet OR v.vocab = 'facet:' || facet)
              AND (term IS NULL OR v.value ILIKE '%' || term || '%')
            ORDER BY v.value
        $$
        """
    )


# --- COMMENTs (the M-Schema additions) ---------------------------------------
_COMMENT_STATEMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.facets IS 'Facet assignments unpivoted: one row per (page, facet, value) across the five array slots AND every scalar/array key of the frontmatter long tail (slotless per-vault facets like priority, official ids like us_id, doc_type, …). Example: SELECT page_id FROM api.facets WHERE facet = ''priority'' AND value = ''mvp'';'",
    "COMMENT ON COLUMN api.facets.page_id IS 'The page carrying this facet value. Example value: ''us.022''.'",
    "COMMENT ON COLUMN api.facets.collection IS 'Corpus partition of the page. Example value: ''field-notes''.'",
    "COMMENT ON COLUMN api.facets.facet IS 'Facet key: a slot name (tasks|domains|stages|modalities|scale_regimes) or a frontmatter key. Example value: ''priority''.'",
    "COMMENT ON COLUMN api.facets.value IS 'One assigned value. Example value: ''mvp''.'",
    "COMMENT ON FUNCTION api.lookup_value IS 'Value grounding: what values exist for a facet, with definitions and page counts across all five slots and the frontmatter long tail (so agents never guess WHERE strings). Example: SELECT * FROM api.lookup_value(''priority'');'",
)
