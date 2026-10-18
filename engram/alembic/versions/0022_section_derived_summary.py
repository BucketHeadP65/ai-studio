"""Sections carry a compiled `summary` column; `api.envelope_page` falls back to it.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-19 00:00:00.000000

Pages already fall back to an authored `summary:` frontmatter value when no active
`unit_summaries` row exists (`api.envelope_page`'s page branch: `COALESCE(unit_summaries.summary,
pages.summary, '')`). Sections had no equivalent — no frontmatter, and until now no compiled
column either — so an un-summarized section fell straight to `''`. `memory.sections.summary` is
nullable and populated at compile time (`engram.compile.section_summary`), and this revision
replaces `api.envelope_page` so its section branch reads `COALESCE(unit_summaries.summary,
sec.summary, '')`, mirroring the page branch exactly.

The function's OUT columns are unchanged from `0017`, so `CREATE OR REPLACE FUNCTION` applies
directly (no DROP needed). Downgrade drops the column (after restoring the pre-0022 function body,
so the column is never referenced by a live function body while it is dropped) and restores
`0017`'s section-branch `COALESCE(t.summary, '')`.
"""

from __future__ import annotations

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "corpus_reader"
_SIGNATURE = "api.envelope_page(text)"

_INLINE_CAP_CHARS = 6_000
"""Sum of section-body characters (~1.5k tokens) up to which a page's text rides its envelope (0017)."""


def upgrade() -> None:
    """Add `memory.sections.summary`, then replace `api.envelope_page` to fall back to it."""
    op.execute("ALTER TABLE memory.sections ADD COLUMN summary text")
    op.execute(
        "COMMENT ON COLUMN memory.sections.summary IS "
        "'Compiled summary derived from this section''s own body at compile time "
        "(engram.compile.section_summary): the whole body when short enough to read inline, "
        "otherwise its leading sentences, its leading list items, or (a table/code-only body) "
        "empty. NULL for a row loaded before this column existed. api.envelope_page falls back to "
        "it only when no active memory.unit_summaries row exists for the section.'"
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION api.envelope_page(p_page_id text)
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
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO {_READER_ROLE}")
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


def downgrade() -> None:
    """Restore 0017's section-branch `COALESCE(t.summary, '')`, then drop `memory.sections.summary`."""
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION api.envelope_page(p_page_id text)
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
    op.execute("ALTER TABLE memory.sections DROP COLUMN summary")
