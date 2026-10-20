<!-- GENERATED from architecture.md by scripts/docs/split_doc.py --dir engram/docs/architecture — do not edit here; edit the master and re-run the script. -->

## 1. Orientation & scope
> **TL;DR:** engram is a domain-general Obsidian-style markdown knowledge-base indexer (knowledge base to Postgres) plus a read-only retrieval server over CLI + HTTP + MCP, organized into five bounded contexts (Ingestion, Knowledge, Retrieval, Evaluation, and Lifecycle & interchange) over a cross-cutting CQRS / DB / DI core.
<!-- load-when: orientation, scope, what is engram, bounded contexts, how to navigate, arc42 -->

engram (`engram/`, dist `engram`) is a **knowledge-base-to-Postgres indexer and
read-only retrieval server**. It indexes an Obsidian-style markdown knowledge base
into Postgres (the local Postgres by default, ParadeDB opt-in,
[ADR-0017](../../adr/0017-local-postgres-primary-testbed.md)), driven by a per-knowledge-base
`KnowledgeBaseSchema`, so it serves any domain
([ADR-0014](../../adr/0014-domain-general-vault-spec-and-plugin.md)), with no
default ontology of its own. A knowledge base that declares no `knowledge-base-schema.json` is
refused, and the schemas, guidance pages and workflows of the knowledge bases this repo
runs live in the top-level `templates/` package
([root ADR-0018](../../../docs/adr/0018-corpus-bundles-and-provider-packages.md)). It then serves hybrid
retrieval (BM25 + vector + graph) read-only over a CLI, a local FastAPI Studio
API, and an MCP stdio server. It owns the whole knowledge-base pipeline: the optional
Zotero-to-knowledge-base mirror reconcile, the indexer, the `wikinote` note grammar, a CQRS
bus, and a retrieval-tuning loop.

**The five bounded contexts** (the building-block organization fixed by
[ADR-0020](../../adr/0020-domain-documentation-architecture.md), section 3 below):

| Context | Owns | Slices |
|---|---|---|
| **Ingestion** (section 3) | getting source documents into the knowledge base as mirrors | `connector`/`converter` (the two ports), `registry` (entry-point discovery), `fetch` (the SSRF guard), `mirror`, `sync`, `source_queue`, `watch_list`. The connector implementations live in `../connectors/` |
| **Knowledge** (section 4) | the note grammar + turning the knowledge base into queryable Postgres | `wikinote` (kernel), `index` |
| **Retrieval** (section 5) | the read-only query surface agents and AI Factory Studio use | `serve` |
| **Evaluation** (section 6) | generating and scoring retrieval quality, tuning the policy | `evals` |
| **Lifecycle & interchange** (section 7) | bootstrap, interchange export, plugin packaging | `init`, `export`, `plugin` |
| **Cross-cutting** (section 8) | the bus, DB foundation, DI, settings, observability, CLI | `cqrs`, `db`, `container`, `settings`, `observability`, the `cli*` modules |

**How to navigate this layered documentation.** The **master** (this doc) is
where you go for *structure*, a diagram of how the pieces fit. The **domain
views** (forthcoming, `engram/docs/explanation/domain/`) are where you go for the
*narrative*, what the domain means, told three ways for three audiences. The
**reference docs** (`engram/docs/reference/<slice>.md`) are where you go for a
slice's *contract*, its entry points, public types, and testing seams. The
**ADRs** (`engram/docs/adr/`) are where you go for the *why*. One fact has one
home. This master does not restate a contract or a rationale that lives in
another layer.
