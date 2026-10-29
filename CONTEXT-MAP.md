---
id: context-map
aliases: ["context-map"]
type: glossary
app: root
status: current
tags: [index]
title: "Context map"
---

# Context map

The single entry point to every governed document in this repo. Conventions,
frontmatter schema, and tooling live in the docs charter at
[docs/README.md](docs/README.md). Known gaps are TODO rows at the bottom, not
silence. A future docs indexer walks this file as its root.

## System-level

| Doc | What it covers |
|---|---|
| [docs/README.md](docs/README.md) | Docs charter: taxonomy, frontmatter schema, split-doc workflow, graduation rule |
| [docs/knowledge-base-schema.json](docs/knowledge-base-schema.json) | The `repo-docs` estate schema: the 12 governed page types, their required frontmatter, and the relations `validate_docs.py` and `studio index` both read as their one contract |
| [docs/architecture/index.md](docs/architecture/index.md) | Architecture retrieval map: load matching `sections/`, not the whole master |
| [docs/architecture/architecture.md](docs/architecture/architecture.md) | The arc42-lite master (edit here, sections are generated) |
| [docs/explanation/tech-stack.md](docs/explanation/tech-stack.md) | Tech choices: why, alternatives, trade-offs, revisit-when |
| [docs/explanation/design-patterns.md](docs/explanation/design-patterns.md) | Repo-wide patterns: intent, where used, trade-offs |
| [docs/explanation/upstream-bugs.md](docs/explanation/upstream-bugs.md) | Bugs found in pydantic-ai and pydantic-ai-harness: symptom, cause, our fix, upstream state, issue status (UB-n) |
| [docs/explanation/upstream-extensions.md](docs/explanation/upstream-extensions.md) | What we add over the two libraries, as our own code, with upstream-candidate calls (UE-n) |
| [docs/explanation/technical-debt.md](docs/explanation/technical-debt.md) | What we knowingly left worse than it should be, what it costs, and what paying it off looks like (TD-n) |
| [docs/explanation/deferred-decisions.md](docs/explanation/deferred-decisions.md) | What we chose not to adopt, why, and the condition that would make us look again (DD-n) |
| [docs/adr/README.md](docs/adr/README.md) | Cross-app ADR index |
| [CLAUDE.md](CLAUDE.md) | Agent/reviewer conventions, cross-stack contracts, known caveats |

## Per app

| App | Glossary | ADRs | Reference docs |
|---|---|---|---|
| [engram](engram/README.md) | [CONTEXT.md](engram/CONTEXT.md) | [engram/docs/adr/](engram/docs/adr/0001-pdf-to-markdown-via-azure-document-intelligence.md) (0001-0051) | [architecture](engram/docs/architecture/index.md), [init](engram/docs/reference/init.md), [index](engram/docs/reference/indexing.md), [serve](engram/docs/reference/serve.md), [sync](engram/docs/reference/sync.md), [export](engram/docs/reference/export.md), [evals](engram/docs/reference/evals.md), [plugin](engram/docs/reference/plugin.md), [conversion](engram/docs/reference/conversion.md), [knowledge base](engram/docs/reference/knowledge-base.md), [zotero](engram/docs/reference/zotero.md), [mirror](engram/docs/reference/mirror.md), [auto-retrieval-mode](engram/docs/reference/auto-retrieval-mode.md) |
| [studio-client](studio-client/README.md) | [CONTEXT.md](studio-client/CONTEXT.md) | root [ADR 0009](docs/adr/0009-ai-factory-studio-consolidation.md), [0021](docs/adr/0021-frontend-shared-code-lives-in-the-spa.md), [studio-client/docs/adr/](studio-client/docs/adr/0001-vercel-ai-protocol-and-sdk.md) (0001-0004) | [harness-station](studio-client/docs/reference/harness-station.md) |
| [core](core/README.md) | (TODO) | root [ADR 0001](docs/adr/0001-wiki-app-placement-and-core.md), [0006](docs/adr/0006-engramai-shared-ai-library.md), [0007](docs/adr/0007-consolidate-shared-python-into-core.md), [0008](docs/adr/0008-progressive-disclosure-protocol.md), [0009](docs/adr/0009-ai-factory-studio-consolidation.md), [core/docs/adr/](core/docs/adr/0001-coordination-layer.md) (0001-0003) | [core-ai](core/docs/reference/core-ai.md) |
| [studio](studio/README.md) | [CONTEXT.md](studio/CONTEXT.md) | root [ADR 0009](docs/adr/0009-ai-factory-studio-consolidation.md), [studio/docs/adr/](studio/docs/adr/0001-explore-two-brain-driver-port.md) (0001-0003) | [knowledge-base-management](studio/docs/reference/knowledge-base-management.md) |
| [harness](harness/README.md) | [CONTEXT.md](harness/CONTEXT.md) | root [ADR 0009](docs/adr/0009-ai-factory-studio-consolidation.md), [0010](docs/adr/0010-harness-extraction.md), [0014](docs/adr/0014-agentic-workflow-contract.md), [harness/docs/adr/](harness/docs/adr/0001-session-journal.md) (0001-0023) | [store-protocol](harness/docs/reference/store-protocol.md), [self-authoring](harness/docs/reference/self-authoring.md), [session-log](harness/docs/reference/session-journal.md), [sessions](harness/docs/reference/sessions.md), [workflow-monitoring](harness/docs/reference/workflow-monitoring.md), [agent-memory](harness/docs/reference/agent-memory.md), [spend](harness/docs/reference/spend.md), [model-tiers](harness/docs/reference/model-tiers.md), [inspection](harness/docs/reference/inspection.md) |
| [connectors](connectors/README.md) | [CLAUDE.md](connectors/CLAUDE.md) (module map) | root [ADR 0018](docs/adr/0018-corpus-bundles-and-provider-packages.md), [0012](docs/adr/0012-youtube-provider-and-inbox-corpus.md), engram [ADR 0001](engram/docs/adr/0001-pdf-to-markdown-via-azure-document-intelligence.md), [0003](engram/docs/adr/0003-hybrid-html-first-conversion.md), [0005](engram/docs/adr/0005-zotero-linked-files-and-collection-filter.md) | (TODO) |

## Knowledge base reads

| Doc | What it covers |
|---|---|
| [The knowledge base app reads a run's value over HTTP](docs/adr/0019-knowledge-base-reads-a-run-value-over-http.md) | Root ADR 0019: studio resolves a run's artifact through the harness's own read-only route at a configured base URL, imports no harness module and takes no address from a request |
| [Refresh runs reach the harness and arm schedules](docs/adr/0020-refresh-runs-arm-schedules.md) | Root ADR 0020: studio's harness forward posts each ended refresh run to the harness, which tells the owner and arms on-refresh schedules once per event, and a run that did its work arms the after-run schedules waiting for its workflow |
| [An index arms on-index schedules with its changed pages](docs/adr/0022-index-runs-arm-schedules-with-their-changed-pages.md) | Root ADR 0022: studio's harness index forward posts each index's changed pages to the harness, which collects them on every on-index schedule naming that knowledge base and fires it once its quiet period passes, with the page ids in the workflow parameter `changed_page_ids` |

## Retrieval boundaries

| Doc | What it covers |
|---|---|
| [A private knowledge base is read through its own login role](engram/docs/adr/0046-private-knowledge-base-read-through-its-own-login-role.md) | engram ADR 0046: visibility keyed by `session_user` through `kb.reader_scopes`, derived reader passwords, the reader connection checked at startup |
| [The agent's SQL is checked before it runs](engram/docs/adr/0047-the-agents-sql-is-checked-before-it-runs.md) | engram ADR 0047: the statement guard, the row cap inside the query, `vector_query` found by tokens and bounded, pinned search paths, the reader lock timeout |
| [Search legs are bounded, tuned and read from one snapshot](engram/docs/adr/0048-search-legs-are-bounded-tuned-and-read-from-one-snapshot.md) | engram ADR 0048: leg budgets, the tuning rows (a default row and one per knowledge base since migration 0071), the title pin and group cap, meta pages before `k`, merge by rank, one snapshot per tool call |
| [Graph walks keep the set of reached nodes](engram/docs/adr/0049-graph-walks-keep-the-set-of-reached-nodes.md) | engram ADR 0049: hop-by-hop walks on both backends, the deepening route search, `api.graph_reach` and the `reach` field |

## Claim grammar

| Doc | What it covers |
|---|---|
| [Claim fields come from the knowledge base's schema](engram/docs/adr/0050-claim-fields-come-from-the-knowledge-base-schema.md) | engram ADR 0050: engram's own claim fields, declared text and link fields stored in `kb.claims.fields` and `kb.claim_qualifiers` or refused, relation qualifiers as groups of link fields, migration 0064 |

## Sources

| Doc | What it covers |
|---|---|
| [A sync admits one mirror per source](engram/docs/adr/0051-a-sync-admits-one-mirror-per-source.md) | engram ADR 0051: a source's identity (URL, arXiv id, DOI), `MirrorFile.url` and `MirrorFile.doi`, the sync refusing an item another active mirror holds, `SyncReport.already_mirrored` |

## Inspection reliability

| Doc | What it covers |
|---|---|
| [Inspection evidence and outcomes](harness/docs/adr/0020-inspection-evidence-and-outcomes.md) | Independent execution, explicit uncertainty and outcome-based evaluation |

## Knowledge base templates

A template is one knowledge base's own vocabulary kept out of the code: its `knowledge-base-schema.json`, its `_meta/`
guidance pages, its workflows, and a README. `studio init --template <slug>` starts a knowledge base from one
and `harness import templates/<slug>/workflows` imports its workflows. Nothing outside `templates/` reads a
template except the `studio init --template` resolver and `engram.testing.specs`
([root ADR 0018](docs/adr/0018-corpus-bundles-and-provider-packages.md)).

| Template | What it covers |
|---|---|
| [templates/research/README.md](templates/research/README.md) | The research knowledge base: source mirrors digested into typed, claim-grounded pages, related to what the knowledge base holds, and read for ideas the owner decides. The Knowledge and HarnessResearch knowledge bases both initialize from it |
| [templates/repo-docs/README.md](templates/repo-docs/README.md) | This repo's own documentation estate: the governed page types, the documentation and compounding policies, and the two docs workflows (notes into an analysis, an executed design into a proposed ADR) |

## Design & vision (engram)

The product vision, solution design, and decision rationale, migrated from the
Knowledge knowledge base into the repo per [engram ADR-0013](engram/docs/adr/0013-design-and-vision-docs-in-repo.md).
These are `explanation` docs (the *why*). The engineering ADRs are the *how*.

| Doc | What it covers |
|---|---|
| [engram/docs/explanation/vision.md](engram/docs/explanation/vision.md) | The grand vision + solution design: index-vs-retrieve, the 3 layers, page types, claims, registries, disputes, refresh, curation, indexing, retrieval, generalization, the roadmap |
| [engram/docs/explanation/product-decisions.md](engram/docs/explanation/product-decisions.md) | The product decision log **D-1…D-25** + a D-n to engineering-ADR cross-reference table |
| [engram/docs/explanation/eval-loop.md](engram/docs/explanation/eval-loop.md) | The eval-loop design, reconciled against the shipped evals (ADR-0010/0011/0012/0018) |
| [engram/docs/explanation/vibe-rag.md](engram/docs/explanation/vibe-rag.md) | The Vibe RAG self-tuning-retrieval program + its 6-phase roadmap (ADR-0016/0018/0019) |
| [engram/docs/explanation/incremental-ingestion/](engram/docs/explanation/incremental-ingestion/index.md) | Incremental ingestion (Vibe RAG Phase 3) told **three ways**, Business / Research / Engineering (the SDD carries a UML class diagram, an ERD, and sequence + activity diagrams), over one shared domain. A concept concordance proves the three views agree (engram ADR-0019) |
| [engram/docs/explanation/domain/](engram/docs/explanation/domain/index.md) | The **whole engram domain** told three ways, Business / Research / Engineering, over the 5 bounded contexts, with a concept concordance proving the views agree. The narrative entry point that links *down* to the arc42/C4 master, the reference contracts, and the vision/ADRs (engram ADR-0020) |
| [engram/docs/explanation/okf.md](engram/docs/explanation/okf.md) | The OKF interchange design, reconciled against the shipped exporter (ADR-0009) |
| [engram/docs/explanation/research/](engram/docs/explanation/research/01-postgres-search.md) | Literature briefs (01-05) that grounded the decisions |

## How-to (engram)

Goal-oriented guides (Diátaxis how-to).

| Guide | What it covers |
|---|---|
| [Index your knowledge base](engram/docs/how-to/index-your-knowledge-base.md) | End-to-end: bring up the DB, init a knowledge base from a schema, index, and query it (CLI + serve-http + MCP) |

## Known gaps (TODO)

Doc-impact runs and audits consume this list. Remove a row when its doc lands.

| Missing doc | Owner scope | Why it matters |
|---|---|---|
| `core/docs/reference/core.md` | core | The shared library's non-AI module contracts (`db`, `files`, `markdown`) |
| `connectors/docs/reference/connectors.md` | connectors | What a connector must implement (`claims` / `key_for` / `source_type_of` / `build`), how a converter is registered, and the two entry-point groups a third party publishes against |
| `core/CONTEXT.md` | core | Glossary for the shared Python library (small) |
