---
id: engram-research-01-postgres-search
aliases: ["engram-research-01-postgres-search"]
type: explanation
app: engram
status: current
tags: [research]
title: "Research notes: Postgres search stack (BM25 + vector + hybrid)"
---

# Research notes: Postgres search stack (BM25 + vector + hybrid)

*Research notes that grounded engram's [product decisions](../product-decisions.md).*

Working file for the knowledge-base indexer stack decision. Owned by the research agent. Per-source progressive notes, newest at bottom.

## ParadeDB docs: Create an Index

- URL: https://docs.paradedb.com/documentation/indexing/create-index
- Accessed: 2026-06-10
- BM25 index is a real Postgres index AM: `CREATE INDEX search_idx ON mock_items USING bm25 (id, description, category) WITH (key_field='id');`
- `key_field` is mandatory. It must (1) have a `UNIQUE` constraint (typically the PK), (2) be the **first column** in the column list, (3) be untokenized if text.
- Only **one BM25 index per table**. Docs recommend indexing every column that may appear in a search query, including columns used for sorting, grouping, filtering, aggregations (so filters can be pushed into the index).
- Indexable types: "Most Postgres types, including text, JSON, numeric, timestamp, range, boolean, and arrays."
- Tokenizers configured via cast syntax inside the index column list: `(description::pdb.icu)`, `(description::pdb.simple('stemmer=english', 'ascii_folding=true', 'stopwords_language=english'))`.

## ParadeDB blog: "Deep Dive into ParadeDB's v2 API" (Dec 4, 2025)

- URL: https://www.paradedb.com/blog/v2api
- Accessed: 2026-06-10
- v2 API (Dec 2025) replaced the JSON-config/`paradedb.*` builder style: schema lives in SQL, tokenization via casts.
- New operators: `|||` (OR/disjunction of terms), `&&&` (AND/conjunction), `###` (phrase), `"==="` (exact token match for categorical fields), `@@@` (complex/parsed query expressions, proximity, regex, query strings).
  ```sql
  SELECT description FROM mock_items WHERE description ||| 'wireless bluetooth';
  ```
- Scoring: `pdb.score(key_field_col)` in SELECT/ORDER BY:
  ```sql
  SELECT description, pdb.score(id) FROM mock_items
  WHERE description ||| 'wireless headphones' ORDER BY pdb.score(id) DESC;
  ```
- Highlighting: `pdb.snippet(column)`.
- Extras: proximity `'sleek' ## 2 ## 'shoes'`, fuzzy `'sheos'::pdb.fuzzy(1)`, regex `pdb.regex('.*shoes.*')`, "query parsing: accepts user search strings with boolean operators", faceting via `pdb.agg()` "computes faceted navigation alongside top-K results".
- Tokenizers testable interactively: `SELECT 'Wireless noise-cancelling headphones'::pdb.unicode_words::text[];`

## ParadeDB docs: Query Parser (Lucene/Tantivy query strings)

- URL: https://docs.paradedb.com/documentation/query-builder/compound/query-parser.md
- Accessed: 2026-06-10
- Yes, Lucene-style query strings exist: `pdb.parse('<query>')` used with the `@@@` operator **on the key field**:
  ```sql
  SELECT description, rating, category FROM mock_items
  WHERE id @@@ pdb.parse('description:(sleek shoes) AND rating:>3');
  ```
- Syntax is **Tantivy's query language** (Lucene-like): `field:term`, `AND`/`OR`/`NOT`, parenthetical grouping `description:(sleek shoes)`, quoted phrases, range comparisons `rating:>3`.
- `pdb.parse('...', lenient => true)`, best-effort parsing. With no field names, query runs over all indexed fields (good for raw user/LLM input).
- `conjunction_mode => true` switches default term combination from OR to AND.
- Caveat to verify in Tantivy docs: `^` boost support is Tantivy-level. ParadeDB also exposes `::pdb.boost(2.0)` casts (see boost page).

## ParadeDB docs: Highlighting / snippets

- URL: https://docs.paradedb.com/documentation/full-text/highlight.md
- Accessed: 2026-06-10
- `pdb.snippet(col)` returns best snippet. Options `start_tag` (default `<b>`), `end_tag`, `max_num_chars` (default 150).
- `pdb.snippets(col, max_num_chars => 15)` returns array of snippets. Extra options `limit` (default 5), `offset`, `sort_by` (`'score'`|`'position'`).
- `pdb.snippet_positions(col)` returns byte offsets `[start, end)` of matches.
- Not supported for fuzzy search. Docs recommend always pairing snippet calls with `LIMIT`.

## ParadeDB docs: Facets / aggregates

- URL: https://docs.paradedb.com/documentation/aggregates/facets.md
- Accessed: 2026-06-10
- Faceting exists: `pdb.agg('<ES-style JSON agg>')` used **as a window function** `pdb.agg(...) OVER ()`, returns aggregates alongside top-K rows in one query.
- Example spec: `pdb.agg('{"value_count": {"field": "id"}}')`. Terms/metrics buckets documented under /documentation/aggregates/.
- Docs claim single faceted query "can significantly improve read throughput" vs separate queries.
- No community-vs-enterprise gating mentioned on this page (older pg_search versions gated fast aggregates, verify license page).

## ParadeDB docs: Boost + self-hosted install, GitHub repo (license/version)

- URLs: https://docs.paradedb.com/documentation/sorting/boost.md, https://docs.paradedb.com/deploy/self-hosted/extension.md, https://github.com/paradedb/paradedb
- Accessed: 2026-06-10
- Boosting is a cast: `'shoes'::pdb.boost(2)` (factor −2048..2048, multiplicative), chainable: `'shose'::pdb.fuzzy(2)::pdb.boost(2)`. Boost page doesn't mention `^`, `title^2`-style boosting inside `pdb.parse()` strings depends on Tantivy parser support (verify separately).
- Self-hosted install: prebuilt binaries for **Postgres 15+** (examples show PG18) on Ubuntu 22.04/24.04, Debian 12/13, RHEL 9/10, macOS 14/15 (deb/rpm/pkg, amd64+arm64). Requires `shared_preload_libraries = 'pg_search'` in postgresql.conf, then `CREATE EXTENSION pg_search;`.
- Docker image: `paradedb/paradedb` (Postgres image with pg_search preinstalled).
- License: **AGPL-3.0** (ParadeDB Community). Separate ParadeDB Enterprise commercial license. AGPL is fine for internal/self-hosted use.
- Latest release: **v0.24.0, June 3, 2026**, ~8.9k stars, Rust + Tantivy (search) + Apache DataFusion (aggregates).

## Tantivy QueryParser docs (boost in query strings)

- URL: https://docs.rs/tantivy/latest/tantivy/query/struct.QueryParser.html (+ https://docs.paradedb.com/documentation/advanced/compound/parse)
- Accessed: 2026-06-10
- Tantivy query-string grammar supports `^boost`: `"SRE"^2.0 OR devops^0.4`, and field-qualified `country:France^3.0`. So Lucene-style `title:term^2` works inside ParadeDB's `pdb.parse()`.
- Negative boosts not allowed. Query-string boost multiplies with parser-level field boosts.

## pgvector GitHub README

- URL: https://github.com/pgvector/pgvector
- Accessed: 2026-06-10
- Latest **v0.8.2**, supports Postgres 13+. ~21.7k stars. Types: `vector` (≤16k dims, 4B/dim), `halfvec` (≤16k dims, 2B/dim, fp16), `bit` (≤64k), `sparsevec`.
- HNSW: better query speed/recall, slower build, more memory, no training step. `CREATE INDEX ON items USING hnsw (embedding vector_l2_ops) WITH (m = 16, ef_construction = 64);`
- IVFFlat: faster build/less memory, needs data first, `lists = rows/1000` (≤1M rows), probes ≈ `sqrt(lists)`. For our 10k-100k scale, HNSW is the default choice.
- **Filtering**: with ANN indexes, `WHERE` is applied **after** the index scan (post-filter), which can return too few rows (e.g. 10% selectivity times ef_search=40 ≈ 4 rows). Mitigations: iterative scans, partial indexes (`WHERE category_id = 123`), partitioning by filter column, or B-tree when the filter is highly selective.
- Iterative scans (0.8.0): `SET hnsw.iterative_scan = strict_order | relaxed_order;` caps: `hnsw.max_scan_tuples` (default 20,000), `hnsw.scan_mem_multiplier`. relaxed_order = better recall, slightly out-of-order distances.
- Binary quantization expression index: `CREATE INDEX ON items USING hnsw ((binary_quantize(embedding)::bit(3)) bit_hamming_ops);` (rerank by full vector).

## pgvector CHANGELOG

- URL: https://github.com/pgvector/pgvector/blob/master/CHANGELOG.md
- Accessed: 2026-06-10
- 0.8.2 (2026-02-25): buffer-overflow fix for parallel HNSW build (CVE-2026-3172), PG18 EXPLAIN fix. 0.8.1 (2025-09-04): PG18 support, faster `binary_quantize`. **No 0.9.x exists** as of June 2026.
- halfvec + sparsevec landed in 0.7.0 (2024-04-29). Iterative index scans in 0.8.0 (2024-10-30, also dropped PG12).
- Takeaway: pgvector is in stabilization mode, 2025-26 releases are fixes/compat, API stable since 0.8.0.

## Fujitsu Fastware blog: "How to store and query embeddings in PostgreSQL without losing your mind"

- URL: https://www.postgresql.fastware.com/blog/how-to-store-and-query-embeddings-in-postgresql-without-losing-your-mind
- Accessed: 2026-06-10
- Inline embedding column on the source table when "embeddings are generated once and remain stable". **Separate embeddings table** when iterating on models/chunking or refreshing embeddings independently of source rows.
- Canonical separate-table shape (chunk-level, composite PK):
  ```sql
  CREATE TABLE listing_embeddings (
      listing_uid integer not null references listing (uid),
      chunk_index integer not null,
      chunk_text text,
      embedding vector(1536),
      primary key (listing_uid, chunk_index)
  );
  ```
- Multiple models: add a `model_version` column included in every query (or partition/index per model), enables A/B tests and rollback "without data loss". Never overwrite old embeddings.
- IVFFlat quirk: index only used when query has a `LIMIT`. Run `ANALYZE` after creation.
- Implication for us: the *chunk table* is the retrieval unit and carries one embedding column. "multiple embedding columns" on a doc table is the anti-pattern (and Postgres can't combine two ANN indexes in one scored query anyway, per Microsoft Q&A 2118689).

## ParadeDB blog: "Hybrid Search in PostgreSQL: The Missing Manual" (Oct 22, 2025)

- URL: https://www.paradedb.com/blog/hybrid-search-in-postgresql-the-missing-manual
- Accessed: 2026-06-10
- **No native hybrid scoring in ParadeDB** (as of this Oct 2025 post): fusion is hand-written SQL. RRF is "scale-independent", so no score normalization needed. `k` constant typically 60.
- Canonical RRF pattern (BM25 CTE + vector CTE, then UNION ALL, then SUM):
  ```sql
  WITH fulltext AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY pdb.score(id) DESC) AS r
    FROM documents WHERE content ||| 'keyboard' LIMIT 20
  ),
  semantic AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> '[1,2,3]') AS r
    FROM documents LIMIT 20
  ),
  rrf AS (
    SELECT id, 1.0 / (60 + r) AS s FROM fulltext
    UNION ALL
    SELECT id, 1.0 / (60 + r) AS s FROM semantic
  )
  SELECT m.id, SUM(s) AS score, m.content
  FROM rrf JOIN documents AS m USING (id)
  GROUP BY m.id, m.content ORDER BY score DESC LIMIT 5;
  ```
- Weighted variant shown (e.g. 0.7 times lexical rank-score + 0.3 times semantic), just multiply each branch's `1.0/(60+r)`.
- Pattern works identically with plain pgvector + any BM25 provider. The BM25 branch is the only ParadeDB-specific part.

## VectorChord-BM25 GitHub README (TensorChord)

- URL: https://github.com/tensorchord/VectorChord-bm25
- Accessed: 2026-06-10
- v0.3.0 (Dec 15, 2025). Dual-licensed **AGPLv3 or Elastic License v2**. Docker tags reference pg17/pg18.
- Three pieces: `pg_tokenizer.rs` (separate tokenizer ext), `bm25vector` type (sparse vocab-id/frequency vector), `bm25` index AM.
- You must pre-tokenize text into a stored `bm25vector` column: `SELECT tokenize('text', 'tokenizer_name')::bm25vector;` then `CREATE INDEX ... USING bm25 (embedding bm25_ops);`
- Query: `embedding <&> to_bm25query('index_name', tokenize('query', 'tokenizer'))`, returns **negative** BM25 score (lower = more relevant).
- **No query-string language**: no field:term, no boolean operators, no phrase queries, no highlighting/faceting documented. English-focused tokenizers.
- Top-k governed by `bm25_catalog.bm25_limit` (default 100, -1 = brute force).
- Verdict for us: scoring-only primitive. LLM agents would have to compose everything in SQL, weaker fit than pg_search's Lucene-style strings.

## TigerData (Timescale) blog: "pg_textsearch 1.0: BM25 on Postgres Pages" (Mar 31, 2026)

- URL: https://www.tigerdata.com/blog/pg-textsearch-bm25-full-text-search-postgres
- Accessed: 2026-06-10
- pg_textsearch: native-C BM25 engine on Postgres storage pages, **GA March 31, 2026**, Postgres **17/18 only**, open source (repo timescale/pg_textsearch, exact license to verify).
- Syntax: `CREATE INDEX ON articles USING bm25(content) WITH (text_config = 'english');` then `SELECT title, content <@> 'database ranking' AS score ... ORDER BY content <@> 'database ranking' LIMIT 10;` (scores negated for ASC order).
- **1.0 gaps**: no phrase queries (no positions stored), no AND/OR/NOT (planned post-1.0), no highlighting (falls back to `ts_headline()`), implicit-OR multi-term only.
- Their ts_rank critique (why tsvector isn't BM25): no IDF (common words weigh same as rare), no term-frequency saturation, no document-length normalization, and "no top-k optimization path: scoring requires touching every matching row".
- Benchmarks vs ParadeDB (MS-MARCO v2, 138M docs): pg_textsearch 2.3x faster median query, 8.7x concurrent throughput. ParadeDB 1.6-2x faster index build and competitive on 7+ lexeme queries.
- Hybrid-with-pgvectorscale article promised as Part 2 (not yet detailed here).

## Richard Yen: "Making JSONB More Queryable with Generated Columns" (May 11, 2026)

- URL: https://richyen.com/postgres/2026/05/11/generated_columns_jsonb.html
- Accessed: 2026-06-10
- Typed/generated columns beat GIN-on-jsonb for equality filters: "they produce typed scalar values with precise statistics, letting the planner make accurate row-count estimates." GIN can't serve range predicates (`ts > ...`) at all. Those fall through to a filter step.
- Expression indexes are fragile: "An index on `cast(data->>'user_id' AS INT)` will not be used by a query written as `(data->>'user_id')::int`." Generated columns eliminate this. Any query referencing the column name benefits. (Critical for LLM-written SQL, which won't reproduce exact cast forms.)
- EAV: normalizing variable attributes into EAV is "worse than JSONB in every measurable way."
- Benchmarks (50k rows): equality 0.112 ms (generated) / 0.106 ms (expr idx) / 0.197 ms (GIN jsonb_path_ops), 5k inserts 157 ms vs 334 ms (GIN jsonb_ops), storage: four targeted B-trees 3.5 MB vs one GIN jsonb_ops 18 MB.
- Decision rule: generated/typed columns for known, frequently-filtered fields (incl. range queries). GIN jsonb for ad-hoc containment/key-existence and genuinely variable documents. Promote hot jsonb keys to columns as patterns stabilize.

## PostgreSQL docs: ltree (current, PG 14-19)

- URL: https://www.postgresql.org/docs/current/ltree.html
- Accessed: 2026-06-10
- ltree is alive and a **trusted extension** (non-superuser installable), contrib module in all supported PG versions, still the standard tool for materialized hierarchy paths.
- Types: `ltree` path (`Top.Science.Astronomy`), `lquery` regex-like patterns, `ltxtquery`. Limits: 1000 chars/label, 65535 labels/path, labels = alnum + `_` + `-` (dots are separators, heading slugs must be sanitized).
- Operators: `@>` ancestor-of, `<@` descendant-of, `~` lquery match (e.g. `path ~ '*.Astronomy.*'`), `||` concat. Functions `nlevel()`, `subpath()`, `lca()`.
- Indexing: GiST (`gist_ltree_ops`, tunable `siglen`) serves `<@ @> ~ ?`. B-tree serves ordering/equality. `CREATE INDEX ON test USING GIST (path gist_ltree_ops(siglen=100));`
- Plays fine with joins/filters, it's an ordinary indexed column type. `WHERE path <@ 'Top.Science'` composes with any other predicate or join.

## Follow-up checks: ParadeDB hybrid status, community/enterprise split, Neon

- URLs: https://docs.paradedb.com/welcome/introduction, https://github.com/paradedb/paradedb, https://neon.com/docs/extensions/pg_search
- Accessed: 2026-06-10
- Still **no native RRF/hybrid-fusion function** in pg_search as of v0.24.0 (June 2026). Hybrid = pg_search BM25 CTE + pgvector CTE + hand-written RRF SQL (the "Missing Manual" pattern). pg_search and pgvector coexist in one DB/table.
- Community (AGPL) includes: BM25 scoring, fuzzy, boosting, **aggregations/faceting, highlighting**, not gated. Enterprise gates: partitioned-table BM25 indexes, RLS propagated into indexes, hot-standby search replicas. None needed at 10k-100k rows.
- Managed-hosting note: **Neon stopped offering pg_search to new projects as of 2026-03-19**, self-hosting (binaries or `paradedb/paradedb` Docker image) is the dependable path.

## Bottom line (synthesis)

- **Primary stack**: self-hosted Postgres 17 + `pg_search` v0.24+ (BM25, Tantivy/Lucene-style `pdb.parse()` query strings with field:term/AND/OR/`^boost`/phrases, `pdb.score()`, `pdb.snippet()`, `pdb.agg()` facets) + `pgvector` 0.8.2 (halfvec, HNSW, iterative scans for filtered ANN) + contrib `ltree` for heading-path hierarchy. Schema: one `chunks` table = retrieval unit, typed (or generated) columns for every hot filter (doc type, year, tags array, path ltree, model_version), small `jsonb` remainder for long-tail frontmatter, hand-written RRF view/function for hybrid. Put all filterable columns **into the BM25 index column list** so agents can push filters into one index. For vector-side filters rely on `hnsw.iterative_scan = relaxed_order`.
- **Fallback (no exotic extensions)**: Postgres built-in FTS (`tsvector` generated column + GIN + `websearch_to_tsquery` + `ts_rank_cd`) + pgvector + identical RRF SQL. Accepts no-IDF/no-TF-saturation/no-length-norm ranking and no top-k pushdown, tolerable at ≤100k rows. Zero non-standard deps (pgvector is ubiquitous on managed Postgres).
- Rejected: VectorChord-BM25 (score-only primitive, no query language, AGPL/ELv2, pre-tokenized column required), pg_textsearch (young 1.0 to 1.3, PG17/18-only, no booleans/phrases/highlighting yet, watch it, PostgreSQL-licensed and fast).
