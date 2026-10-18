---
id: engram-research-04-agent-sql-discovery
aliases: ["engram-research-04-agent-sql-discovery"]
type: explanation
app: engram
status: current
tags: [research]
title: "Research notes: how LLM agents discover and use SQL schemas"
---

# Research notes: how LLM agents discover and use SQL schemas

*Research notes that grounded engram's [product decisions](../product-decisions.md).*

Working notes for the self-describing-Postgres design (research-paper knowledge base to Postgres BM25+pgvector). Progressive, per-source. Owner: research agent. Started 2026-06-10.

## XiYan-SQL / M-Schema (arXiv 2411.08599)

- URL: https://arxiv.org/html/2411.08599v3 (also https://github.com/XGenerationLab/M-Schema)
- Accessed: 2026-06-10

- M-Schema is a semi-structured schema text format: `【DB_ID】` header, `# Table` per table (name + optional description), columns as tuples of `(column name, data type, column description, primary key flag, example values)`, plus a `【Foreign Keys】` section listing FK pairs.
- Example values are pulled by actually connecting to the DB, with display rules (string-length caps, capped number of examples). That is, schema and sampled values in one artifact.
- Ablation on BIRD dev (same pipeline, only schema format varies): M-Schema vs raw DDL vs MAC-SQL schema. GPT-4o 57.95 vs 55.67 (DDL). DeepSeek 55.15 vs 53.52. Claude 3.5 Sonnet 51.04 vs 49.74. Gemini 1.5 Pro 52.15 vs 49.22. Avg +2.03 pts over DDL. Roughly ties MAC-SQL (wins 2/4 models).
- Takeaway: format matters but modestly (~2 pts). The consistent wins come from *including types, descriptions, PK flags, FKs, and example values* in a compact layout, not from any magic syntax. DDL alone is the worst of the three.
- XiYan-SQL overall: multi-generator ensemble, SOTA-at-the-time Spider 89.65% exec accuracy.

## Spider 2.0: Real-World Enterprise Text-to-SQL Workflows (arXiv 2411.07763) + EmergentMind topic synthesis

- URLs: https://arxiv.org/abs/2411.07763, https://www.emergentmind.com/topics/spider-2-0-benchmark
- Accessed: 2026-06-10

- Scale: 632 tasks over enterprise DBs (BigQuery/Snowflake). Avg ~800+ columns per schema (803.6 lite / 812.1 snow), up to ~3,000 columns. Multi-schema, nested/partitioned tables.
- Cliff: models that score >90% EX on Spider 1.0 and ~73% on BIRD drop to 5-27% on Spider 2.0. o1-preview baseline 21.3%. Best published agent ReFoRCE (o1) 26.69%. Spider-Agent 20.29%.
- Killer datapoint: DAIL-SQL (classic single-prompt schema-in-context + few-shot pipeline) gets **2.20%** on Spider 2.0. Non-agentic prompt-stuffing collapses at enterprise scale. Agentic exploration is ~10x better.
- Error analysis: "wrong schema linking" ≈ 27.6% of SQL errors (column-level linking ~16.6%). Plus dialect/function errors and multi-step workflow failures. Tasks often need >100-line SQL and reading external docs/metadata.
- What helps (ReFoRCE): compressing DB info by pattern-grouping tables, **iterative column exploration via exploratory queries**, execution-feedback self-refinement, majority-vote over parallel runs.
- Design implication for us: keep agent-visible schema small (curated views, few well-named columns) and support cheap exploratory queries. At ~800 columns even frontier agents fail mostly on schema linking.

## BIRD benchmark (arXiv 2305.03111) + 2025-26 annotation-error audits

- URLs: https://bird-bench.github.io/, https://arxiv.org/abs/2305.03111, https://www.vldb.org/cidrdb/papers/2026/p5-jin.pdf
- Accessed: 2026-06-10

- BIRD's core thesis: real text-to-SQL needs *database value comprehension*, not just semantic parsing. The challenges are dirty values, external knowledge linking NL terms to DB contents, and efficiency on large DBs.
- BIRD ships "evidence"/oracle external knowledge per question (mapping domain terms to columns/values). Systems that ignore it lose heavily, direct evidence that NL-term-to-value mapping is a first-class failure mode, separate from schema linking.
- CIDR 2026 audit: ~32% of BIRD mini-dev examples (49% in the financial domain) carry annotation errors (wrong gold SQL/questions/evidence). Model performance is underestimated. Leaderboard deltas near the top are inside the noise floor.
- Implication: treat BIRD/Spider point-estimates as directional. The robust signal across both is *where* systems fail (schema linking + value grounding), not exact percentages.

## The Death of Schema Linking? (Distyl AI, arXiv 2408.07702)

- URL: https://arxiv.org/html/2408.07702v1
- Accessed: 2026-06-10

- Empirical claim (BIRD, 95 DBs, GPT-4o, Gemini 1.5 Pro, Claude 3.5 Sonnet, Llama 3.1...): when the whole schema fits in context, *skipping* schema-linking filters gives net-better execution accuracy for frontier models. Filtering risks dropping required columns and that downside dominates.
- Their reported deltas show aggressive filtering is catastrophic when it misses columns (single-column linking and table-to-column linking far below full-schema baseline. Exact figures in paper Table, treat the fetched 94.6/67.2/9.8 numbers as schema-recall-flavored, verify before citing).
- Schema linking still helps *weaker/smaller* models (e.g., Llama 3.1-8B improves with linking).
- Better spends than filtering: augmentation (CoT, decomposition, iterative refinement), candidate selection, execution-feedback correction, each worth ~1.4-3.4 pts.
- Reconciliation with Spider 2.0: BIRD schemas are small enough to fit in context (this paper's regime). At ~800+ columns (Spider 2.0) one-shot context-stuffing collapses and *agentic exploration* replaces static linking. Both agree: don't pre-filter with a lossy retrieval step. Either give everything or let the agent navigate.
- Design implication: our curated schema should simply FIT. Then we can hand the agent the full annotated schema and skip linking machinery entirely.

## OpenAI vs Anthropic in-house data agents (datachain.ai comparison)

- URLs: https://datachain.ai/blog/openai-anthropic-data-agents, originals: https://openai.com/index/inside-our-in-house-data-agent/, https://claude.com/blog/how-anthropic-enables-self-service-data-analytics-with-claude
- Accessed: 2026-06-10

- Shared conclusion: the data-agent bottleneck is **discovery, not SQL syntax**: "the hard part isn't writing SQL, it's finding the right tables."
- Anthropic's internal text-to-SQL agent: 21% to 95%+ accuracy purely by adding curated context: a semantic layer consulted first, Markdown "skills" (knowledge routing to reference docs, analyst-workflow runbooks, how-to-use-this-data docs). Model unchanged.
- Curation > retrieval: giving the agent raw access to query logs/warehouse added **<1 accuracy point** even though answers were in there. Curated docs did the work.
- Maintenance is load-bearing: when skills/docs went stale, accuracy fell 95% to 65%. Docs live in the same repo as data models, updated via PRs.
- OpenAI's data agent: offline-precomputed per-table indices + code crawls (freshness, business intent). Reduced tool count 40 to 13 because overlapping tools confused the model.
- Design implication: ship a small number of non-overlapping tools and put effort into a *maintained* semantic/doc layer. Our COMMENT ON + meta-docs are that layer, and they must live in the schema migration repo so they update with DDL.

## Anthropic: Writing effective tools for agents

- URL: https://www.anthropic.com/engineering/writing-tools-for-agents
- Accessed: 2026-06-10

- Consolidate: fewer, higher-leverage tools that wrap multi-step workflows beat many granular wrappers (e.g., `get_customer_context` over three list/get calls). Maps to wrapping hybrid search as one SQL function instead of raw operator syntax.
- Return high-signal context, not raw payloads: human-readable names and resolved values over UUIDs, "contextual relevance over flexibility."
- Token efficiency: pagination/filtering/truncation with sensible defaults. Truncation messages should steer the agent to a *more targeted* next query. Applies to row limits on agent SQL (limit + tell it how to narrow).
- Namespace tools (`db_*`) and prompt-engineer descriptions hard: "even small refinements to tool descriptions can yield dramatic improvements". Same logic applies to view/column COMMENTs, which are the SQL world's tool descriptions.

## Postgres MCP Pro (crystaldba/postgres-mcp)

- URL: https://github.com/crystaldba/postgres-mcp
- Accessed: 2026-06-10

- Tool surface (the de-facto pattern): `list_schemas` to `list_objects` to `get_object_details` (columns, constraints, indexes) as *separate* schema-discovery tools, plus `execute_sql`, `explain_query` (with hypothetical-index simulation), and health/index-tuning tools. Progressive disclosure baked into the tool split.
- Stated rationale: LLMs *can* introspect system catalogs themselves, but dedicated schema tools "improve reliability and reduce execution costs". This answers our Q2: agents can use information_schema, harnesses prefer canned introspection.
- Guardrails (restricted mode): read-only transactions + statement execution time limits + SQL parsing via `pglast` to reject `COMMIT`/`ROLLBACK` (prevents escaping the read-only transaction). Unrestricted mode reserved for dev.
- Philosophy: deterministic tools + classical algorithms for critical paths (index tuning), LLM for orchestration.

## Supabase MCP server docs

- URL: https://supabase.com/docs/guides/getting-started/mcp
- Accessed: 2026-06-10

- Tool surface: `list_tables`, `list_extensions`, `execute_sql`, `apply_migration` (DDL deliberately split from query execution), `search_docs`, plus logs/advisors/typegen. Same describe-vs-execute split as postgres-mcp.
- Safety: read-only mode = run everything as a read-only *Postgres role* (DB-level, not parser-level), project scoping, feature groups to shrink the tool surface.
- Prompt injection called "the primary attack vector unique to LLMs": Supabase wraps SQL *results* with instructions discouraging the model from obeying commands embedded in returned data, admits not foolproof, says don't point agents at prod.
- Relevant to us: our row contents are research-paper text (untrusted-ish). Result-wrapping and read-only role are cheap mitigations.

## V-SQL: View-based two-stage text-to-SQL (arXiv 2502.15686)

- URL: https://arxiv.org/pdf/2502.15686
- Accessed: 2026-06-10

- Approach: decompose the schema into *logical views* grouping semantically related tables/columns. LLM works against the view layer rather than the flat schema.
- Claimed mechanisms: views reduce schema complexity/cognitive load, scope the visible schema (fewer hallucinated tables/columns), and pre-encode join relationships.
- Reports consistent gains over flat-schema baselines (order of ~3-8 pts depending on model/benchmark. Older models GPT-3.5/Davinci, verify exact table before citing numbers).
- Academic corroboration for views-as-API: pre-joined, semantically named views are a *schema simplification* device, not just governance.

## Towards Agentic Schema Refinement (arXiv 2412.07786, NeurIPS 2024 TRL workshop)

- URL: https://arxiv.org/abs/2412.07786
- Accessed: 2026-06-10

- Problem: enterprise schemas are "complex and messy, obscuring the data semantics needed for analytical tasks". Raw schema is the wrong interface for LLMs.
- Proposal: an intermediary semantic layer of "small and easy-to-interpret database views, effectively acting as a refined version of the schema". Multi-agent loop defines/refines the views with minimal human guidance.
- Authors: Rissaki, Fountalis, Vasiloglou (RelationalAI), Gatterbauer (Northeastern), industry and academic.
- Direct academic endorsement of our views-as-API plan: views = the refined schema the agent should see.

## DAIL-SQL: few-shot selection/organization (VLDB'24, arXiv 2308.15363)

- URLs: https://arxiv.org/pdf/2308.15363, https://github.com/BeachWang/DAIL-SQL
- Accessed: 2026-06-10

- Systematic study of prompt engineering for text-to-SQL: schema format, example selection, and example organization. Result: 86.6% EX on Spider 1.0 (then #1) at only ~1600 tokens/question.
- Example *selection*: pick exemplars by similarity of BOTH the question and the (predicted) SQL query. Question-only similarity is weaker. Matching the *query shape* matters.
- Example *organization*: present exemplars as bare question-to-SQL pairs WITHOUT their schemas. Schema-stripped exemplars are the efficient sweet spot for strong models.
- Direct evidence for Q5: canonical Q-to-SQL exemplars demonstrably lift accuracy, and per-question-shape matching is the right retrieval key. Caveat: DAIL-SQL collapses on Spider 2.0 (2.2%). Exemplars help the *SQL-writing* step but don't fix discovery at scale. They're a complement to schema curation, not a substitute.

## CHESS: Contextual Harnessing for Efficient SQL Synthesis (arXiv 2405.16755)

- URL: https://arxiv.org/abs/2405.16755
- Accessed: 2026-06-10

- Multi-agent pipeline. The relevant agent is the Information Retriever with a `retrieve_entity` tool: extract keywords from the question, then look up *similar database values*, then return matched values WITH their columns.
- Mechanics: preprocessing indexes all unique DB values with LSH (MinHash). Query-time hierarchical retrieval = LSH (syntactic) + embedding (semantic) similarity.
- Motivation: SQL references few values but mostly in WHERE filters. The semantic gap between user phrasing and stored strings makes value retrieval "essential for query correctness."
- For us: the same role can be played by enum/dimension tables + a `search_values(term)` helper. CHESS shows value lookup should be a first-class tool/step, not left to guessing.

## Supabase hybrid search guide (function-wrapped BM25-ish FTS + pgvector)

- URL: https://supabase.com/docs/guides/ai/hybrid-search
- Accessed: 2026-06-10

- Canonical pattern: a single SQL function `hybrid_search(query_text, query_embedding, match_count, full_text_weight=1, semantic_weight=1, rrf_k=50)` returning `setof documents`.
- Internals: two CTEs (FTS via `websearch_to_tsquery` + vector `<=>` ordering), `full outer join` on id, score = weighted `1/(rrf_k+rank)` per leg (RRF), sort desc, limit.
- `fts tsvector` kept as a *generated column* so indexing stays in sync. Callers invoke via plain `select * from hybrid_search(...)` or PostgREST RPC.
- Presented as the recommended app interface, not framed for agents specifically. But the shape (one function, tunable weights, no operator syntax exposed) is exactly the consolidated-tool pattern Anthropic recommends.
- For us: agents writing SQL can compose it: `select * from hybrid_search(...) join paper using (paper_id) where facet = ...`. Function output stays relational, so facet filters/joins still work around it.

## Official reference Postgres MCP server (archived) + its security lesson

- URLs: https://github.com/modelcontextprotocol/servers-archived/tree/main/src/postgres, https://securitylabs.datadoghq.com/articles/mcp-vulnerability-case-study-SQL-injection-in-the-postgresql-mcp-server/
- Accessed: 2026-06-10

- Minimal design: ONE `query` tool ("execute read-only SQL... within a READ ONLY transaction") + per-table MCP *resources* `postgres://<host>/<table>/schema` (column names + types auto-discovered from metadata). I.e., schema-as-resources, query-as-tool, the original describe/execute split.
- Deprecated July 2025, archived. Datadog Security Labs showed SQL injection could bypass the read-only protection: `SET TRANSACTION READ ONLY` at statement level can be escaped by injecting transaction-control statements (hence crystaldba's pglast COMMIT/ROLLBACK rejection).
- Lesson: enforce read-only at the *role/connection* level (GRANT SELECT-only user, `default_transaction_read_only`), not by wrapping statements. Parser-level blocking is a patch, role-level is the floor.

## TigerData (Timescale): "The database has a new user: LLMs"

- URL: https://www.tigerdata.com/blog/the-database-new-user-llms-need-a-different-database
- Accessed: 2026-06-10

- Premise: humans learn a DB via conversation/code/institutional knowledge. "LLMs need to answer questions about the data just from the context provided by the database." Their test: 42% of context-less LLM SQL missed critical filters or misread relationships.
- Fix: a **semantic catalog**, natural-language descriptions of every table/column/function/business rule, stored in version-controlled YAML, reviewed via PRs, retrievable at query time. Reported accuracy 58% to 86% on some schemas.
- Tiered access-control recommendation (their words): start with "tightly scoped functions, then expand access as confidence grows", views as the intermediate tier, base tables last. Functions-first is notable, even stronger than views-first.
- Use deterministic `EXPLAIN` as a pre-execution verifier of agent SQL (catch missing indexes/insane plans/nonexistent columns before running).
- Catalog can live outside the prod DB (no need to ALTER prod), but for our greenfield DB, in-DB COMMENTs + catalog both work. Version control is the non-negotiable part.

## Synthetic SQL column descriptions and text-to-SQL performance (arXiv 2408.04691)

- URL: https://arxiv.org/abs/2408.04691
- Accessed: 2026-06-10

- On BIRD: "incorporating generated column descriptions consistently enhances text-to-SQL model performance," especially for larger models (GPT-4o, Qwen2-72B, Mixtral 8x22B).
- LLM-generated (Qwen2) descriptions *outperformed* manually curated gold descriptions. The "superfluous" extra detail annotators would cut actually helps models.
- Limits: inherently ambiguous columns still fail without manual expert input. Descriptions don't rescue genuinely ambiguous naming. Fix the naming or add expert docs.
- For us: write generous column COMMENTs (don't economize words). LLM-draft then human-review is a validated pipeline. Ambiguity must be resolved in the schema itself.

## pgEdge Postgres MCP server (beta)

- URL: https://www.pgedge.com/blog/introducing-the-pgedge-postgres-mcp-server
- Accessed: 2026-06-10

- Beta ships read-only-only mode. Write support later behind a flag. Read-only-by-default is now the industry default posture.
- "Full schema introspection" tool returns PKs, FKs, indexes, column types, constraints in one shot (vs basic table listing) so the model can "reason about your data model". Marketed as reducing token usage vs raw catalog dumps.
- Confirms convergent MCP pattern: rich describe-tool + read-only query tool.

## Practitioner checklist: designing schemas LLMs can understand (Jay Gajera, Medium)

- URL: https://medium.com/@gajerajay9/designing-database-schemas-so-that-llms-can-understand-300db353ba36
- Accessed: 2026-06-10

- Naming: self-describing names with units (`duration_in_minutes`, `refunded_amount_in_inr`), descriptive junction-table names, one date format everywhere.
- Metadata: use `COMMENT ON`. ALSO keep a *schema metadata table* (centralized registry: descriptions, sample values, units, enum meanings), queryable by the agent itself.
- Enums: never magic numbers/letters without documenting the mapping (`'C' = Completed...`), document in comments or the metadata table.
- Business logic: computed metrics belong in views/materialized views (not app code) so the agent can SELECT them. Document derived-metric formulas.
- Few-shot: store example queries + field mappings + business definitions retrievably (RAG) for prompt augmentation.
- Explicit FKs always (agents read FK constraints as the join map).

## AutoLink: autonomous schema exploration (arXiv 2511.17190)

- URL: https://arxiv.org/abs/2511.17190
- Accessed: 2026-06-10

- Reframes schema linking as an *iterative agent loop*: dynamically explore + expand the linked schema subset instead of one-shot retrieval or full-schema stuffing.
- Results: 97.4% schema-linking recall on BIRD-dev, 91.2% on Spider 2.0-lite. 34.9% EX on Spider 2.0-lite (2nd on leaderboard). Maintains recall + token efficiency at 3,000+ columns where conventional methods degrade.
- One-shot retrieval linking "incurs prohibitive costs, struggles to trade off recall and noise, and scales poorly". Agentic exploration is the scalable alternative.
- Confirms the 2025-26 consensus: small schema means the full annotated schema in context. Big schema means agent explores incrementally. Either way, lossy one-shot filters lose.

---

# Synthesis (2026-06-10)

**Q1 format.** M-Schema-style compact semi-structured schema (types + descriptions + PK/FK + example values) beats raw DDL by ~2 pts consistently. The content (descriptions, FKs, example values) matters more than the syntax. Failures at scale are schema linking (~28% of Spider 2.0 errors) and value grounding. Accuracy collapses between BIRD scale (~70 cols, ~73% EX) and Spider 2.0 scale (~800 cols, <30% EX). Below ~100 columns, put the whole annotated schema in context and skip linking (Distyl). Above, agentic iterative exploration (AutoLink, ReFoRCE).

**Q2 self-describing PG.** Column COMMENTs demonstrably lift accuracy (arXiv 2408.04691, TigerData 58 to 86%). Verbose beats terse. LLM-drafted + human-reviewed works. Agents *can* use information_schema/pg_catalog but production harnesses front-load canned introspection tools (list_schemas to list_objects to get_object_details incl. PK/FK/indexes/comments) for reliability + token cost. Also keep a queryable schema-metadata table and version-control the descriptions (stale docs: 95 to 65% accuracy).

**Q3 views as API.** Triangulated support: academic (V-SQL, Agentic Schema Refinement: "small, easy-to-interpret views acting as a refined schema"), practitioner (TigerData tiering: functions to views to tables). Guardrails consensus: read-only ROLE (not statement-level SET TRANSACTION, bypassable, see Datadog), statement_timeout, row limits w/ truncation messages, EXPLAIN-before-execute.

**Q4 MCP patterns.** Convergent design: separate describe-schema tool(s) + one execute-readonly-sql tool (reference server: schema-as-resources + query tool, crystaldba: 3 describe tools + execute + explain, Supabase: list_tables + execute_sql + apply_migration split, pgEdge same). Progressive disclosure is the norm.

**Q5 exemplars.** DAIL-SQL: question+query-similarity-selected Q-to-SQL pairs (schema-stripped) drove then-SOTA Spider 86.6%. Anthropic's data agent got 21 to 95% largely from runbook-style docs with canonical workflows. Store in versioned docs/meta-table retrievable per question-shape. Nobody stores them in COMMENT ON (comments stay short). Prompt-time injection is the delivery mechanism.

**Q6 value grounding.** M-Schema embeds example values per column. CHESS makes value lookup a tool (LSH+embedding over distinct values, returns value+column). BIRD shows NL-term-to-value mapping is a top failure mode. For low-cardinality columns: enumerate all values in the comment/metadata. For high-cardinality: provide a search_values() helper or rely on hybrid search.

**Q7 hybrid ergonomics.** Supabase canonicalizes `hybrid_search(query_text, query_embedding, match_count, weights, rrf_k)` as a SQL function returning rows that compose with WHERE/JOIN. Matches Anthropic's consolidate-tools guidance. Wrap operators (`<=>`, tsquery, RRF) in functions. Let agents compose relationally around them.

**Design decisions for our DB:**
1. Curated agent-facing schema (one PG schema, e.g. `api`) of ~5-15 well-named views ≤ ~100 total columns. Base tables not granted.
2. COMMENT ON every view/column: generous prose, units, enum value lists, join hints. Comments returned by the describe tool.
3. `meta.schema_guide` table (or doc): per-view purpose, canonical exemplar queries per question shape, business terms. Version-controlled with migrations.
4. SQL functions: `search_chunks(query_text, k, ...)` hybrid RRF. `search_values(col, term)` for value grounding.
5. Read-only role + statement_timeout + default LIMIT discipline. EXPLAIN tool optional.
6. Tool split: describe_schema / run_sql (+ maybe explain_sql).

**Uncertainties:** exact Distyl ablation numbers (fetch summary suspect), V-SQL gains from older models, whether exemplar queries in a meta-table beat prompt-injected exemplars (no direct A/B found), BIRD/Spider noise floor (CIDR 2026: ~32% annotation errors), how well comments survive context compaction in long agent sessions (no published study).
