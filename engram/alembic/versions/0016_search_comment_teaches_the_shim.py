# noqa: INP001 — alembic version files are scripts, not a package
"""``api.search``'s comment teaches the door's text shim, not hand-built vectors.

The comment told agents to pass ``query_vec (a 1024-dim halfvec embedding)`` — a vector no agent
can produce by hand, and guidance that contradicted the ``run_sql`` tool description, which
teaches ``vector_query => '<text>'`` (the door embeds the text server-side and rewrites it into
``query_vec``). The comment now teaches the shim first and names the direct-caller form second,
and says what ``k`` is. ``api.graph_expand``'s comment (shared constant in
``engram.retrieval.graph_backend.backend``) gets the same ``k`` clarification, re-issued here.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

from alembic import op

from engram.retrieval.graph_backend.backend import EXPAND_COMMENT

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_SEARCH_COMMENT = (
    "COMMENT ON FUNCTION api.search(text, int, halfvec, text, text, text, text, text, boolean) IS "
    "'Hybrid search collapsed across levels (RRF of BM25 + an optional semantic leg). Through the "
    "corpus doors (run_sql), add vector_query => ''<text>'' to the call and the door embeds that "
    "text server-side into query_vec for you — never build a vector by hand; omit it for BM25-only "
    "lexical search. (Only a direct caller outside the doors passes query_vec, a halfvec query "
    "embedding, or NULL.) k caps how many hits come back (default 12, max 50). The ''level'' arg "
    "filters to one tier (card|section|claim|raw); facet args (task/stage/domain) pre-filter BEFORE "
    "ranking; ''collection'' routes the search to one corpus (default ''research''). Returns id, "
    "hit_level (card|section|claim|raw), page_id, page_title, snippet, score. Example: SELECT * "
    "FROM api.search(''graph rag token cost'', k => 8, vector_query => ''graph-structured retrieval "
    "token cost'');'"
)

# The text 0010 left live, restored on downgrade.
_PREVIOUS_SEARCH_COMMENT = (
    "COMMENT ON FUNCTION api.search(text, int, halfvec, text, text, text, text, text, boolean) IS "
    "'Hybrid search collapsed across levels (RRF of BM25 + optional ANN). Pass query_vec (a "
    "1024-dim halfvec embedding of the query) for full hybrid; omit it (NULL) for BM25-only "
    "lexical search. k is hard-capped at 50. The ''level'' arg filters to one tier "
    "(card|section|claim|raw); facet args (task/stage/domain) pre-filter BEFORE ranking. Returns "
    "id, hit_level (card|section|claim|raw), page_id, page_title, snippet, score. Example: SELECT "
    "* FROM api.search(''graph rag token cost'', k => 8);'"
)

_PREVIOUS_EXPAND_COMMENT = (
    "COMMENT ON FUNCTION api.graph_expand(text[], int, text[], int) IS "
    "'Typed-relation neighbourhood expansion: from one or more seed ids, walk the link graph out to N hops "
    "(default 2), optionally filtered to rel_types, returning one row per reached node — its id/type/title/status/"
    "superseded plus the edge that reached it (src_id, dst_id, rel, provenance_claim; NULL on a seed). k is capped "
    "at 50. Example: SELECT * FROM api.graph_expand(ARRAY[''ent.graphrag''], hops => 2);'"
)


def upgrade() -> None:
    """Re-issue the two function comments with door-first, k-explaining wording."""
    op.execute(_SEARCH_COMMENT)
    op.execute(EXPAND_COMMENT)


def downgrade() -> None:
    """Restore the texts 0010 and the shared constant previously left live."""
    op.execute(_PREVIOUS_SEARCH_COMMENT)
    op.execute(_PREVIOUS_EXPAND_COMMENT)
