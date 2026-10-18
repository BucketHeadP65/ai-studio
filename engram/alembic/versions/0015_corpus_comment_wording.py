# noqa: INP001 — alembic version files are scripts, not a package
"""The M-Schema comments say corpus, not vault or wiki.

The ``COMMENT ON`` texts agents read through ``api.describe_schema`` still carried the retired
vocabulary ("this vault", "wiki page", "per-vault facets") and one factual staleness:
``api.log_session``'s comment named ``wiki.query_log``, a schema 0010 renamed to
``memory.query_log``. Re-issued here at head; the frozen revisions keep their original texts.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_UPGRADED: tuple[str, ...] = (
    "COMMENT ON VIEW api.about IS 'L0 orientation for QUERYING this corpus — one row: what it covers, what it "
    "excludes, and its retrieval policy (how to route a question). Read once per session before querying. Does NOT "
    "contain the authoring/ingestion contract — that is api.authoring_rules. Example: SELECT body FROM api.about;'",
    "COMMENT ON COLUMN api.about.body IS 'The orientation prose (markdown). Example value: ''This corpus indexes "
    "RAG/retrieval literature; start at api.cards, then api.search…''.'",
    "COMMENT ON VIEW api.authoring_rules IS 'The contract for WRITING pages in this corpus: frontmatter keys, page "
    "types, canonical sections, claims grammar, typed relations, lifecycle, ingest procedure, lint. Read this only "
    "when authoring or ingesting — a querying agent needs api.about instead. Empty when the corpus declares no "
    "authoring document. Example: SELECT body FROM api.authoring_rules;'",
    "COMMENT ON COLUMN api.authoring_rules.body IS 'The authoring contract (markdown). Example value: ''# Corpus "
    "schema — the contract … ## Frontmatter contract''.'",
    "COMMENT ON VIEW api.sections IS 'L2 read unit: one row per H2/H3 leaf section of a page, with its container "
    "page named. Example: SELECT page_title, body FROM api.sections WHERE canon = ''limitations'' LIMIT 10;'",
    "COMMENT ON FUNCTION api.log_session(text, text, text[], text[], int, text) IS 'Session telemetry: the only "
    "write the read-only role may perform (SECURITY DEFINER insert into memory.query_log). Example: SELECT "
    "api.log_session(''q?'', ''compare'', ARRAY[''api.search''], ARRAY[''paper.x''], 1200);'",
    "COMMENT ON VIEW api.facets IS 'Facet assignments unpivoted: one row per (page, facet, value) across the five "
    "array slots AND every scalar/array key of the frontmatter long tail (slotless per-corpus facets like priority, "
    "official ids like us_id, doc_type, …). Example: SELECT page_id FROM api.facets WHERE facet = ''priority'' AND "
    "value = ''mvp'';'",
)

# The exact texts the frozen revisions (0002, 0007, 0008) left live, restored on downgrade.
_RESTORED: tuple[str, ...] = (
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
    "COMMENT ON VIEW api.sections IS 'L2 read unit: one row per H2/H3 leaf section of a wiki page, with its container "
    "page named. Example: SELECT page_title, body FROM api.sections WHERE canon = ''limitations'' LIMIT 10;'",
    "COMMENT ON FUNCTION api.log_session(text, text, text[], text[], int, text) IS 'Session telemetry: the only "
    "write the read-only role may perform (SECURITY DEFINER insert into wiki.query_log). Example: SELECT "
    "api.log_session(''q?'', ''compare'', ARRAY[''api.search''], ARRAY[''paper.x''], 1200);'",
    "COMMENT ON VIEW api.facets IS 'Facet assignments unpivoted: one row per (page, facet, value) across the five "
    "array slots AND every scalar/array key of the frontmatter long tail (slotless per-vault facets like priority, "
    "official ids like us_id, doc_type, …). Example: SELECT page_id FROM api.facets WHERE facet = ''priority'' AND "
    "value = ''mvp'';'",
)


def upgrade() -> None:
    """Re-issue the stale comments with corpus/memory wording."""
    for statement in _UPGRADED:
        op.execute(statement)


def downgrade() -> None:
    """Restore the comment texts the frozen revisions left live."""
    for statement in _RESTORED:
        op.execute(statement)
