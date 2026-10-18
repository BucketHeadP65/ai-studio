"""api envelope surface: SECURITY DEFINER functions so the reader role can render disclosure envelopes.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-09 00:00:00.000000

``engram.retrieval.envelope.EnvelopeHandler`` renders envelopes from ``memory.pages`` /
``memory.sections`` / ``memory.unit_summaries``, but ``wiki_reader`` (0003) is granted the
``api`` schema **only** — a reader-path query naming ``memory.*`` raises ``permission denied
for schema memory``.

The house pattern (0002 / 0007 / 0010): a reader-facing read over ``memory.*`` is a
hand-authored ``SECURITY DEFINER`` function in ``api`` with ``search_path`` pinned to
``memory, public, pg_catalog`` — the function runs as its **owner** (who can see
``memory.*``), not as the calling ``wiki_reader`` (who cannot), the same reasoning that made
``api.search`` / ``api.fetch`` / ``api.lookup_value`` / ``api.log_session`` SECURITY DEFINER
in 0002. Three functions mirror ``EnvelopeHandler``'s three SQL statements **exactly** — same
``WHERE``/``LEFT JOIN``/``COALESCE``/``ORDER BY``, same OUT columns — so `envelope.py`'s SQL
constants are thin ``SELECT * FROM api.envelope_*(:param)`` wrappers:

* ``api.envelope_collection(p_collection text)`` — the root map: a collection's pages
  (id/kind/title/summary), filtered **only** by ``collection``, never by page ``status`` —
  envelope consumers must not assume every listed unit is ``active``.
* ``api.envelope_page(p_page_id text)`` — one page's own row (``row_kind = 'page'``) **and**
  its section children (``row_kind = 'section'``) in a single ``UNION ALL``, so a caller gets
  exactly one result shape per ref. The page leg's ``ord = -1`` sentinel sorts it
  before every real (``>= 0``) section ``ord``.
* ``api.envelope_section(p_section_id text)`` — one section's id/title/body/summary, a
  disclosure leaf.

Every summary column ``COALESCE``s ``memory.unit_summaries.summary`` (LEFT JOINed with
``status = 'active'`` **in the join predicate**, never a ``WHERE`` filter — that would turn
the LEFT JOIN into an implicit INNER JOIN and drop every unit with no active summary) → a
page's own ``memory.pages.summary`` frontmatter → ``''``; a section has no frontmatter column
so it falls straight to ``''``.

Function parameters are prefixed ``p_`` (``p_collection`` / ``p_page_id`` / ``p_section_id``)
rather than bare ``collection`` / ``page_id`` — ``memory.pages.collection`` and
``memory.sections.page_id`` are real column names, so an unprefixed parameter would make the
body's unqualified reference ambiguous between "the column" and "the parameter" (a genuine
``LANGUAGE sql`` parse error, not a style nit). Callers bind positionally
(``SELECT * FROM api.envelope_page(:page_id)``), so the internal parameter name is invisible
to `envelope.py`'s SQL constants — this is purely an implementation-internal disambiguation.

Grants: 0003's ``ALTER DEFAULT PRIVILEGES IN SCHEMA api GRANT EXECUTE ON FUNCTIONS TO
wiki_reader`` auto-grants EXECUTE on every function created in ``api``; the explicit
``GRANT EXECUTE ... TO wiki_reader`` per function below is a redundant no-op kept for parity
with 0004/0010's precedent for brand-new (non-``REPLACE``) functions.

Downgrade drops all three functions by signature (their grants go with them).
"""

from __future__ import annotations

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "wiki_reader"

_ENVELOPE_COLLECTION_SIGNATURE = "api.envelope_collection(text)"
_ENVELOPE_PAGE_SIGNATURE = "api.envelope_page(text)"
_ENVELOPE_SECTION_SIGNATURE = "api.envelope_section(text)"


def upgrade() -> None:
    """Create the three ``api.envelope_*`` SECURITY DEFINER functions, grant, and comment them."""
    op.execute(
        """
        CREATE FUNCTION api.envelope_collection(p_collection text)
          RETURNS TABLE (id text, kind text, title text, summary text)
          LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
            SELECT p.id, p.kind, p.title,
                   COALESCE(s.summary, p.summary, '') AS summary
            FROM memory.pages p
            LEFT JOIN memory.unit_summaries s ON s.unit_id = p.id AND s.status = 'active'
            WHERE p.collection = p_collection
            ORDER BY p.title, p.id
        $$
        """
    )

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

    op.execute(
        """
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
    )

    op.execute(f"GRANT EXECUTE ON FUNCTION {_ENVELOPE_COLLECTION_SIGNATURE} TO {_READER_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_ENVELOPE_PAGE_SIGNATURE} TO {_READER_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_ENVELOPE_SECTION_SIGNATURE} TO {_READER_ROLE}")

    for statement in _COMMENT_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Drop all three ``api.envelope_*`` functions (their grants and comments go with them)."""
    op.execute(f"DROP FUNCTION {_ENVELOPE_COLLECTION_SIGNATURE}")
    op.execute(f"DROP FUNCTION {_ENVELOPE_PAGE_SIGNATURE}")
    op.execute(f"DROP FUNCTION {_ENVELOPE_SECTION_SIGNATURE}")


# --- COMMENTs (the M-Schema additions) ---------------------------------------
_COMMENT_STATEMENTS: tuple[str, ...] = (
    "COMMENT ON FUNCTION api.envelope_collection(text) IS "
    "'Progressive disclosure, root map: the pages belonging to one collection (id, kind, title, "
    "summary), filtered only by collection, never by page status. Example: "
    "SELECT * FROM api.envelope_collection(''workdocs'');'",
    "COMMENT ON FUNCTION api.envelope_page(text) IS "
    "'Progressive disclosure, page envelope: one page row (row_kind=''page'') plus its section "
    "children (row_kind=''section''), ordered by ord, in a single call. Example: "
    "SELECT * FROM api.envelope_page(''mu.x'');'",
    "COMMENT ON FUNCTION api.envelope_section(text) IS "
    "'Progressive disclosure, section envelope: one section''s id/title/body/summary, a disclosure "
    "leaf (no children). Example: SELECT * FROM api.envelope_section(''mu.x#s1'');'",
)
