---
id: paper.lightrag-2025
type: paper
title: "LightRAG: Simple and Fast Retrieval-Augmented Generation"
aliases: [LightRAG paper]
summary: Graph-indexed RAG with dual-level (entity+theme) retrieval and incremental index updates. LLM-judged win-rate gains over NaiveRAG/RQ-RAG/HyDE/GraphRAG on UltraDomain long corpora (esp. Legal), ~100-token single-call retrieval — but near-tie vs GraphRAG on Mix.
status: synthesized
created: 2026-06-10
updated: 2026-06-10
zotero_key: 8QXFXYMI
citekey: guo2025lightrag
year: 2025
venue: arXiv
paper_type: method
source_note: "[[LightRAG - Simple and Fast Retrieval-Augmented Generation]]"
source_hash: bd72eebf611df1366b251c16ce4eac7aac699e519783c586feb2c5c43899347a
artifacts:
  code: https://github.com/HKUDS/LightRAG
tasks: [long-doc-qa, sensemaking-qa]
domains: [mixed]
stages: [indexing, retrieval]
modalities: [text]
proposes: ["[[lightrag]]", "[[dual-level-retrieval]]"]
evaluates: ["[[graphrag]]", "[[naive-rag]]", "[[hyde]]", "[[rq-rag]]"]
uses_datasets: ["[[ultradomain]]"]
builds_on: ["[[graphrag-2024]]"]
---

## TL;DR

[[lightrag]] indexes a corpus as an LLM-extracted entity/relation graph and retrieves at two levels — local keywords → specific entities, global keywords → relations/themes — plus one-hop neighbors, instead of traversing GraphRAG-style community reports. On four UltraDomain long corpora it wins GPT-4o-mini-judged comparisons against chunk-based baselines decisively and against [[graphrag]] moderately (except a near-tie on Mix), while cutting retrieval to <100 tokens and one API call and supporting incremental index updates without graph rebuilds. Evidence caveat: all results are LLM-judged win rates on LLM-generated questions; no factual-accuracy metric.

## Context

Targets two failure modes of flat chunk-vector RAG: fragmented answers to questions that span entity inter-dependencies, and stale indexes in dynamic corpora. Positions against [[graphrag]] (community summaries are expensive to build, query, and rebuild on update) — same graph-indexing family ([[graph-enhanced-rag]]), different retrieval and maintenance economics.

## Approach

- **Graph-based text indexing:** chunks (1,200 tokens) → LLM extracts entities + relations with text descriptions → dedup/profiling into a knowledge graph; key-value profiles store names, descriptions, and source excerpts.
- **[[dual-level-retrieval]]:** the query yields *local* keywords (matched to entity nodes) and *global* keywords (matched to relations); retrieved elements are expanded with one-hop neighbors for higher-order relatedness; entity/relation profiles + text excerpts feed generation.
- **[[incremental-index-update]]:** new documents run the same extraction and merge into the existing graph — no community/structure rebuild (§3.4).

## Evaluation setup

- **Data:** four [[ultradomain]] subsets — Agriculture, CS, Legal, Mix; 600k–5M tokens each (§4.1, Table 4).
- **Questions:** 125 per dataset, LLM-generated via the GraphRAG method (5 personas × 5 tasks × 5 questions), targeting whole-corpus sensemaking.
- **Baselines:** [[naive-rag]], [[rq-rag]], [[hyde]], [[graphrag]]; all LLM ops and judging use [[gpt-4o-mini]]; chunk size 1,200; gleaning 1; nano vector DB.
- **Metric:** [[llm-judged-win-rate]] over comprehensiveness / diversity / empowerment / overall, answer order alternated.

## Findings

- LightRAG achieves higher LLM-judged Overall win rates than GraphRAG on the three large UltraDomain subsets (Agriculture 54.8%, CS 52.0%, Legal 52.8%). ^c1
    - loc:: §4.2, Table 1 (GraphRAG block, Overall row)
    - quote:: "LightRAG consistently outperforms GraphRAG, particularly in larger datasets with complex language contexts"
    - kind:: benchmark-result
    - outperforms:: [[graphrag]]
    - on_dataset:: [[ultradomain]]
    - metric:: [[llm-judged-win-rate]]
    - condition:: hybrid mode; GPT-4o-mini as generator and judge; 125 LLM-generated sensemaking questions per dataset; chunk 1200, gleaning 1
    - magnitude:: Overall 54.8/52.0/52.8% vs 45.2/48.0/47.2%
- On the Mix subset LightRAG and GraphRAG are effectively tied, with GraphRAG slightly ahead on Overall (50.4% vs 49.6%) and Empowerment (50.8% vs 49.2%) — the paper's "consistently outperforms" framing does not hold there. ^c2
    - loc:: §4.2, Table 1 (GraphRAG block, Overall + Empowerment rows, Mix column)
    - quote:: "50.4%" vs "49.6%"; "50.8%" vs "49.2%"
    - derived:: table
    - kind:: negative-result
    - comparable_to:: [[graphrag]]
    - on_dataset:: [[ultradomain]]
    - metric:: [[llm-judged-win-rate]]
    - condition:: Mix subset — literary/biographical/philosophical texts, the least technical and most heterogeneous of the four
- Graph-based RAG beats chunk-based baselines with the gap widening as corpus size grows; on Legal, chunk-based baselines win only ~20% of comparisons. ^c3
    - loc:: §4.2, Table 1
    - quote:: "consistently outperform purely chunk-based retrieval methods such as NaiveRAG, HyDE, and RQRAG"
    - kind:: benchmark-result
    - outperforms:: [[naive-rag]]
    - supports:: [[graph-enhanced-rag]]
    - on_dataset:: [[ultradomain]]
    - metric:: [[llm-judged-win-rate]]
    - condition:: effect strongest on the largest subset (Legal, ~5M tokens); authors generalize to HyDE and RQ-RAG as well
    - magnitude:: "with baseline methods achieving only about 20% win rates" (Legal)
- LightRAG's largest dimension-level advantage is Diversity, which the authors attribute to dual-level retrieval. ^c4
    - loc:: §4.2
    - quote:: "demonstrates a significant advantage in the Diversity metric, particularly within the larger Legal dataset"
    - kind:: benchmark-result
    - supports:: [[dual-level-retrieval]]
    - metric:: [[llm-judged-win-rate]]
    - condition:: Diversity dimension; the causal attribution is the authors' interpretation, not an isolated test
- Ablation: removing high-level (thematic) retrieval (-High) significantly degrades performance across nearly all datasets and metrics. ^c5
    - loc:: §4.3, Table 2 (-High)
    - quote:: "leading to a significant performance decline across nearly all datasets and metrics"
    - kind:: ablation
    - supports:: [[dual-level-retrieval]]
- Ablation: high-level-only retrieval (-Low) gains comprehensiveness but loses the entity-level depth needed for precise answers. ^c6
    - loc:: §4.3 (-Low)
    - quote:: "the trade-off is a reduced depth in examining specific entities"
    - kind:: ablation
    - supports:: [[dual-level-retrieval]]
- Ablation: removing original text chunks from retrieval (-Origin) causes no significant decline on any of the four subsets and sometimes improves results — entity/relation profiles alone carry sufficient (and less noisy) context. ^c7
    - loc:: §4.3, Table 2 (-Origin)
    - quote:: "does not exhibit significant performance declines across all four datasets"
    - kind:: negative-result
    - supports:: [[graph-enhanced-rag]]
    - condition:: -Origin still uses entity/relation profiles built from the text; improvements seen on Agriculture and Mix
    - magnitude:: -Origin vs NaiveRAG, Overall: 74.4/60.8/84.4/55.6% across Agriculture/CS/Legal/Mix
- LightRAG's retrieval phase costs <100 tokens and one API call, vs GraphRAG's ~610,000 tokens (610 active level-2 community reports × ~1,000 tokens) and hundreds of calls. ^c8
    - loc:: §4.5, Fig. 2
    - quote:: "using fewer than 100 tokens for keyword generation and retrieval, requiring only a single API call"
    - kind:: benchmark-result
    - outperforms:: [[graphrag]]
    - metric:: [[retrieval-cost]]
    - on_dataset:: [[ultradomain]]
    - condition:: Legal subset; GraphRAG built 1,399 communities of which 610 level-2 were used
    - magnitude:: <100 tokens & 1 call vs ~610k tokens & hundreds of calls per query
    - status:: disputed
    - disputed_by:: [[hirag-2025#^c13]]
- For new data, LightRAG merges extracted entities/relations into the existing graph, while GraphRAG must dismantle and regenerate community reports (authors' cost model: ~1,399 × 2 × 5,000 tokens for a Legal-sized increment). ^c9
    - loc:: §4.5
    - quote:: "seamlessly integrates newly extracted entities and relationships into the existing graph without the need for full reconstruction"
    - kind:: theoretical
    - outperforms:: [[graphrag]]
    - metric:: [[index-update-cost]]
    - supports:: [[incremental-index-update]]
    - condition:: authors' analytical cost model, not a measured run; assumes an increment equal in size to the Legal corpus
- The evaluation is entirely GPT-4o-mini-judged pairwise win rates on 125 LLM-generated sensemaking questions per dataset; the judge model is also every system's generator, and no factual-accuracy or groundedness metric is reported. ^c10
    - loc:: §4.1
    - quote:: "We employ a robust LLM, specifically GPT-4o-mini, to rank each baseline against our LightRAG"
    - kind:: observation
    - subject:: [[lightrag]]

## Limitations

Authors acknowledge none explicitly. Observed by us: the eval protocol favors exactly what graph methods provide (global sensemaking questions, diversity-weighted dimensions; see ^c10); the Mix near-tie (^c2) undercuts the headline "consistent" superiority over GraphRAG and suggests the advantage is domain-dependent; win rates measure judge preference, not correctness — retrieval-precision or groundedness on factoid tasks is untested here (chunk-based methods may still win there); cost comparison (^c8) depends on GraphRAG's specific community configuration.

## Practical notes

For co-design: (1) on large heterogeneous corpora with global/sensemaking questions, graph indexing earns its ingest cost — but ^c2 says validate per-domain before defaulting to it; (2) the -Origin result (^c7) is the most reusable surprise: well-profiled entity/relation summaries may *replace* raw-chunk retrieval, shrinking context per query; (3) if the corpus updates frequently, LightRAG-style local graph merges beat community-report architectures on maintenance cost (^c9); (4) treat all magnitudes here as judge-preference, not accuracy. Open question tracked at [[llm-judged-eval-validity]]: how well do LLM-judged win rates track factual quality in RAG comparisons?
