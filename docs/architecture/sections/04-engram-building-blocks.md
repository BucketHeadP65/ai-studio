<!-- GENERATED from architecture.md by scripts/docs/split_doc.py --dir docs/architecture — do not edit here; edit the master and re-run the script. -->

## 4. engram building blocks
> **TL;DR:** A knowledge base to Postgres indexer and read-only retrieval library, organized into five bounded contexts (Ingestion, Knowledge, Retrieval, Evaluation, Lifecycle & interchange). Its structure has graduated into engram's own arc42/C4 master, linked below.
<!-- load-when: engram, knowledge base, memory, compile, indexing, serve, sync, retrieval, knowledge base pipeline, note grammar, mcp, Studio backend, bounded contexts -->

The `engram` library (`engram/`, distribution `engram`) owns the whole knowledge base
pipeline. It absorbed the standalone Zotero sync tool as slices
([ADR 0003](../../adr/0003-absorb-zotero-sync-into-wiki-app.md)) and then shed its
application half to `studio.corpus`
([engram ADR 0039](../../../engram/docs/adr/0039-library-app-package-split.md),
[ADR 0009](../../adr/0009-ai-factory-studio-consolidation.md)). Its building blocks
are organized into **five bounded contexts**, **Ingestion** (Zotero to
`Sources/` mirror reconcile and PDF/HTML to Markdown conversion), **Knowledge**
(the note-grammar kernel and the indexing pipeline), **Retrieval** (the
read-only query surface the CLI / HTTP / MCP plugins serve, behind the
`corpus_reader` role), **Evaluation** (structural ground-truths, the tuning
loop, and retrieval metrics), and **Lifecycle & interchange** (knowledge base
init/scaffold, OKF export, and the Claude Code plugin), over a cross-cutting
CQRS / DB / DI core. Outside consumers reach all of it through the lazy plugins in
`engram/__init__.py`.

engram's structure has **graduated out of this section** per
[ADR-0005](../../adr/0005-docs-as-code-standard.md)'s graduation rule and the layered
three-view pattern of engram ADR-0020: the per-slice C4 L3 **component** diagrams, the per-slice
**domain-model class** diagrams, and the single system **ERD** now live in
engram's own arc42/C4 master, **[engram/docs/architecture/index.md](../../../engram/docs/architecture/index.md)**.
Glossary: [engram/CONTEXT.md](../../../engram/CONTEXT.md).
