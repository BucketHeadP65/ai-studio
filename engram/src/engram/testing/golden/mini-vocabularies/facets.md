---
id: meta.vocabulary-facets
type: meta
title: "Vocabulary: facet keys & controlled values"
summary: The structured filter vocabulary — flat list-typed frontmatter keys that index to typed Postgres columns. Values are open (add rows); keys need an ADR.
status: verified
---

# Vocabulary: facets

Facets are the **noise-reduction surface**. Facets are flat frontmatter keys, list-typed unless noted, values are lowercase-hyphen slugs.

## `tasks` — on paper, entity, comparison, playbook, question

What problem formulation it concerns. Seed: `open-domain-qa`, `multi-hop-qa`, `long-doc-qa`, `summarization`, `fact-verification`, `sensemaking-qa` (global corpus-understanding questions, GraphRAG-eval style; added at lightrag ingest 2026-06-10).

## `domains` — on paper, playbook

Corpus/application domain of the evidence. Seed: `encyclopedic`, `legal`, `biomed`, `mixed`, `literary` (fiction/biography/philosophy corpora — the domain where GraphRAG-vs-LightRAG ordering flips; added at memgraphrag ingest 2026-06-10).

## `stages` — on entity (techniques), comparison, playbook

Pipeline stage a technique lives in (generic key; seeded for RAG). Seed: `indexing`, `chunking`, `retrieval`, `reranking`, `generation`.

## `scale_regimes` — on playbook, comparison

Corpus size band guidance applies to (**closed**, defined):

| Value | Definition |
|---|---|
| `toy` | < 1k documents |
| `small` | 1k – 100k documents |
| `large` | 100k – 10M documents |
| `web-scale` | > 10M documents |

## `paper_type` — on paper (single value)

`method` · `survey` · `benchmark` · `analysis` · `position` · `engineering-report`

## `kind` — on claim blocks (single value)

Evidence kind, ordered roughly by weight:

| Value | Meaning |
|---|---|
| `benchmark-result` | Quantitative result on a named dataset/metric |
| `ablation` | Component-removal evidence |
| `negative-result` | No-effect / failure finding (first-class — pruning depends on these) |
| `theoretical` | Proof or complexity argument |
| `observation` | Our own synthesis-time observation (must say so) |

## `eval_setting` — claim qualifier (single value)

`zero-shot` · `few-shot` · `fine-tuned`
