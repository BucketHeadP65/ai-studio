"""api schema: agent-facing views + functions, every object and column COMMENTed.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-11 00:00:01.000000

Defines the agent-facing ``api`` schema: the ``api`` schema is the *only* thing the
``wiki_reader`` role sees — ~14 views + 4 functions, every object and column carrying
a COMMENT (the comments **are** the M-Schema agents read via ``describe_schema``).
Views expose names alongside ids, never bare ids.

This migration is the source of truth for that surface — it authors the views' SQL and
the functions directly. Three properties of the surface are worth naming:

* **``api.search`` carries ``query_vec halfvec(1024) DEFAULT NULL``** between ``k``
  and ``level``. Postgres cannot embed text, so the vector must be supplied by the
  caller: ``query_vec IS NULL`` runs the BM25 RRF leg only (lexical-only — still good
  for agents calling raw SQL); a non-NULL vector adds the ANN leg for full hybrid RRF.
* **Guardrails baked into the bodies:** ``api.search`` hard-caps ``k`` at 50;
  ``api.fetch`` truncates bodies past ~8k tokens with the
  ``[truncated — fetch sections instead]`` marker.
* **``SECURITY DEFINER`` on the three reader-facing functions** (``search`` /
  ``fetch`` / ``lookup_value``): they read the ``wiki.*`` base tables, but the
  ``wiki_reader`` role is granted ``api`` only (0003). Views run as their owner so
  the reader sees through them, but a ``SECURITY INVOKER`` function (the default)
  runs as the *caller* — so the reader would hit "permission denied for schema
  wiki" inside the body. Making them ``SECURITY DEFINER`` (as ``log_session``
  already is) runs the body as the owner, which can read ``wiki.*``; ``search_path``
  is pinned to ``wiki, public, pg_catalog`` (the schemas the bodies resolve
  unqualified names + the ``<=>`` / ``@@@`` operators against) — the standard
  ``SECURITY DEFINER`` hardening so the path cannot be hijacked by the caller.

All DDL is hand-written ``op.execute`` (views/functions/comments are not
autogeneratable). Downgrade drops the schema cascade.
"""

from __future__ import annotations

from alembic import context, op
from core.ai.retrieval.contracts import SearchSchema, Facet, FacetPool, ParadeDBHybridSearchBuilder, PgTextSearchHybridSearchBuilder, SearchSurface

from engram.retrieval.search_dialect import SearchDialect

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

# ~8k tokens ≈ 32k chars (4 chars/token heuristic); fetch truncates page bodies past this.
_FETCH_CHAR_BUDGET = 32000
_FETCH_TRUNCATION_MARKER = "[truncated — fetch sections instead]"


def _dialect() -> SearchDialect:
    """Return the BM25 :class:`SearchDialect` env.py stashed on ``config.attributes``.

    The channel is ``context.config.attributes["search_dialect"]``, set once in
    ``alembic/env.py`` from the resolved :class:`engram.config.DbDialect` before
    any revision runs. The lookup is narrowed against the runtime-checkable
    :class:`~engram.retrieval.search_dialect.dialect.SearchDialect` Protocol so the
    typed return needs no ``cast``.

    Returns:
        The configured dialect (``pg_search`` by default, ``pg_textsearch`` when
        ``APP_RETRIEVAL__DIALECT=pg_textsearch``).

    Raises:
        RuntimeError: If env.py has not stashed a dialect (revision run outside it).
    """
    dialect = context.config.attributes.get("search_dialect")
    if dialect is None:
        msg = "search_dialect not configured: alembic/env.py must stash it in config.attributes"
        raise RuntimeError(msg)
    if not isinstance(dialect, SearchDialect):
        msg = f"search_dialect must be a SearchDialect, got {type(dialect).__name__}"
        raise TypeError(msg)
    return dialect


def _search_schema() -> SearchSchema:
    """The hybrid-search shape this revision has always created, frozen against ``wiki.*``.

    Migration-local rather than ``engram.retrieval.corpus_schema.MEMORY_CORPUS_SCHEMA``: that
    constant tracks the library's current descriptor shape, which later revisions (0010's own
    ``0010_memory_schema_rename.py._search_schema``, this migration's downstream DROP/CREATE
    targets) assume stays this exact ``task``/``stage``/``domain`` shape. Reaching into the live
    constant here would drift this migration's ``CREATE FUNCTION`` out of step with its own
    hardcoded ``COMMENT`` the moment the library descriptor changes for a reason unrelated to
    ``wiki``'s schema history.
    """
    return SearchSchema(
        surfaces=(
            SearchSurface(
                name="card",
                table="wiki.pages",
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
                table="wiki.sections",
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
                table="wiki.claims",
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
                table="wiki.raw_chunks",
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
                table="wiki.pages",
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
                table="wiki.sources_raw",
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


def _search_body() -> str:
    """The full ``CREATE FUNCTION api.search(...)`` statement, selected by the configured dialect.

    Calls the dialect's builder directly over :func:`_search_schema` rather than
    ``_dialect().api_search_body()``, whose implementations are permanently bound to
    ``engram.retrieval.corpus_schema.MEMORY_CORPUS_SCHEMA`` (the library's current, evolving
    descriptor) — this revision must keep emitting the ``wiki``-qualified, ``task``/``stage``/
    ``domain`` shape it has always created, unaffected by later descriptor changes.
    """
    dialect_name = _dialect().name
    builder: ParadeDBHybridSearchBuilder | PgTextSearchHybridSearchBuilder
    if dialect_name == "pg_search":
        builder = ParadeDBHybridSearchBuilder()
    elif dialect_name == "pg_textsearch":
        builder = PgTextSearchHybridSearchBuilder()
    else:
        msg = f"search_dialect not configured (or unrecognised name {dialect_name!r}): alembic/env.py must stash a SearchDialect"
        raise RuntimeError(msg)
    return builder.api_search_body(_search_schema())


def upgrade() -> None:
    """Create the ``api`` schema, its views and functions, and all COMMENTs."""
    op.execute("CREATE SCHEMA IF NOT EXISTS api")
    _create_views()
    _create_functions()
    _create_comments()


def _create_views() -> None:
    """The agent-facing views — names alongside ids throughout."""
    op.execute(
        """
        CREATE VIEW api.about AS
        SELECT card_text AS body FROM wiki.pages WHERE id = 'meta.about'
        """
    )

    op.execute(
        """
        CREATE VIEW api.schema_guide AS
        SELECT value AS surface, definition AS purpose, meta AS notes
        FROM wiki.vocab WHERE vocab = 'schema_guide'
        """
    )

    op.execute(
        """
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
          (SELECT count(*) FROM wiki.claims c WHERE c.page_id = p.id)        AS n_claims,
          (SELECT count(*) FROM wiki.edges e WHERE e.src_id = p.id)          AS out_degree,
          (SELECT count(*) FROM wiki.edges e WHERE e.dst_id = p.id)          AS in_degree,
          p.updated
        FROM wiki.pages p
        """
    )

    op.execute(
        """
        CREATE VIEW api.papers AS
        SELECT
          p.id, p.title, p.year, p.venue, p.citekey, p.paper_type,
          p.tasks, p.domains, p.stages,
          p.zotero_key,
          s.doi, s.arxiv_id, s.pdf_path,
          (SELECT count(*) FROM wiki.claims c WHERE c.page_id = p.id) AS n_claims,
          p.status, p.updated
        FROM wiki.pages p
        LEFT JOIN wiki.sources_raw s ON s.zotero_key = p.zotero_key
        WHERE p.kind = 'paper'
        """
    )

    op.execute(
        """
        CREATE VIEW api.entities AS
        SELECT
          p.id, p.title, p.entity_type, p.summary, p.status,
          p.tasks, p.domains, p.stages,
          (SELECT count(*) FROM wiki.edges e WHERE e.src_id = p.id) AS out_degree,
          (SELECT count(*) FROM wiki.edges e WHERE e.dst_id = p.id) AS in_degree,
          (SELECT count(*) FROM wiki.claims c WHERE c.subject_id = p.id) AS n_evidence,
          p.updated
        FROM wiki.pages p
        WHERE p.kind = 'entity'
        """
    )

    op.execute(
        """
        CREATE VIEW api.sections AS
        SELECT
          s.id           AS section_id,
          s.page_id,
          p.title        AS page_title,
          p.kind         AS page_kind,
          s.canon,
          s.path::text   AS heading_path,
          s.body,
          s.tokens
        FROM wiki.sections s
        JOIN wiki.pages p ON p.id = s.page_id
        """
    )

    op.execute(
        """
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
        FROM wiki.claims c
        JOIN wiki.pages p ON p.id = c.page_id
        LEFT JOIN wiki.pages subj ON subj.id = c.subject_id
        LEFT JOIN wiki.pages ds   ON ds.id   = c.on_dataset
        LEFT JOIN wiki.pages tk   ON tk.id   = c.on_task
        LEFT JOIN wiki.pages mt   ON mt.id   = c.metric
        """
    )

    op.execute(
        """
        CREATE VIEW api.edges AS
        SELECT
          e.src_id,
          src.title AS src_title,
          e.rel,
          e.dst_id,
          dst.title AS dst_title,
          e.qualifiers,
          e.provenance_claim
        FROM wiki.edges e
        LEFT JOIN wiki.pages src ON src.id = e.src_id
        LEFT JOIN wiki.pages dst ON dst.id = e.dst_id
        """
    )

    op.execute(
        """
        CREATE VIEW api.matrix AS
        SELECT
          cc.comparison_id,
          comp.title AS comparison_title,
          cc.row_entity,
          re.title   AS row_entity_title,
          cc.dimension,
          cc.value,
          cc.claim_refs
        FROM wiki.comparison_cells cc
        JOIN wiki.pages comp ON comp.id = cc.comparison_id
        JOIN wiki.pages re   ON re.id   = cc.row_entity
        """
    )

    # Computed trust: supports/refutes/wins/losses/null-results per entity subject.
    op.execute(
        """
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
        FROM wiki.pages p
        LEFT JOIN wiki.claims c ON c.subject_id = p.id
        LEFT JOIN wiki.pages  pp ON pp.id = c.page_id
        WHERE p.kind = 'entity'
        GROUP BY p.id, p.title, p.stages
        """
    )

    # The computed map: facet value × page type → counts.
    op.execute(
        """
        CREATE VIEW api.coverage AS
        SELECT
          'tasks'    AS facet,
          t.value    AS facet_value,
          p.kind     AS page_type,
          count(*)   AS n_pages
        FROM wiki.pages p, unnest(p.tasks) AS t(value)
        GROUP BY t.value, p.kind
        UNION ALL
        SELECT 'stages', s.value, p.kind, count(*)
        FROM wiki.pages p, unnest(p.stages) AS s(value)
        GROUP BY s.value, p.kind
        UNION ALL
        SELECT 'domains', d.value, p.kind, count(*)
        FROM wiki.pages p, unnest(p.domains) AS d(value)
        GROUP BY d.value, p.kind
        """
    )

    # Disputed claim pairs with both quotes + conditions.
    op.execute(
        """
        CREATE VIEW api.contradictions AS
        SELECT
          a.id        AS claim_a,
          b.id        AS claim_b,
          a.subject_id,
          subj.title  AS subject_name,
          a.text      AS text_a,
          b.text      AS text_b,
          a.quote     AS quote_a,
          b.quote     AS quote_b,
          a.condition AS condition_a,
          b.condition AS condition_b
        FROM wiki.claims a
        JOIN wiki.claims b
          ON a.subject_id = b.subject_id
         AND a.id < b.id
        LEFT JOIN wiki.pages subj ON subj.id = a.subject_id
        WHERE a.status = 'disputed' AND b.status = 'disputed'
        """
    )

    op.execute(
        """
        CREATE VIEW api.raw_chunks AS
        SELECT
          r.zotero_key,
          r.section_path::text AS section_path,
          r.heading,
          r.body
        FROM wiki.raw_chunks r
        """
    )

    # Freshness rules: copied source_hash drifted from the mirror's current hash.
    op.execute(
        """
        CREATE VIEW api.stale AS
        SELECT
          p.id,
          p.title,
          p.status,
          p.source_hash    AS page_source_hash,
          s.source_hash    AS mirror_source_hash,
          p.updated
        FROM wiki.pages p
        JOIN wiki.sources_raw s ON s.zotero_key = p.zotero_key
        WHERE p.source_hash IS DISTINCT FROM s.source_hash
        """
    )


def _create_functions() -> None:
    """``search`` (hybrid, vector-optional), ``lookup_value``, ``fetch``, ``log_session``."""
    # Hybrid search collapsed across levels: RRF of BM25 + (optional) ANN, per level, unioned;
    # k hard-capped at 50; query_vec NULL → BM25-only RRF leg. The whole function body is
    # dialect-routed: ParadeDB's dialect emits a dynamic ``RETURN QUERY EXECUTE`` with
    # ``MATERIALIZED``-per-leg CTEs, working around two pg_search query-shape constraints (a static
    # BM25 custom scan mis-projects base-table columns; ``paradedb.score()`` cannot lift across a
    # multi-relation ``UNION ALL``), while the ``pg_textsearch`` dialect emits the simplified static
    # ``pg_textsearch`` (``<@>`` / ``to_bm25query``) body. Only the BM25 legs differ between
    # dialects; the vector legs, the ``Σ 1/(60+r)`` RRF and the k cap are identical. See
    # ``engram/retrieval/search_dialect/{paradedb,pg_textsearch}.py``.
    op.execute(_search_body())

    op.execute(
        """
        CREATE FUNCTION api.lookup_value(facet text, term text DEFAULT NULL)
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

    # fetch: page → whole body, else section/claim/raw text; page bodies truncated past ~8k tokens.
    op.execute(
        f"""
        CREATE FUNCTION api.fetch(unit_id text)
          RETURNS TABLE (id text, level text, title text, body text)
          LANGUAGE sql STABLE SECURITY DEFINER SET search_path = wiki, public, pg_catalog AS $$
            SELECT p.id, 'page'::text, p.title,
                   CASE WHEN length(p.card_text) > {_FETCH_CHAR_BUDGET}
                        THEN left(p.card_text, {_FETCH_CHAR_BUDGET}) || ' {_FETCH_TRUNCATION_MARKER}'
                        ELSE p.card_text END
            FROM wiki.pages p WHERE p.id = unit_id
            UNION ALL
            SELECT s.id, 'section', s.heading, s.body
            FROM wiki.sections s WHERE s.id = unit_id
            UNION ALL
            SELECT c.id, 'claim', c.kind, c.text
            FROM wiki.claims c WHERE c.id = unit_id
            UNION ALL
            SELECT r.id, 'raw', r.heading, r.body
            FROM wiki.raw_chunks r WHERE r.id = unit_id
        $$
        """
    )

    # The only write the read-only role can perform: SECURITY DEFINER insert into query_log.
    op.execute(
        """
        CREATE FUNCTION api.log_session(
          question text, archetype text, surfaces text[], cited_ids text[],
          tokens_loaded int, wrote_back text DEFAULT NULL
        ) RETURNS void
        LANGUAGE sql SECURITY DEFINER SET search_path = wiki, public, pg_catalog AS $$
          INSERT INTO wiki.query_log (question, archetype, surfaces, cited_ids, tokens_loaded, wrote_back)
          VALUES (question, archetype, surfaces, cited_ids, tokens_loaded, wrote_back)
        $$
        """
    )


def _create_comments() -> None:
    """COMMENT ON every api object and column — the M-Schema agents read."""
    for statement in _COMMENT_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Drop the ``api`` schema and every view/function in it."""
    op.execute("DROP SCHEMA IF EXISTS api CASCADE")


# --- COMMENTs (the M-Schema) ------------------------------------------------
# Object + column comments with units and example values. Kept as a flat list so
# the upgrade body stays readable and the set is easy to audit.
_COMMENT_STATEMENTS: tuple[str, ...] = (
    # api.about
    "COMMENT ON VIEW api.about IS 'L0 orientation — one row of guidance text generated from the vault''s schema/purpose/retrieval-policy. Read once per session before querying. Example: SELECT body FROM api.about;'",
    "COMMENT ON COLUMN api.about.body IS 'The orientation prose (markdown). Example value: ''This wiki indexes RAG/retrieval literature; start at api.cards, then api.search…''.'",
    # api.schema_guide
    "COMMENT ON VIEW api.schema_guide IS 'One row per api surface: its purpose and 1-3 exemplar queries. The map of this schema. Example: SELECT surface, purpose FROM api.schema_guide;'",
    "COMMENT ON COLUMN api.schema_guide.surface IS 'The api object name. Example value: ''api.search''.'",
    "COMMENT ON COLUMN api.schema_guide.purpose IS 'What the surface is for, in one sentence.'",
    "COMMENT ON COLUMN api.schema_guide.notes IS 'jsonb: column notes + exemplar queries for the surface.'",
    # api.cards
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
    # api.papers
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
    # api.entities
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
    # api.sections
    "COMMENT ON VIEW api.sections IS 'L2 read unit: one row per H2/H3 leaf section of a wiki page, with its container page named. Example: SELECT page_title, body FROM api.sections WHERE canon = ''limitations'' LIMIT 10;'",
    "COMMENT ON COLUMN api.sections.section_id IS 'Section id ''<page_id>#<heading-slug>''. Example value: ''paper.lightrag-2025#findings''.'",
    "COMMENT ON COLUMN api.sections.page_id IS 'Owning page id.'",
    "COMMENT ON COLUMN api.sections.page_title IS 'Owning page title (the breadcrumb).'",
    "COMMENT ON COLUMN api.sections.page_kind IS 'Owning page type: paper|entity|…'",
    "COMMENT ON COLUMN api.sections.canon IS 'Canonical H2 slug when the heading matches the type''s canon set. Example value: ''limitations''.'",
    "COMMENT ON COLUMN api.sections.heading_path IS 'ltree path as text. Example value: ''approach.graph_indexing''.'",
    "COMMENT ON COLUMN api.sections.body IS 'Section markdown body.'",
    "COMMENT ON COLUMN api.sections.tokens IS 'int — approximate token count of the body.'",
    # api.claims
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
    # api.edges
    "COMMENT ON VIEW api.edges IS 'Typed relations between pages/claims, both endpoints named. Example: SELECT src_title, rel, dst_title FROM api.edges WHERE rel = ''evaluates'' AND dst_id = ''ent.graphrag'' LIMIT 20;'",
    "COMMENT ON COLUMN api.edges.src_id IS 'Source id (page or claim).'",
    "COMMENT ON COLUMN api.edges.src_title IS 'Source title.'",
    "COMMENT ON COLUMN api.edges.rel IS 'Registry-validated relation. Example value: ''outperforms''.'",
    "COMMENT ON COLUMN api.edges.dst_id IS 'Destination id.'",
    "COMMENT ON COLUMN api.edges.dst_title IS 'Destination title.'",
    "COMMENT ON COLUMN api.edges.qualifiers IS 'jsonb qualifiers copied from the claim block for claim-level edges.'",
    "COMMENT ON COLUMN api.edges.provenance_claim IS 'Claim id that produced this edge (claim-level edges only).'",
    # api.matrix
    "COMMENT ON VIEW api.matrix IS 'Comparison matrices as data: one row per (comparison, row entity, dimension) cell, with names. Example: SELECT row_entity_title, dimension, value FROM api.matrix WHERE comparison_id = ''cmp.graph-rag-systems'';'",
    "COMMENT ON COLUMN api.matrix.comparison_id IS 'Comparison page id.'",
    "COMMENT ON COLUMN api.matrix.comparison_title IS 'Comparison page title.'",
    "COMMENT ON COLUMN api.matrix.row_entity IS 'Row entity page id.'",
    "COMMENT ON COLUMN api.matrix.row_entity_title IS 'Row entity name.'",
    "COMMENT ON COLUMN api.matrix.dimension IS 'The compared dimension. Example value: ''indexing cost''.'",
    "COMMENT ON COLUMN api.matrix.value IS 'The cell value (text).'",
    "COMMENT ON COLUMN api.matrix.claim_refs IS 'text[] — claim ids backing this cell.'",
    # api.scoreboard
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
    # api.coverage
    "COMMENT ON VIEW api.coverage IS 'The computed map: facet value x page type -> counts (what we know about X, and how much). Example: SELECT facet_value, page_type, n_pages FROM api.coverage WHERE facet = ''tasks'' ORDER BY n_pages DESC LIMIT 20;'",
    "COMMENT ON COLUMN api.coverage.facet IS 'Which facet: tasks|stages|domains.'",
    "COMMENT ON COLUMN api.coverage.facet_value IS 'A value of that facet. Example value: ''long-doc-qa''.'",
    "COMMENT ON COLUMN api.coverage.page_type IS 'Page type the count is grouped by.'",
    "COMMENT ON COLUMN api.coverage.n_pages IS 'int — pages with this facet value and type.'",
    # api.contradictions
    "COMMENT ON VIEW api.contradictions IS 'Disputed claim pairs about the same subject, with both quotes + conditions. Example: SELECT subject_name, text_a, text_b FROM api.contradictions LIMIT 20;'",
    "COMMENT ON COLUMN api.contradictions.claim_a IS 'First claim id of the disputed pair.'",
    "COMMENT ON COLUMN api.contradictions.claim_b IS 'Second claim id of the disputed pair.'",
    "COMMENT ON COLUMN api.contradictions.subject_id IS 'Shared subject entity id.'",
    "COMMENT ON COLUMN api.contradictions.subject_name IS 'Shared subject name.'",
    "COMMENT ON COLUMN api.contradictions.text_a IS 'Claim A sentence.'",
    "COMMENT ON COLUMN api.contradictions.text_b IS 'Claim B sentence.'",
    "COMMENT ON COLUMN api.contradictions.quote_a IS 'Claim A source quote.'",
    "COMMENT ON COLUMN api.contradictions.quote_b IS 'Claim B source quote.'",
    "COMMENT ON COLUMN api.contradictions.condition_a IS 'Claim A qualifying condition.'",
    "COMMENT ON COLUMN api.contradictions.condition_b IS 'Claim B qualifying condition.'",
    # api.raw_chunks
    "COMMENT ON VIEW api.raw_chunks IS 'L4 verification spans: raw mirror fulltext chunked by numbered headings. Example: SELECT heading, body FROM api.raw_chunks WHERE zotero_key = ''ABCD1234'' ORDER BY section_path;'",
    "COMMENT ON COLUMN api.raw_chunks.zotero_key IS 'Zotero item key of the source mirror.'",
    "COMMENT ON COLUMN api.raw_chunks.section_path IS 'ltree path as text. Example value: ''s4.s1''.'",
    "COMMENT ON COLUMN api.raw_chunks.heading IS 'The numbered heading of the chunk. Example value: ''4.1 Experiments''.'",
    "COMMENT ON COLUMN api.raw_chunks.body IS 'Raw verbatim source text of the chunk.'",
    # api.stale
    "COMMENT ON VIEW api.stale IS 'Pages failing freshness rules: the copied source_hash drifted from the mirror''s current hash. Example: SELECT id, title FROM api.stale LIMIT 20;'",
    "COMMENT ON COLUMN api.stale.id IS 'Stale page id.'",
    "COMMENT ON COLUMN api.stale.title IS 'Page title.'",
    "COMMENT ON COLUMN api.stale.status IS 'Lifecycle status.'",
    "COMMENT ON COLUMN api.stale.page_source_hash IS 'source_hash recorded on the page at ingest.'",
    "COMMENT ON COLUMN api.stale.mirror_source_hash IS 'Current source_hash of the mirror — drift means stale.'",
    "COMMENT ON COLUMN api.stale.updated IS 'date — last edit of the note.'",
    # functions
    "COMMENT ON FUNCTION api.search(text, int, halfvec, text, text, text, text, text, boolean) IS 'Hybrid search collapsed across levels (RRF of BM25 + optional ANN). Pass query_vec (a 1024-dim halfvec embedding of the query) for full hybrid; omit it (NULL) for BM25-only lexical search. k is hard-capped at 50. The ''level'' arg filters to one tier (card|section|claim|raw); facet args (task/stage/domain) pre-filter BEFORE ranking. Returns id, hit_level (card|section|claim|raw), page_id, page_title, snippet, score. Example: SELECT * FROM api.search(''graph rag token cost'', k => 8);'",
    "COMMENT ON FUNCTION api.lookup_value(text, text) IS 'Value grounding: what values exist for a facet, with definitions and page counts (so agents never guess WHERE strings). Example: SELECT * FROM api.lookup_value(''tasks'', ''qa'');'",
    "COMMENT ON FUNCTION api.fetch(text) IS 'Fetch a unit''s full text by id (page -> whole body, else section/claim/raw chunk). Page bodies past ~8k tokens are truncated with a [truncated — fetch sections instead] marker. Example: SELECT * FROM api.fetch(''paper.lightrag-2025#c1'');'",
    "COMMENT ON FUNCTION api.log_session(text, text, text[], text[], int, text) IS 'Session telemetry: the only write the read-only role may perform (SECURITY DEFINER insert into wiki.query_log). Example: SELECT api.log_session(''q?'', ''compare'', ARRAY[''api.search''], ARRAY[''paper.x''], 1200);'",
)
