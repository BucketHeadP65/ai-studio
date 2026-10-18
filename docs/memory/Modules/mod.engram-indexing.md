---
id: mod.engram-indexing
aliases: [mod.engram-indexing]
type: module
title: "engram.indexing: the knowledge base indexer"
status: active
path: engram/src/engram/indexing
governed_by:
  - "[[engram-adr-0014-domain-general-vault-spec-and-plugin]]"
  - "[[engram-adr-0019-true-incremental-ingestion]]"
---

# engram.indexing: the knowledge base indexer

Turns a knowledge base's markdown files into the `kb.*` Postgres tables. It parses, resolves, validates, loads and
embeds every file in a full run, or only the changed files and the pages that link to them in an incremental run.
The decisions behind the shape are the ones linked above.
