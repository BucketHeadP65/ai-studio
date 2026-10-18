"""Spine maturation: the seven research scalars and five facet slots demote to declarations.

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-20 00:00:00.000000

The storage spine stops speaking the research profile's vocabulary. Three demotions, one
release:

* `pages` sheds seven typed scalars (`entity_type`, `paper_type`, `year`, `venue`, `citekey`,
  `maturity`, `question_status`) into `frontmatter` jsonb, and the five fixed facet arrays
  (`tasks`, `domains`, `stages`, `modalities`, `scale_regimes`) into one `facets` jsonb map
  (facet name -> value array), GIN-indexed. `source_hash` stays: mirror-freshness invalidation
  is spine machinery, not profile vocabulary.
* `sources_raw` goes vendor-neutral: `source_id` (engram's own identity) replaces `zotero_key`
  as the primary key, `provider` + `provider_key` name the adapter and its native key, and
  `doi`/`arxiv_id`/`venue`/`year` fold into `meta` jsonb. `raw_chunks` re-points its FK.
* `claims` sheds its three typed, lossy (element-zero-only) qualifier columns (`on_dataset`,
  `on_task`, `metric`) for the new `memory.claim_qualifiers` table, one row per qualifier
  target — a multilink qualifier now keeps every resolved target, not just the first.

Both `pages.collection` and `sources_raw.collection` lose their `'research'` server default:
a row arriving without a collection is now an error, not a silent file into the founding corpus.

Four phases, each transactional:

1. Add every new column nullable (no sensible universal default for `sources_raw.source_id`/
   `provider`/`provider_key`), and create `claim_qualifiers` (a brand-new table needs no
   nullable-first phase — its own rows never predate it).
2. Fold: one `UPDATE` per demoted table, one `INSERT` populating `claim_qualifiers` from the
   three typed columns (`ord = 0`, the stored element is all today's grammar ever kept).
3. Finalize: `NOT NULL` where the target shape wants it, `sources_raw`'s primary key swaps from
   `zotero_key` to `source_id`, `raw_chunks` re-points, the folded columns and both collection
   defaults drop, the BM25 indexes naming a dropped/renamed column are rebuilt, and the new
   `pages_facets` GIN index lands.
4. Re-create every `api` function and view whose body read a column this revision just moved or
   renamed: `api.search` (a fresh `facets jsonb` parameter replaces `task`/`stage`/`domain`),
   `api.lookup_value` (counts through `pages.facets` instead of the five slot arrays), and the
   views `cards`, `papers`, `entities`, `coverage`, `scoreboard`, `claims`, `facets`, `stale`,
   `raw_chunks` (its `zotero_key` column would otherwise keep that stale display name forever —
   a view's own output column name survives a rename of the column backing it).

Downgrade reverses all four phases: it reconstructs the dropped columns from their jsonb homes
and `claim_qualifiers` (`ord = 0` targets only — honest and lossless for exactly what the old
shape could hold), restores the old primary key and both collection defaults, and recreates the
pre-maturation function/view bodies.
"""

from __future__ import annotations

from alembic import context, op
from core.ai.retrieval.contracts import SearchSchema, Facet, FacetPool, ParadeDBHybridSearchBuilder, PgTextSearchHybridSearchBuilder, SearchSurface

from engram.retrieval.search_dialect import SearchDialect

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "corpus_reader"

_OLD_SEARCH_SIGNATURE = "api.search(text, int, halfvec, text, text, text, text, text, boolean)"
_NEW_SEARCH_SIGNATURE = "api.search(text, int, halfvec, text, jsonb, text, boolean)"

# The nine views this revision drops before the column drops/rename (dependency order irrelevant
# — none of them reads another), and recreates in `_recreate_api_surface_new` / `_recreate_api_surface_old`.
_TOUCHED_VIEWS: tuple[str, ...] = ("cards", "papers", "entities", "coverage", "scoreboard", "claims", "facets", "stale", "raw_chunks")

_NEW_SEARCH_COMMENT = (
    "COMMENT ON FUNCTION api.search(text, int, halfvec, text, jsonb, text, boolean) IS "
    "'Hybrid search collapsed across levels (RRF of BM25 + an optional semantic leg). Through the "
    "corpus doors (run_sql), add vector_query => ''<text>'' to the call and the door embeds that "
    "text server-side into query_vec for you — never build a vector by hand; omit it for BM25-only "
    "lexical search. (Only a direct caller outside the doors passes query_vec, a halfvec query "
    "embedding, or NULL.) k caps how many hits come back (default 12, max 50). The ''level'' arg "
    "filters to one tier (card|section|claim|raw); ''facets'' is one jsonb bag of facet name to "
    "value-list narrowing (e.g. ''{\"tasks\": [\"retrieval\"]}''), matched against each page''s own "
    "declared facets — values for one key OR''d, several keys AND''d — applied BEFORE ranking; "
    "''collection'' routes the search to one corpus (default ''research''). Returns id, hit_level "
    "(card|section|claim|raw), page_id, page_title, snippet, score. Example: SELECT * FROM "
    "api.search(''graph rag token cost'', k => 8, facets => ''{\"tasks\": [\"retrieval\"]}'');'"
)

# The text 0016 left live, restored on downgrade.
_OLD_SEARCH_COMMENT = (
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


def _dialect() -> SearchDialect:
    """Return the BM25 :class:`SearchDialect` env.py stashed on ``config.attributes`` (mirrors 0001/0002/0010)."""
    dialect = context.config.attributes.get("search_dialect")
    if dialect is None:
        msg = "search_dialect not configured: alembic/env.py must stash it in config.attributes"
        raise RuntimeError(msg)
    if not isinstance(dialect, SearchDialect):
        msg = f"search_dialect must be a SearchDialect, got {type(dialect).__name__}"
        raise TypeError(msg)
    return dialect


def _new_search_schema() -> SearchSchema:
    """The post-maturation hybrid-search shape: one jsonb `facets` parameter, `source_id`-keyed raw pool.

    Migration-local rather than ``engram.retrieval.corpus_schema.MEMORY_CORPUS_SCHEMA`` (the
    library's current descriptor): a downstream revision changing that constant for an unrelated
    reason must never silently drift this migration's `CREATE FUNCTION` out of step with its own
    hardcoded `COMMENT`. Byte-identical to `corpus_schema.py`'s shape as of this revision otherwise.
    """
    return SearchSchema(
        surfaces=(
            SearchSurface(
                name="card",
                table="memory.pages",
                alias="p",
                key_col="id",
                text_col="card_text",
                embedding_col="embedding",
                hit_level="card",
                parent_id_expr="p.id",
                title_expr="p.title",
                bm25_index="pages_bm25",
                membership="p.id IN (SELECT page_pool.id FROM page_pool)",
            ),
            SearchSurface(
                name="section",
                table="memory.sections",
                alias="s",
                key_col="id",
                text_col="body",
                embedding_col="embedding",
                hit_level="section",
                parent_id_expr="s.page_id",
                title_expr="pp.title",
                bm25_index="sections_bm25",
                join="JOIN page_pool pp ON pp.id = s.page_id",
            ),
            SearchSurface(
                name="claim",
                table="memory.claims",
                alias="c",
                key_col="id",
                text_col="text",
                embedding_col="embedding",
                hit_level="claim",
                parent_id_expr="c.page_id",
                title_expr="pp.title",
                bm25_index="claims_bm25",
                join="JOIN page_pool pp ON pp.id = c.page_id",
                filters=("(NOT {only_current} OR c.status = 'active')",),
            ),
            SearchSurface(
                name="raw",
                table="memory.raw_chunks",
                alias="r",
                key_col="id",
                text_col="body",
                embedding_col="embedding",
                hit_level="raw",
                parent_id_expr="NULL::text",
                title_expr="rk.title",
                bm25_index="raw_chunks_bm25",
                join="JOIN raw_keys rk ON rk.source_id = r.source_id",
            ),
        ),
        pools=(
            FacetPool(
                name="page_pool",
                table="memory.pages",
                alias="p",
                select_cols=("p.id", "p.title", "p.card_text", "p.embedding"),
                predicates=(
                    "({collection} IS NULL OR p.collection = {collection})",
                    "({facets} = '{}'::jsonb OR NOT EXISTS (\n"
                    "  SELECT 1 FROM jsonb_each({facets}) AS f(key, vals)\n"
                    "  WHERE (p.facets -> f.key ?| ARRAY(SELECT jsonb_array_elements_text(f.vals))) IS NOT TRUE))",
                    "(NOT {only_current} OR (p.superseded_by IS NULL AND p.status <> 'stub'))",
                ),
            ),
            FacetPool(
                name="raw_keys",
                table="memory.sources_raw",
                alias="sr",
                select_cols=("sr.source_id", "sr.title"),
                predicates=("({collection} IS NULL OR sr.collection = {collection})",),
            ),
        ),
        facets=(
            Facet(name="level", sql_type="text"),
            Facet(name="facets", sql_type="jsonb", default="'{}'::jsonb"),
            Facet(name="collection", sql_type="text", default="'research'"),
            Facet(name="only_current", sql_type="boolean", default="true"),
        ),
        level_facet="level",
        embedding_sql_type="halfvec(1024)",
    )


def _old_search_schema() -> SearchSchema:
    """The pre-maturation hybrid-search shape, byte-identical to 0010's own frozen `_search_schema("memory")`."""
    return SearchSchema(
        surfaces=(
            SearchSurface(
                name="card",
                table="memory.pages",
                alias="p",
                key_col="id",
                text_col="card_text",
                embedding_col="embedding",
                hit_level="card",
                parent_id_expr="p.id",
                title_expr="p.title",
                bm25_index="pages_bm25",
                membership="p.id IN (SELECT page_pool.id FROM page_pool)",
            ),
            SearchSurface(
                name="section",
                table="memory.sections",
                alias="s",
                key_col="id",
                text_col="body",
                embedding_col="embedding",
                hit_level="section",
                parent_id_expr="s.page_id",
                title_expr="pp.title",
                bm25_index="sections_bm25",
                join="JOIN page_pool pp ON pp.id = s.page_id",
            ),
            SearchSurface(
                name="claim",
                table="memory.claims",
                alias="c",
                key_col="id",
                text_col="text",
                embedding_col="embedding",
                hit_level="claim",
                parent_id_expr="c.page_id",
                title_expr="pp.title",
                bm25_index="claims_bm25",
                join="JOIN page_pool pp ON pp.id = c.page_id",
                filters=("(NOT {only_current} OR c.status = 'active')",),
            ),
            SearchSurface(
                name="raw",
                table="memory.raw_chunks",
                alias="r",
                key_col="id",
                text_col="body",
                embedding_col="embedding",
                hit_level="raw",
                parent_id_expr="NULL::text",
                title_expr="rk.title",
                bm25_index="raw_chunks_bm25",
                join="JOIN raw_keys rk ON rk.zotero_key = r.zotero_key",
            ),
        ),
        pools=(
            FacetPool(
                name="page_pool",
                table="memory.pages",
                alias="p",
                select_cols=("p.id", "p.title", "p.card_text", "p.embedding"),
                predicates=(
                    "({collection} IS NULL OR p.collection = {collection})",
                    "({task} IS NULL OR {task} = ANY(p.tasks))",
                    "({stage} IS NULL OR {stage} = ANY(p.stages))",
                    "({domain} IS NULL OR {domain} = ANY(p.domains))",
                    "(NOT {only_current} OR (p.superseded_by IS NULL AND p.status <> 'stub'))",
                ),
            ),
            FacetPool(
                name="raw_keys",
                table="memory.sources_raw",
                alias="sr",
                select_cols=("sr.zotero_key", "sr.title"),
                predicates=(
                    "({collection} IS NULL OR sr.collection = {collection})",
                    "{task} IS NULL",
                    "{stage} IS NULL",
                    "{domain} IS NULL",
                ),
            ),
        ),
        facets=(
            Facet(name="level", sql_type="text"),
            Facet(name="task", sql_type="text"),
            Facet(name="stage", sql_type="text"),
            Facet(name="domain", sql_type="text"),
            Facet(name="collection", sql_type="text", default="'research'"),
            Facet(name="only_current", sql_type="boolean", default="true"),
        ),
        level_facet="level",
        embedding_sql_type="halfvec(1024)",
    )


def _search_body(schema: SearchSchema) -> str:
    """The full ``CREATE FUNCTION api.search(...)`` statement for `schema`, selected by the configured dialect."""
    dialect_name = _dialect().name
    builder: ParadeDBHybridSearchBuilder | PgTextSearchHybridSearchBuilder
    if dialect_name == "pg_search":
        builder = ParadeDBHybridSearchBuilder()
    elif dialect_name == "pg_textsearch":
        builder = PgTextSearchHybridSearchBuilder()
    else:
        msg = f"search_dialect not configured (or unrecognised name {dialect_name!r}): alembic/env.py must stash a SearchDialect"
        raise RuntimeError(msg)
    return builder.api_search_body(schema)


def _guarded_year_cast(alias: str) -> str:
    """A `frontmatter->>'year'` read, cast to `int`, NULL instead of raising on a non-numeric value.

    The typed `pages.year` column this replaces enforced `int` at write time; the jsonb home does
    not — an authored `year: FY24` frontmatter value lands in `frontmatter` verbatim and would
    otherwise crash every view below at query time on that one row. Every view reading `year` back
    out of `frontmatter` guards the cast the same way.
    """
    return f"CASE WHEN {alias}.frontmatter->>'year' ~ '^-?[0-9]+$' THEN ({alias}.frontmatter->>'year')::int END"


_NEW_LOOKUP_VALUE_BODY = """
CREATE OR REPLACE FUNCTION api.lookup_value(facet text, term text DEFAULT NULL)
  RETURNS TABLE (value text, definition text, n_pages int)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT v.value, v.definition,
           (SELECT count(*)::int FROM memory.pages p
            WHERE (v.vocab = 'entity_type' AND p.frontmatter ->> 'entity_type' = v.value)
               OR (v.vocab LIKE 'facet:%' AND (p.facets -> split_part(v.vocab, ':', 2)) ? v.value)) AS n_pages
    FROM memory.vocab v
    WHERE (v.vocab = facet OR v.vocab = 'facet:' || facet)
      AND (term IS NULL OR v.value ILIKE '%' || term || '%')
    ORDER BY v.value
$$
"""

_OLD_LOOKUP_VALUE_BODY = """
CREATE OR REPLACE FUNCTION api.lookup_value(facet text, term text DEFAULT NULL)
  RETURNS TABLE (value text, definition text, n_pages int)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT v.value, v.definition,
           (SELECT count(*)::int FROM memory.pages p
            WHERE v.value = ANY(p.tasks) OR v.value = ANY(p.domains)
               OR v.value = ANY(p.stages) OR v.value = ANY(p.modalities)
               OR v.value = ANY(p.scale_regimes) OR p.entity_type = v.value
               OR (v.vocab LIKE 'facet:%' AND (
                     p.frontmatter ->> split_part(v.vocab, ':', 2) = v.value
                     OR (jsonb_typeof(p.frontmatter -> split_part(v.vocab, ':', 2)) = 'array'
                         AND p.frontmatter -> split_part(v.vocab, ':', 2) ? v.value)))) AS n_pages
    FROM memory.vocab v
    WHERE (v.vocab = facet OR v.vocab = 'facet:' || facet)
      AND (term IS NULL OR v.value ILIKE '%' || term || '%')
    ORDER BY v.value
$$
"""

# -- api.cards ----------------------------------------------------------------

_NEW_CARDS_VIEW = f"""
CREATE VIEW api.cards AS
SELECT
  p.id,
  p.kind                              AS type,
  p.title,
  p.summary,
  p.status,
  (p.superseded_by IS NOT NULL)       AS superseded,
  p.frontmatter->>'entity_type'       AS entity_type,
  p.frontmatter->>'paper_type'        AS paper_type,
  p.frontmatter->>'maturity'          AS maturity,
  {_guarded_year_cast("p")}           AS year,
  COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets->'tasks')), ARRAY[]::text[])   AS tasks,
  COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets->'domains')), ARRAY[]::text[]) AS domains,
  COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets->'stages')), ARRAY[]::text[])  AS stages,
  (SELECT count(*) FROM memory.claims c WHERE c.page_id = p.id)        AS n_claims,
  (SELECT count(*) FROM memory.edges e WHERE e.src_id = p.id)          AS out_degree,
  (SELECT count(*) FROM memory.edges e WHERE e.dst_id = p.id)          AS in_degree,
  p.updated,
  p.frontmatter
FROM memory.pages p
"""

_OLD_CARDS_VIEW = """
CREATE VIEW api.cards AS
SELECT
  p.id,
  p.kind                              AS type,
  p.title,
  p.summary,
  p.status,
  (p.superseded_by IS NOT NULL)       AS superseded,
  p.entity_type,
  p.paper_type,
  p.maturity,
  p.year,
  p.tasks,
  p.domains,
  p.stages,
  (SELECT count(*) FROM memory.claims c WHERE c.page_id = p.id)        AS n_claims,
  (SELECT count(*) FROM memory.edges e WHERE e.src_id = p.id)          AS out_degree,
  (SELECT count(*) FROM memory.edges e WHERE e.dst_id = p.id)          AS in_degree,
  p.updated,
  p.frontmatter
FROM memory.pages p
"""

# -- api.papers (column rename zotero_key -> source_id: DROP + CREATE, not OR REPLACE) ---------

_NEW_PAPERS_VIEW = f"""
CREATE VIEW api.papers AS
SELECT
  p.id, p.title,
  {_guarded_year_cast("p")} AS year,
  p.frontmatter->>'venue' AS venue,
  p.frontmatter->>'citekey' AS citekey,
  p.frontmatter->>'paper_type' AS paper_type,
  COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets->'tasks')), ARRAY[]::text[])   AS tasks,
  COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets->'domains')), ARRAY[]::text[]) AS domains,
  COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets->'stages')), ARRAY[]::text[])  AS stages,
  p.source_id,
  s.meta->>'doi' AS doi,
  s.meta->>'arxiv_id' AS arxiv_id,
  s.pdf_path,
  (SELECT count(*) FROM memory.claims c WHERE c.page_id = p.id) AS n_claims,
  p.status, p.updated
FROM memory.pages p
LEFT JOIN memory.sources_raw s ON s.source_id = p.source_id
WHERE p.kind = 'paper'
"""

_OLD_PAPERS_VIEW = """
CREATE VIEW api.papers AS
SELECT
  p.id, p.title, p.year, p.venue, p.citekey, p.paper_type,
  p.tasks, p.domains, p.stages,
  p.zotero_key,
  s.doi, s.arxiv_id, s.pdf_path,
  (SELECT count(*) FROM memory.claims c WHERE c.page_id = p.id) AS n_claims,
  p.status, p.updated
FROM memory.pages p
LEFT JOIN memory.sources_raw s ON s.zotero_key = p.zotero_key
WHERE p.kind = 'paper'
"""

_NEW_PAPERS_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.papers IS 'Bibliographic rows for papers: metadata + facets + source_id + artifact URLs + claim counts. Example: SELECT id, title, year, doi FROM api.papers WHERE year >= 2024 ORDER BY year DESC LIMIT 20;'",
    "COMMENT ON COLUMN api.papers.id IS 'Page id. Example value: ''paper.lightrag-2025''.'",
    "COMMENT ON COLUMN api.papers.title IS 'Paper title.'",
    "COMMENT ON COLUMN api.papers.year IS 'Publication year. Example value: 2025.'",
    "COMMENT ON COLUMN api.papers.venue IS 'Publication venue. Example value: ''EMNLP 2025''.'",
    "COMMENT ON COLUMN api.papers.citekey IS 'Citation key. Example value: ''guo2025lightrag''.'",
    "COMMENT ON COLUMN api.papers.paper_type IS 'e.g. ''empirical''|''survey''|''system''.'",
    "COMMENT ON COLUMN api.papers.tasks IS 'text[] task facet.'",
    "COMMENT ON COLUMN api.papers.domains IS 'text[] domain facet.'",
    "COMMENT ON COLUMN api.papers.stages IS 'text[] pipeline-stage facet.'",
    "COMMENT ON COLUMN api.papers.source_id IS 'Raw-mirror source id linking to the source layer. Example value: ''ABCD1234''.'",
    "COMMENT ON COLUMN api.papers.doi IS 'DOI. Example value: ''10.18653/v1/2025.emnlp-main.1''.'",
    "COMMENT ON COLUMN api.papers.arxiv_id IS 'arXiv id. Example value: ''2410.05779''.'",
    "COMMENT ON COLUMN api.papers.pdf_path IS 'Path to the mirrored PDF, if any.'",
    "COMMENT ON COLUMN api.papers.n_claims IS 'int — claim blocks extracted from this paper.'",
    "COMMENT ON COLUMN api.papers.status IS 'Lifecycle: stub|draft|synthesized|verified.'",
    "COMMENT ON COLUMN api.papers.updated IS 'date — last edit of the note.'",
)

_OLD_PAPERS_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.papers IS 'Bibliographic rows for papers: metadata + facets + zotero_key + artifact URLs + claim counts. Example: SELECT id, title, year, doi FROM api.papers WHERE year >= 2024 ORDER BY year DESC LIMIT 20;'",
    "COMMENT ON COLUMN api.papers.id IS 'Page id. Example value: ''paper.lightrag-2025''.'",
    "COMMENT ON COLUMN api.papers.title IS 'Paper title.'",
    "COMMENT ON COLUMN api.papers.year IS 'Publication year. Example value: 2025.'",
    "COMMENT ON COLUMN api.papers.venue IS 'Publication venue. Example value: ''EMNLP 2025''.'",
    "COMMENT ON COLUMN api.papers.citekey IS 'Citation key. Example value: ''guo2025lightrag''.'",
    "COMMENT ON COLUMN api.papers.paper_type IS 'e.g. ''empirical''|''survey''|''system''.'",
    "COMMENT ON COLUMN api.papers.tasks IS 'text[] task facet.'",
    "COMMENT ON COLUMN api.papers.domains IS 'text[] domain facet.'",
    "COMMENT ON COLUMN api.papers.stages IS 'text[] pipeline-stage facet.'",
    "COMMENT ON COLUMN api.papers.zotero_key IS 'Zotero item key linking to the raw mirror. Example value: ''ABCD1234''.'",
    "COMMENT ON COLUMN api.papers.doi IS 'DOI. Example value: ''10.18653/v1/2025.emnlp-main.1''.'",
    "COMMENT ON COLUMN api.papers.arxiv_id IS 'arXiv id. Example value: ''2410.05779''.'",
    "COMMENT ON COLUMN api.papers.pdf_path IS 'Path to the mirrored PDF, if any.'",
    "COMMENT ON COLUMN api.papers.n_claims IS 'int — claim blocks extracted from this paper.'",
    "COMMENT ON COLUMN api.papers.status IS 'Lifecycle: stub|draft|synthesized|verified.'",
    "COMMENT ON COLUMN api.papers.updated IS 'date — last edit of the note.'",
)

# Every touched view is DROP + CREATE (not OR REPLACE), because the finalize phase must drop
# `entity_type`/`tasks`/`source_id`-etc columns these views depend on before the columns can go —
# Postgres refuses `DROP COLUMN` while a view still reads it. DROP + CREATE loses the view's
# comments (unlike `CREATE OR REPLACE`, which preserves them when the column set is unchanged),
# so every touched view gets its comments re-issued below, not only `papers`' (whose column rename
# already forced DROP + CREATE regardless).

_CARDS_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.cards IS 'L1 card scan (~50-150 tokens/row): one compact row per page across all 8 types. Filter by type + facet arrays, exclude stubs/superseded, order by updated. Example: SELECT id, title, summary FROM api.cards WHERE type = ''playbook'' AND ''long-doc-qa'' = ANY(tasks) AND NOT superseded LIMIT 20;'",
    "COMMENT ON COLUMN api.cards.id IS 'Page id (primary key everywhere). Example value: ''paper.lightrag-2025''.'",
    "COMMENT ON COLUMN api.cards.type IS 'Page type: paper|entity|concept|comparison|playbook|question|moc|meta.'",
    "COMMENT ON COLUMN api.cards.title IS 'Human-readable page title.'",
    "COMMENT ON COLUMN api.cards.summary IS 'One-paragraph summary (<= 280 chars), or NULL for stubs.'",
    "COMMENT ON COLUMN api.cards.status IS 'Lifecycle: stub|draft|synthesized|verified.'",
    "COMMENT ON COLUMN api.cards.superseded IS 'boolean — true when a newer page supersedes this one.'",
    "COMMENT ON COLUMN api.cards.entity_type IS 'For entities: the registry entity_type. Example value: ''technique''.'",
    "COMMENT ON COLUMN api.cards.paper_type IS 'For papers: e.g. ''empirical''|''survey''|''system''.'",
    "COMMENT ON COLUMN api.cards.maturity IS 'For playbooks: e.g. ''experimental''|''established''.'",
    "COMMENT ON COLUMN api.cards.year IS 'Publication year (papers). Example value: 2025.'",
    "COMMENT ON COLUMN api.cards.tasks IS 'text[] facet — task tags. Example value: {long-doc-qa,multi-hop-qa}.'",
    "COMMENT ON COLUMN api.cards.domains IS 'text[] facet — domain tags. Example value: {biomedical}.'",
    "COMMENT ON COLUMN api.cards.stages IS 'text[] facet — pipeline stage tags. Example value: {retrieval,indexing}.'",
    "COMMENT ON COLUMN api.cards.n_claims IS 'int — number of claim blocks on this page.'",
    "COMMENT ON COLUMN api.cards.out_degree IS 'int — outgoing typed edges from this page.'",
    "COMMENT ON COLUMN api.cards.in_degree IS 'int — incoming typed edges to this page.'",
    "COMMENT ON COLUMN api.cards.updated IS 'date — last edit date of the source note.'",
    "COMMENT ON COLUMN api.cards.frontmatter IS 'jsonb: the page''s long-tail frontmatter as a document (the same keys api.facets unpivots, plus values deeper than a scalar). Example: SELECT frontmatter->''priority'' FROM api.cards WHERE id = ''ent.lightrag'';'",
)

_ENTITIES_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.entities IS 'Entity cards with graph degree + evidence counts. Example: SELECT id, title, n_evidence FROM api.entities WHERE entity_type = ''technique'' ORDER BY n_evidence DESC LIMIT 20;'",
    "COMMENT ON COLUMN api.entities.id IS 'Entity page id. Example value: ''ent.graphrag''.'",
    "COMMENT ON COLUMN api.entities.title IS 'Entity name.'",
    "COMMENT ON COLUMN api.entities.entity_type IS 'Registry entity_type. Example value: ''technique''.'",
    "COMMENT ON COLUMN api.entities.summary IS 'One-paragraph summary.'",
    "COMMENT ON COLUMN api.entities.status IS 'Lifecycle: stub|draft|synthesized|verified.'",
    "COMMENT ON COLUMN api.entities.tasks IS 'text[] task facet.'",
    "COMMENT ON COLUMN api.entities.domains IS 'text[] domain facet.'",
    "COMMENT ON COLUMN api.entities.stages IS 'text[] pipeline-stage facet.'",
    "COMMENT ON COLUMN api.entities.out_degree IS 'int — outgoing typed edges.'",
    "COMMENT ON COLUMN api.entities.in_degree IS 'int — incoming typed edges.'",
    "COMMENT ON COLUMN api.entities.n_evidence IS 'int — claims whose subject is this entity.'",
    "COMMENT ON COLUMN api.entities.updated IS 'date — last edit of the note.'",
)

_COVERAGE_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.coverage IS 'The computed map: facet value x page type -> counts (what we know about X, and how much). Example: SELECT facet_value, page_type, n_pages FROM api.coverage WHERE facet = ''tasks'' ORDER BY n_pages DESC LIMIT 20;'",
    "COMMENT ON COLUMN api.coverage.facet IS 'Which facet: tasks|stages|domains.'",
    "COMMENT ON COLUMN api.coverage.facet_value IS 'A value of that facet. Example value: ''long-doc-qa''.'",
    "COMMENT ON COLUMN api.coverage.page_type IS 'Page type the count is grouped by.'",
    "COMMENT ON COLUMN api.coverage.n_pages IS 'int — pages with this facet value and type.'",
)

_SCOREBOARD_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.scoreboard IS 'Computed trust per entity: support/refute/win/null-result counts, datasets covered, latest evidence year. Example: SELECT entity, n_supports, n_wins, last_evidence_year FROM api.scoreboard WHERE ''retrieval'' = ANY(stages) ORDER BY n_wins + n_supports DESC LIMIT 15;'",
    "COMMENT ON COLUMN api.scoreboard.entity IS 'Entity page id.'",
    "COMMENT ON COLUMN api.scoreboard.entity_title IS 'Entity name.'",
    "COMMENT ON COLUMN api.scoreboard.stages IS 'text[] pipeline-stage facet of the entity.'",
    "COMMENT ON COLUMN api.scoreboard.n_supports IS 'int — supporting claims.'",
    "COMMENT ON COLUMN api.scoreboard.n_refutes IS 'int — refuting claims.'",
    "COMMENT ON COLUMN api.scoreboard.n_wins IS 'int — active benchmark-result wins.'",
    "COMMENT ON COLUMN api.scoreboard.n_losses IS 'int — ablation/loss claims.'",
    "COMMENT ON COLUMN api.scoreboard.n_null_results IS 'int — null-result claims.'",
    "COMMENT ON COLUMN api.scoreboard.n_papers IS 'int — distinct papers contributing evidence.'",
    "COMMENT ON COLUMN api.scoreboard.datasets_covered IS 'int — distinct datasets in the evidence.'",
    "COMMENT ON COLUMN api.scoreboard.last_evidence_year IS 'int — most recent evidence year. Example value: 2025.'",
)

_CLAIMS_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.claims IS 'Joined, human-readable claims: paper title+year and subject/dataset/task/metric NAMES (never bare ids), plus quote+loc for L4 verification. Example: SELECT paper_title, text, quote, loc FROM api.claims WHERE claim_id = ''paper.lightrag-2025#c1'';'",
    "COMMENT ON COLUMN api.claims.claim_id IS 'Claim id ''<page_id>#cN''. Example value: ''paper.lightrag-2025#c1''.'",
    "COMMENT ON COLUMN api.claims.paper_id IS 'Owning paper page id.'",
    "COMMENT ON COLUMN api.claims.paper_title IS 'Owning paper title.'",
    "COMMENT ON COLUMN api.claims.paper_year IS 'Owning paper year. Example value: 2025.'",
    "COMMENT ON COLUMN api.claims.subject_name IS 'Name of the entity the claim is about. Example value: ''LightRAG''.'",
    "COMMENT ON COLUMN api.claims.dataset_name IS 'Dataset name the result is on. Example value: ''HotpotQA''.'",
    "COMMENT ON COLUMN api.claims.task_name IS 'Task name. Example value: ''multi-hop-qa''.'",
    "COMMENT ON COLUMN api.claims.metric_name IS 'Metric name. Example value: ''F1''.'",
    "COMMENT ON COLUMN api.claims.text IS 'The atomic claim sentence.'",
    "COMMENT ON COLUMN api.claims.quote IS 'Verbatim source snippet supporting the claim (may be NULL).'",
    "COMMENT ON COLUMN api.claims.loc IS 'Raw locator into the source. Example value: ''§5.1, Table 2''.'",
    "COMMENT ON COLUMN api.claims.kind IS 'Claim kind (registry). Example value: ''benchmark-result''.'",
    "COMMENT ON COLUMN api.claims.condition IS 'Qualifying condition. Example value: ''zero-shot''.'",
    "COMMENT ON COLUMN api.claims.magnitude IS 'Effect size text. Example value: ''+4.2 F1''.'",
    "COMMENT ON COLUMN api.claims.eval_setting IS 'Evaluation setting. Example value: ''closed-book''.'",
    "COMMENT ON COLUMN api.claims.status IS 'Claim status: active|disputed|superseded|retracted-source.'",
)

_NEW_FACETS_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.facets IS 'Facet assignments unpivoted: one row per (page, facet, value) across every declared facet in the page''s facets map AND every scalar/array key of the frontmatter long tail (slotless per-corpus fields like priority, official ids like us_id, doc_type, …). Example: SELECT page_id FROM api.facets WHERE facet = ''priority'' AND value = ''mvp'';'",
    "COMMENT ON COLUMN api.facets.page_id IS 'The page carrying this facet value. Example value: ''us.022''.'",
    "COMMENT ON COLUMN api.facets.collection IS 'Corpus partition of the page. Example value: ''field-notes''.'",
    "COMMENT ON COLUMN api.facets.facet IS 'Facet key: a declared facet name (tasks|domains|stages|…) or a frontmatter key. Example value: ''priority''.'",
    "COMMENT ON COLUMN api.facets.value IS 'One assigned value. Example value: ''mvp''.'",
)

_OLD_FACETS_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.facets IS 'Facet assignments unpivoted: one row per (page, facet, value) across the five array slots AND every scalar/array key of the frontmatter long tail (slotless per-vault facets like priority, official ids like us_id, doc_type, …). Example: SELECT page_id FROM api.facets WHERE facet = ''priority'' AND value = ''mvp'';'",
    "COMMENT ON COLUMN api.facets.page_id IS 'The page carrying this facet value. Example value: ''us.022''.'",
    "COMMENT ON COLUMN api.facets.collection IS 'Corpus partition of the page. Example value: ''field-notes''.'",
    "COMMENT ON COLUMN api.facets.facet IS 'Facet key: a slot name (tasks|domains|stages|modalities|scale_regimes) or a frontmatter key. Example value: ''priority''.'",
    "COMMENT ON COLUMN api.facets.value IS 'One assigned value. Example value: ''mvp''.'",
)

_STALE_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.stale IS 'Pages failing freshness rules: the copied source_hash drifted from the mirror''s current hash. Example: SELECT id, title FROM api.stale LIMIT 20;'",
    "COMMENT ON COLUMN api.stale.id IS 'Stale page id.'",
    "COMMENT ON COLUMN api.stale.title IS 'Page title.'",
    "COMMENT ON COLUMN api.stale.status IS 'Lifecycle status.'",
    "COMMENT ON COLUMN api.stale.page_source_hash IS 'source_hash recorded on the page at ingest.'",
    "COMMENT ON COLUMN api.stale.mirror_source_hash IS 'Current source_hash of the mirror — drift means stale.'",
    "COMMENT ON COLUMN api.stale.updated IS 'date — last edit of the note.'",
)

# -- api.entities ---------------------------------------------------------------

_NEW_ENTITIES_VIEW = """
CREATE VIEW api.entities AS
SELECT
  p.id, p.title,
  p.frontmatter->>'entity_type' AS entity_type,
  p.summary, p.status,
  COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets->'tasks')), ARRAY[]::text[])   AS tasks,
  COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets->'domains')), ARRAY[]::text[]) AS domains,
  COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets->'stages')), ARRAY[]::text[])  AS stages,
  (SELECT count(*) FROM memory.edges e WHERE e.src_id = p.id) AS out_degree,
  (SELECT count(*) FROM memory.edges e WHERE e.dst_id = p.id) AS in_degree,
  (SELECT count(*) FROM memory.claims c WHERE c.subject_id = p.id) AS n_evidence,
  p.updated
FROM memory.pages p
WHERE p.kind = 'entity'
"""

_OLD_ENTITIES_VIEW = """
CREATE VIEW api.entities AS
SELECT
  p.id, p.title, p.entity_type, p.summary, p.status,
  p.tasks, p.domains, p.stages,
  (SELECT count(*) FROM memory.edges e WHERE e.src_id = p.id) AS out_degree,
  (SELECT count(*) FROM memory.edges e WHERE e.dst_id = p.id) AS in_degree,
  (SELECT count(*) FROM memory.claims c WHERE c.subject_id = p.id) AS n_evidence,
  p.updated
FROM memory.pages p
WHERE p.kind = 'entity'
"""

# -- api.coverage -----------------------------------------------------------------

_NEW_COVERAGE_VIEW = """
CREATE VIEW api.coverage AS
SELECT
  'tasks'    AS facet,
  t.value    AS facet_value,
  p.kind     AS page_type,
  count(*)   AS n_pages
FROM memory.pages p, jsonb_array_elements_text(p.facets->'tasks') AS t(value)
GROUP BY t.value, p.kind
UNION ALL
SELECT 'stages', s.value, p.kind, count(*)
FROM memory.pages p, jsonb_array_elements_text(p.facets->'stages') AS s(value)
GROUP BY s.value, p.kind
UNION ALL
SELECT 'domains', d.value, p.kind, count(*)
FROM memory.pages p, jsonb_array_elements_text(p.facets->'domains') AS d(value)
GROUP BY d.value, p.kind
"""

_OLD_COVERAGE_VIEW = """
CREATE VIEW api.coverage AS
SELECT
  'tasks'    AS facet,
  t.value    AS facet_value,
  p.kind     AS page_type,
  count(*)   AS n_pages
FROM memory.pages p, unnest(p.tasks) AS t(value)
GROUP BY t.value, p.kind
UNION ALL
SELECT 'stages', s.value, p.kind, count(*)
FROM memory.pages p, unnest(p.stages) AS s(value)
GROUP BY s.value, p.kind
UNION ALL
SELECT 'domains', d.value, p.kind, count(*)
FROM memory.pages p, unnest(p.domains) AS d(value)
GROUP BY d.value, p.kind
"""

# -- api.scoreboard ---------------------------------------------------------------

_NEW_SCOREBOARD_VIEW = f"""
CREATE VIEW api.scoreboard AS
SELECT
  p.id     AS entity,
  p.title  AS entity_title,
  COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets->'stages')), ARRAY[]::text[]) AS stages,
  count(*) FILTER (WHERE c.kind = 'support')      AS n_supports,
  count(*) FILTER (WHERE c.kind = 'refute')       AS n_refutes,
  count(*) FILTER (WHERE c.kind = 'benchmark-result' AND c.status = 'active') AS n_wins,
  count(*) FILTER (WHERE c.kind = 'ablation')     AS n_losses,
  count(*) FILTER (WHERE c.kind = 'null-result')  AS n_null_results,
  count(DISTINCT c.page_id)                       AS n_papers,
  count(DISTINCT ds.target_id) FILTER (WHERE ds.target_id IS NOT NULL) AS datasets_covered,
  max({_guarded_year_cast("pp")})                 AS last_evidence_year
FROM memory.pages p
LEFT JOIN memory.claims c ON c.subject_id = p.id
LEFT JOIN memory.pages  pp ON pp.id = c.page_id
LEFT JOIN memory.claim_qualifiers ds ON ds.claim_id = c.id AND ds.qualifier = 'on_dataset'
WHERE p.kind = 'entity'
GROUP BY p.id, p.title
"""

_OLD_SCOREBOARD_VIEW = """
CREATE VIEW api.scoreboard AS
SELECT
  p.id     AS entity,
  p.title  AS entity_title,
  p.stages,
  count(*) FILTER (WHERE c.kind = 'support')      AS n_supports,
  count(*) FILTER (WHERE c.kind = 'refute')       AS n_refutes,
  count(*) FILTER (WHERE c.kind = 'benchmark-result' AND c.status = 'active') AS n_wins,
  count(*) FILTER (WHERE c.kind = 'ablation')     AS n_losses,
  count(*) FILTER (WHERE c.kind = 'null-result')  AS n_null_results,
  count(DISTINCT c.page_id)                       AS n_papers,
  count(DISTINCT c.on_dataset) FILTER (WHERE c.on_dataset IS NOT NULL) AS datasets_covered,
  max(pp.year)                                    AS last_evidence_year
FROM memory.pages p
LEFT JOIN memory.claims c ON c.subject_id = p.id
LEFT JOIN memory.pages  pp ON pp.id = c.page_id
WHERE p.kind = 'entity'
GROUP BY p.id, p.title, p.stages
"""

# -- api.claims ---------------------------------------------------------------

_NEW_CLAIMS_VIEW = f"""
CREATE VIEW api.claims AS
SELECT
  c.id           AS claim_id,
  c.page_id      AS paper_id,
  p.title        AS paper_title,
  {_guarded_year_cast("p")} AS paper_year,
  subj.title     AS subject_name,
  ds.title       AS dataset_name,
  tk.title       AS task_name,
  mt.title       AS metric_name,
  c.text,
  c.quote,
  c.loc,
  c.kind,
  c.condition,
  c.magnitude,
  c.eval_setting,
  c.status
FROM memory.claims c
JOIN memory.pages p ON p.id = c.page_id
LEFT JOIN memory.pages subj ON subj.id = c.subject_id
LEFT JOIN memory.claim_qualifiers qds ON qds.claim_id = c.id AND qds.qualifier = 'on_dataset' AND qds.ord = 0
LEFT JOIN memory.pages ds   ON ds.id   = qds.target_id
LEFT JOIN memory.claim_qualifiers qtk ON qtk.claim_id = c.id AND qtk.qualifier = 'on_task' AND qtk.ord = 0
LEFT JOIN memory.pages tk   ON tk.id   = qtk.target_id
LEFT JOIN memory.claim_qualifiers qmt ON qmt.claim_id = c.id AND qmt.qualifier = 'metric' AND qmt.ord = 0
LEFT JOIN memory.pages mt   ON mt.id   = qmt.target_id
"""

_OLD_CLAIMS_VIEW = """
CREATE VIEW api.claims AS
SELECT
  c.id           AS claim_id,
  c.page_id      AS paper_id,
  p.title        AS paper_title,
  p.year         AS paper_year,
  subj.title     AS subject_name,
  ds.title       AS dataset_name,
  tk.title       AS task_name,
  mt.title       AS metric_name,
  c.text,
  c.quote,
  c.loc,
  c.kind,
  c.condition,
  c.magnitude,
  c.eval_setting,
  c.status
FROM memory.claims c
JOIN memory.pages p ON p.id = c.page_id
LEFT JOIN memory.pages subj ON subj.id = c.subject_id
LEFT JOIN memory.pages ds   ON ds.id   = c.on_dataset
LEFT JOIN memory.pages tk   ON tk.id   = c.on_task
LEFT JOIN memory.pages mt   ON mt.id   = c.metric
"""

# -- api.facets ---------------------------------------------------------------

_NEW_FACETS_VIEW = """
CREATE VIEW api.facets AS
SELECT p.id AS page_id, p.collection, f.facet, f.value
FROM memory.pages p
CROSS JOIN LATERAL (
  SELECT kv.key AS facet, el.v AS value
  FROM jsonb_each(p.facets) kv(key, value)
  CROSS JOIN LATERAL jsonb_array_elements_text(kv.value) el(v)
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

_OLD_FACETS_VIEW = """
CREATE VIEW api.facets AS
SELECT p.id AS page_id, p.collection, f.facet, f.value
FROM memory.pages p
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

# -- api.stale ---------------------------------------------------------------

_NEW_STALE_VIEW = """
CREATE VIEW api.stale AS
SELECT
  p.id,
  p.title,
  p.status,
  p.source_hash    AS page_source_hash,
  s.source_hash    AS mirror_source_hash,
  p.updated
FROM memory.pages p
JOIN memory.sources_raw s ON s.source_id = p.source_id
WHERE p.source_hash IS DISTINCT FROM s.source_hash
"""

_OLD_STALE_VIEW = """
CREATE VIEW api.stale AS
SELECT
  p.id,
  p.title,
  p.status,
  p.source_hash    AS page_source_hash,
  s.source_hash    AS mirror_source_hash,
  p.updated
FROM memory.pages p
JOIN memory.sources_raw s ON s.zotero_key = p.zotero_key
WHERE p.source_hash IS DISTINCT FROM s.source_hash
"""

# -- api.raw_chunks -----------------------------------------------------------
# Not itself touched by a column drop — `raw_chunks.zotero_key` only renames — but a view's own
# output column name is fixed at CREATE time and does not follow a later rename of the column
# backing it (`pg_get_viewdef` would keep rendering it as `source_id AS zotero_key` forever), so
# this view is DROP + CREATE too, purely to give its one renamed column an honest display name.

_NEW_RAW_CHUNKS_VIEW = """
CREATE VIEW api.raw_chunks AS
SELECT
  r.source_id,
  r.section_path::text AS section_path,
  r.heading,
  r.body
FROM memory.raw_chunks r
"""

_OLD_RAW_CHUNKS_VIEW = """
CREATE VIEW api.raw_chunks AS
SELECT
  r.zotero_key,
  r.section_path::text AS section_path,
  r.heading,
  r.body
FROM memory.raw_chunks r
"""

_NEW_RAW_CHUNKS_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.raw_chunks IS 'L4 verification spans: raw mirror fulltext chunked by numbered headings. Example: SELECT heading, body FROM api.raw_chunks WHERE source_id = ''ABCD1234'' ORDER BY section_path;'",
    "COMMENT ON COLUMN api.raw_chunks.source_id IS 'Vendor-neutral source id of the mirror (memory.sources_raw.source_id). Example value: ''ABCD1234''.'",
    "COMMENT ON COLUMN api.raw_chunks.section_path IS 'ltree path as text. Example value: ''s4.s1''.'",
    "COMMENT ON COLUMN api.raw_chunks.heading IS 'The numbered heading of the chunk. Example value: ''4.1 Experiments''.'",
    "COMMENT ON COLUMN api.raw_chunks.body IS 'Raw verbatim source text of the chunk.'",
)

_OLD_RAW_CHUNKS_COMMENTS: tuple[str, ...] = (
    "COMMENT ON VIEW api.raw_chunks IS 'L4 verification spans: raw mirror fulltext chunked by numbered headings. Example: SELECT heading, body FROM api.raw_chunks WHERE zotero_key = ''ABCD1234'' ORDER BY section_path;'",
    "COMMENT ON COLUMN api.raw_chunks.zotero_key IS 'Zotero item key of the source mirror.'",
    "COMMENT ON COLUMN api.raw_chunks.section_path IS 'ltree path as text. Example value: ''s4.s1''.'",
    "COMMENT ON COLUMN api.raw_chunks.heading IS 'The numbered heading of the chunk. Example value: ''4.1 Experiments''.'",
    "COMMENT ON COLUMN api.raw_chunks.body IS 'Raw verbatim source text of the chunk.'",
)


def _add_new_columns() -> None:
    """Phase 1: every new column, nullable, plus the brand-new `claim_qualifiers` table."""
    op.execute("ALTER TABLE memory.pages ADD COLUMN source_id text")
    op.execute("ALTER TABLE memory.pages ADD COLUMN facets jsonb")
    op.execute("ALTER TABLE memory.sources_raw ADD COLUMN source_id text")
    op.execute("ALTER TABLE memory.sources_raw ADD COLUMN provider text")
    op.execute("ALTER TABLE memory.sources_raw ADD COLUMN provider_key text")
    op.execute(
        """
        CREATE TABLE memory.claim_qualifiers (
          collection        text NOT NULL,
          claim_id          text NOT NULL REFERENCES memory.claims (id) ON DELETE CASCADE,
          qualifier         text NOT NULL,
          ord               int  NOT NULL,
          target_collection text NOT NULL,
          target_id         text NOT NULL REFERENCES memory.pages (id),
          PRIMARY KEY (collection, claim_id, qualifier, ord)
        )
        """
    )


def _fold_columns() -> None:
    """Phase 2: fold the demoted columns into their jsonb homes, populate `claim_qualifiers`."""
    op.execute(
        """
        UPDATE memory.pages SET
          frontmatter = frontmatter || jsonb_strip_nulls(jsonb_build_object(
            'entity_type', entity_type, 'paper_type', paper_type, 'year', year, 'venue', venue,
            'citekey', citekey, 'maturity', maturity, 'question_status', question_status)),
          facets = jsonb_strip_nulls(jsonb_build_object(
            'tasks', CASE WHEN cardinality(tasks) > 0 THEN to_jsonb(tasks) END,
            'domains', CASE WHEN cardinality(domains) > 0 THEN to_jsonb(domains) END,
            'stages', CASE WHEN cardinality(stages) > 0 THEN to_jsonb(stages) END,
            'modalities', CASE WHEN cardinality(modalities) > 0 THEN to_jsonb(modalities) END,
            'scale_regimes', CASE WHEN cardinality(scale_regimes) > 0 THEN to_jsonb(scale_regimes) END)),
          source_id = zotero_key
        """
    )
    op.execute(
        """
        UPDATE memory.sources_raw SET
          source_id = zotero_key,
          provider = 'zotero',
          provider_key = zotero_key,
          meta = meta || jsonb_strip_nulls(jsonb_build_object(
            'doi', doi, 'arxiv_id', arxiv_id, 'venue', venue, 'year', year))
        """
    )
    op.execute(
        """
        INSERT INTO memory.claim_qualifiers (collection, claim_id, qualifier, ord, target_collection, target_id)
        SELECT p.collection, c.id, 'on_dataset', 0, p.collection, c.on_dataset
        FROM memory.claims c JOIN memory.pages p ON p.id = c.page_id
        WHERE c.on_dataset IS NOT NULL
        UNION ALL
        SELECT p.collection, c.id, 'on_task', 0, p.collection, c.on_task
        FROM memory.claims c JOIN memory.pages p ON p.id = c.page_id
        WHERE c.on_task IS NOT NULL
        UNION ALL
        SELECT p.collection, c.id, 'metric', 0, p.collection, c.metric
        FROM memory.claims c JOIN memory.pages p ON p.id = c.page_id
        WHERE c.metric IS NOT NULL
        """
    )


def _finalize_new_shape() -> None:
    """Phase 3: NOT NULLs, the `sources_raw` PK swap, the `raw_chunks` re-point, drop the old, rebuild indexes."""
    # `_TOUCHED_VIEWS` read the columns this phase drops, or (`raw_chunks`) the column it renames
    # under a display name the rename would otherwise leave stale — Postgres refuses DROP COLUMN
    # while a view still depends on it. They come back, over the new shape, in `_recreate_api_surface_new`.
    for name in _TOUCHED_VIEWS:
        op.execute(f"DROP VIEW api.{name}")

    op.execute("ALTER TABLE memory.pages ALTER COLUMN facets SET NOT NULL, ALTER COLUMN facets SET DEFAULT '{}'::jsonb")
    op.execute("ALTER TABLE memory.sources_raw ALTER COLUMN source_id SET NOT NULL")
    op.execute("ALTER TABLE memory.sources_raw ALTER COLUMN provider SET NOT NULL")
    op.execute("ALTER TABLE memory.sources_raw ALTER COLUMN provider_key SET NOT NULL")

    # sources_raw's PK swaps from zotero_key to source_id; raw_chunks/pages re-point their FKs onto it.
    op.execute("ALTER TABLE memory.raw_chunks DROP CONSTRAINT raw_chunks_zotero_key_fkey")
    op.execute("ALTER TABLE memory.pages DROP CONSTRAINT pages_zotero_key_fkey")
    op.execute("ALTER TABLE memory.sources_raw DROP CONSTRAINT sources_raw_pkey")
    op.execute("ALTER TABLE memory.sources_raw ADD PRIMARY KEY (source_id)")
    op.execute("ALTER TABLE memory.raw_chunks RENAME COLUMN zotero_key TO source_id")
    op.execute(
        "ALTER TABLE memory.raw_chunks ADD CONSTRAINT raw_chunks_source_id_fkey"
        " FOREIGN KEY (source_id) REFERENCES memory.sources_raw (source_id) ON DELETE CASCADE"
    )
    op.execute("ALTER TABLE memory.pages ADD CONSTRAINT pages_source_id_fkey FOREIGN KEY (source_id) REFERENCES memory.sources_raw (source_id)")

    # BM25 indexes naming a dropped/renamed column must go before the column does.
    op.execute("DROP INDEX memory.pages_bm25")
    op.execute("DROP INDEX memory.raw_chunks_bm25")
    op.execute("DROP INDEX memory.pages_tasks")
    op.execute("DROP INDEX memory.pages_stages")

    op.execute(
        "ALTER TABLE memory.pages"
        " DROP COLUMN entity_type, DROP COLUMN paper_type, DROP COLUMN year, DROP COLUMN venue,"
        " DROP COLUMN citekey, DROP COLUMN zotero_key, DROP COLUMN maturity, DROP COLUMN question_status,"
        " DROP COLUMN tasks, DROP COLUMN domains, DROP COLUMN stages, DROP COLUMN modalities, DROP COLUMN scale_regimes,"
        " ALTER COLUMN collection DROP DEFAULT"
    )
    op.execute(
        "ALTER TABLE memory.sources_raw"
        " DROP COLUMN zotero_key, DROP COLUMN doi, DROP COLUMN arxiv_id, DROP COLUMN venue, DROP COLUMN year,"
        " ALTER COLUMN collection DROP DEFAULT"
    )
    op.execute("ALTER TABLE memory.claims DROP COLUMN on_dataset, DROP COLUMN on_task, DROP COLUMN metric")

    # The ParadeDB grammar indexes `text_cols` directly (unlike the `pg_textsearch` grammar's single fixed column,
    # resolved from the table name): `entity_type`/`paper_type`/`year`/`tasks`/`domains`/`stages`
    # drop out of the lexical index here along with the columns themselves, no replacement — the
    # three facets already ride in `card_text`'s own facet line (`card.card_text`), the four
    # scalars do not; re-indexing the jsonb homes is a fork, not this round.
    dialect = _dialect()
    op.execute(
        dialect.bm25_index_ddl(
            index="pages_bm25", table="memory.pages", key="id", text_cols=("title", "summary", "card_text", "kind", "status")
        )
    )
    op.execute(dialect.bm25_index_ddl(index="raw_chunks_bm25", table="memory.raw_chunks", key="id", text_cols=("heading", "body", "source_id")))
    op.execute("CREATE INDEX pages_facets ON memory.pages USING gin (facets)")


def _recreate_api_surface_new() -> None:
    """Phase 4: re-create `api.search`, `api.lookup_value`, and the views `_finalize_new_shape` dropped."""
    op.execute(f"DROP FUNCTION {_OLD_SEARCH_SIGNATURE}")
    op.execute(_search_body(_new_search_schema()))
    op.execute(f"GRANT EXECUTE ON FUNCTION {_NEW_SEARCH_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_NEW_SEARCH_COMMENT)

    op.execute(_NEW_LOOKUP_VALUE_BODY)

    for view_sql, view_name, comments in (
        (_NEW_CARDS_VIEW, "cards", _CARDS_COMMENTS),
        (_NEW_PAPERS_VIEW, "papers", _NEW_PAPERS_COMMENTS),
        (_NEW_ENTITIES_VIEW, "entities", _ENTITIES_COMMENTS),
        (_NEW_COVERAGE_VIEW, "coverage", _COVERAGE_COMMENTS),
        (_NEW_SCOREBOARD_VIEW, "scoreboard", _SCOREBOARD_COMMENTS),
        (_NEW_CLAIMS_VIEW, "claims", _CLAIMS_COMMENTS),
        (_NEW_FACETS_VIEW, "facets", _NEW_FACETS_COMMENTS),
        (_NEW_STALE_VIEW, "stale", _STALE_COMMENTS),
        (_NEW_RAW_CHUNKS_VIEW, "raw_chunks", _NEW_RAW_CHUNKS_COMMENTS),
    ):
        op.execute(view_sql)
        op.execute(f"GRANT SELECT ON api.{view_name} TO {_READER_ROLE}")
        for statement in comments:
            op.execute(statement)


def upgrade() -> None:
    """Fold the research profile's typed columns into declarations, one release, four phases."""
    _add_new_columns()
    _fold_columns()
    _finalize_new_shape()
    _recreate_api_surface_new()


def _add_old_columns() -> None:
    """Downgrade phase 1: the pre-maturation columns, nullable (the five arrays default `'{}'`)."""
    op.execute("ALTER TABLE memory.pages ADD COLUMN entity_type text")
    op.execute("ALTER TABLE memory.pages ADD COLUMN paper_type text")
    op.execute("ALTER TABLE memory.pages ADD COLUMN year int")
    op.execute("ALTER TABLE memory.pages ADD COLUMN venue text")
    op.execute("ALTER TABLE memory.pages ADD COLUMN citekey text")
    op.execute("ALTER TABLE memory.pages ADD COLUMN maturity text")
    op.execute("ALTER TABLE memory.pages ADD COLUMN question_status text")
    op.execute("ALTER TABLE memory.pages ADD COLUMN zotero_key text")
    op.execute("ALTER TABLE memory.pages ADD COLUMN tasks text[] NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE memory.pages ADD COLUMN domains text[] NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE memory.pages ADD COLUMN stages text[] NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE memory.pages ADD COLUMN modalities text[] NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE memory.pages ADD COLUMN scale_regimes text[] NOT NULL DEFAULT '{}'")

    op.execute("ALTER TABLE memory.sources_raw ADD COLUMN zotero_key text")
    op.execute("ALTER TABLE memory.sources_raw ADD COLUMN doi text")
    op.execute("ALTER TABLE memory.sources_raw ADD COLUMN arxiv_id text")
    op.execute("ALTER TABLE memory.sources_raw ADD COLUMN venue text")
    op.execute("ALTER TABLE memory.sources_raw ADD COLUMN year int")

    op.execute("ALTER TABLE memory.claims ADD COLUMN on_dataset text REFERENCES memory.pages (id)")
    op.execute("ALTER TABLE memory.claims ADD COLUMN on_task text REFERENCES memory.pages (id)")
    op.execute("ALTER TABLE memory.claims ADD COLUMN metric text REFERENCES memory.pages (id)")


def _backfill_old_columns() -> None:
    """Downgrade phase 2: reconstruct the pre-maturation columns from their jsonb homes + `claim_qualifiers`."""
    op.execute(
        """
        UPDATE memory.pages SET
          entity_type = frontmatter->>'entity_type',
          paper_type = frontmatter->>'paper_type',
          year = (frontmatter->>'year')::int,
          venue = frontmatter->>'venue',
          citekey = frontmatter->>'citekey',
          maturity = frontmatter->>'maturity',
          question_status = frontmatter->>'question_status',
          zotero_key = source_id,
          tasks = COALESCE(ARRAY(SELECT jsonb_array_elements_text(facets->'tasks')), ARRAY[]::text[]),
          domains = COALESCE(ARRAY(SELECT jsonb_array_elements_text(facets->'domains')), ARRAY[]::text[]),
          stages = COALESCE(ARRAY(SELECT jsonb_array_elements_text(facets->'stages')), ARRAY[]::text[]),
          modalities = COALESCE(ARRAY(SELECT jsonb_array_elements_text(facets->'modalities')), ARRAY[]::text[]),
          scale_regimes = COALESCE(ARRAY(SELECT jsonb_array_elements_text(facets->'scale_regimes')), ARRAY[]::text[])
        """
    )
    op.execute(
        """
        UPDATE memory.sources_raw SET
          zotero_key = source_id,
          doi = meta->>'doi',
          arxiv_id = meta->>'arxiv_id',
          venue = meta->>'venue',
          year = (meta->>'year')::int
        """
    )
    op.execute(
        """
        UPDATE memory.claims c SET
          on_dataset = (SELECT q.target_id FROM memory.claim_qualifiers q
                        WHERE q.claim_id = c.id AND q.qualifier = 'on_dataset' AND q.ord = 0),
          on_task    = (SELECT q.target_id FROM memory.claim_qualifiers q
                        WHERE q.claim_id = c.id AND q.qualifier = 'on_task' AND q.ord = 0),
          metric     = (SELECT q.target_id FROM memory.claim_qualifiers q
                        WHERE q.claim_id = c.id AND q.qualifier = 'metric' AND q.ord = 0)
        """
    )


def _finalize_old_shape() -> None:
    """Downgrade phase 3: restore the old PK/FKs/defaults, drop the new columns and `claim_qualifiers`, rebuild indexes."""
    # `_TOUCHED_VIEWS` (over the new shape) read the columns this phase drops. They come back,
    # over the old shape, in `_recreate_api_surface_old`.
    for name in _TOUCHED_VIEWS:
        op.execute(f"DROP VIEW api.{name}")

    op.execute("DROP INDEX memory.pages_facets")
    op.execute("DROP INDEX memory.pages_bm25")
    op.execute("DROP INDEX memory.raw_chunks_bm25")

    op.execute("ALTER TABLE memory.raw_chunks DROP CONSTRAINT raw_chunks_source_id_fkey")
    op.execute("ALTER TABLE memory.pages DROP CONSTRAINT pages_source_id_fkey")
    op.execute("ALTER TABLE memory.sources_raw DROP CONSTRAINT sources_raw_pkey")
    op.execute("ALTER TABLE memory.sources_raw ADD PRIMARY KEY (zotero_key)")
    op.execute("ALTER TABLE memory.raw_chunks RENAME COLUMN source_id TO zotero_key")
    op.execute(
        "ALTER TABLE memory.raw_chunks ADD CONSTRAINT raw_chunks_zotero_key_fkey"
        " FOREIGN KEY (zotero_key) REFERENCES memory.sources_raw (zotero_key) ON DELETE CASCADE"
    )
    op.execute("ALTER TABLE memory.pages ADD CONSTRAINT pages_zotero_key_fkey FOREIGN KEY (zotero_key) REFERENCES memory.sources_raw (zotero_key)")

    op.execute("ALTER TABLE memory.pages DROP COLUMN source_id, DROP COLUMN facets, ALTER COLUMN collection SET DEFAULT 'research'")
    op.execute(
        "ALTER TABLE memory.sources_raw"
        " DROP COLUMN source_id, DROP COLUMN provider, DROP COLUMN provider_key, ALTER COLUMN collection SET DEFAULT 'research'"
    )
    op.execute("DROP TABLE memory.claim_qualifiers")

    dialect = _dialect()
    op.execute(
        dialect.bm25_index_ddl(
            index="pages_bm25",
            table="memory.pages",
            key="id",
            text_cols=("title", "summary", "card_text", "kind", "status", "entity_type", "paper_type", "year", "tasks", "domains", "stages"),
        )
    )
    op.execute(dialect.bm25_index_ddl(index="raw_chunks_bm25", table="memory.raw_chunks", key="id", text_cols=("heading", "body", "zotero_key")))
    op.execute("CREATE INDEX pages_tasks  ON memory.pages USING gin (tasks)")
    op.execute("CREATE INDEX pages_stages ON memory.pages USING gin (stages)")


def _recreate_api_surface_old() -> None:
    """Downgrade phase 4: restore the pre-maturation `api.search` / `api.lookup_value` / view bodies."""
    op.execute(f"DROP FUNCTION {_NEW_SEARCH_SIGNATURE}")
    op.execute(_search_body(_old_search_schema()))
    op.execute(f"GRANT EXECUTE ON FUNCTION {_OLD_SEARCH_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_OLD_SEARCH_COMMENT)

    op.execute(_OLD_LOOKUP_VALUE_BODY)

    # cards/entities/coverage/scoreboard/claims/stale: their comment prose never named the
    # implementation (typed column vs. jsonb derivation), so the same texts restore verbatim.
    for view_sql, view_name, comments in (
        (_OLD_CARDS_VIEW, "cards", _CARDS_COMMENTS),
        (_OLD_PAPERS_VIEW, "papers", _OLD_PAPERS_COMMENTS),
        (_OLD_ENTITIES_VIEW, "entities", _ENTITIES_COMMENTS),
        (_OLD_COVERAGE_VIEW, "coverage", _COVERAGE_COMMENTS),
        (_OLD_SCOREBOARD_VIEW, "scoreboard", _SCOREBOARD_COMMENTS),
        (_OLD_CLAIMS_VIEW, "claims", _CLAIMS_COMMENTS),
        (_OLD_FACETS_VIEW, "facets", _OLD_FACETS_COMMENTS),
        (_OLD_STALE_VIEW, "stale", _STALE_COMMENTS),
        (_OLD_RAW_CHUNKS_VIEW, "raw_chunks", _OLD_RAW_CHUNKS_COMMENTS),
    ):
        op.execute(view_sql)
        op.execute(f"GRANT SELECT ON api.{view_name} TO {_READER_ROLE}")
        for statement in comments:
            op.execute(statement)


def downgrade() -> None:
    """Reconstruct the pre-maturation columns losslessly (for exactly what that shape could hold) and restore its api surface."""
    _add_old_columns()
    _backfill_old_columns()
    _finalize_old_shape()
    _recreate_api_surface_old()
