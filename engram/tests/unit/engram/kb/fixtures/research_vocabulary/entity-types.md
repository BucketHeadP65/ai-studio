---
id: meta.vocabulary-entity-types
type: meta
title: "Vocabulary: entity types"
summary: Controlled vocabulary for `entity_type` on entity pages. Add rows freely (with a definition); adding obligations or removing types needs an ADR.
status: verified
created: 2026-06-10
updated: 2026-06-10
---

# Vocabulary: entity types

The value of `entity_type` in entity-page frontmatter. Research-generic; seeded for the RAG/retrieval domain. **Rule:** before minting an entity, search existing pages *and aliases*; when fuzzy between two types, prefer the more specific one and note the doubt in the page.

| entity_type | Definition | Examples (illustrative) |
|---|---|---|
| `technique` | A method/algorithm/strategy that can be applied, composed, or compared | `lightrag`, `hyde`, `contextual-retrieval`, `semantic-chunking` |
| `task` | A problem formulation systems are evaluated on | `multi-hop-qa`, `long-doc-qa`, `fact-verification` |
| `problem` | A failure mode or difficulty techniques address | `lost-in-the-middle`, `retrieval-latency`, `context-poisoning` |
| `dataset` | A concrete evaluation/training corpus | `ultradomain`, `hotpotqa`, `musique` |
| `benchmark` | A suite/protocol aggregating datasets + methodology (`includes` its datasets) | `beir`, `ragbench` |
| `metric` | A quantity used to score systems | `ndcg-at-10`, `llm-judged-win-rate`, `faithfulness` |
| `tool` | A concrete software system/library/product | `faiss`, `paradedb`, `neo4j` |
| `model` | A specific LLM/embedding/reranker model that evidence depends on | `gpt-4o-mini`, `bge-m3` |
| `concept` | A named abstract notion that is referenced as a thing but isn't a technique | `attention-budget`, `knowledge-compounding` |

## Boundary rules

- **technique vs tool:** LightRAG-the-method is a `technique`; the HKUDS repo implementing it is an `artifact` URL on the paper note, not a page. Mint a `tool` page only when the tool itself accumulates evidence (e.g. ParadeDB).
- **technique families** are `technique` pages too, linked by `is_a`/`variant_of` (e.g. `semantic-chunking` *is_a* `chunking`). No separate "family" type.
- **dataset vs benchmark:** if it has sub-datasets and a protocol, it's a `benchmark` with `includes` edges; otherwise `dataset`.
- **model pages are cheap but load-bearing:** claims' conditions often hinge on judge/generator models — link them so SQL can ask "evidence judged by which model?".
- Authors, venues, and papers are **not** entities — papers are `paper` pages; authors/venues live in Zotero.
