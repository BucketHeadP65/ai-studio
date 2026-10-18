"""api.authoring_rules: the L0 orientation read stops carrying somebody else's contract.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-31 00:00:00.000000

``api.about`` is the L0 read a query agent makes once per session. Its body used to be
``_meta/schema`` + ``_meta/purpose`` + ``_meta/retrieval-policy`` concatenated, so that one read also
delivered the authoring and ingestion contract — most of an orientation budget spent on rules for a
job the querying agent does not do. This migration splits the two audiences into two surfaces:

* ``api.about`` -- unchanged definition, narrowed meaning: orientation for whoever is QUERYING.
* ``api.authoring_rules`` -- the contract for whoever WRITES pages.

The split is driven by data, not prose parsing: each ``_meta/`` orientation document declares
``audience: query | authoring | both`` in its own frontmatter and ``handlers.read_orientation``
routes by it, emitting one synthetic page per audience.

**Backwards compatible by default.** An orientation document that declares no audience is treated as
``query``, so a vault compiled before this returns byte-identical ``api.about`` rows and the same page
count; ``api.authoring_rules`` is empty until a vault marks its authoring contract.
"""

from __future__ import annotations

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Create ``api.authoring_rules`` and re-comment ``api.about`` as the query-facing surface."""
    op.execute(
        """
        CREATE VIEW api.authoring_rules AS
        SELECT card_text AS body FROM wiki.pages WHERE id = 'meta.authoring'
        """
    )
    for statement in _COMMENT_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Drop the new view and restore ``api.about``'s original comment."""
    op.execute("DROP VIEW IF EXISTS api.authoring_rules")
    op.execute(
        "COMMENT ON VIEW api.about IS 'L0 orientation — one row of guidance text generated from the vault''s "
        "schema/purpose/retrieval-policy. Read once per session before querying. Example: SELECT body FROM api.about;'"
    )


# --- COMMENTs (the M-Schema additions) ---------------------------------------
# `api.about`'s is rewritten, not just added to: an agent reading the M-Schema was being told the row
# contains the vault's schema contract, which is now the other view's job to answer.
_COMMENT_STATEMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.about IS 'L0 orientation for QUERYING this vault — one row: what it covers, what it "
    "excludes, and its retrieval policy (how to route a question). Read once per session before querying. Does NOT "
    "contain the authoring/ingestion contract — that is api.authoring_rules. Example: SELECT body FROM api.about;'",
    "COMMENT ON COLUMN api.about.body IS 'The orientation prose (markdown). Example value: ''This wiki indexes "
    "RAG/retrieval literature; start at api.cards, then api.search…''.'",
    "COMMENT ON VIEW api.authoring_rules IS 'The contract for WRITING pages in this vault: frontmatter keys, page "
    "types, canonical sections, claims grammar, typed relations, lifecycle, ingest procedure, lint. Read this only "
    "when authoring or ingesting — a querying agent needs api.about instead. Empty when the vault declares no "
    "authoring document. Example: SELECT body FROM api.authoring_rules;'",
    "COMMENT ON COLUMN api.authoring_rules.body IS 'The authoring contract (markdown). Example value: ''# Wiki schema "
    "— the contract … ## Frontmatter contract''.'",
)
