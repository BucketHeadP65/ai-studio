"""`api.raw_chunks` serves each chunk's `seq`, and its comments describe chunks cut at headings and to the chunk sizes.

Revision ID: 0068
Revises: 0067
Create Date: 2026-10-18 00:00:00.000000

A mirror with no heading can now be many chunks that all carry a null section path, so ordering by `section_path` no
longer reads a mirror in order. The view gains `seq`, each chunk's place in its mirror's reading order, as its last
column, and the view comment's example orders by it. The `heading` and new `seq` columns carry comments that say what
a chunk of a filled heading section holds. Downgrade drops `seq`: a view cannot lose a column in place, so the view is
dropped and created again as it stood at 0067, with every comment and the reader's grant.
"""

from __future__ import annotations

from alembic import op

revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "kb_reader"

_VIEW = """
CREATE OR REPLACE VIEW api.raw_chunks AS
SELECT r.collection, r.source_id, r.section_path::text AS section_path, r.heading, r.body, r.seq
FROM kb.raw_chunks r
WHERE api.visible_collection(r.collection)
"""

_PREVIOUS_VIEW = """
CREATE VIEW api.raw_chunks AS
SELECT r.collection, r.source_id, r.section_path::text AS section_path, r.heading, r.body
FROM kb.raw_chunks r
WHERE api.visible_collection(r.collection)
"""

_COMMENTS: tuple[tuple[str, str], ...] = (
    (
        "VIEW api.raw_chunks",
        (
            "L4 verification spans: raw mirror fulltext cut at headings and to the chunk sizes, so one heading section can "
            "span several chunks that share its heading. Example: SELECT heading, body FROM api.raw_chunks WHERE collection "
            "= '<collection>' AND source_id = '<source-id>' ORDER BY seq;"
        ),
    ),
    (
        "COLUMN api.raw_chunks.heading",
        (
            "The heading the chunk sits under, numbered or not, null for the text before the first heading. Every chunk of "
            "one heading section carries it. Example value: '4.1 Experiments'."
        ),
    ),
    (
        "COLUMN api.raw_chunks.seq",
        "The chunk's place in its mirror's reading order, counted from 0. Order by it to read a mirror in order. Example value: 3.",
    ),
)

_PREVIOUS_COMMENTS: tuple[tuple[str, str], ...] = (
    (
        "VIEW api.raw_chunks",
        (
            "L4 verification spans: raw mirror fulltext chunked by numbered headings. Example: SELECT heading, body FROM "
            "api.raw_chunks WHERE source_id = 'ABCD1234' ORDER BY section_path;"
        ),
    ),
    (
        "COLUMN api.raw_chunks.collection",
        "This chunk's own collection (its owning source mirror's collection). Example value: '<collection>'.",
    ),
    (
        "COLUMN api.raw_chunks.source_id",
        "Vendor-neutral source id of the mirror \u2014 unique only paired with collection. Example value: 'ABCD1234'.",
    ),
    ("COLUMN api.raw_chunks.section_path", "ltree path as text. Example value: 's4.s1'."),
    ("COLUMN api.raw_chunks.heading", "The numbered heading of the chunk. Example value: '4.1 Experiments'."),
    ("COLUMN api.raw_chunks.body", "Raw verbatim source text of the chunk."),
)


def upgrade() -> None:
    """Serve `seq` as the view's last column and describe chunks cut at headings and to the chunk sizes."""
    op.execute(_VIEW)
    _write_comments(_COMMENTS)


def downgrade() -> None:
    """Put the view, every comment and the reader's grant back as they stood at 0067."""
    op.execute("DROP VIEW api.raw_chunks")
    op.execute(_PREVIOUS_VIEW)
    _write_comments(_PREVIOUS_COMMENTS)
    op.execute(f"GRANT SELECT ON TABLE api.raw_chunks TO {_READER_ROLE}")


def _write_comments(comments: tuple[tuple[str, str], ...]) -> None:
    """Write each comment on its target."""
    for target, text in comments:
        op.execute(f"COMMENT ON {target} IS {_literal(text)}")


def _literal(text: str) -> str:
    """`text` as a single-quoted SQL string literal."""
    return "'" + text.replace("'", "''") + "'"
