---
id: ent.ultradomain
type: entity
title: UltraDomain
aliases: [UltraDomain benchmark]
summary: Long-corpus RAG benchmark (Qian et al., 2024) built from 428 college textbooks across 18 domains; papers typically use subsets (e.g. Agriculture, CS, Legal, Mix; 600k–5M tokens each) with LLM-generated global sensemaking questions.
status: stub
created: 2026-06-10
updated: 2026-06-10
entity_type: benchmark
tasks: [long-doc-qa, sensemaking-qa]
modalities: [text]
---

## TL;DR

The de-facto arena for graph-RAG sensemaking claims: very long domain corpora + corpus-level questions. Note: question sets are usually LLM-generated per paper (GraphRAG's persona method), so "UltraDomain results" across papers are not strictly the same test.

## Definition

A benchmark sourcing long, domain-specific corpora from college textbooks (18 domains). Evaluations in this wiki so far use the Agriculture, CS, Legal, and Mix subsets with 125 LLM-generated sensemaking questions each ([[lightrag-2025]] §4.1).

## Evidence

- Used in [[lightrag-2025]] (^c1, ^c2, ^c3, ^c7, ^c8) — LLM-judged win-rate protocol.
- ⚠ Comparability caveat: per-paper question generation means cross-paper number comparisons on "UltraDomain" need protocol checks.
- [[leanrag-2025#^c1]] (+ ^c2, ^c3, ^c4, ^c5, ^c6, ^c7, ^c8, ^c10, ^c11, ^c12) — Same four subsets as the LightRAG eval; doc/token statistics now on record (Mix 61/626k, CS 10/2.2M, Legal 94/5.3M, Agriculture 12/2.0M — Appendix B Table 5).
- [[hirag-2025#^c1]] (+ ^c2, ^c13, ^c14) — Main benchmark; uses the benchmark-provided queries (unlike LightRAG's self-generated ones); doc/token stats per subset in Appendix A.
