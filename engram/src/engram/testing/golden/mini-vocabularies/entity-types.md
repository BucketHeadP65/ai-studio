---
id: meta.vocabulary-entity-types
type: meta
title: "Vocabulary: entity types"
summary: Controlled vocabulary for `entity_type` on entity pages. Add rows freely (with a definition); adding obligations or removing types needs an ADR.
status: verified
---

# Vocabulary: entity types

The value of `entity_type` in entity-page frontmatter. Research-generic; seeded for the RAG/retrieval domain. **Rule:** before minting an entity, search existing pages *and aliases*; when fuzzy between two types, prefer the more specific one and note the doubt in the page.

| entity_type | Definition | Examples (illustrative) |
|---|---|---|
| `technique` | A method/algorithm/strategy that can be applied, composed, or compared | `lightrag`, `hyde`, `contextual-retrieval`, `semantic-chunking` |
| `task` | A problem formulation systems are evaluated on | `multi-hop-qa`, `long-doc-qa`, `fact-verification` |
| `dataset` | A concrete evaluation/training corpus | `ultradomain`, `hotpotqa`, `musique` |
| `benchmark` | A suite/protocol aggregating datasets + methodology (`includes` its datasets) | `beir`, `ragbench` |
| `metric` | A quantity used to score systems | `ndcg-at-10`, `llm-judged-win-rate`, `faithfulness` |
| this row has no closing pipe value
| `model` | A specific LLM/embedding/reranker model that evidence depends on | `gpt-4o-mini`, `bge-m3` |

## Boundary rules

- **technique vs tool:** LightRAG-the-method is a `technique`; the HKUDS repo implementing it is an `artifact` URL on the paper note, not a page.
- Authors, venues, and papers are **not** entities — papers are `paper` pages; authors/venues live in Zotero.
