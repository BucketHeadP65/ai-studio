"""The comment on `api.search` says what the owner tunes and the two rules a search applies after ranking.

Revision ID: 0063
Revises: 0062
Create Date: 2026-10-11 00:00:00.000000

Since 0059 the k ceiling and the snippet length come from `kb.retrieval_tuning`, and since 0061 a search pins a lone
exact title and keeps at most `rows_per_group` hits of one page or source. The comment an agent reads still said k stops
at 50 and a snippet at 480 characters, and named neither rule. It now gives both numbers as the owner's, with their
defaults and bounds, and states both rules. Only the comment changes. Downgrade puts 0061's comment back.
"""

from __future__ import annotations

from alembic import op

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_SEARCH_SIGNATURE = "api.search(text, integer, halfvec, text, jsonb, text, boolean, date, date, text, boolean)"

_COMMENT_TEXT = (
    'Hybrid search collapsed across levels (RRF of BM25 + an optional semantic leg). Through the knowledge base doors '
    "(run_sql), add vector_query => '<text>' to the call and the door embeds that text server-side into query_vec for "
    'you. Never build a vector by hand. Omit it for BM25-only lexical search. (Only a direct caller outside the doors '
    'passes query_vec, a halfvec query embedding, or NULL, and query_model, the embedding model that made it. With '
    'query_model set, a knowledge base indexed under another embedding model gets no semantic leg, because its '
    "vectors are not comparable.) k caps how many hits come back (default 12, at most the owner's k ceiling, 50 "
    "unless the owner sets it lower). The 'level' arg filters to one tier (card|section|claim|raw). 'facets' is one "
    'jsonb bag of facet name to value-list narrowing (e.g. \'{"<facet>": ["<value>"]}\'), matched against each page\'s '
    "own declared facets (values for one key OR'd, several keys AND'd), applied BEFORE ranking. 'collection' routes "
    "the search to one knowledge base, defaulting to the reader session's own collection (the app.collection setting) "
    'rather than any fixed one. Pass collection => NULL explicitly to rank over every collection visible to the '
    "session. 'only_current' defaults to true, which ranks only over live pages and active claims. Pass false to rank "
    "over superseded pages, stubs and retired claims too. 'since' and 'until' are dates, both inclusive and both NULL "
    'by default: either one keeps only pages whose created date falls in the range, applied BEFORE ranking, so a page '
    "with no created date and every raw chunk drop out once either is set. 'include_meta' defaults to true. Pass "
    "false to leave out the knowledge base's own meta.* pages (orientation, policy and schema pages) before k is "
    'applied. Returns id, src_collection, hit_level (card|section|claim|raw), page_id, page_title, snippet, score, '
    'lexical_rank, vector_rank, vector_distance, snippet_truncated. src_collection is the knowledge base the hit '
    'belongs to. An id is unique only within its knowledge base, so join a hit to api.cards on src_collection and id '
    "together. snippet is an excerpt for judging whether to fetch, cut at the owner's snippet length (480 characters "
    'unless the owner sets another, between 80 and 2,000) with a trailing ellipsis when the matched text is longer, '
    'and snippet_truncated says whether it was cut. Fetch the id for the whole unit. After ranking, a card whose '
    'title equals the query, ignoring case and surrounding spaces, comes first when no other card holds that title, '
    'and each page (its card, sections and claims together) or source keeps at most its best few hits (3 unless the '
    "owner sets another), the pinned card outside that count. lexical_rank and vector_rank are each leg's own rank "
    "for the hit and vector_distance the semantic leg's raw distance. A NULL rank means that leg did not select this "
    'row, so the pair says which components support a hit rather than how good it is. Example: SELECT * FROM '
    "api.search('<query>', k => 8, since => '2026-09-01');"
)

# 0061's comment, byte-identical: what `downgrade` restores.
_PREVIOUS_COMMENT_TEXT = (
    'Hybrid search collapsed across levels (RRF of BM25 + an optional semantic leg). Through the knowledge base doors '
    "(run_sql), add vector_query => '<text>' to the call and the door embeds that text server-side into query_vec for "
    'you. Never build a vector by hand. Omit it for BM25-only lexical search. (Only a direct caller outside the doors '
    'passes query_vec, a halfvec query embedding, or NULL, and query_model, the embedding model that made it. With '
    'query_model set, a knowledge base indexed under another embedding model gets no semantic leg, because its '
    "vectors are not comparable.) k caps how many hits come back (default 12, max 50). The 'level' arg filters to one "
    "tier (card|section|claim|raw). 'facets' is one jsonb bag of facet name to value-list narrowing (e.g. "
    '\'{"<facet>": ["<value>"]}\'), matched against each page\'s own declared facets (values for one key OR\'d, several '
    "keys AND'd), applied BEFORE ranking. 'collection' routes the search to one knowledge base, defaulting to the "
    "reader session's own collection (the app.collection setting) rather than any fixed one. Pass collection => NULL "
    "explicitly to rank over every collection visible to the session. 'only_current' defaults to true, which ranks "
    'only over live pages and active claims. Pass false to rank over superseded pages, stubs and retired claims too. '
    "'since' and 'until' are dates, both inclusive and both NULL by default: either one keeps only pages whose "
    'created date falls in the range, applied BEFORE ranking, so a page with no created date and every raw chunk drop '
    "out once either is set. 'include_meta' defaults to true. Pass false to leave out the knowledge base's own meta.* "
    'pages (orientation, policy and schema pages) before k is applied. Returns id, src_collection, hit_level '
    '(card|section|claim|raw), page_id, page_title, snippet, score, lexical_rank, vector_rank, vector_distance, '
    'snippet_truncated. src_collection is the knowledge base the hit belongs to. An id is unique only within its '
    'knowledge base, so join a hit to api.cards on src_collection and id together. snippet is an excerpt for judging '
    'whether to fetch, cut at 480 characters with a trailing ellipsis when the matched text is longer, and '
    'snippet_truncated says whether it was cut. Fetch the id for the whole unit. lexical_rank and vector_rank are '
    "each leg's own rank for the hit and vector_distance the semantic leg's raw distance. A NULL rank means that leg "
    'did not select this row, so the pair says which components support a hit rather than how good it is. Example: '
    "SELECT * FROM api.search('<query>', k => 8, since => '2026-09-01');"
)


def _set_comment(text: str) -> None:
    """Set `text` as the comment on `api.search`."""
    op.execute(f"COMMENT ON FUNCTION {_SEARCH_SIGNATURE} IS '{text.replace("'", "''")}'")


def upgrade() -> None:
    """Say the owner's numbers and the two ranking rules in the comment on `api.search`."""
    _set_comment(_COMMENT_TEXT)


def downgrade() -> None:
    """Put 0061's comment back."""
    _set_comment(_PREVIOUS_COMMENT_TEXT)
