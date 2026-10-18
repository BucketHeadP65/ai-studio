---
id: pb.when-to-graph
type: playbook
title: "When to use graph-RAG over chunk-RAG"
status: draft
summary: Decision guide for choosing graph-RAG vs chunk-RAG based on task type, corpus structure, and accuracy requirements.
maturity: draft
tasks: [retrieval, knowledge-management]
recommends:
  - "[[lightrag]]"
advises_against:
  - "[[naive-rag]]"
synthesizes:
  - "[[lightrag-2025]]"
---

## TL;DR

Use graph-RAG for sensemaking tasks over long, inter-connected corpora. Use chunk-RAG for factoid retrieval with tight accuracy requirements.

## Decision criteria

Apply graph-RAG when the task is sensemaking (comprehension, synthesis) over a domain-specific corpus and win-rate metrics are acceptable.
