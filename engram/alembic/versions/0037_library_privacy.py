"""`api.visible_collection`: a collection declaring `library: false` is served only to its own session.

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-01 00:00:00.000000

The compiled fingerprint's `switches` now carries the corpus's own `library` declaration
(`engram.compile.switches.RetrievalSwitches`). `api.visible_collection(text)` reads it: a
collection is visible when it is the session's own (`app.collection`, the GUC a reader connection
sets) or when its fingerprint does not carry `library: false`. A collection with no fingerprint row
and a fingerprint with no `library` key both read as shared, matching the compile-side default.

Every `api` view gains the predicate on its collection column, aggregates apply it inside the join
they count over, and `api.edges` applies it to both endpoints so a private collection never appears
even as a neighbour id. Every read function taking a collection guards on it, so an invisible
collection answers exactly like a collection that does not exist: an empty result of the same
shape, never a distinct refusal. `api.search`'s two pools carry the predicate, so a query with no
collection argument still ranks only over visible rows. The two graph functions become guarded
wrappers over the generated walk, which moves to `memory.graph_expand_walk` /
`memory.graph_paths_walk` (ungranted, reached only through the wrappers); the recursive-CTE walk
additionally reads `memory.visible_edges`, whose rows have both endpoints visible.

No role-model change: `corpus_reader` keeps its `api`-only grants, every read function stays
`SECURITY DEFINER`, and no grant on `memory` is added. A session that sets `app.collection` to a
private collection reads it exactly as before.
"""

from __future__ import annotations

from alembic import context, op
from core.ai.retrieval.contracts import (
    AgeGraphTraversalBuilder,
    SearchSchema,
    CteGraphTraversalBuilder,
    Facet,
    FacetPool,
    GraphSchema,
    ParadeDBHybridSearchBuilder,
    PgTextSearchHybridSearchBuilder,
    SearchSurface,
)

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "corpus_reader"

_VISIBLE_SIGNATURE = "api.visible_collection(text)"
_SEARCH_SIGNATURE = "api.search(text, integer, halfvec, text, jsonb, text, boolean)"
_EXPAND_SIGNATURE = "api.graph_expand(text, text[], integer, text[], integer)"
_PATHS_SIGNATURE = "api.graph_paths(text, text, text, integer, text[], integer)"
_EXPAND_WALK = "memory.graph_expand_walk"
_PATHS_WALK = "memory.graph_paths_walk"
_EXPAND_WALK_SIGNATURE = f"{_EXPAND_WALK}(text, text[], integer, text[], integer)"
_PATHS_WALK_SIGNATURE = f"{_PATHS_WALK}(text, text, text, integer, text[], integer)"

_VISIBLE_COLLECTION = """
CREATE FUNCTION api.visible_collection(p_collection text)
  RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT p_collection IS NOT NULL
       AND (p_collection IS NOT DISTINCT FROM current_setting('app.collection', true)
            OR NOT EXISTS (
              SELECT 1 FROM memory.compile_fingerprint f
              WHERE f.collection = p_collection AND f.switches ->> 'library' = 'false'
            ))
$$
"""

_VISIBLE_COMMENT = (
    "COMMENT ON FUNCTION api.visible_collection(text) IS "
    "'Whether this session may read one collection: true for the session''s own collection "
    "(the app.collection setting a reader connection carries) and for every collection whose "
    "compiled switches do not say library: false. A collection this answers false for behaves "
    "throughout the api surface exactly like one that does not exist. Example: "
    "SELECT api.visible_collection(''research'');'"
)

_VISIBLE_EDGES = """
CREATE VIEW memory.visible_edges AS
SELECT e.*
FROM memory.edges e
WHERE api.visible_collection(e.src_collection) AND api.visible_collection(e.dst_collection)
"""

_VISIBLE_EDGES_COMMENT = (
    "COMMENT ON VIEW memory.visible_edges IS "
    "'memory.edges with both endpoints visible to this session, the relation the recursive-CTE "
    "graph walk reads so a private collection never surfaces as a neighbour id. Ungranted, like "
    "every other relation in this schema.'"
)

_SWITCHES_COMMENT = (
    "COMMENT ON FUNCTION api.retrieval_switches(text) IS "
    "'The compiled retrieval switches for one collection (embeddings/sql/graph, each boolean, plus "
    "library: false when the collection keeps out of the shared library), or an empty object when "
    "that collection has never compiled or is not visible to this session. The one reader-granted "
    "door onto compile_fingerprint -- the table itself stays ungranted. Example: "
    "SELECT api.retrieval_switches(''research'');'"
)

_OLD_SWITCHES_COMMENT = (
    "COMMENT ON FUNCTION api.retrieval_switches(text) IS "
    "'The compiled retrieval switches (embeddings/sql/graph, each boolean) for one collection, or "
    "an empty object when that collection has never compiled. The one reader-granted door onto "
    "compile_fingerprint -- the table itself stays ungranted. Example: "
    "SELECT api.retrieval_switches(''research'');'"
)


# -- the api views, each in its pre-0037 and its guarded form ----------------------------------
#
# Every statement is a CREATE OR REPLACE: the guard only ever adds a predicate, so each view keeps
# its column list, and dependents (api.cards is read by the graph walk) keep their dependency.

_ABOUT = """
CREATE OR REPLACE VIEW api.about AS
SELECT p.collection, p.card_text AS body
FROM memory.pages p
WHERE p.id = (CASE WHEN p.collection = 'research' THEN 'meta.about' ELSE 'meta.about@' || p.collection END){guard}
"""

_AUTHORING_RULES = """
CREATE OR REPLACE VIEW api.authoring_rules AS
SELECT p.collection, p.card_text AS body
FROM memory.pages p
WHERE p.id = (CASE WHEN p.collection = 'research' THEN 'meta.authoring' ELSE 'meta.authoring@' || p.collection END){guard}
"""

_CARDS = """
CREATE OR REPLACE VIEW api.cards AS
SELECT p.id,
       p.collection AS src_collection,
       p.kind AS type,
       p.title,
       p.summary,
       p.status,
       p.superseded_by IS NOT NULL AS superseded,
       p.frontmatter ->> 'entity_type' AS entity_type,
       p.frontmatter ->> 'paper_type' AS paper_type,
       p.frontmatter ->> 'maturity' AS maturity,
       CASE WHEN (p.frontmatter ->> 'year') ~ '^-?[0-9]+$' THEN (p.frontmatter ->> 'year')::integer END AS year,
       COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets -> 'tasks')), ARRAY[]::text[]) AS tasks,
       COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets -> 'domains')), ARRAY[]::text[]) AS domains,
       COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets -> 'stages')), ARRAY[]::text[]) AS stages,
       (SELECT count(*) FROM memory.claims c WHERE c.collection = p.collection AND c.page_id = p.id) AS n_claims,
       (SELECT count(*) FROM memory.edges e WHERE e.src_collection = p.collection AND e.src_id = p.id) AS out_degree,
       (SELECT count(*) FROM memory.edges e WHERE e.dst_collection = p.collection AND e.dst_id = p.id) AS in_degree,
       p.updated,
       p.frontmatter
FROM memory.pages p{where_guard}
"""

_CLAIMS = """
CREATE OR REPLACE VIEW api.claims AS
SELECT c.id AS claim_id,
       c.collection,
       c.page_id AS paper_id,
       p.title AS paper_title,
       CASE WHEN (p.frontmatter ->> 'year') ~ '^-?[0-9]+$' THEN (p.frontmatter ->> 'year')::integer END AS paper_year,
       subj.title AS subject_name,
       ds.title AS dataset_name,
       tk.title AS task_name,
       mt.title AS metric_name,
       c.text,
       c.quote,
       c.loc,
       c.kind,
       c.condition,
       c.magnitude,
       c.eval_setting,
       c.status
FROM memory.claims c
JOIN memory.pages p ON p.collection = c.collection AND p.id = c.page_id
LEFT JOIN memory.pages subj ON subj.collection = c.subject_collection AND subj.id = c.subject_id{subject_guard}
LEFT JOIN memory.claim_qualifiers qds ON qds.collection = c.collection AND qds.claim_id = c.id AND qds.qualifier = 'on_dataset' AND qds.ord = 0
LEFT JOIN memory.pages ds ON ds.collection = qds.target_collection AND ds.id = qds.target_id{dataset_guard}
LEFT JOIN memory.claim_qualifiers qtk ON qtk.collection = c.collection AND qtk.claim_id = c.id AND qtk.qualifier = 'on_task' AND qtk.ord = 0
LEFT JOIN memory.pages tk ON tk.collection = qtk.target_collection AND tk.id = qtk.target_id{task_guard}
LEFT JOIN memory.claim_qualifiers qmt ON qmt.collection = c.collection AND qmt.claim_id = c.id AND qmt.qualifier = 'metric' AND qmt.ord = 0
LEFT JOIN memory.pages mt ON mt.collection = qmt.target_collection AND mt.id = qmt.target_id{metric_guard}{where_guard}
"""

_CONTRADICTIONS = """
CREATE OR REPLACE VIEW api.contradictions AS
SELECT a.id AS claim_a,
       b.id AS claim_b,
       a.subject_id,
       subj.title AS subject_name,
       a.text AS text_a,
       b.text AS text_b,
       a.quote AS quote_a,
       b.quote AS quote_b,
       a.condition AS condition_a,
       b.condition AS condition_b
FROM memory.claims a
JOIN memory.claims b ON a.subject_id = b.subject_id AND a.id < b.id
LEFT JOIN memory.pages subj ON subj.id = a.subject_id{subject_guard}
WHERE a.status = 'disputed' AND b.status = 'disputed'{guard}
"""

_COVERAGE = """
CREATE OR REPLACE VIEW api.coverage AS
SELECT 'tasks'::text AS facet, t.value AS facet_value, p.kind AS page_type, count(*) AS n_pages
FROM memory.pages p, LATERAL jsonb_array_elements_text(p.facets -> 'tasks') t(value){where_guard}
GROUP BY t.value, p.kind
UNION ALL
SELECT 'stages'::text AS facet, s.value AS facet_value, p.kind AS page_type, count(*) AS n_pages
FROM memory.pages p, LATERAL jsonb_array_elements_text(p.facets -> 'stages') s(value){where_guard}
GROUP BY s.value, p.kind
UNION ALL
SELECT 'domains'::text AS facet, d.value AS facet_value, p.kind AS page_type, count(*) AS n_pages
FROM memory.pages p, LATERAL jsonb_array_elements_text(p.facets -> 'domains') d(value){where_guard}
GROUP BY d.value, p.kind
"""

_EDGES = """
CREATE OR REPLACE VIEW api.edges AS
SELECT e.src_collection,
       e.src_id,
       src.title AS src_title,
       e.rel,
       e.dst_collection,
       e.dst_id,
       dst.title AS dst_title,
       e.qualifiers,
       e.provenance_claim
FROM memory.edges e
LEFT JOIN memory.pages src ON src.collection = e.src_collection AND src.id = e.src_id
LEFT JOIN memory.pages dst ON dst.collection = e.dst_collection AND dst.id = e.dst_id{where_guard}
"""

_ENTITIES = """
CREATE OR REPLACE VIEW api.entities AS
SELECT p.id,
       p.title,
       p.frontmatter ->> 'entity_type' AS entity_type,
       p.summary,
       p.status,
       COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets -> 'tasks')), ARRAY[]::text[]) AS tasks,
       COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets -> 'domains')), ARRAY[]::text[]) AS domains,
       COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets -> 'stages')), ARRAY[]::text[]) AS stages,
       (SELECT count(*) FROM memory.edges e WHERE e.src_id = p.id{src_edge_guard}) AS out_degree,
       (SELECT count(*) FROM memory.edges e WHERE e.dst_id = p.id{dst_edge_guard}) AS in_degree,
       (SELECT count(*) FROM memory.claims c WHERE c.subject_id = p.id{claim_guard}) AS n_evidence,
       p.updated
FROM memory.pages p
WHERE p.kind = 'entity'{guard}
"""

_FACETS = """
CREATE OR REPLACE VIEW api.facets AS
SELECT p.id AS page_id, p.collection, f.facet, f.value
FROM memory.pages p
CROSS JOIN LATERAL (
  SELECT kv.key AS facet, el.v AS value
  FROM jsonb_each(p.facets) kv(key, value)
  CROSS JOIN LATERAL jsonb_array_elements_text(kv.value) el(v)
  UNION ALL
  SELECT kv.key, kv.value
  FROM jsonb_each_text(p.frontmatter) kv(key, value)
  WHERE jsonb_typeof(p.frontmatter -> kv.key) = ANY (ARRAY['string', 'number', 'boolean'])
  UNION ALL
  SELECT kv.key, el.v
  FROM jsonb_each(p.frontmatter) kv(key, value)
  CROSS JOIN LATERAL jsonb_array_elements_text(kv.value) el(v)
  WHERE jsonb_typeof(kv.value) = 'array'
) f
WHERE f.value IS NOT NULL{guard}
"""

_MATRIX = """
CREATE OR REPLACE VIEW api.matrix AS
SELECT cc.comparison_id,
       comp.title AS comparison_title,
       cc.row_entity,
       re.title AS row_entity_title,
       cc.dimension,
       cc.value,
       cc.claim_refs
FROM memory.comparison_cells cc
JOIN memory.pages comp ON comp.id = cc.comparison_id{comparison_guard}
JOIN memory.pages re ON re.id = cc.row_entity{row_guard}{where_guard}
"""

_PAGE_TYPES = """
CREATE OR REPLACE VIEW api.page_types AS
SELECT t.collection,
       t.kind,
       COALESCE(t.contract ->> 'id_prefix', '') AS id_prefix,
       t.contract ->> 'folder' AS folder,
       ARRAY(SELECT jsonb_array_elements_text(t.contract -> 'status_values')) AS status_values,
       ARRAY(
         SELECT CASE WHEN jsonb_typeof(elem.value) = 'object' THEN elem.value ->> 'slug' ELSE elem.value #>> '{{}}' END
         FROM jsonb_array_elements(t.contract -> 'canonical_sections') elem(value)
       ) AS canonical_sections,
       ARRAY(SELECT jsonb_array_elements_text(t.contract -> 'edge_fields')) AS edge_fields,
       COALESCE((t.contract ->> 'claims_enabled')::boolean, false) AS claims_enabled,
       COALESCE((t.contract ->> 'disclosed')::boolean, true) AS disclosed,
       COALESCE(t.contract -> 'frontmatter_fields', '[]'::jsonb) AS frontmatter_fields,
       t.contract
FROM memory.page_type_contracts t{where_guard}
"""

_PAPERS = """
CREATE OR REPLACE VIEW api.papers AS
SELECT p.id,
       p.title,
       CASE WHEN (p.frontmatter ->> 'year') ~ '^-?[0-9]+$' THEN (p.frontmatter ->> 'year')::integer END AS year,
       p.frontmatter ->> 'venue' AS venue,
       p.frontmatter ->> 'citekey' AS citekey,
       p.frontmatter ->> 'paper_type' AS paper_type,
       COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets -> 'tasks')), ARRAY[]::text[]) AS tasks,
       COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets -> 'domains')), ARRAY[]::text[]) AS domains,
       COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets -> 'stages')), ARRAY[]::text[]) AS stages,
       p.source_id,
       s.meta ->> 'doi' AS doi,
       s.meta ->> 'arxiv_id' AS arxiv_id,
       s.pdf_path,
       (SELECT count(*) FROM memory.claims c WHERE c.page_id = p.id{claim_guard}) AS n_claims,
       p.status,
       p.updated
FROM memory.pages p
LEFT JOIN memory.sources_raw s ON s.source_id = p.source_id{mirror_guard}
WHERE p.kind = 'paper'{guard}
"""

_RAW_CHUNKS = """
CREATE OR REPLACE VIEW api.raw_chunks AS
SELECT r.collection, r.source_id, r.section_path::text AS section_path, r.heading, r.body
FROM memory.raw_chunks r{where_guard}
"""

_SCHEMA_GUIDE = """
CREATE OR REPLACE VIEW api.schema_guide AS
SELECT v.collection, v.value AS surface, v.definition AS purpose, v.meta AS notes
FROM memory.vocab v
WHERE v.vocab = 'schema_guide'{guard}
"""

_SCOREBOARD = """
CREATE OR REPLACE VIEW api.scoreboard AS
SELECT p.id AS entity,
       p.collection AS entity_collection,
       p.title AS entity_title,
       COALESCE(ARRAY(SELECT jsonb_array_elements_text(p.facets -> 'stages')), ARRAY[]::text[]) AS stages,
       count(*) FILTER (WHERE c.kind = 'support') AS n_supports,
       count(*) FILTER (WHERE c.kind = 'refute') AS n_refutes,
       count(*) FILTER (WHERE c.kind = 'benchmark-result' AND c.status = 'active') AS n_wins,
       count(*) FILTER (WHERE c.kind = 'ablation') AS n_losses,
       count(*) FILTER (WHERE c.kind = 'null-result') AS n_null_results,
       count(DISTINCT c.page_id) AS n_papers,
       count(DISTINCT ds.target_id) FILTER (WHERE ds.target_id IS NOT NULL) AS datasets_covered,
       max(CASE WHEN (pp.frontmatter ->> 'year') ~ '^-?[0-9]+$' THEN (pp.frontmatter ->> 'year')::integer END) AS last_evidence_year
FROM memory.pages p
LEFT JOIN memory.claims c ON c.subject_collection = p.collection AND c.subject_id = p.id{claim_guard}
LEFT JOIN memory.pages pp ON pp.collection = c.collection AND pp.id = c.page_id
LEFT JOIN memory.claim_qualifiers ds ON ds.collection = c.collection AND ds.claim_id = c.id AND ds.qualifier = 'on_dataset'
WHERE p.kind = 'entity'{guard}
GROUP BY p.collection, p.id, p.title
"""

_SECTIONS = """
CREATE OR REPLACE VIEW api.sections AS
SELECT s.id AS section_id,
       s.collection,
       s.page_id,
       p.title AS page_title,
       p.kind AS page_kind,
       s.canon,
       s.path::text AS heading_path,
       s.body,
       s.tokens
FROM memory.sections s
JOIN memory.pages p ON p.collection = s.collection AND p.id = s.page_id{where_guard}
"""

_STALE = """
CREATE OR REPLACE VIEW api.stale AS
SELECT p.id,
       p.title,
       p.status,
       p.source_hash AS page_source_hash,
       s.source_hash AS mirror_source_hash,
       p.updated
FROM memory.pages p
JOIN memory.sources_raw s ON s.source_id = p.source_id{mirror_guard}
WHERE p.source_hash IS DISTINCT FROM s.source_hash{guard}
"""


def _view_statements(*, guarded: bool) -> list[str]:
    """Every api view's definition, with the visibility predicate woven in or left out.

    Args:
        guarded: `True` renders each view's guarded form (the upgrade), `False` its pre-0037 form
            (the downgrade). Both render the same column lists, so every statement stays a
            `CREATE OR REPLACE`.

    Returns:
        One `CREATE OR REPLACE VIEW` statement per api view, each a single SQL command.
    """

    def guard(expression: str, *, keyword: str = "AND") -> str:
        return f" {keyword} api.visible_collection({expression})" if guarded else ""

    def join_guard(expression: str) -> str:
        return f" AND api.visible_collection({expression})" if guarded else ""

    return [
        _ABOUT.format(guard=guard("p.collection")),
        _AUTHORING_RULES.format(guard=guard("p.collection")),
        _CARDS.format(where_guard=guard("p.collection", keyword="\nWHERE")),
        _CLAIMS.format(
            subject_guard=join_guard("subj.collection"),
            dataset_guard=join_guard("ds.collection"),
            task_guard=join_guard("tk.collection"),
            metric_guard=join_guard("mt.collection"),
            where_guard=guard("c.collection", keyword="\nWHERE"),
        ),
        _CONTRADICTIONS.format(subject_guard=join_guard("subj.collection"), guard=guard("a.collection") + guard("b.collection")),
        _COVERAGE.format(where_guard=guard("p.collection", keyword="\nWHERE")),
        _EDGES.format(where_guard=guard("e.src_collection", keyword="\nWHERE") + guard("e.dst_collection")),
        _ENTITIES.format(
            src_edge_guard=join_guard("e.src_collection"),
            dst_edge_guard=join_guard("e.dst_collection"),
            claim_guard=join_guard("c.collection"),
            guard=guard("p.collection"),
        ),
        _FACETS.format(guard=guard("p.collection")),
        _MATRIX.format(
            comparison_guard=join_guard("comp.collection"),
            row_guard=join_guard("re.collection"),
            where_guard=guard("cc.collection", keyword="\nWHERE"),
        ),
        _PAGE_TYPES.format(where_guard=guard("t.collection", keyword="\nWHERE")),
        _PAPERS.format(claim_guard=join_guard("c.collection"), mirror_guard=join_guard("s.collection"), guard=guard("p.collection")),
        _RAW_CHUNKS.format(where_guard=guard("r.collection", keyword="\nWHERE")),
        _SCHEMA_GUIDE.format(guard=guard("v.collection")),
        _SCOREBOARD.format(claim_guard=join_guard("c.collection"), guard=guard("p.collection")),
        _SECTIONS.format(where_guard=guard("s.collection", keyword="\nWHERE")),
        _STALE.format(mirror_guard=join_guard("s.collection"), guard=guard("p.collection")),
    ]


# -- the api read functions taking a collection ------------------------------------------------
#
# Each keeps its 0025 body and signature; the guard is one more conjunct, so an invisible
# collection answers the empty result a collection that does not exist already answers.

_COLLECTION_ABOUT = """
CREATE OR REPLACE FUNCTION api.collection_about(p_collection text)
  RETURNS TABLE(body text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT p.card_text AS body
    FROM memory.pages p
    WHERE p.id = (CASE WHEN p_collection = 'research' THEN 'meta.about' ELSE 'meta.about@' || p_collection END)
      AND p.collection = p_collection{guard}
$$
"""

_COLLECTION_AUTHORING_RULES = """
CREATE OR REPLACE FUNCTION api.collection_authoring_rules(p_collection text)
  RETURNS TABLE(body text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT p.card_text AS body
    FROM memory.pages p
    WHERE p.id = (CASE WHEN p_collection = 'research' THEN 'meta.authoring' ELSE 'meta.authoring@' || p_collection END)
      AND p.collection = p_collection{guard}
$$
"""

_COLLECTION_SCHEMA_GUIDE = """
CREATE OR REPLACE FUNCTION api.collection_schema_guide(p_collection text)
  RETURNS TABLE(surface text, purpose text, notes jsonb)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT v.value AS surface, v.definition AS purpose, v.meta AS notes
    FROM memory.vocab v
    WHERE v.vocab = 'schema_guide' AND v.collection = p_collection{guard}
$$
"""

_ENVELOPE_COLLECTION = """
CREATE OR REPLACE FUNCTION api.envelope_collection(p_collection text, p_excluded_kinds text[] DEFAULT ARRAY[]::text[])
  RETURNS TABLE(id text, kind text, title text, summary text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT p.id, p.kind, p.title,
           COALESCE(s.summary, p.summary, '') AS summary
    FROM memory.pages p
    LEFT JOIN memory.unit_summaries s ON s.unit_id = p.id AND s.status = 'active'
    WHERE p.collection = p_collection
      AND p.kind <> ALL(p_excluded_kinds){guard}
    ORDER BY p.title, p.id
$$
"""

_ENVELOPE_COLLECTION_GROUPS = """
CREATE OR REPLACE FUNCTION api.envelope_collection_groups(p_collection text, p_excluded_kinds text[])
  RETURNS TABLE(app text, pages bigint, sample_titles text[])
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT COALESCE(p.frontmatter ->> 'app', '') AS app,
           count(*) AS pages,
           (array_agg(p.title ORDER BY p.title))[1:3] AS sample_titles
    FROM memory.pages p
    WHERE p.collection = p_collection
      AND p.kind <> ALL(p_excluded_kinds){guard}
    GROUP BY 1
    ORDER BY 1
$$
"""

_ENVELOPE_COLLECTION_KINDS = """
CREATE OR REPLACE FUNCTION api.envelope_collection_kinds(p_collection text, p_excluded_kinds text[])
  RETURNS TABLE(kind text, pages bigint, sample_titles text[])
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT p.kind AS kind,
           count(*) AS pages,
           (array_agg(p.title ORDER BY p.title))[1:3] AS sample_titles
    FROM memory.pages p
    WHERE p.collection = p_collection
      AND p.kind <> ALL(p_excluded_kinds){guard}
    GROUP BY p.kind
    ORDER BY p.kind
$$
"""

_ENVELOPE_GROUP = """
CREATE OR REPLACE FUNCTION api.envelope_group(p_collection text, p_app text, p_excluded_kinds text[])
  RETURNS TABLE(id text, kind text, title text, summary text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT p.id, p.kind, p.title,
           COALESCE(s.summary, p.summary, '') AS summary
    FROM memory.pages p
    LEFT JOIN memory.unit_summaries s ON s.unit_id = p.id AND s.status = 'active'
    WHERE p.collection = p_collection
      AND p.kind <> ALL(p_excluded_kinds)
      AND COALESCE(p.frontmatter ->> 'app', '') = p_app{guard}
    ORDER BY p.title, p.id
$$
"""

_ENVELOPE_GROUP_KIND = """
CREATE OR REPLACE FUNCTION api.envelope_group_kind(p_collection text, p_app text, p_kind text, p_excluded_kinds text[])
  RETURNS TABLE(id text, kind text, title text, summary text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT p.id, p.kind, p.title,
           COALESCE(s.summary, p.summary, '') AS summary
    FROM memory.pages p
    LEFT JOIN memory.unit_summaries s ON s.unit_id = p.id AND s.status = 'active'
    WHERE p.collection = p_collection
      AND p.kind <> ALL(p_excluded_kinds)
      AND COALESCE(p.frontmatter ->> 'app', '') = p_app
      AND p.kind = p_kind{guard}
    ORDER BY p.title, p.id
$$
"""

_ENVELOPE_GROUP_KINDS = """
CREATE OR REPLACE FUNCTION api.envelope_group_kinds(p_collection text, p_app text, p_excluded_kinds text[])
  RETURNS TABLE(kind text, pages bigint, sample_titles text[])
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT p.kind AS kind,
           count(*) AS pages,
           (array_agg(p.title ORDER BY p.title))[1:3] AS sample_titles
    FROM memory.pages p
    WHERE p.collection = p_collection
      AND p.kind <> ALL(p_excluded_kinds)
      AND COALESCE(p.frontmatter ->> 'app', '') = p_app{guard}
    GROUP BY p.kind
    ORDER BY p.kind
$$
"""

_ENVELOPE_KIND = """
CREATE OR REPLACE FUNCTION api.envelope_kind(p_collection text, p_kind text, p_excluded_kinds text[])
  RETURNS TABLE(id text, kind text, title text, summary text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT p.id, p.kind, p.title,
           COALESCE(s.summary, p.summary, '') AS summary
    FROM memory.pages p
    LEFT JOIN memory.unit_summaries s ON s.unit_id = p.id AND s.status = 'active'
    WHERE p.collection = p_collection
      AND p.kind <> ALL(p_excluded_kinds)
      AND p.kind = p_kind{guard}
    ORDER BY p.title, p.id
$$
"""

_ENVELOPE_PAGE = """
CREATE OR REPLACE FUNCTION api.envelope_page(p_collection text, p_page_id text)
  RETURNS TABLE(row_kind text, id text, kind text, title text, ord integer, summary text, body text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    WITH page_size AS (
      SELECT COALESCE(sum(length(sec.body)), 0) AS chars
      FROM memory.sections sec
      WHERE sec.collection = p_collection AND sec.page_id = p_page_id
    )
    SELECT 'page'::text, p.id, p.kind, p.title, -1 AS ord,
           COALESCE(s.summary, p.summary, '') AS summary,
           NULL::text AS body
    FROM memory.pages p
    LEFT JOIN memory.unit_summaries s ON s.collection = p.collection AND s.unit_id = p.id AND s.status = 'active'
    WHERE p.collection = p_collection AND p.id = p_page_id{guard}
    UNION ALL
    SELECT 'section'::text, sec.id, 'section'::text, sec.heading, sec.ord AS ord,
           COALESCE(t.summary, sec.summary, '') AS summary,
           CASE WHEN page_size.chars <= 6000 THEN sec.body END AS body
    FROM memory.sections sec
    CROSS JOIN page_size
    LEFT JOIN memory.unit_summaries t ON t.collection = sec.collection AND t.unit_id = sec.id AND t.status = 'active'
    WHERE sec.collection = p_collection AND sec.page_id = p_page_id{guard}
    ORDER BY ord
$$
"""

_ENVELOPE_SECTION = """
CREATE OR REPLACE FUNCTION api.envelope_section(p_collection text, p_section_id text)
  RETURNS TABLE(id text, title text, body text, summary text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT sec.id, sec.heading, sec.body,
           COALESCE(s.summary, '') AS summary
    FROM memory.sections sec
    LEFT JOIN memory.unit_summaries s ON s.collection = sec.collection AND s.unit_id = sec.id AND s.status = 'active'
    WHERE sec.collection = p_collection AND sec.id = p_section_id{guard}
$$
"""

_FETCH = """
CREATE OR REPLACE FUNCTION api.fetch(p_collection text, unit_id text)
  RETURNS TABLE(id text, level text, title text, body text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT p.id, 'page'::text, p.title,
           CASE WHEN length(p.card_text) > 32000
                THEN left(p.card_text, 32000) || ' [truncated — fetch sections instead]'
                ELSE p.card_text END
    FROM memory.pages p WHERE p.collection = p_collection AND p.id = unit_id{guard}
    UNION ALL
    SELECT s.id, 'section', s.heading, s.body
    FROM memory.sections s WHERE s.collection = p_collection AND s.id = unit_id{guard}
    UNION ALL
    SELECT c.id, 'claim', c.kind, c.text
    FROM memory.claims c WHERE c.collection = p_collection AND c.id = unit_id{guard}
    UNION ALL
    SELECT r.id, 'raw', r.heading, r.body
    FROM memory.raw_chunks r WHERE r.collection = p_collection AND r.id = unit_id{guard}
$$
"""

_LOOKUP_VALUE = """
CREATE OR REPLACE FUNCTION api.lookup_value(p_collection text, facet text, term text DEFAULT NULL)
  RETURNS TABLE(value text, definition text, n_pages integer)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT v.value, v.definition,
           (SELECT count(*)::int FROM memory.pages p
            WHERE p.collection = p_collection
              AND ((v.vocab = 'entity_type' AND p.frontmatter ->> 'entity_type' = v.value)
               OR (v.vocab LIKE 'facet:%' AND (p.facets -> split_part(v.vocab, ':', 2)) ? v.value))) AS n_pages
    FROM memory.vocab v
    WHERE v.collection = p_collection
      AND (v.vocab = facet OR v.vocab = 'facet:' || facet)
      AND (term IS NULL OR v.value ILIKE '%' || term || '%'){guard}
    ORDER BY v.value
$$
"""

_RETRIEVAL_SWITCHES = """
CREATE OR REPLACE FUNCTION api.retrieval_switches(p_collection text)
  RETURNS jsonb
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT COALESCE(
      (SELECT f.switches FROM memory.compile_fingerprint f WHERE f.collection = p_collection{guard}),
      '{{}}'::jsonb
    )
$$
"""

_COLLECTION_FUNCTIONS = (
    _COLLECTION_ABOUT,
    _COLLECTION_AUTHORING_RULES,
    _COLLECTION_SCHEMA_GUIDE,
    _ENVELOPE_COLLECTION,
    _ENVELOPE_COLLECTION_GROUPS,
    _ENVELOPE_COLLECTION_KINDS,
    _ENVELOPE_GROUP,
    _ENVELOPE_GROUP_KIND,
    _ENVELOPE_GROUP_KINDS,
    _ENVELOPE_KIND,
    _ENVELOPE_PAGE,
    _ENVELOPE_SECTION,
    _FETCH,
    _LOOKUP_VALUE,
    _RETRIEVAL_SWITCHES,
)


def _function_statements(*, guarded: bool) -> list[str]:
    """Every collection-taking api read function, guarded (the upgrade) or bare (the downgrade)."""
    guard = " AND api.visible_collection(p_collection)" if guarded else ""
    return [body.format(guard=guard) for body in _COLLECTION_FUNCTIONS]


# -- api.search: the same generated body, over pools that see only visible collections ----------


def _search_schema(*, guarded: bool) -> SearchSchema:
    """The hybrid-search shape this revision creates, frozen against `memory.*`.

    Migration-local rather than `engram.retrieval.corpus_schema.MEMORY_CORPUS_SCHEMA`, the
    convention every revision that regenerates `api.search` follows: the constant tracks the
    library's current descriptor, which later revisions are free to change for unrelated reasons.

    Args:
        guarded: `True` adds the visibility predicate to both pools, `False` renders the pre-0037
            pools.

    Returns:
        The corpus schema the configured dialect's builder generates `api.search` from.
    """
    page_predicates = (
        "({collection} IS NULL OR p.collection = {collection})",
        ("({facets} = '{}'::jsonb OR NOT EXISTS (\n"
         "  SELECT 1 FROM jsonb_each({facets}) AS f(key, vals)\n"
         "  WHERE (p.facets -> f.key ?| ARRAY(SELECT jsonb_array_elements_text(f.vals))) IS NOT TRUE))"),
        "(NOT {only_current} OR (p.superseded_by IS NULL AND p.status <> 'stub'))",
    )
    raw_predicates = ("({collection} IS NULL OR sr.collection = {collection})",)
    if guarded:
        page_predicates = (*page_predicates, "api.visible_collection(p.collection)")
        raw_predicates = (*raw_predicates, "api.visible_collection(sr.collection)")
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
                predicates=page_predicates,
            ),
            FacetPool(
                name="raw_keys",
                table="memory.sources_raw",
                alias="sr",
                select_cols=("sr.source_id", "sr.title"),
                predicates=raw_predicates,
            ),
        ),
        facets=(
            Facet(name="level", sql_type="text"),
            Facet(name="facets", sql_type="jsonb", default="'{}'::jsonb"),
            Facet(name="collection", sql_type="text", default="'research'"),
            Facet(name="only_current", sql_type="boolean", default="true"),
        ),
        level_facet="level",
        collection_facet="collection",
        embedding_sql_type="halfvec(1024)",
    )


def _search_body(*, guarded: bool) -> str:
    """The full `CREATE FUNCTION api.search(...)` statement in the target database's BM25 grammar."""
    dialect_name = getattr(context.config.attributes.get("search_dialect"), "name", None)
    builder: ParadeDBHybridSearchBuilder | PgTextSearchHybridSearchBuilder
    if dialect_name == "pg_search":
        builder = ParadeDBHybridSearchBuilder()
    elif dialect_name == "pg_textsearch":
        builder = PgTextSearchHybridSearchBuilder()
    else:
        msg = f"search_dialect not configured (or unrecognised name {dialect_name!r}): alembic/env.py must stash a SearchDialect"
        raise RuntimeError(msg)
    return builder.api_search_body(_search_schema(guarded=guarded))


# Byte-identical to 0024's, the caller-facing contract this revision does not change: a collection
# invisible to the session already behaves like one that does not exist. Re-emitted on both paths
# because dropping the function to recreate it drops its COMMENT with it.
_SEARCH_COMMENT = (
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


# -- the graph walk: generated as before, reached through a guarded api wrapper -----------------


def _graph_schema(*, edge_table: str) -> GraphSchema:
    """The link-graph shape the traversal builds from (0026's shape, over `edge_table`)."""
    return GraphSchema(
        edge_table=edge_table,
        src_col="src_id",
        dst_col="dst_id",
        rel_col="rel",
        edge_payload_cols=("provenance_claim",),
        node_view="api.cards",
        node_cols=("id", "type", "title", "status", "superseded"),
        node_col_types=("text", "text", "text", "text", "boolean"),
        partition_col="src_collection",
    )


def _graph_backend_name() -> str:
    """The configured graph backend's name (`age` or `cte`), the way 0025 and 0026 read it."""
    backend_name = getattr(context.config.attributes.get("graph_backend"), "name", None)
    if backend_name in ("age", "cte"):
        return backend_name
    msg = f"graph_backend not configured (or unrecognised name {backend_name!r}): alembic/env.py must stash a GraphBackendSql"
    raise RuntimeError(msg)


def _graph_builder(backend_name: str) -> AgeGraphTraversalBuilder | CteGraphTraversalBuilder:
    """The traversal builder for one backend name (the AGE graph is `wiki_kg`, as 0026 named it)."""
    return AgeGraphTraversalBuilder("wiki_kg") if backend_name == "age" else CteGraphTraversalBuilder()


# The cypher walk keeps every hop inside `p_collection`, so it needs no edge-level visibility
# filter; the recursive-CTE walk crosses collections at its destination endpoint, so it reads the
# both-endpoints-visible view instead of the edge table.
_WALK_EDGE_TABLE = {"age": "memory.edges", "cte": "memory.visible_edges"}

# The two wrappers' volatility follows the walk they call: the CTE bodies are STABLE reads, the
# AGE bodies are VOLATILE (they run cypher through EXECUTE).
_WALK_VOLATILITY = {"age": "VOLATILE", "cte": "STABLE"}

_EXPAND_WRAPPER = """
CREATE FUNCTION api.graph_expand(
  p_collection text,
  seed_ids text[],
  hops integer DEFAULT 2,
  rel_types text[] DEFAULT NULL,
  k integer DEFAULT 20
)
  RETURNS TABLE(
    id text, type text, title text, status text, superseded boolean,
    src_id text, dst_id text, rel text, provenance_claim text
  )
  LANGUAGE sql {volatility} SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT w.*
    FROM memory.graph_expand_walk(p_collection, seed_ids, hops, rel_types, k) w
    WHERE api.visible_collection(p_collection)
$$
"""

_PATHS_WRAPPER = """
CREATE FUNCTION api.graph_paths(
  p_collection text,
  from_id text,
  to_id text,
  max_hops integer DEFAULT 4,
  rel_types text[] DEFAULT NULL,
  k integer DEFAULT 10
)
  RETURNS TABLE(
    id text, type text, title text, status text, superseded boolean,
    src_id text, dst_id text, rel text, provenance_claim text,
    path_rank integer, score double precision
  )
  LANGUAGE sql {volatility} SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT w.*
    FROM memory.graph_paths_walk(p_collection, from_id, to_id, max_hops, rel_types, k) w
    WHERE api.visible_collection(p_collection)
$$
"""

# Byte-identical to 0026's, the caller-facing contract this revision does not change.
_EXPAND_COMMENT = (
    "COMMENT ON FUNCTION api.graph_expand(text, text[], int, text[], int) IS "
    "'Typed-relation neighbourhood expansion, scoped to one collection: from one or more seed ids in "
    "p_collection, walk the link graph out to N hops (default 2), optionally filtered to rel_types, "
    "returning one row per reached node — its id/type/title/status/superseded plus the edge that "
    "reached it (src_id, dst_id, rel, provenance_claim; NULL on a seed). k caps how many rows come "
    "back (max 50). Example: SELECT * FROM api.graph_expand(''research'', ARRAY[''ent.graphrag''], hops => 2);'"
)
_PATHS_COMMENT = (
    "COMMENT ON FUNCTION api.graph_paths(text, text, text, int, text[], int) IS "
    "'Shortest typed-relation paths between two nodes, scoped to one collection: enumerate the paths "
    "from from_id to to_id within p_collection within max_hops (default 4), optionally filtered to "
    "rel_types, returning one row per node along each path — its id/type/title/status/superseded plus "
    "the edge into it (src_id, dst_id, rel, provenance_claim; NULL on the start node), the path_rank "
    "group and the path score. k caps the number of paths. Example: "
    "SELECT * FROM api.graph_paths(''research'', ''ent.graphrag'', ''ent.lightrag'');'"
)


def upgrade() -> None:
    """Add the visibility predicate to every api view, read function, search pool and graph walk."""
    op.execute(_VISIBLE_COLLECTION)
    op.execute(f"GRANT EXECUTE ON FUNCTION {_VISIBLE_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_VISIBLE_COMMENT)
    op.execute(_VISIBLE_EDGES)
    op.execute(_VISIBLE_EDGES_COMMENT)

    for statement in _view_statements(guarded=True):
        op.execute(statement)
    for statement in _function_statements(guarded=True):
        op.execute(statement)
    op.execute(_SWITCHES_COMMENT)

    op.execute(f"DROP FUNCTION {_SEARCH_SIGNATURE}")
    op.execute(_search_body(guarded=True))
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SEARCH_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_SEARCH_COMMENT)

    backend_name = _graph_backend_name()
    builder = _graph_builder(backend_name)
    schema = _graph_schema(edge_table=_WALK_EDGE_TABLE[backend_name])
    op.execute(f"DROP FUNCTION {_EXPAND_SIGNATURE}")
    op.execute(f"DROP FUNCTION {_PATHS_SIGNATURE}")
    op.execute(builder.expand_body(schema, function_name=_EXPAND_WALK))
    op.execute(builder.paths_body(schema, function_name=_PATHS_WALK))
    op.execute(_EXPAND_WRAPPER.format(volatility=_WALK_VOLATILITY[backend_name]))
    op.execute(_PATHS_WRAPPER.format(volatility=_WALK_VOLATILITY[backend_name]))
    op.execute(f"GRANT EXECUTE ON FUNCTION {_EXPAND_SIGNATURE} TO {_READER_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_PATHS_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_EXPAND_COMMENT)
    op.execute(_PATHS_COMMENT)


def downgrade() -> None:
    """Restore the unguarded views, functions, search body and graph functions."""
    backend_name = _graph_backend_name()
    builder = _graph_builder(backend_name)
    op.execute(f"DROP FUNCTION {_EXPAND_SIGNATURE}")
    op.execute(f"DROP FUNCTION {_PATHS_SIGNATURE}")
    op.execute(f"DROP FUNCTION {_EXPAND_WALK_SIGNATURE}")
    op.execute(f"DROP FUNCTION {_PATHS_WALK_SIGNATURE}")
    schema = _graph_schema(edge_table="memory.edges")
    op.execute(builder.expand_body(schema))
    op.execute(builder.paths_body(schema))
    op.execute(f"GRANT EXECUTE ON FUNCTION {_EXPAND_SIGNATURE} TO {_READER_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_PATHS_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_EXPAND_COMMENT)
    op.execute(_PATHS_COMMENT)

    op.execute(f"DROP FUNCTION {_SEARCH_SIGNATURE}")
    op.execute(_search_body(guarded=False))
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SEARCH_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_SEARCH_COMMENT)

    for statement in _function_statements(guarded=False):
        op.execute(statement)
    op.execute(_OLD_SWITCHES_COMMENT)
    for statement in _view_statements(guarded=False):
        op.execute(statement)

    op.execute("DROP VIEW memory.visible_edges")
    op.execute(f"DROP FUNCTION {_VISIBLE_SIGNATURE}")
