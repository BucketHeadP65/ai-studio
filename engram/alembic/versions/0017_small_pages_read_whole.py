"""api.envelope_page inlines section bodies when the whole page is small.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-17 00:00:00.000000

The leveled read exists to make reading cost-proportional: a page envelope lists section stubs so
the next tokens go only to sections the reader chose. For a page whose entire text costs less than
the choosing ceremony, that machinery taxes exactly the case it cannot help — so
``api.envelope_page`` now returns a ``body`` column on its section rows, filled when the page's
summed section text is at most ``_INLINE_CAP_CHARS`` characters and NULL otherwise. The envelope
handler renders filled bodies as the page's own content (the whole small page in one read); a page
over the cap behaves exactly as before.

The cap lives in the function body so the decision is made in the database, before any text is
transferred: a large page never ships its bodies to be discarded client-side.

Changing a function's OUT columns requires DROP + CREATE (``CREATE OR REPLACE`` refuses a changed
return table), and the drop takes the 0012 grant and comment with it, so both are re-issued.
Downgrade restores 0012's body-less definition, grant, and comment verbatim.
"""

from __future__ import annotations

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "corpus_reader"
_SIGNATURE = "api.envelope_page(text)"

_INLINE_CAP_CHARS = 6_000
"""Sum of section-body characters (~1.5k tokens) up to which a page's text rides its envelope."""


def upgrade() -> None:
    """Recreate ``api.envelope_page`` with the threshold-gated ``body`` column, re-grant, re-comment."""
    op.execute(f"DROP FUNCTION {_SIGNATURE}")
    op.execute(
        f"""
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
                   COALESCE(t.summary, '') AS summary,
                   CASE WHEN page_size.chars <= {_INLINE_CAP_CHARS} THEN sec.body END AS body
            FROM memory.sections sec
            CROSS JOIN page_size
            LEFT JOIN memory.unit_summaries t ON t.unit_id = sec.id AND t.status = 'active'
            WHERE sec.page_id = p_page_id
            ORDER BY ord
          $$
        """
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO {_READER_ROLE}")
    op.execute(
        "COMMENT ON FUNCTION api.envelope_page(text) IS "
        "'Progressive disclosure, page envelope: one page row (row_kind=''page'') plus its section "
        "children (row_kind=''section''), ordered by ord, in a single call. When the page''s whole "
        f"text is at most {_INLINE_CAP_CHARS} characters, each section row also carries its body "
        "(a small page reads whole in one call); larger pages return body NULL. Example: "
        "SELECT * FROM api.envelope_page(''mu.x'');'"
    )


def downgrade() -> None:
    """Restore 0012's body-less ``api.envelope_page``, its grant, and its comment."""
    op.execute(f"DROP FUNCTION {_SIGNATURE}")
    op.execute(
        """
        CREATE FUNCTION api.envelope_page(p_page_id text)
          RETURNS TABLE (row_kind text, id text, kind text, title text, ord int, summary text)
          LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
            SELECT 'page'::text, p.id, p.kind, p.title, -1 AS ord,
                   COALESCE(s.summary, p.summary, '') AS summary
            FROM memory.pages p
            LEFT JOIN memory.unit_summaries s ON s.unit_id = p.id AND s.status = 'active'
            WHERE p.id = p_page_id
            UNION ALL
            SELECT 'section'::text, sec.id, 'section'::text, sec.heading, sec.ord AS ord,
                   COALESCE(t.summary, '') AS summary
            FROM memory.sections sec
            LEFT JOIN memory.unit_summaries t ON t.unit_id = sec.id AND t.status = 'active'
            WHERE sec.page_id = p_page_id
            ORDER BY ord
          $$
        """
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO {_READER_ROLE}")
    op.execute(
        "COMMENT ON FUNCTION api.envelope_page(text) IS "
        "'Progressive disclosure, page envelope: one page row (row_kind=''page'') plus its section "
        "children (row_kind=''section''), ordered by ord, in a single call. Example: "
        "SELECT * FROM api.envelope_page(''mu.x'');'"
    )
