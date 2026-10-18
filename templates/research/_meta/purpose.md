---
id: meta.purpose
aliases: [meta.purpose]
type: meta
title: Purpose & scope
summary: What this knowledge base is for, what belongs in it, what stays out, and who consumes it.
status: draft
---

# Purpose & scope

## Mission

One or two sentences: the knowledge base's domain, the indexed form its knowledge takes, and the compounding claim
it makes (each new admission enriches existing pages rather than sitting beside them as a chunk).

## In scope

What counts as a primary source here, and what synthesis this knowledge base authors itself over those sources.

## Out of scope

What looks related but never becomes a page here, and where that material belongs instead.

## Consumers

Who reads this knowledge base and through which surface, one row per consumer.

## Growth rule

How a new domain enters this knowledge base by registering vocabulary rather than inventing new page types,
folders, or frontmatter keys, and what requires a decision recorded first.

## Non-negotiables

The handful of rules this knowledge base never breaks, regardless of domain.

## Example (the research knowledge base)

The text below is quoted verbatim from the Knowledge base's own `purpose.md`, dangling `[[links]]` and
`_meta/...` paths included. They point into that knowledge base, not into this template.

### Mission

An **indexed research knowledge base**: research literature is ingested once into durable, cross-linked, claim-grounded Markdown, which a Python indexer loads into Postgres (SQL + BM25 + vectors) to supercharge specialized agents. The knowledge base is **research-generic by structure**. Its first domain is **retrieval/RAG and context engineering for LLM systems**, where the end goal is agents that co-design RAG pipelines: which strategy, when, how to combine, and which strategies to prune as evidence accumulates.

Knowledge must **compound**: every ingested paper enriches entity pages, comparisons, and playbooks. It is never just an appended chunk.

### In scope

- Peer-reviewed papers and arXiv preprints (primary, `paper_type: method | survey | benchmark | analysis | position`).
- Benchmark/leaderboard reports and high-signal engineering reports from teams about their own systems (secondary, `paper_type: engineering-report`).
- Synthesis we author ourselves: entity pages, concepts, comparisons, playbooks, open questions.

### Out of scope

- News, release notes, marketing, tutorials without evaluable claims.
- Full bibliographies (Zotero owns reference management, this knowledge base holds *knowledge*, keyed to Zotero by `zotero_key`).
- Project/task tracking of any kind.

### Consumers

| Consumer | Surface |
|---|---|
| Human (you) | Obsidian: browse, search, Dataview dashboard |
| Authoring agents (Claude) | `_meta/schema.md` contract + templates + registries |
| Query agents (RAG co-designers) | Postgres `api.*` views/functions indexed from this knowledge base, see `_meta/compilation/postgres-target.md` |

### Growth rule

New domains (e.g. fine-tuning, agent architectures, non-ML research) are added by **registering vocabulary** (new facet values, entity types, relation types in `_meta/vocabulary/`), **never** by inventing new page types, folders, or frontmatter keys. A new page type requires an ADR in `_meta/design/decisions.md` first.

Whole new knowledge bases are **collections** (D-19): a `collection` frontmatter key + their own `purpose.md`/maps on the same backbone, in the same DB. Knowledge bases with different privacy/sharing requirements get their own *deployment* of the identical contract. Agents federate across deployments by fanning out the same query (no DB gateway).

### Non-negotiables

1. `Sources/` (raw full-text mirrors) is immutable, the knowledge base points into it, never edits it.
2. Every substantive claim carries a span-level locator and quote (see claims grammar in [[schema]]).
3. Contradictions are preserved and flagged, never blended away.
4. Anything in Postgres must be rebuildable from this knowledge base. The database is a disposable derivative.
