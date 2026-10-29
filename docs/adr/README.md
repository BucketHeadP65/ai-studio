---
id: adr-index
aliases: ["adr-index"]
type: explanation
app: root
status: current
tags: []
title: "Architecture Decision Records"
---

# Architecture Decision Records

Repo-level ADRs for the monorepo. App-local decisions live with their app (e.g. `engram/docs/adr/` for the conversion-pipeline ADRs the Zotero sync tool brought with it).

## Index

| ADR | Title | Status | Date |
|-----|-------|--------|------|
| [0001](0001-wiki-app-placement-and-core.md) | Wiki app placement and the `core` shared package | accepted | 2026-06-11 |
| [0002](0002-wiki-cqrs-bus-and-wikinote-kernel.md) | Wiki CQRS message bus and the `wikinote` shared kernel | accepted | 2026-06-11 |
| [0003](0003-absorb-zotero-sync-into-wiki-app.md) | Absorb the Zotero sync tool into the wiki app as feature slices | accepted | 2026-06-11 |
| [0004](0004-client-core-shared-frontend-package.md) | `client-core` shared frontend package via `file:` path dependencies | superseded ([0021](0021-frontend-shared-code-lives-in-the-spa.md)) | 2026-06-11 |
| [0005](0005-docs-as-code-standard.md) | Docs-as-code standard: Diátaxis + arc42-lite, federated topology, split-doc masters | accepted | 2026-06-12 |
| [0006](0006-engramai-shared-ai-library.md) | `engramai` shared AI library (`engram.ai`) via a uv path source | superseded ([0007](0007-consolidate-shared-python-into-core.md)) | 2026-07-09 |
| [0007](0007-consolidate-shared-python-into-core.md) | Consolidate shared Python into `core` | accepted | 2026-08-08 |
| [0008](0008-progressive-disclosure-protocol.md) | Progressive-disclosure protocol: envelope contract, descent convention, enforcement tiers | accepted | 2026-08-09 |
| [0009](0009-ai-factory-studio-consolidation.md) | AI Factory Studio consolidation: one engine in `core`, one app, one client, one name | accepted | 2026-08-10 |
| [0010](0010-harness-extraction.md) | The harness extraction: the coding agent is its own application | accepted | 2026-08-22 |
| [0011](0011-documentation-estate-one-corpus.md) | One engram corpus for the whole documentation estate | proposed | 2026-08-29 |
| [0012](0012-youtube-provider-and-inbox-corpus.md) | YouTube intake through a SourceProvider adapter and an inbox corpus | proposed | 2026-08-29 |
| [0013](0013-self-authoring-gates-on-owner-activation.md) | Agent-authored harness artifacts stay pending until the owner activates them | proposed | 2026-08-29 |
| [0014](0014-agentic-workflow-contract.md) | An agentic workflow is a deterministic spine that calls agentic steps | proposed | 2026-08-29 |
| [0015](0015-agent-memory-as-engram-corpus.md) | Agent memory is an engram corpus behind the vendor Memory capability | proposed | 2026-08-29 |
| [0016](0016-studio-corpus-registry-and-write-gates.md) | The studio manages every knowledge base through a settings-file registry, a gated terminal, and governed vocabulary | proposed | 2026-08-29 |
| [0017](0017-corpora-earn-retrieval-machinery.md) | Corpora earn their retrieval machinery at compile time | proposed | 2026-08-29 |
| [0018](0018-corpus-bundles-and-provider-packages.md) | A corpus's vocabulary lives in its bundle, a provider owns its host | proposed | 2026-09-02 |
| [0019](0019-knowledge-base-reads-a-run-value-over-http.md) | The knowledge base application reads a run's fixed value over one configured first-party HTTP route | accepted | 2026-09-27 |
| [0020](0020-refresh-runs-arm-schedules.md) | A refresh run reaches the harness over HTTP, and schedules fire on a refresh run or a run's end | accepted | 2026-10-06 |
| [0021](0021-frontend-shared-code-lives-in-the-spa.md) | The shared frontend code lives inside studio-client, in `src/shared/` | accepted | 2026-10-23 |
| [0022](0022-index-runs-arm-schedules-with-their-changed-pages.md) | An index tells the harness which pages it changed, and an on-index schedule passes them to its run | accepted | 2026-10-28 |

## Status legend

- **proposed** — under discussion
- **accepted** — decision made, implementing
- **deprecated** — no longer relevant
- **superseded** — replaced by another ADR (linked)
- **rejected** — considered and not adopted
