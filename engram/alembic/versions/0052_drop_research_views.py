"""The research views and the research columns of cards and claims go: engram names no knowledge base's page types or facets.

Revision ID: 0052
Revises: 0051
Create Date: 2026-10-07 00:00:00.000000

`api.papers`, `api.scoreboard`, `api.entities` and `api.coverage` are dropped. Each named one knowledge base's page
types, frontmatter keys, facets or claim kinds. `api.cards` loses `entity_type`, `paper_type`, `maturity`, `year`,
`tasks`, `domains` and `stages`. A page's frontmatter stays in its `frontmatter` column and its facet values in
`api.facets`. `api.claims` names its owning page `page_id` and `page_title` and loses `paper_year`. A view cannot
drop or rename a column in place, so both are dropped and created again, with their comments and the reader's
grant. Downgrade puts all six views, their comments and their grants back as they stood at 0051.
"""

from __future__ import annotations

from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "kb_reader"

_RESEARCH_VIEWS = ("api.papers", "api.scoreboard", "api.entities", "api.coverage")

_CARDS = """
CREATE VIEW api.cards AS
 SELECT id,
    collection AS src_collection,
    page_type AS type,
    title,
    summary,
    status,
    (superseded_by IS NOT NULL) AS superseded,
    ( SELECT count(*) AS count
           FROM kb.claims c
          WHERE ((c.collection = p.collection) AND (c.page_id = p.id))) AS n_claims,
    ( SELECT count(*) AS count
           FROM kb.edges e
          WHERE ((e.src_collection = p.collection) AND (e.src_id = p.id))) AS out_degree,
    ( SELECT count(*) AS count
           FROM kb.edges e
          WHERE ((e.dst_collection = p.collection) AND (e.dst_id = p.id))) AS in_degree,
    updated,
    frontmatter
   FROM kb.pages p
  WHERE api.visible_collection(collection)
"""

_CLAIMS = """
CREATE VIEW api.claims AS
 SELECT c.id AS claim_id,
    c.collection,
    c.page_id,
    p.title AS page_title,
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
   FROM ((((((((kb.claims c
     JOIN kb.pages p ON (((p.collection = c.collection) AND (p.id = c.page_id))))
     LEFT JOIN kb.pages subj ON (((subj.collection = c.subject_collection) AND (subj.id = c.subject_id) AND api.visible_collection(subj.collection))))
     LEFT JOIN kb.claim_qualifiers qds ON (((qds.collection = c.collection) AND (qds.claim_id = c.id) AND (qds.qualifier = 'on_dataset'::text) AND (qds.ord = 0))))
     LEFT JOIN kb.pages ds ON (((ds.collection = qds.target_collection) AND (ds.id = qds.target_id) AND api.visible_collection(ds.collection))))
     LEFT JOIN kb.claim_qualifiers qtk ON (((qtk.collection = c.collection) AND (qtk.claim_id = c.id) AND (qtk.qualifier = 'on_task'::text) AND (qtk.ord = 0))))
     LEFT JOIN kb.pages tk ON (((tk.collection = qtk.target_collection) AND (tk.id = qtk.target_id) AND api.visible_collection(tk.collection))))
     LEFT JOIN kb.claim_qualifiers qmt ON (((qmt.collection = c.collection) AND (qmt.claim_id = c.id) AND (qmt.qualifier = 'metric'::text) AND (qmt.ord = 0))))
     LEFT JOIN kb.pages mt ON (((mt.collection = qmt.target_collection) AND (mt.id = qmt.target_id) AND api.visible_collection(mt.collection))))
  WHERE api.visible_collection(c.collection)
"""

_CARDS_COMMENTS = (
    (
        "COMMENT ON VIEW api.cards IS 'L1 card scan (~50-150 tokens/row): one compact row per page across all 8 types. "
        "Filter by type, exclude stubs/superseded, order by updated. Example: SELECT id, title, summary FROM api.cards WHERE "
        "type = ''playbook'' AND NOT superseded LIMIT 20;'"
    ),
    (
        "COMMENT ON COLUMN api.cards.id IS 'Page id \u2014 unique only paired with src_collection. Example value: "
        "''paper.lightrag-2025''.'"
    ),
    (
        "COMMENT ON COLUMN api.cards.src_collection IS 'This page''s own collection. Named to match kb.edges.src_collection "
        "\u2014 api.graph_expand/api.graph_paths join this view on that pair. Example value: ''research''.'"
    ),
    "COMMENT ON COLUMN api.cards.type IS 'Page type: paper|entity|concept|comparison|playbook|question|moc|meta.'",
    "COMMENT ON COLUMN api.cards.title IS 'Human-readable page title.'",
    "COMMENT ON COLUMN api.cards.summary IS 'One-paragraph summary (<= 280 chars), or NULL for stubs.'",
    "COMMENT ON COLUMN api.cards.status IS 'Lifecycle: stub|draft|synthesized|verified.'",
    "COMMENT ON COLUMN api.cards.superseded IS 'boolean \u2014 true when a newer page supersedes this one.'",
    "COMMENT ON COLUMN api.cards.n_claims IS 'int \u2014 number of claim blocks on this page.'",
    "COMMENT ON COLUMN api.cards.out_degree IS 'int \u2014 outgoing typed edges from this page.'",
    "COMMENT ON COLUMN api.cards.in_degree IS 'int \u2014 incoming typed edges to this page.'",
    "COMMENT ON COLUMN api.cards.updated IS 'date \u2014 last edit date of the source note.'",
    (
        "COMMENT ON COLUMN api.cards.frontmatter IS 'jsonb: the page''s long-tail frontmatter as a document (the same keys "
        "api.facets unpivots, plus values deeper than a scalar). Declared link and link-list fields (the page type''s "
        "edge_fields in api.page_types, e.g. a flag''s about or resolved_by) are NOT here: each is a row in api.edges with "
        "rel = the field name, filter on src_id. Example: SELECT frontmatter->''priority'' FROM api.cards WHERE id = "
        "''ent.lightrag'';'"
    ),
)

_CLAIMS_COMMENTS = (
    (
        "COMMENT ON VIEW api.claims IS 'Joined, human-readable claims: owning page title and subject/dataset/task/metric "
        "NAMES (never bare ids), plus quote+loc for L4 verification. Example: SELECT page_title, text, quote, loc FROM "
        "api.claims WHERE claim_id = ''paper.lightrag-2025#c1'';'"
    ),
    (
        "COMMENT ON COLUMN api.claims.claim_id IS 'Claim id ''<page_id>#cN'' \u2014 unique only paired with collection. Example "
        "value: ''paper.lightrag-2025#c1''.'"
    ),
    (
        "COMMENT ON COLUMN api.claims.collection IS 'This claim''s own collection (its owning page''s collection). Example "
        "value: ''research''.'"
    ),
    "COMMENT ON COLUMN api.claims.page_id IS 'Owning page id.'",
    "COMMENT ON COLUMN api.claims.page_title IS 'Owning page title.'",
    "COMMENT ON COLUMN api.claims.subject_name IS 'Name of the entity the claim is about. Example value: ''LightRAG''.'",
    "COMMENT ON COLUMN api.claims.dataset_name IS 'Dataset name the result is on. Example value: ''HotpotQA''.'",
    "COMMENT ON COLUMN api.claims.task_name IS 'Task name. Example value: ''multi-hop-qa''.'",
    "COMMENT ON COLUMN api.claims.metric_name IS 'Metric name. Example value: ''F1''.'",
    "COMMENT ON COLUMN api.claims.text IS 'The atomic claim sentence.'",
    "COMMENT ON COLUMN api.claims.quote IS 'Verbatim source snippet supporting the claim (may be NULL).'",
    "COMMENT ON COLUMN api.claims.loc IS 'Raw locator into the source. Example value: ''\u00a75.1, Table 2''.'",
    "COMMENT ON COLUMN api.claims.kind IS 'Claim type (vocabulary). Example value: ''benchmark-result''.'",
    "COMMENT ON COLUMN api.claims.condition IS 'Qualifying condition. Example value: ''zero-shot''.'",
    "COMMENT ON COLUMN api.claims.magnitude IS 'Effect size text. Example value: ''+4.2 F1''.'",
    "COMMENT ON COLUMN api.claims.eval_setting IS 'Evaluation setting. Example value: ''closed-book''.'",
    "COMMENT ON COLUMN api.claims.status IS 'Claim status: active|disputed|superseded|retracted-source.'",
)

# The six views as they stood at 0051, in the order the downgrade creates them.

_PREVIOUS_VIEWS = (
    """
CREATE VIEW api.cards AS
 SELECT id,
    collection AS src_collection,
    page_type AS type,
    title,
    summary,
    status,
    (superseded_by IS NOT NULL) AS superseded,
    (frontmatter ->> 'entity_type'::text) AS entity_type,
    (frontmatter ->> 'paper_type'::text) AS paper_type,
    (frontmatter ->> 'maturity'::text) AS maturity,
        CASE
            WHEN ((frontmatter ->> 'year'::text) ~ '^-?[0-9]+$'::text) THEN ((frontmatter ->> 'year'::text))::integer
            ELSE NULL::integer
        END AS year,
    COALESCE(ARRAY( SELECT jsonb_array_elements_text((p.facets -> 'tasks'::text)) AS jsonb_array_elements_text), ARRAY[]::text[]) AS tasks,
    COALESCE(ARRAY( SELECT jsonb_array_elements_text((p.facets -> 'domains'::text)) AS jsonb_array_elements_text), ARRAY[]::text[]) AS domains,
    COALESCE(ARRAY( SELECT jsonb_array_elements_text((p.facets -> 'stages'::text)) AS jsonb_array_elements_text), ARRAY[]::text[]) AS stages,
    ( SELECT count(*) AS count
           FROM kb.claims c
          WHERE ((c.collection = p.collection) AND (c.page_id = p.id))) AS n_claims,
    ( SELECT count(*) AS count
           FROM kb.edges e
          WHERE ((e.src_collection = p.collection) AND (e.src_id = p.id))) AS out_degree,
    ( SELECT count(*) AS count
           FROM kb.edges e
          WHERE ((e.dst_collection = p.collection) AND (e.dst_id = p.id))) AS in_degree,
    updated,
    frontmatter
   FROM kb.pages p
  WHERE api.visible_collection(collection)
""",
    """
CREATE VIEW api.claims AS
 SELECT c.id AS claim_id,
    c.collection,
    c.page_id AS paper_id,
    p.title AS paper_title,
        CASE
            WHEN ((p.frontmatter ->> 'year'::text) ~ '^-?[0-9]+$'::text) THEN ((p.frontmatter ->> 'year'::text))::integer
            ELSE NULL::integer
        END AS paper_year,
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
   FROM ((((((((kb.claims c
     JOIN kb.pages p ON (((p.collection = c.collection) AND (p.id = c.page_id))))
     LEFT JOIN kb.pages subj ON (((subj.collection = c.subject_collection) AND (subj.id = c.subject_id) AND api.visible_collection(subj.collection))))
     LEFT JOIN kb.claim_qualifiers qds ON (((qds.collection = c.collection) AND (qds.claim_id = c.id) AND (qds.qualifier = 'on_dataset'::text) AND (qds.ord = 0))))
     LEFT JOIN kb.pages ds ON (((ds.collection = qds.target_collection) AND (ds.id = qds.target_id) AND api.visible_collection(ds.collection))))
     LEFT JOIN kb.claim_qualifiers qtk ON (((qtk.collection = c.collection) AND (qtk.claim_id = c.id) AND (qtk.qualifier = 'on_task'::text) AND (qtk.ord = 0))))
     LEFT JOIN kb.pages tk ON (((tk.collection = qtk.target_collection) AND (tk.id = qtk.target_id) AND api.visible_collection(tk.collection))))
     LEFT JOIN kb.claim_qualifiers qmt ON (((qmt.collection = c.collection) AND (qmt.claim_id = c.id) AND (qmt.qualifier = 'metric'::text) AND (qmt.ord = 0))))
     LEFT JOIN kb.pages mt ON (((mt.collection = qmt.target_collection) AND (mt.id = qmt.target_id) AND api.visible_collection(mt.collection))))
  WHERE api.visible_collection(c.collection)
""",
    """
CREATE VIEW api.papers AS
 SELECT p.id,
    p.title,
        CASE
            WHEN ((p.frontmatter ->> 'year'::text) ~ '^-?[0-9]+$'::text) THEN ((p.frontmatter ->> 'year'::text))::integer
            ELSE NULL::integer
        END AS year,
    (p.frontmatter ->> 'venue'::text) AS venue,
    (p.frontmatter ->> 'citekey'::text) AS citekey,
    (p.frontmatter ->> 'paper_type'::text) AS paper_type,
    COALESCE(ARRAY( SELECT jsonb_array_elements_text((p.facets -> 'tasks'::text)) AS jsonb_array_elements_text), ARRAY[]::text[]) AS tasks,
    COALESCE(ARRAY( SELECT jsonb_array_elements_text((p.facets -> 'domains'::text)) AS jsonb_array_elements_text), ARRAY[]::text[]) AS domains,
    COALESCE(ARRAY( SELECT jsonb_array_elements_text((p.facets -> 'stages'::text)) AS jsonb_array_elements_text), ARRAY[]::text[]) AS stages,
    p.source_id,
    (s.meta ->> 'doi'::text) AS doi,
    (s.meta ->> 'arxiv_id'::text) AS arxiv_id,
    s.pdf_path,
    ( SELECT count(*) AS count
           FROM kb.claims c
          WHERE ((c.page_id = p.id) AND api.visible_collection(c.collection))) AS n_claims,
    p.status,
    p.updated
   FROM (kb.pages p
     LEFT JOIN kb.sources_raw s ON (((s.source_id = p.source_id) AND api.visible_collection(s.collection))))
  WHERE ((p.page_type = 'paper'::text) AND api.visible_collection(p.collection))
""",
    """
CREATE VIEW api.scoreboard AS
 SELECT p.id AS entity,
    p.collection AS entity_collection,
    p.title AS entity_title,
    COALESCE(ARRAY( SELECT jsonb_array_elements_text((p.facets -> 'stages'::text)) AS jsonb_array_elements_text), ARRAY[]::text[]) AS stages,
    count(*) FILTER (WHERE (c.kind = 'support'::text)) AS n_supports,
    count(*) FILTER (WHERE (c.kind = 'refute'::text)) AS n_refutes,
    count(*) FILTER (WHERE ((c.kind = 'benchmark-result'::text) AND (c.status = 'active'::text))) AS n_wins,
    count(*) FILTER (WHERE (c.kind = 'ablation'::text)) AS n_losses,
    count(*) FILTER (WHERE (c.kind = 'null-result'::text)) AS n_null_results,
    count(DISTINCT c.page_id) AS n_papers,
    count(DISTINCT ds.target_id) FILTER (WHERE (ds.target_id IS NOT NULL)) AS datasets_covered,
    max(
        CASE
            WHEN ((pp.frontmatter ->> 'year'::text) ~ '^-?[0-9]+$'::text) THEN ((pp.frontmatter ->> 'year'::text))::integer
            ELSE NULL::integer
        END) AS last_evidence_year
   FROM (((kb.pages p
     LEFT JOIN kb.claims c ON (((c.subject_collection = p.collection) AND (c.subject_id = p.id) AND api.visible_collection(c.collection))))
     LEFT JOIN kb.pages pp ON (((pp.collection = c.collection) AND (pp.id = c.page_id))))
     LEFT JOIN kb.claim_qualifiers ds ON (((ds.collection = c.collection) AND (ds.claim_id = c.id) AND (ds.qualifier = 'on_dataset'::text))))
  WHERE ((p.page_type = 'entity'::text) AND api.visible_collection(p.collection))
  GROUP BY p.collection, p.id, p.title
""",
    """
CREATE VIEW api.entities AS
 SELECT id,
    title,
    (frontmatter ->> 'entity_type'::text) AS entity_type,
    summary,
    status,
    COALESCE(ARRAY( SELECT jsonb_array_elements_text((p.facets -> 'tasks'::text)) AS jsonb_array_elements_text), ARRAY[]::text[]) AS tasks,
    COALESCE(ARRAY( SELECT jsonb_array_elements_text((p.facets -> 'domains'::text)) AS jsonb_array_elements_text), ARRAY[]::text[]) AS domains,
    COALESCE(ARRAY( SELECT jsonb_array_elements_text((p.facets -> 'stages'::text)) AS jsonb_array_elements_text), ARRAY[]::text[]) AS stages,
    ( SELECT count(*) AS count
           FROM kb.edges e
          WHERE ((e.src_id = p.id) AND api.visible_collection(e.src_collection))) AS out_degree,
    ( SELECT count(*) AS count
           FROM kb.edges e
          WHERE ((e.dst_id = p.id) AND api.visible_collection(e.dst_collection))) AS in_degree,
    ( SELECT count(*) AS count
           FROM kb.claims c
          WHERE ((c.subject_id = p.id) AND api.visible_collection(c.collection))) AS n_evidence,
    updated
   FROM kb.pages p
  WHERE ((page_type = 'entity'::text) AND api.visible_collection(collection))
""",
    """
CREATE VIEW api.coverage AS
 SELECT 'tasks'::text AS facet,
    t.value AS facet_value,
    p.page_type,
    count(*) AS n_pages
   FROM kb.pages p,
    LATERAL jsonb_array_elements_text((p.facets -> 'tasks'::text)) t(value)
  WHERE api.visible_collection(p.collection)
  GROUP BY t.value, p.page_type
UNION ALL
 SELECT 'stages'::text AS facet,
    s.value AS facet_value,
    p.page_type,
    count(*) AS n_pages
   FROM kb.pages p,
    LATERAL jsonb_array_elements_text((p.facets -> 'stages'::text)) s(value)
  WHERE api.visible_collection(p.collection)
  GROUP BY s.value, p.page_type
UNION ALL
 SELECT 'domains'::text AS facet,
    d.value AS facet_value,
    p.page_type,
    count(*) AS n_pages
   FROM kb.pages p,
    LATERAL jsonb_array_elements_text((p.facets -> 'domains'::text)) d(value)
  WHERE api.visible_collection(p.collection)
  GROUP BY d.value, p.page_type
""",
)

_PREVIOUS_COMMENTS = (
    (
        "COMMENT ON VIEW api.cards IS 'L1 card scan (~50-150 tokens/row): one compact row per page across all 8 types. "
        "Filter by type + facet arrays, exclude stubs/superseded, order by updated. Example: SELECT id, title, summary FROM "
        "api.cards WHERE type = ''playbook'' AND ''long-doc-qa'' = ANY(tasks) AND NOT superseded LIMIT 20;'"
    ),
    (
        "COMMENT ON COLUMN api.cards.id IS 'Page id \u2014 unique only paired with src_collection. Example value: "
        "''paper.lightrag-2025''.'"
    ),
    (
        "COMMENT ON COLUMN api.cards.src_collection IS 'This page''s own collection. Named to match kb.edges.src_collection "
        "\u2014 api.graph_expand/api.graph_paths join this view on that pair. Example value: ''research''.'"
    ),
    "COMMENT ON COLUMN api.cards.type IS 'Page type: paper|entity|concept|comparison|playbook|question|moc|meta.'",
    "COMMENT ON COLUMN api.cards.title IS 'Human-readable page title.'",
    "COMMENT ON COLUMN api.cards.summary IS 'One-paragraph summary (<= 280 chars), or NULL for stubs.'",
    "COMMENT ON COLUMN api.cards.status IS 'Lifecycle: stub|draft|synthesized|verified.'",
    "COMMENT ON COLUMN api.cards.superseded IS 'boolean \u2014 true when a newer page supersedes this one.'",
    "COMMENT ON COLUMN api.cards.entity_type IS 'For entities: the registry entity_type. Example value: ''technique''.'",
    "COMMENT ON COLUMN api.cards.paper_type IS 'For papers: e.g. ''empirical''|''survey''|''system''.'",
    "COMMENT ON COLUMN api.cards.maturity IS 'For playbooks: e.g. ''experimental''|''established''.'",
    "COMMENT ON COLUMN api.cards.year IS 'Publication year (papers). Example value: 2025.'",
    "COMMENT ON COLUMN api.cards.tasks IS 'text[] facet \u2014 task tags. Example value: {long-doc-qa,multi-hop-qa}.'",
    "COMMENT ON COLUMN api.cards.domains IS 'text[] facet \u2014 domain tags. Example value: {biomedical}.'",
    "COMMENT ON COLUMN api.cards.stages IS 'text[] facet \u2014 pipeline stage tags. Example value: {retrieval,indexing}.'",
    "COMMENT ON COLUMN api.cards.n_claims IS 'int \u2014 number of claim blocks on this page.'",
    "COMMENT ON COLUMN api.cards.out_degree IS 'int \u2014 outgoing typed edges from this page.'",
    "COMMENT ON COLUMN api.cards.in_degree IS 'int \u2014 incoming typed edges to this page.'",
    "COMMENT ON COLUMN api.cards.updated IS 'date \u2014 last edit date of the source note.'",
    (
        "COMMENT ON COLUMN api.cards.frontmatter IS 'jsonb: the page''s long-tail frontmatter as a document (the same keys "
        "api.facets unpivots, plus values deeper than a scalar). Declared link and link-list fields (the page type''s "
        "edge_fields in api.page_types, e.g. a flag''s about or resolved_by) are NOT here: each is a row in api.edges with "
        "rel = the field name, filter on src_id. Example: SELECT frontmatter->''priority'' FROM api.cards WHERE id = "
        "''ent.lightrag'';'"
    ),
    (
        "COMMENT ON VIEW api.claims IS 'Joined, human-readable claims: paper title+year and subject/dataset/task/metric "
        "NAMES (never bare ids), plus quote+loc for L4 verification. Example: SELECT paper_title, text, quote, loc FROM "
        "api.claims WHERE claim_id = ''paper.lightrag-2025#c1'';'"
    ),
    (
        "COMMENT ON COLUMN api.claims.claim_id IS 'Claim id ''<page_id>#cN'' \u2014 unique only paired with collection. Example "
        "value: ''paper.lightrag-2025#c1''.'"
    ),
    (
        "COMMENT ON COLUMN api.claims.collection IS 'This claim''s own collection (its owning page''s collection). Example "
        "value: ''research''.'"
    ),
    "COMMENT ON COLUMN api.claims.paper_id IS 'Owning paper page id.'",
    "COMMENT ON COLUMN api.claims.paper_title IS 'Owning paper title.'",
    "COMMENT ON COLUMN api.claims.paper_year IS 'Owning paper year. Example value: 2025.'",
    "COMMENT ON COLUMN api.claims.subject_name IS 'Name of the entity the claim is about. Example value: ''LightRAG''.'",
    "COMMENT ON COLUMN api.claims.dataset_name IS 'Dataset name the result is on. Example value: ''HotpotQA''.'",
    "COMMENT ON COLUMN api.claims.task_name IS 'Task name. Example value: ''multi-hop-qa''.'",
    "COMMENT ON COLUMN api.claims.metric_name IS 'Metric name. Example value: ''F1''.'",
    "COMMENT ON COLUMN api.claims.text IS 'The atomic claim sentence.'",
    "COMMENT ON COLUMN api.claims.quote IS 'Verbatim source snippet supporting the claim (may be NULL).'",
    "COMMENT ON COLUMN api.claims.loc IS 'Raw locator into the source. Example value: ''\u00a75.1, Table 2''.'",
    "COMMENT ON COLUMN api.claims.kind IS 'Claim type (vocabulary). Example value: ''benchmark-result''.'",
    "COMMENT ON COLUMN api.claims.condition IS 'Qualifying condition. Example value: ''zero-shot''.'",
    "COMMENT ON COLUMN api.claims.magnitude IS 'Effect size text. Example value: ''+4.2 F1''.'",
    "COMMENT ON COLUMN api.claims.eval_setting IS 'Evaluation setting. Example value: ''closed-book''.'",
    "COMMENT ON COLUMN api.claims.status IS 'Claim status: active|disputed|superseded|retracted-source.'",
    (
        "COMMENT ON VIEW api.papers IS 'Bibliographic rows for papers: metadata + facets + source_id + artifact URLs + claim "
        "counts. Example: SELECT id, title, year, doi FROM api.papers WHERE year >= 2024 ORDER BY year DESC LIMIT 20;'"
    ),
    "COMMENT ON COLUMN api.papers.id IS 'Page id. Example value: ''paper.lightrag-2025''.'",
    "COMMENT ON COLUMN api.papers.title IS 'Paper title.'",
    "COMMENT ON COLUMN api.papers.year IS 'Publication year. Example value: 2025.'",
    "COMMENT ON COLUMN api.papers.venue IS 'Publication venue. Example value: ''EMNLP 2025''.'",
    "COMMENT ON COLUMN api.papers.citekey IS 'Citation key. Example value: ''guo2025lightrag''.'",
    "COMMENT ON COLUMN api.papers.paper_type IS 'e.g. ''empirical''|''survey''|''system''.'",
    "COMMENT ON COLUMN api.papers.tasks IS 'text[] task facet.'",
    "COMMENT ON COLUMN api.papers.domains IS 'text[] domain facet.'",
    "COMMENT ON COLUMN api.papers.stages IS 'text[] pipeline-stage facet.'",
    (
        "COMMENT ON COLUMN api.papers.source_id IS 'Raw-mirror source id linking to the source layer. Example value: "
        "''ABCD1234''.'"
    ),
    "COMMENT ON COLUMN api.papers.doi IS 'DOI. Example value: ''10.18653/v1/2025.emnlp-main.1''.'",
    "COMMENT ON COLUMN api.papers.arxiv_id IS 'arXiv id. Example value: ''2410.05779''.'",
    "COMMENT ON COLUMN api.papers.pdf_path IS 'Path to the mirrored PDF, if any.'",
    "COMMENT ON COLUMN api.papers.n_claims IS 'int \u2014 claim blocks extracted from this paper.'",
    "COMMENT ON COLUMN api.papers.status IS 'Lifecycle: stub|draft|synthesized|verified.'",
    "COMMENT ON COLUMN api.papers.updated IS 'date \u2014 last edit of the note.'",
    (
        "COMMENT ON VIEW api.scoreboard IS 'Computed trust per entity: support/refute/win/null-result counts, datasets "
        "covered, latest evidence year. Example: SELECT entity, n_supports, n_wins, last_evidence_year FROM api.scoreboard "
        "WHERE ''retrieval'' = ANY(stages) ORDER BY n_wins + n_supports DESC LIMIT 15;'"
    ),
    "COMMENT ON COLUMN api.scoreboard.entity IS 'Entity page id \u2014 unique only paired with entity_collection.'",
    "COMMENT ON COLUMN api.scoreboard.entity_collection IS 'The entity''s own collection. Example value: ''research''.'",
    "COMMENT ON COLUMN api.scoreboard.entity_title IS 'Entity name.'",
    "COMMENT ON COLUMN api.scoreboard.stages IS 'text[] pipeline-stage facet of the entity.'",
    "COMMENT ON COLUMN api.scoreboard.n_supports IS 'int \u2014 supporting claims.'",
    "COMMENT ON COLUMN api.scoreboard.n_refutes IS 'int \u2014 refuting claims.'",
    "COMMENT ON COLUMN api.scoreboard.n_wins IS 'int \u2014 active benchmark-result wins.'",
    "COMMENT ON COLUMN api.scoreboard.n_losses IS 'int \u2014 ablation/loss claims.'",
    "COMMENT ON COLUMN api.scoreboard.n_null_results IS 'int \u2014 null-result claims.'",
    "COMMENT ON COLUMN api.scoreboard.n_papers IS 'int \u2014 distinct papers contributing evidence.'",
    "COMMENT ON COLUMN api.scoreboard.datasets_covered IS 'int \u2014 distinct datasets in the evidence.'",
    "COMMENT ON COLUMN api.scoreboard.last_evidence_year IS 'int \u2014 most recent evidence year. Example value: 2025.'",
    (
        "COMMENT ON VIEW api.entities IS 'Entity cards with graph degree + evidence counts. Example: SELECT id, title, "
        "n_evidence FROM api.entities WHERE entity_type = ''technique'' ORDER BY n_evidence DESC LIMIT 20;'"
    ),
    "COMMENT ON COLUMN api.entities.id IS 'Entity page id. Example value: ''ent.graphrag''.'",
    "COMMENT ON COLUMN api.entities.title IS 'Entity name.'",
    "COMMENT ON COLUMN api.entities.entity_type IS 'Registry entity_type. Example value: ''technique''.'",
    "COMMENT ON COLUMN api.entities.summary IS 'One-paragraph summary.'",
    "COMMENT ON COLUMN api.entities.status IS 'Lifecycle: stub|draft|synthesized|verified.'",
    "COMMENT ON COLUMN api.entities.tasks IS 'text[] task facet.'",
    "COMMENT ON COLUMN api.entities.domains IS 'text[] domain facet.'",
    "COMMENT ON COLUMN api.entities.stages IS 'text[] pipeline-stage facet.'",
    "COMMENT ON COLUMN api.entities.out_degree IS 'int \u2014 outgoing typed edges.'",
    "COMMENT ON COLUMN api.entities.in_degree IS 'int \u2014 incoming typed edges.'",
    "COMMENT ON COLUMN api.entities.n_evidence IS 'int \u2014 claims whose subject is this entity.'",
    "COMMENT ON COLUMN api.entities.updated IS 'date \u2014 last edit of the note.'",
    (
        "COMMENT ON VIEW api.coverage IS 'The computed map: facet value x page type -> counts (what we know about X, and how "
        "much). Example: SELECT facet_value, page_type, n_pages FROM api.coverage WHERE facet = ''tasks'' ORDER BY n_pages "
        "DESC LIMIT 20;'"
    ),
    "COMMENT ON COLUMN api.coverage.facet IS 'Which facet: tasks|stages|domains.'",
    "COMMENT ON COLUMN api.coverage.facet_value IS 'A value of that facet. Example value: ''long-doc-qa''.'",
    "COMMENT ON COLUMN api.coverage.page_type IS 'Page type the count is grouped by.'",
    "COMMENT ON COLUMN api.coverage.n_pages IS 'int \u2014 pages with this facet value and type.'",
)



def upgrade() -> None:
    """Drop the four research views, then publish cards and claims without their research columns."""
    for view in _RESEARCH_VIEWS:
        op.execute(f"DROP VIEW {view}")
    op.execute("DROP VIEW api.cards")
    op.execute("DROP VIEW api.claims")
    op.execute(_CARDS)
    op.execute(_CLAIMS)
    for comment in (*_CARDS_COMMENTS, *_CLAIMS_COMMENTS):
        op.execute(comment)
    for view in ("api.cards", "api.claims"):
        op.execute(f"GRANT SELECT ON TABLE {view} TO {_READER_ROLE}")


def downgrade() -> None:
    """Put the six views, their comments and the reader's grants back as they stood at 0051."""
    op.execute("DROP VIEW api.cards")
    op.execute("DROP VIEW api.claims")
    for view in _PREVIOUS_VIEWS:
        op.execute(view)
    for comment in _PREVIOUS_COMMENTS:
        op.execute(comment)
    for view in ("api.cards", "api.claims", *_RESEARCH_VIEWS):
        op.execute(f"GRANT SELECT ON TABLE {view} TO {_READER_ROLE}")
