"""Every agent-read `api` comment describes the database in neutral words, with no knowledge base's examples.

Revision ID: 0053
Revises: 0052
Create Date: 2026-10-07 00:00:00.000000

The comments named one knowledge base's collection, page types, page ids, facets, claim kinds, datasets and
metrics in their examples. Each comment keeps its facts and takes placeholders such as `<collection>`,
`<page-id>` and `<facet>` in its examples. `api.cards.type` points at `api.page_types` for the page types.
Downgrade puts every comment back as it stood at 0052.
"""

from __future__ import annotations

from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

# One entry per rewritten comment: the COMMENT ON target, the comment at 0052, and the neutral comment.
_REWRITES: tuple[tuple[str, str, str], ...] = (
    (
        "VIEW api.about",
        (
            "L0 orientation for QUERYING -- one row per collection indexed into this database: what it covers, what it "
            "excludes, and its retrieval policy (how to route a question). Read once per session before querying. A "
            "single-knowledge-base database returns one row; a multi-knowledge-base database returns one per collection -- "
            "filter by collection, or call api.collection_about(p_collection) for just yours. Does NOT contain the "
            "authoring/ingestion contract -- that is api.authoring_rules. Example: SELECT body FROM api.about WHERE "
            "collection = 'research';"
        ),
        (
            "L0 orientation for QUERYING -- one row per collection indexed into this database: what it covers, what it "
            "excludes, and its retrieval policy (how to route a question). Read once per session before querying. A "
            "single-knowledge-base database returns one row; a multi-knowledge-base database returns one per collection -- "
            "filter by collection, or call api.collection_about(p_collection) for just yours. Does NOT contain the "
            "authoring/ingestion contract -- that is api.authoring_rules. Example: SELECT body FROM api.about WHERE "
            "collection = '<collection>';"
        ),
    ),
    (
        "COLUMN api.about.collection",
        "The collection this orientation row belongs to. Example value: 'research'.",
        "The collection this orientation row belongs to. Example value: '<collection>'.",
    ),
    (
        "COLUMN api.about.body",
        (
            "The orientation prose (markdown). Example value: 'This knowledge base indexes RAG/retrieval literature; start "
            "at api.cards, then api.search\u2026'."
        ),
        "The orientation prose (markdown).",
    ),
    (
        "VIEW api.authoring_rules",
        (
            "The contract for WRITING pages, one row per collection indexed into this database: frontmatter keys, page "
            "types, canonical sections, claims grammar, typed relations, lifecycle, ingest procedure, validation. Read "
            "this only when authoring or ingesting -- a querying agent needs api.about instead. A single-knowledge-base "
            "database returns one row; a multi-knowledge-base database returns one per collection -- filter by collection, "
            "or call api.collection_authoring_rules(p_collection) for just yours. Empty for a collection that declares no "
            "authoring document. Example: SELECT body FROM api.authoring_rules WHERE collection = 'research';"
        ),
        (
            "The contract for WRITING pages, one row per collection indexed into this database: frontmatter keys, page "
            "types, canonical sections, claims grammar, typed relations, lifecycle, ingest procedure, validation. Read "
            "this only when authoring or ingesting -- a querying agent needs api.about instead. A single-knowledge-base "
            "database returns one row; a multi-knowledge-base database returns one per collection -- filter by collection, "
            "or call api.collection_authoring_rules(p_collection) for just yours. Empty for a collection that declares no "
            "authoring document. Example: SELECT body FROM api.authoring_rules WHERE collection = '<collection>';"
        ),
    ),
    (
        "COLUMN api.authoring_rules.collection",
        "The collection this authoring-contract row belongs to. Example value: 'research'.",
        "The collection this authoring-contract row belongs to. Example value: '<collection>'.",
    ),
    (
        "VIEW api.cards",
        (
            "L1 card scan (~50-150 tokens/row): one compact row per page across all 8 types. Filter by type, exclude "
            "stubs/superseded, order by updated. Example: SELECT id, title, summary FROM api.cards WHERE type = 'playbook' "
            "AND NOT superseded LIMIT 20;"
        ),
        (
            "L1 card scan (~50-150 tokens/row): one compact row per page across every page type. Filter by type, exclude "
            "stubs/superseded, order by updated. Example: SELECT id, title, summary FROM api.cards WHERE type = "
            "'<page-type>' AND NOT superseded LIMIT 20;"
        ),
    ),
    (
        "COLUMN api.cards.id",
        "Page id \u2014 unique only paired with src_collection. Example value: 'paper.lightrag-2025'.",
        "Page id \u2014 unique only paired with src_collection. Example value: '<page-id>'.",
    ),
    (
        "COLUMN api.cards.src_collection",
        (
            "This page's own collection. Named to match kb.edges.src_collection \u2014 api.graph_expand/api.graph_paths join "
            "this view on that pair. Example value: 'research'."
        ),
        (
            "This page's own collection. Named to match kb.edges.src_collection \u2014 api.graph_expand/api.graph_paths join "
            "this view on that pair. Example value: '<collection>'."
        ),
    ),
    (
        "COLUMN api.cards.type",
        "Page type: paper|entity|concept|comparison|playbook|question|moc|meta.",
        "One of this knowledge base's page types. api.page_types lists them.",
    ),
    (
        "COLUMN api.cards.frontmatter",
        (
            "jsonb: the page's long-tail frontmatter as a document (the same keys api.facets unpivots, plus values deeper "
            "than a scalar). Declared link and link-list fields (the page type's edge_fields in api.page_types, e.g. a "
            "flag's about or resolved_by) are NOT here: each is a row in api.edges with rel = the field name, filter on "
            "src_id. Example: SELECT frontmatter->'priority' FROM api.cards WHERE id = 'ent.lightrag';"
        ),
        (
            "jsonb: the page's long-tail frontmatter as a document (the same keys api.facets unpivots, plus values deeper "
            "than a scalar). Declared link and link-list fields (the page type's edge_fields in api.page_types) are NOT "
            "here: each is a row in api.edges with rel = the field name, filter on src_id. Example: SELECT "
            "frontmatter->'<key>' FROM api.cards WHERE id = '<page-id>';"
        ),
    ),
    (
        "VIEW api.claims",
        (
            "Joined, human-readable claims: owning page title and subject/dataset/task/metric NAMES (never bare ids), plus "
            "quote+loc for L4 verification. Example: SELECT page_title, text, quote, loc FROM api.claims WHERE claim_id = "
            "'paper.lightrag-2025#c1';"
        ),
        (
            "Joined, human-readable claims: owning page title and subject/dataset/task/metric NAMES (never bare ids), plus "
            "quote+loc for L4 verification. Example: SELECT page_title, text, quote, loc FROM api.claims WHERE claim_id = "
            "'<page-id>#c1';"
        ),
    ),
    (
        "COLUMN api.claims.claim_id",
        "Claim id '<page_id>#cN' \u2014 unique only paired with collection. Example value: 'paper.lightrag-2025#c1'.",
        "Claim id '<page_id>#cN' \u2014 unique only paired with collection. Example value: '<page-id>#c1'.",
    ),
    (
        "COLUMN api.claims.collection",
        "This claim's own collection (its owning page's collection). Example value: 'research'.",
        "This claim's own collection (its owning page's collection). Example value: '<collection>'.",
    ),
    (
        "COLUMN api.claims.subject_name",
        "Name of the entity the claim is about. Example value: 'LightRAG'.",
        "Title of the page the claim is about.",
    ),
    (
        "COLUMN api.claims.dataset_name",
        "Dataset name the result is on. Example value: 'HotpotQA'.",
        "Dataset name the result is on.",
    ),
    (
        "COLUMN api.claims.task_name",
        "Task name. Example value: 'multi-hop-qa'.",
        "Task name.",
    ),
    (
        "COLUMN api.claims.metric_name",
        "Metric name. Example value: 'F1'.",
        "Metric name.",
    ),
    (
        "COLUMN api.claims.kind",
        "Claim type (vocabulary). Example value: 'benchmark-result'.",
        "Claim type (vocabulary).",
    ),
    (
        "COLUMN api.claims.condition",
        "Qualifying condition. Example value: 'zero-shot'.",
        "Qualifying condition.",
    ),
    (
        "COLUMN api.claims.magnitude",
        "Effect size text. Example value: '+4.2 F1'.",
        "Effect size text.",
    ),
    (
        "COLUMN api.claims.eval_setting",
        "Evaluation setting. Example value: 'closed-book'.",
        "Evaluation setting.",
    ),
    (
        "COLUMN api.contradictions.subject_id",
        "Shared subject entity id.",
        "Shared subject page id.",
    ),
    (
        "FUNCTION api.collection_about(text)",
        (
            "One collection's about text (its compiled meta.about page), served as the collection map's own content. Zero "
            "rows when the collection compiled none. Example: SELECT body FROM api.collection_about('research');"
        ),
        (
            "One collection's about text (its compiled meta.about page), served as the collection map's own content. Zero "
            "rows when the collection compiled none. Example: SELECT body FROM api.collection_about('<collection>');"
        ),
    ),
    (
        "FUNCTION api.collection_authoring_rules(text)",
        (
            "One collection's authoring contract (its compiled meta.authoring page): frontmatter keys, page types, "
            "canonical sections, claims grammar, typed relations, lifecycle, ingest procedure, lint -- scoped to one "
            "corpus. Zero rows when the collection declared no authoring document. Example: SELECT body FROM "
            "api.collection_authoring_rules('research');"
        ),
        (
            "One collection's authoring contract (its compiled meta.authoring page): frontmatter keys, page types, "
            "canonical sections, claims grammar, typed relations, lifecycle, ingest procedure, lint -- scoped to one "
            "corpus. Zero rows when the collection declared no authoring document. Example: SELECT body FROM "
            "api.collection_authoring_rules('<collection>');"
        ),
    ),
    (
        "FUNCTION api.collection_schema_guide(text)",
        (
            "One collection's schema guide: its own api-surface documentation rows (surface, purpose, notes), scoped to "
            "one corpus's vocab. Zero rows when the collection wrote none. Example: SELECT * FROM "
            "api.collection_schema_guide('research');"
        ),
        (
            "One collection's schema guide: its own api-surface documentation rows (surface, purpose, notes), scoped to "
            "one corpus's vocab. Zero rows when the collection wrote none. Example: SELECT * FROM "
            "api.collection_schema_guide('<collection>');"
        ),
    ),
    (
        "VIEW api.edges",
        (
            "Typed relations between pages/claims, both endpoints named. Example: SELECT src_title, rel, dst_title FROM "
            "api.edges WHERE rel = 'evaluates' AND dst_id = 'ent.graphrag' LIMIT 20;"
        ),
        (
            "Typed relations between pages/claims, both endpoints named. Example: SELECT src_title, rel, dst_title FROM "
            "api.edges WHERE rel = '<relation>' AND dst_id = '<page-id>' LIMIT 20;"
        ),
    ),
    (
        "COLUMN api.edges.src_collection",
        "Source endpoint's collection. Example value: 'research'.",
        "Source endpoint's collection. Example value: '<collection>'.",
    ),
    (
        "COLUMN api.edges.dst_collection",
        "Destination endpoint's collection. Example value: 'research'.",
        "Destination endpoint's collection. Example value: '<collection>'.",
    ),
    (
        "FUNCTION api.envelope_page(text, text)",
        (
            "Progressive disclosure, page envelope: one page row (row_type='page') plus its section children "
            "(row_type='section'), ordered by ord, in a single call. When the page's whole text is at most 6000 "
            "characters, each section row also carries its body (a small page reads whole in one call); larger pages "
            "return body NULL. A section's summary falls back to its own compiled kb.sections.summary when it has no "
            "active unit_summaries row, exactly as a page falls back to its frontmatter summary. Example: SELECT * FROM "
            "api.envelope_page('research', 'mu.x');"
        ),
        (
            "Progressive disclosure, page envelope: one page row (row_type='page') plus its section children "
            "(row_type='section'), ordered by ord, in a single call. When the page's whole text is at most 6000 "
            "characters, each section row also carries its body (a small page reads whole in one call); larger pages "
            "return body NULL. A section's summary falls back to its own compiled kb.sections.summary when it has no "
            "active unit_summaries row, exactly as a page falls back to its frontmatter summary. Example: SELECT * FROM "
            "api.envelope_page('<collection>', '<page-id>');"
        ),
    ),
    (
        "FUNCTION api.envelope_section(text, text)",
        (
            "Progressive disclosure, section envelope: one section's id/title/body/summary, a disclosure leaf (no "
            "children). Example: SELECT * FROM api.envelope_section('research', 'mu.x#s1');"
        ),
        (
            "Progressive disclosure, section envelope: one section's id/title/body/summary, a disclosure leaf (no "
            "children). Example: SELECT * FROM api.envelope_section('<collection>', '<page-id>#<heading-slug>');"
        ),
    ),
    (
        "VIEW api.facets",
        (
            "Facet assignments unpivoted: one row per (page, facet, value) across every declared facet in the page's "
            "facets map AND every scalar/array key of the frontmatter long tail (slotless per-knowledge-base fields like "
            "priority, official ids like us_id, doc_type, \u2026). Example: SELECT page_id FROM api.facets WHERE facet = "
            "'priority' AND value = 'mvp';"
        ),
        (
            "Facet assignments unpivoted: one row per (page, facet, value) across every declared facet in the page's "
            "facets map AND every scalar/array key of the frontmatter long tail (slotless per-knowledge-base fields). "
            "Example: SELECT page_id FROM api.facets WHERE facet = '<facet>' AND value = '<value>';"
        ),
    ),
    (
        "COLUMN api.facets.facet",
        "Facet key: a declared facet name (tasks|domains|stages|\u2026) or a frontmatter key. Example value: 'priority'.",
        "Facet key: a declared facet name or a frontmatter key. Example value: '<facet>'.",
    ),
    (
        "COLUMN api.facets.value",
        "One assigned value. Example value: 'mvp'.",
        "One assigned value.",
    ),
    (
        "FUNCTION api.fetch(text, text)",
        (
            "Fetch one unit's full text by (collection, id) \u2014 a page, section, claim, or raw chunk. A page's card_text is "
            "truncated past ~8k tokens (fetch its sections instead). Example: SELECT * FROM api.fetch('research', 'mu.x');"
        ),
        (
            "Fetch one unit's full text by (collection, id) \u2014 a page, section, claim, or raw chunk. A page's card_text is "
            "truncated past ~8k tokens (fetch its sections instead). Example: SELECT * FROM api.fetch('<collection>', "
            "'<page-id>');"
        ),
    ),
    (
        "FUNCTION api.graph_expand(text, text[], integer, text[], integer, boolean)",
        (
            "Typed-relation neighbourhood expansion, scoped to one collection: from one or more seed ids in p_collection, "
            "walk the link graph out to N hops (default 2), optionally filtered to rel_types, returning one row per "
            "reached node \u2014 its id/type/title/status/superseded plus the edge that reached it (src_id, dst_id, rel, "
            "provenance_claim; NULL on a seed). k caps how many rows come back (max 50). include_superseded defaults to "
            "false, which skips a superseded node at the hop that would reach it, so a node reachable only through one is "
            "absent too; pass true to walk them. A seed is never judged, so expanding a superseded page still answers. "
            "Example: SELECT * FROM api.graph_expand('research', ARRAY['ent.graphrag'], hops => 2);"
        ),
        (
            "Typed-relation neighbourhood expansion, scoped to one collection: from one or more seed ids in p_collection, "
            "walk the link graph out to N hops (default 2), optionally filtered to rel_types, returning one row per "
            "reached node \u2014 its id/type/title/status/superseded plus the edge that reached it (src_id, dst_id, rel, "
            "provenance_claim; NULL on a seed). k caps how many rows come back (max 50). include_superseded defaults to "
            "false, which skips a superseded node at the hop that would reach it, so a node reachable only through one is "
            "absent too; pass true to walk them. A seed is never judged, so expanding a superseded page still answers. "
            "Example: SELECT * FROM api.graph_expand('<collection>', ARRAY['<page-id>'], hops => 2);"
        ),
    ),
    (
        "FUNCTION api.graph_paths(text, text, text, integer, text[], integer, boolean)",
        (
            "Shortest typed-relation paths between two nodes, scoped to one collection: enumerate the paths from from_id "
            "to to_id within p_collection within max_hops (default 4), optionally filtered to rel_types, returning one row "
            "per node along each path \u2014 its id/type/title/status/superseded plus the edge into it (src_id, dst_id, rel, "
            "provenance_claim; NULL on the start node), the path_rank group and the path score. k caps the number of "
            "paths. include_superseded defaults to false, which drops any path running through a superseded node past the "
            "start; pass true to keep them. Example: SELECT * FROM api.graph_paths('research', 'ent.graphrag', "
            "'ent.lightrag');"
        ),
        (
            "Shortest typed-relation paths between two nodes, scoped to one collection: enumerate the paths from from_id "
            "to to_id within p_collection within max_hops (default 4), optionally filtered to rel_types, returning one row "
            "per node along each path \u2014 its id/type/title/status/superseded plus the edge into it (src_id, dst_id, rel, "
            "provenance_claim; NULL on the start node), the path_rank group and the path score. k caps the number of "
            "paths. include_superseded defaults to false, which drops any path running through a superseded node past the "
            "start; pass true to keep them. Example: SELECT * FROM api.graph_paths('<collection>', '<page-id>', "
            "'<other-page-id>');"
        ),
    ),
    (
        "FUNCTION api.log_session(text, text, text[], text[], integer, text)",
        (
            "Session telemetry: the only write the read-only role may perform (SECURITY DEFINER insert into kb.query_log). "
            "Example: SELECT api.log_session('q?', 'compare', ARRAY['api.search'], ARRAY['paper.x'], 1200);"
        ),
        (
            "Session telemetry: the only write the read-only role may perform (SECURITY DEFINER insert into kb.query_log). "
            "Example: SELECT api.log_session('q?', 'compare', ARRAY['api.search'], ARRAY['<page-id>'], 1200);"
        ),
    ),
    (
        "FUNCTION api.lookup_value(text, text, text)",
        (
            "Resolve a facet value's definition + usage count within one collection (vocab is corpus-scoped). facet is a "
            "declared facet name or 'entity_type'; term filters by substring. Example: SELECT * FROM "
            "api.lookup_value('research', 'facet:tasks', 'retrieval');"
        ),
        (
            "Resolve a facet value's definition + usage count within one collection (vocab is corpus-scoped). facet is a "
            "declared facet name or 'entity_type'; term filters by substring. Example: SELECT * FROM "
            "api.lookup_value('<collection>', 'facet:<facet>', '<term>');"
        ),
    ),
    (
        "VIEW api.page_types",
        (
            "The writing contract per page type: which statuses, sections, and frontmatter a page type accepts. One row "
            "per collection and page type. Example: SELECT status_values FROM api.page_types WHERE collection = 'research' "
            "AND page_type = 'flag';"
        ),
        (
            "The writing contract per page type: which statuses, sections, and frontmatter a page type accepts. One row "
            "per collection and page type. Example: SELECT status_values FROM api.page_types WHERE collection = "
            "'<collection>' AND page_type = '<page-type>';"
        ),
    ),
    (
        "COLUMN api.page_types.page_type",
        "The page type slug, e.g. flag or paper.",
        "The page type slug.",
    ),
    (
        "COLUMN api.page_types.decision",
        (
            "jsonb: the page type's decision block, null for a page type with no declared decision. An object with open "
            "(the status a decision opens in) and outcomes (an array of objects, each with status and an optional link "
            "naming the edge_fields entry that outcome asks the owner to write). Example: SELECT decision FROM "
            "api.page_types WHERE collection = 'research' AND page_type = 'flag';"
        ),
        (
            "jsonb: the page type's decision block, null for a page type with no declared decision. An object with open "
            "(the status a decision opens in) and outcomes (an array of objects, each with status and an optional link "
            "naming the edge_fields entry that outcome asks the owner to write). Example: SELECT decision FROM "
            "api.page_types WHERE collection = '<collection>' AND page_type = '<page-type>';"
        ),
    ),
    (
        "COLUMN api.raw_chunks.collection",
        "This chunk's own collection (its owning source mirror's collection). Example value: 'research'.",
        "This chunk's own collection (its owning source mirror's collection). Example value: '<collection>'.",
    ),
    (
        "FUNCTION api.retrieval_switches(text)",
        (
            "The indexed retrieval switches for one collection (embeddings/sql/graph, each boolean, plus private: true "
            "when the collection keeps out of the library over several knowledge bases), or an empty object when that "
            "collection has never been indexed or is not visible to this session. The one reader-granted read onto "
            "index_fingerprint, the table itself stays ungranted. Example: SELECT api.retrieval_switches('research');"
        ),
        (
            "The indexed retrieval switches for one collection (embeddings/sql/graph, each boolean, plus private: true "
            "when the collection keeps out of the library over several knowledge bases), or an empty object when that "
            "collection has never been indexed or is not visible to this session. The one reader-granted read onto "
            "index_fingerprint, the table itself stays ungranted. Example: SELECT api.retrieval_switches('<collection>');"
        ),
    ),
    (
        "VIEW api.schema_guide",
        (
            "One row per (collection, api surface): its purpose and 1-3 exemplar queries, the map of this schema. A "
            "single-knowledge-base database returns one row per surface; a multi-knowledge-base database returns one per "
            "collection too -- filter by collection, or call api.collection_schema_guide(p_collection) for just yours. "
            "Example: SELECT surface, purpose FROM api.schema_guide WHERE collection = 'research';"
        ),
        (
            "One row per (collection, api surface): its purpose and 1-3 exemplar queries, the map of this schema. A "
            "single-knowledge-base database returns one row per surface; a multi-knowledge-base database returns one per "
            "collection too -- filter by collection, or call api.collection_schema_guide(p_collection) for just yours. "
            "Example: SELECT surface, purpose FROM api.schema_guide WHERE collection = '<collection>';"
        ),
    ),
    (
        "COLUMN api.schema_guide.collection",
        "The collection this schema-guide row belongs to. Example value: 'research'.",
        "The collection this schema-guide row belongs to. Example value: '<collection>'.",
    ),
    (
        "FUNCTION api.search(text, integer, halfvec, text, jsonb, text, boolean, date, date)",
        (
            "Hybrid search collapsed across levels (RRF of BM25 + an optional semantic leg). Through the knowledge base "
            "doors (run_sql), add vector_query => '<text>' to the call and the door embeds that text server-side into "
            "query_vec for you. Never build a vector by hand. Omit it for BM25-only lexical search. (Only a direct caller "
            "outside the doors passes query_vec, a halfvec query embedding, or NULL.) k caps how many hits come back "
            "(default 12, max 50). The 'level' arg filters to one tier (card|section|claim|raw). 'facets' is one jsonb bag "
            "of facet name to value-list narrowing (e.g. '{\"tasks\": [\"retrieval\"]}'), matched against each page's own "
            "declared facets (values for one key OR'd, several keys AND'd), applied BEFORE ranking. 'collection' routes "
            "the search to one knowledge base, defaulting to the reader session's own collection (the app.collection "
            "setting) rather than any fixed one. Pass collection => NULL explicitly to rank over every collection visible "
            "to the session. 'only_current' defaults to true, which ranks only over live pages and active claims. Pass "
            "false to rank over superseded pages, stubs and retired claims too. 'since' and 'until' are dates, both "
            "inclusive and both NULL by default: either one keeps only pages whose created date falls in the range, "
            "applied BEFORE ranking, so a page with no created date and every raw chunk drop out once either is set. "
            "Returns id, src_collection, hit_level (card|section|claim|raw), page_id, page_title, snippet, score, "
            "lexical_rank, vector_rank, vector_distance, snippet_truncated. src_collection is the knowledge base the hit "
            "belongs to. An id is unique only within its knowledge base, so join a hit to api.cards on src_collection and "
            "id together. snippet is an excerpt for judging whether to fetch, cut at 480 characters with a trailing "
            "ellipsis when the matched text is longer, and snippet_truncated says whether it was cut. Fetch the id for the "
            "whole unit. lexical_rank and vector_rank are each leg's own rank for the hit and vector_distance the semantic "
            "leg's raw distance. A NULL rank means that leg did not select this row, so the pair says which components "
            "support a hit rather than how good it is. Example: SELECT * FROM api.search('graph rag token cost', k => 8, "
            "since => '2026-09-01');"
        ),
        (
            "Hybrid search collapsed across levels (RRF of BM25 + an optional semantic leg). Through the knowledge base "
            "doors (run_sql), add vector_query => '<text>' to the call and the door embeds that text server-side into "
            "query_vec for you. Never build a vector by hand. Omit it for BM25-only lexical search. (Only a direct caller "
            "outside the doors passes query_vec, a halfvec query embedding, or NULL.) k caps how many hits come back "
            "(default 12, max 50). The 'level' arg filters to one tier (card|section|claim|raw). 'facets' is one jsonb bag "
            "of facet name to value-list narrowing (e.g. '{\"<facet>\": [\"<value>\"]}'), matched against each page's own "
            "declared facets (values for one key OR'd, several keys AND'd), applied BEFORE ranking. 'collection' routes "
            "the search to one knowledge base, defaulting to the reader session's own collection (the app.collection "
            "setting) rather than any fixed one. Pass collection => NULL explicitly to rank over every collection visible "
            "to the session. 'only_current' defaults to true, which ranks only over live pages and active claims. Pass "
            "false to rank over superseded pages, stubs and retired claims too. 'since' and 'until' are dates, both "
            "inclusive and both NULL by default: either one keeps only pages whose created date falls in the range, "
            "applied BEFORE ranking, so a page with no created date and every raw chunk drop out once either is set. "
            "Returns id, src_collection, hit_level (card|section|claim|raw), page_id, page_title, snippet, score, "
            "lexical_rank, vector_rank, vector_distance, snippet_truncated. src_collection is the knowledge base the hit "
            "belongs to. An id is unique only within its knowledge base, so join a hit to api.cards on src_collection and "
            "id together. snippet is an excerpt for judging whether to fetch, cut at 480 characters with a trailing "
            "ellipsis when the matched text is longer, and snippet_truncated says whether it was cut. Fetch the id for the "
            "whole unit. lexical_rank and vector_rank are each leg's own rank for the hit and vector_distance the semantic "
            "leg's raw distance. A NULL rank means that leg did not select this row, so the pair says which components "
            "support a hit rather than how good it is. Example: SELECT * FROM api.search('<query>', k => 8, since => "
            "'2026-09-01');"
        ),
    ),
    (
        "COLUMN api.sections.section_id",
        (
            "Section id '<page_id>#<heading-slug>' \u2014 unique only paired with collection. Example value: "
            "'paper.lightrag-2025#findings'."
        ),
        (
            "Section id '<page_id>#<heading-slug>' \u2014 unique only paired with collection. Example value: "
            "'<page-id>#<heading-slug>'."
        ),
    ),
    (
        "COLUMN api.sections.collection",
        "This section's own collection (its owning page's collection). Example value: 'research'.",
        "This section's own collection (its owning page's collection). Example value: '<collection>'.",
    ),
    (
        "COLUMN api.sections.page_type",
        "Owning page type: paper|entity|\u2026",
        "Owning page type.",
    ),
    (
        "FUNCTION api.visible_collection(text)",
        (
            "Whether this session may read one collection: true for the session's own collection (the app.collection "
            "setting a reader connection carries) and for every collection whose indexed switches do not say private: "
            "true. A collection this answers false for behaves throughout the api surface exactly like one that does not "
            "exist. Example: SELECT api.visible_collection('research');"
        ),
        (
            "Whether this session may read one collection: true for the session's own collection (the app.collection "
            "setting a reader connection carries) and for every collection whose indexed switches do not say private: "
            "true. A collection this answers false for behaves throughout the api surface exactly like one that does not "
            "exist. Example: SELECT api.visible_collection('<collection>');"
        ),
    ),
)


def _literal(text: str) -> str:
    """`text` as a single-quoted SQL string literal."""
    return "'" + text.replace("'", "''") + "'"


def upgrade() -> None:
    """Write the neutral comment on every rewritten object."""
    for target, _previous, neutral in _REWRITES:
        op.execute(f"COMMENT ON {target} IS {_literal(neutral)}")


def downgrade() -> None:
    """Put every rewritten comment back as it stood at 0052."""
    for target, previous, _neutral in _REWRITES:
        op.execute(f"COMMENT ON {target} IS {_literal(previous)}")
