---
id: engram-research-02-hierarchical-retrieval
aliases: ["engram-research-02-hierarchical-retrieval"]
type: explanation
app: engram
status: current
tags: [research]
title: "Hierarchical / progressive retrieval for agentic RAG, research notes"
---

# Hierarchical / progressive retrieval for agentic RAG, research notes

*Research notes that grounded engram's [product decisions](../product-decisions.md).*

Research log for the knowledge base's SQL "progressive disclosure" ladder design (cards to sections to pages to source spans).
Method: per-source progressive notes. Primary sources preferred. Started 2026-06-10.

## PageIndex GitHub README (VectifyAI/PageIndex)
URL: https://github.com/VectifyAI/PageIndex, accessed 2026-06-10

- PageIndex = "Document Index for Vectorless, Reasoning-based RAG". Two phases: (1) index generation builds a "table-of-contents" tree from a PDF/doc. (2) tree search where an LLM reasons over the hierarchy to pick nodes, instead of embedding similarity.
- Tree node schema: `title` (section heading), `node_id` (e.g. "0006"), `start_index`/`end_index` (page numbers), `summary` (LLM-generated section overview), `nodes` (children). So: human-readable ID + title + page-span pointer + summary per node. Node text itself is NOT stored in the index, only pointers into the source.
- Pitch against vectors: "no vector DB", results "reasoning-driven and grounded in explicit page and section references" give traceability/audit trail. They call embedding top-k "vibe retrieval".
- Benchmark claim: powers Mafin 2.5, 98.7% accuracy on FinanceBench (vs ~50% often cited for naive vector RAG). Vendor-reported. Eval repo exists (VectifyAI/Mafin2.5-FinanceBench), check methodology.
- Adoption signal (June 2026): 32.8k GitHub stars, 2.8k forks, 292 commits. Recent additions: Agentic RAG demo and a "PageIndex File System" aimed at million-document scale.
- Relevance to our ladder: their node = our "section card" (title + summary + span pointer). Validates storing summaries + spans rather than chunk text at the navigation layer.

## PageIndex docs, Quickstart / Chat API
URL: https://docs.pageindex.ai/quickstart, accessed 2026-06-10

- Hosted API: submit doc to get hierarchical tree index. Chat API runs "a full agentic, reasoning-based RAG workflow" over processed docs (multi-doc: `doc_id` accepts a list).
- Tree-search internals (prompts, depth limits, node-size targets) are not in quickstart. Pointed to /sdk/documents, /sdk/chat, and an "Agentic Retrieval cookbook" (/cookbook/agentic-retrieval-pageindex).
- Confirms the production shape: navigation happens by LLM reasoning over node titles+summaries returning node IDs, then page content is fetched for selected nodes, i.e., a 2-step disclose-then-read loop, same as our ladder.

## Mafin 2.5 FinanceBench eval repo (VectifyAI)
URL: https://github.com/VectifyAI/Mafin2.5-FinanceBench, accessed 2026-06-10

- 98.7% accuracy on FinanceBench public set at 100% question coverage. Comparisons: Quantly 94%, Fintool 98% (only 66.7% coverage), ChatGPT-4o+Search 31%, Perplexity 45%. No plain vector-RAG baseline in this repo (the "~50% vector RAG" number circulates from the original FinanceBench paper/marketing, not this eval).
- Methodology caveats: vendor-run, human experts annotated ambiguous/multi-answer cases. All docs in one database ("realistic" setup). Treat as vendor-reported, partially independent-verifiable.
- Their own stated limitation: FinanceBench lacks multi-document reasoning tasks. Real use needs multi-step retrieval across documents, exactly the agentic-ladder territory.

## arXiv 2511.18177, "Rethinking Retrieval: ... Agentic and Non-Vector Reasoning Systems in the Financial Domain" (Nov 2025)
URL: https://arxiv.org/abs/2511.18177, accessed 2026-06-10

- Independent(ish) head-to-head: vector-based agentic RAG (hybrid search + metadata filtering + reranking + small-to-big) vs hierarchical node-based / PageIndex-style traversal, on 1,200 SEC filings, 150-question benchmark, LLM-as-judge.
- Result: vector-based agentic RAG won 68% of comparisons over hierarchical node-traversal, at comparable latency (5.2s vs 5.98s). Counterweight to PageIndex's vendor claims, tree-only navigation is not automatically better.
- Cross-encoder reranking: +59% absolute MRR@5 improvement at (retrieve 10, rerank to 5).
- Small-to-big chunk retrieval: 65% win rate over baseline chunking, +0.2s latency, strong direct evidence for parent-child retrieval (Q5).
- Takeaway for us: the winning recipe combined metadata filtering + hybrid search + small-to-big + reranking inside an agentic loop, i.e., our SQL ladder should keep BM25/pgvector search at the bottom, not replace it with pure tree navigation.

## PageIndex raw README, index-generation parameters
URL: https://raw.githubusercontent.com/VectifyAI/PageIndex/main/README.md, accessed 2026-06-10

- Concrete node-size defaults: `--max-pages-per-node 10`, `--max-tokens-per-node 20000`, `--toc-check-pages 20` (looks for a printed TOC in first 20 pages), default model gpt-4o.
- Summaries and node IDs are optional flags, both default yes: `--if-add-node-summary`, `--if-add-node-id`, plus `--if-add-doc-description` (a doc-level description = our "page card").
- Accepts PDF or Markdown input (`--md_path`), directly applicable to an Obsidian-Markdown knowledge base.
- Retrieval examples: `examples/agentic_vectorless_rag_demo.py` (OpenAI Agents SDK + self-hosted PageIndex) and a vision-based notebook retrieving over page images. Navigation prompt itself not in README, but flow = LLM reasons over tree JSON, returns node IDs, fetches page spans.
- Unit-size signal for our ladder: their "section" node is capped at ~20k tokens / 10 pages, far bigger than classic 256-512-token chunks. The summary layer is what keeps navigation cheap.

## RAPTOR paper (arXiv 2401.18059, ICLR 2024)
URL: https://arxiv.org/html/2401.18059v1, accessed 2026-06-10

- Construction: 100-token leaf chunks (sentence-preserving), then UMAP + soft GMM clustering (BIC picks cluster count, global-then-local clustering), then gpt-3.5-turbo summary per cluster (~131 tokens from children avg 86 tokens, compression ratio 0.28), then recurse upward.
- All nodes (leaves + every summary level) are embedded with SBERT into ONE index.
- Retrieval: "collapsed tree" (flatten tree, cosine top-k across all levels until ~2000-token budget ≈ 20 nodes) consistently BEAT tree traversal (layer-by-layer pruning). Key lesson: don't force level-by-level descent, let query similarity pick the right granularity.
- Numbers: QuALITY 82.6% acc w/ GPT-4 (+20% abs over prior SOTA), QASPER 55.7 F1 (+2.7 over DPR), NarrativeQA METEOR 19.1 SOTA.
- For our schema: embedding card/summary rows AND chunk rows in the same vector space, letting one query hit any level, is the RAPTOR-validated pattern. The agent ladder is complementary (for reasoning), not a replacement for flat multi-level search.

## RAPTOR adoption status (RAGFlow integration)
URL: https://ragflow.io/docs/enable_raptor (+ https://ragflow.io/blog/long-context-rag-raptor), accessed 2026-06-10 (via search snippets)

- RAGFlow integrated RAPTOR from v0.6.0 as a preprocessing option, but the switch is OFF by default because it "consume[s] more token quotas", summarization cost is the adoption blocker.
- Practitioner consensus 2025-2026: RAPTOR helps long/complex docs and multi-step reasoning questions, but LLM-summary build cost + index complexity keep it niche. Collapsed-tree mode is the only variant people use.
- Mostly survives as an optional feature in frameworks (RAGFlow, LlamaIndex llama-pack) rather than a default architecture.

## LazyGraphRAG announcement (Microsoft Research blog, Nov 2024, editor's note Jun 2025)
URL: https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost/, accessed 2026-06-10

- Full GraphRAG index = LLM-extracted entities + relationships + LLM entity/relationship summaries + hierarchical (Leiden) community structure + LLM community summaries. Expensive: LazyGraphRAG's index costs 0.1% of full GraphRAG's.
- LazyGraphRAG: defer ALL LLM work to query time. Index = NLP noun-phrase concept extraction + co-occurrence graph + graph stats only (indexing cost ≈ identical to plain vector RAG). At query time: iterative deepening with a "relevance test budget", cheap LLM relevance tests over best-first-ranked text chunks/communities, then partial answers.
- Numbers: at 4% of GraphRAG global-search query cost it beat all 8 competing conditions (vector RAG, GraphRAG local/global/DRIFT, RAPTOR...) on local AND global queries. >700x lower query cost than GraphRAG global for comparable quality (budget=500 variant strongest).
- Big design lesson: pre-paying LLM summarization for EVERYTHING is wasteful. Lazy, query-time summarization over a cheap structural index wins on cost-quality. Supports our "index cheap cards universally, summarize deeply on demand" instinct.

## GraphRAG status mid-2026 (search roundup)
URLs: https://github.com/microsoft/graphrag, https://github.com/microsoft/graphrag/discussions/1490, accessed 2026-06-10 (search snippets)

- GraphRAG 1.0 shipped Dec 2024. ~31.6k stars. v3.0.0 Jan 2025 was a monorepo restructure. Enterprise adoption real but concentrated where multi-hop/global "whole-knowledge-base" questions matter (legal, compliance, research). $33k-class indexing bills for large knowledge bases drove the Lazy/LightRAG wave. (One SEO blog claimed a "Mar 2026 release", not corroborated by the GitHub releases page, ignore.)
- LazyGraphRAG shipped inside Microsoft Discovery + Azure Local (2025). OSS-library integration was long an open discussion (#1490) and is NOT in OSS release notes through v3.1.0 (May 2025).
- Community consensus: full GraphRAG = high-cost, high-structure outlier. LazyGraphRAG/LightRAG-style "cheap graph + lazy summarization" is the practically adopted variant.
- For us: entity/community extraction is orthogonal to our doc-tree ladder. The transferable piece is lazy query-time deepening with an explicit relevance-test budget.

## microsoft/graphrag releases page
URL: https://github.com/microsoft/graphrag/releases, accessed 2026-06-10

- Latest visible release: v3.1.0 (May 28, 2025). v3.0.0 (Jan 2025) split the repo into seven packages (graphrag-cache/-chunking/-common/-input/-llm/-storage/-vectors).
- No LazyGraphRAG / lazy-search in any OSS release notes, confirms LazyGraphRAG remains productized (Microsoft Discovery, Azure Local) rather than open-sourced as of mid-2026.
- Recent OSS work is infrastructure (Cosmos storage, parquet input, NLP streaming), not new retrieval modes. Project is in maintenance/platform mode, hype has cooled.

## Anthropic, Introducing Contextual Retrieval (Sep 2024)
URL: https://www.anthropic.com/news/contextual-retrieval, accessed 2026-06-10

- Technique: for each chunk, Claude generates 50-100 tokens of situating context from the WHOLE document ("situate this chunk within the overall document...for search retrieval"), prepended before BOTH embedding and BM25 indexing (contextual embeddings + contextual BM25).
- Failure-rate numbers (top-20 retrieval failure): baseline 5.7%, contextual embeddings 3.7% (-35%), plus contextual BM25 2.9% (-49%), plus reranking 1.9% (-67%).
- Cost: $1.02 per million document tokens one-time, using prompt caching (cache the full doc, iterate chunks).
- Guidance: retrieve top-20 (beat top-5/10). Best embedders Gemini/Voyage. Skip RAG entirely under ~200k-token knowledge bases (just prompt-cache the whole thing).
- For our schema: validates storing a `context_header`/breadcrumb column per chunk and indexing title+context+chunk text in BOTH tsvector/BM25 and pgvector columns, cheap, large win, no architecture change.

## Contextual chunk headers / breadcrumb-prefix variants (practitioner roundup)
URLs: https://github.com/NirDiamant/RAG_Techniques/blob/main/all_rag_techniques/contextual_chunk_headers.ipynb (dsRAG-derived), https://mbrenndoerfer.com/writing/document-chunking-rag-strategies-retrieval, accessed 2026-06-10 (search snippets)

- The cheap variant of Anthropic's technique: "Contextual Chunk Headers" (CCH, from dsRAG's AutoContext), prepend document title + section-heading path (a breadcrumb trail) to each chunk before embedding. No LLM call needed if headings exist (LLM only to synthesize doc title/summary).
- Claimed effect: "substantial improvement in retrieval quality". Practitioners cite Anthropic's ~35% failure reduction as the ceiling reference. CCH gets much of it for free in heading-structured knowledge bases (like Markdown).
- Key implementation rule repeated across posts: put the breadcrumb IN the chunk text, not in a metadata column, that way it rides into both the embedding and the BM25/tsvector index.
- Heading-aware chunking (split on Markdown headings, carry the `H1 > H2 > H3` path) is the 2025-2026 default in tools like Unstructured ("contextual chunking") and most RAG cookbooks.
- For us: our knowledge base is Obsidian Markdown with real headings. Breadcrumb prefix (`Paper > Section > Subsection`) is nearly free and should be baked into the chunk-row text at indexing time. Optionally add LLM situating context only for chunks whose headings are weak.

## LlamaIndex Auto-Merging Retriever (official example notebook)
URL: https://developers.llamaindex.ai/python/examples/retrievers/auto_merging_retriever/, accessed 2026-06-10

- HierarchicalNodeParser defaults: 3 levels at 2048 / 512 / 128 tokens. Only LEAF nodes (128) go in the vector index. All nodes live in a docstore keyed by parent links.
- Auto-merge rule: if a majority of retrieved leaves share a parent, swap them for the parent node (e.g., "merging 4 nodes into parent", 6 base to 3 merged nodes).
- Their OWN eval on the example knowledge base: auto-merging 4.27 vs base 4.21 correctness, pairwise preference 0.525, "roughly the same". Honest null-ish result. Contrast with arXiv 2511.18177's 65% win rate for small-to-big in finance docs. Domain-dependent: helps when answers need surrounding context (long structured docs), not on short-form knowledge bases.
- Pattern for us: embed small (~128-512 tokens), return big (section), implemented in SQL as chunk-to-section FK + a "merge to section when ≥N sibling chunks hit" rule in the retrieval tool, or simply expose section fetch as the next ladder rung and let the agent decide.

## Agentic retrieval 2025-2026, shift away from one-shot RAG
URLs: https://sesamedisk.com/direct-corpus-interaction-ai-retrieval/ (DCI roundup), https://arxiv.org/abs/2602.03442 (A-RAG, Feb 2026), https://arxiv.org/abs/2509.04820 (Fishing for Answers, Sep 2025), accessed 2026-06-10

- Strongest production signal: Claude Code dropped vector RAG for agentic glob+grep+read. Boris Cherny (Latent Space, May 2025) said it outperformed vector RAG "by a lot". 2026 papers formalize this as "Direct KnowledgeBase Interaction" (Li, Lu, Choi, Han et al., May 2026).
- A-RAG (arXiv 2602.03442): agent gets exactly three hierarchical tools, keyword search, semantic search, chunk read, and adaptively retrieves "across multiple granularities". Consistently beats existing approaches at comparable or LOWER retrieved-token counts. Gains scale with model size + test-time compute. This is our ladder, validated: don't predefine the workflow, give the model granular tools.
- "Fishing for Answers" (arXiv 2509.04820, gov/legal docs): frames the two camps, one-shot with adaptive token budgets vs iterative agentic refinement. Motivation: fixed top-k "frequently misses golden chunks". (Quant results paywalled in abstract.)
- LlamaIndex's own benchmarks (cited in 2026 roundups): agentic retrieval gives higher correctness/relevance than single-shot, at higher latency, the recurring trade-off.
- Consensus mid-2026: iterative beats one-shot for complex/multi-hop questions. One-shot still fine for simple lookups. Best systems route between them.

## SQL/metadata pre-filtering before vector search (pgvector mechanics)
URLs: https://github.com/pgvector/pgvector (README), https://encore.dev/blog/you-probably-dont-need-a-vector-database, accessed 2026-06-10

- pgvector's HNSW filtering is POST-filter by default ("filtering is applied after the index is scanned"), selective WHERE clauses can starve results. v0.8.0 added iterative index scans (`hnsw.iterative_scan = strict_order|relaxed_order`) that keep scanning until the limit is met.
- Pre-filtering (SQL WHERE first, then exact/brute-force vector scan over the subset) is recommended when the filtered subset is small. For a knowledge-base-scale knowledge base (thousands of sections), exact scan over a metadata-filtered subset is cheap and gives perfect recall. The ANN index is for the unfiltered case.
- pgvector's own recommended patterns for filters: B-tree index on filter columns (exact, fast when few rows match), partial HNSW indexes for low-cardinality filters, LIST partitioning for high-cardinality.
- Agent-constructed metadata filters (Mastra, LlamaIndex auto-retrieval, LangChain self-query) are a standard 2025-2026 pattern: LLM turns the question into filters + query text. Filters shrink the candidate set before similarity. Evidence base is mostly engineering reports, not papers, but the mechanism (smaller search space gives higher precision, exact recall within filter) is uncontroversial and arXiv 2511.18177's winning system used metadata filtering.
- For our SQL ladder: expose paper/section metadata (year, venue, tags, paper_id) as real columns. Teach the agent to WHERE-filter first, then rank by BM25/vector within the subset. Postgres is unusually good at this because filter+search is one planner.

## VectorChord 0.3, late interaction / MaxSim in PostgreSQL (Apr 14, 2025)
URL: https://blog.vectorchord.ai/vectorchord-03-bringing-efficient-multi-vector-contextual-late-interaction-in-postgresql, accessed 2026-06-10

- VectorChord 0.3 (pgvector-compatible extension, NOT pgvector itself) added a MaxSim SQL operator `@#` and multi-vector indexing (`vchordrq` + `vector_maxsim_ops`): late interaction implemented as one single-vector search per query token (IVF + RaBitQ) aggregated by MaxSim.
- FiQA (57k docs): NDCG@10 34.1 (≥ WARP paper's 33.6), ~35 ms/query, credible Postgres-native ColBERT.
- Their recommended pattern: MaxSim as a RERANK layer over a candidate set from fast first-stage search (vector or keyword), not as primary retrieval.
- pgvector proper has NO native multi-vector/maxsim support (confirmed via README, June 2026). 2026 benchmark roundups call VectorChord the late-interaction leader in Postgres. MUVERA (multi-vector to fixed single-vector encodings) shows sub-ms rerank latencies in research but isn't in pgvector.
- Verdict for us: late interaction in Postgres = possible but requires swapping to VectorChord, defer. BM25 + dense + (cross-encoder or MaxSim app-side) rerank covers the need at our scale.

## LangChain ParentDocumentRetriever / MultiVectorRetriever (multi-representation indexing)
URLs: https://python.langchain.com/api_reference/langchain/retrievers/langchain.retrievers.parent_document_retriever.ParentDocumentRetriever.html, https://community.fullstackretrieval.com/indexing/parent-document-retriever, accessed 2026-06-10

- ParentDocumentRetriever: embed small child chunks, look up parent IDs at query time, return the larger parent. Canonical sizes in the official example: child chunk_size=400 chars (~100 tokens), parent chunk_size=2000 chars (~500 tokens). Parents in a docstore, children in the vectorstore, linked by doc_id (`id_key`).
- MultiVectorRetriever generalizes it: per document, embed any of (a) smaller chunks, (b) an LLM summary, (c) hypothetical questions, all pointing at the same parent via id_key. Retrieval returns the parent doc, never the proxy representation.
- "Embed summaries, return full docs" is the exact multi-representation pattern recommended for our card layer: the card text is the retrieval proxy. The payload is the section/page.
- Still shipped and documented in LangChain 2026 (classic retrievers package). Among the most-recommended "advanced RAG" techniques in practitioner guides, low complexity, no extra infra.

---

# Synthesis, recommended ladder for the knowledge base's SQL schema (2026-06-10)

What the evidence converges on:

1. **Multi-resolution beats single-resolution** (RAPTOR, A-RAG, small-to-big finance eval), but **flat access across levels beats forced top-down traversal** (RAPTOR collapsed-tree, arXiv 2511.18177 vector-agentic beats tree-only). The ladder should be a set of independently searchable levels + cheap moves between them, not a mandatory descent.
2. **Navigation layer = title + summary + span pointer** (PageIndex node schema), kept cheap. **Payload text lives once** at the bottom (PageIndex stores page spans, LangChain docstore).
3. **Lazy beats eager summarization** at knowledge base scale (LazyGraphRAG 0.1% index cost, 700x cheaper global queries). Pre-compute cheap cards for everything. Deep summaries on demand/by tier.
4. **Context prefixes are the cheapest big win** (Anthropic -49% retrieval failures w/ contextual embeddings+BM25, -67% w/ rerank, CCH breadcrumbs ≈ free on Markdown headings).
5. **Agent-controlled granular tools scale** (A-RAG: keyword + semantic + chunk-read tools, lower tokens, better answers, Claude Code's grep+read precedent).

Proposed levels:

| Level | Unit | Size | Embedded? | Notes |
|---|---|---|---|---|
| L0 card | one-line per page/paper | ~50-150 tokens | YES | also BM25, scan-all is feasible knowledge-base-wide (LazyGraphRAG-style cheap index) |
| L1 section card | heading-path + 3-6 sentence summary + span pointer | ~100-200 tokens | YES | PageIndex node analog, node payload cap ~2k-8k tokens (PageIndex allows 20k, papers' sections are smaller) |
| L2 chunk | breadcrumb prefix + text | 200-500 tokens (child ~400 chars-512 tokens) | YES | the only "classic" retrieval unit, prefix baked into text for BM25+vector |
| L3 full section/page | rendered Markdown | 0.5k-8k tokens | no (fetch by ID) | returned via parent_id from L2 hits (small-to-big) |
| L4 raw source span | PDF page range / source lines | n/a | no | provenance, PageIndex start/end_index analog |

- All embedded rows (L0, L1, L2) share one vector space + one BM25 index, with a `level` column, RAPTOR-collapsed-tree-style cross-level search in a single query.
- Metadata columns (paper_id, year, venue, tags, level) for SQL WHERE pre-filtering. At knowledge-base scale prefer exact scan within filter. pgvector ≥0.8.0 iterative scans if using HNSW.
- Rerank step (cross-encoder) before answering: biggest single quality lever in two independent sources (+59% MRR@5, -67% failures).
- Skip late interaction for now (needs VectorChord, rerank covers it). Skip entity/community graph (GraphRAG), cost not justified for single-knowledge-base paper collection. Revisit for cross-paper "global" questions.

Uncertainties: PageIndex's 98.7% is vendor-graded. Parent-child gains are domain-dependent (LlamaIndex's own eval ≈ tie). Agentic-ladder gains cost latency. LazyGraphRAG is closed-source so its budgeted relevance-test loop must be reimplemented, not reused.
