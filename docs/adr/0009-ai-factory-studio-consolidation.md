---
id: adr-0009-ai-factory-studio-consolidation
aliases: ["adr-0009-ai-factory-studio-consolidation"]
type: adr
app: root
status: accepted
absorbs: ["[[spec-2026-08-10-ai-factory-studio-consolidation-design]]"]
tags: []
title: "ADR-0009 — AI Factory Studio consolidation: one engine in `core`, one app, one client, one name"
---

# ADR-0009 — AI Factory Studio consolidation: one engine in `core`, one app, one client, one name

**Status:** Accepted (2026-08-10). Executes
the AI Factory Studio consolidation design.

**Context.** After the engram library/app split, the monorepo still carried two half-products under three names: `engram_app`
(the corpus CLI plus the dashboard `/api` on :8100), `workflows/app` (the coding agent and workflow runtime on :8770), and
`engram-client` (the SPA that already hosted both halves' stations). `workflows/` was not one thing either — it held the
framework-pure workflow engine, that app, the Rematch reuse experiments, and example scripts, under a project name that described
only the first. Two FastAPI apps, two composition roots, and three names composed the same engram doors for one user-facing
product, so every cross-cutting change (a door signature, a settings shape, a brand string) had to be made twice and reasoned
about three times.

**Decision.** One product, **AI Factory Studio**, assembled from four moves.

**The engine becomes `core.ai.workflows`, behind extras.** `pydantic_ai_workflows` moved wholesale into `core` (adapters
included) and ships behind `core[workflows]` / `core[workflows-temporal]`. What those extras actually gate for a consumer is
**temporalio** and **pydantic-handlebars**; `pydantic-graph` is a hard dependency of `pydantic-ai` and is therefore present in
every consumer venv whether the extra is installed or not. It is declared in the extra because the engine imports it directly —
declaration follows the import, not the isolation. The isolation that does hold is structural: `core.ai.__init__` never imports
`workflows`, so consumers reach the engine only by explicit submodule import, and plain `core` (hence engram) stays free of it.

**One app: `studio`.** `engram_app` became `studio.corpus` (the corpus CLI and the dashboard `/api`); `workflows/app` became
`studio.agent` (the coding agent, the workflow runtime, ACP). The coding agent is app-domain code, not a new library. The two
halves keep **different composition styles on purpose**: the corpus half stays wireup DI (resource shapes from `engram.config`,
registration in `studio/corpus/wiring/<slice>.py`, structural gates in `tests/unit/corpus/test_di_gates.py`), the agent half
stays plain `studio_session()` composition in `studio/agent/main.py` with no container. Neither is migrated toward the other,
and the DI gates are scoped to the corpus half so the codebase states that rather than implying it.

**One alembic chain.** The repo has exactly one — engram's, at `engram/alembic`, resolved checkout-relative — and it migrates
**both** databases by URL parameter: the corpus DB (`memory` schema) and the workflow-store DB (`engram_workflows`, the same
shape). The studio adds no chain; it points engram's at the store URL. `studio/docker-compose.yml` brings up the Temporal dev
server only; the corpus DB is engram's compose file.

**One client, and the name is copy.** `engram-client` became `studio-client`. "AI Factory Studio" lives in user-visible copy —
FastAPI titles, SPA chrome, READMEs, the plugin manifest — and nowhere else. Routes, ports (8100, 8770, 3031), env-var names
(`APP_*`, `NEXT_PUBLIC_*`), the SQL schema (`memory.*`), the `engram_workflows` database name, and the Temporal task-queue value
`"pydantic-ai-workflows"` are byte-identical across the consolidation. Code identifiers that are not user-visible
(`WIKI_API_URL`, `DashboardPrefs`) stay as they are.

**The CLI is a clean break.** `engram` is gone as a console script; `studio` is the one command, and no alias survives. The
Claude Code plugin's wrapper is `bin/studio`, honouring `STUDIO_BIN`. engram keeps its package name, its lazy doors, and its
boundary: it is the retrieval library, and it imports neither the studio nor the engine.

**Consequences.**
- (+) One product surface: one app to boot, one CLI to learn, one client to build, one brand to change. Cross-cutting edits are
  made once.
- (+) The engine is shared infrastructure in the library that already owns the AI namespace, so a second consumer needs a
  dependency line rather than a path source.
- (+) The one-chain fact is now a property of the repo rather than a coincidence: there is no second chain to drift from the
  first, and no ambiguity about which one owns the `memory` schema.
- (−) `core`'s manifest grows two extras, and one of them (`workflows`) buys less isolation than its name suggests: it cannot
  withhold `pydantic-graph`, because `pydantic-ai` already brings it. Consumers that must prove they are engine-free assert on
  `temporalio`, not on `pydantic-graph`.
- (−) `studio` is a large app with two composition styles inside it. That is the accepted cost of not inventing a third library
  for the agent; it is documented in `studio/CLAUDE.md` so neither style is mistaken for drift.
- (−) Both manifests are now derived from what each project actually imports (engram trimmed of app-tier deps with floors
  raised to what it tests against; studio re-derived with its direct imports — fastmcp, monty, the ACP protocol, temporalio —
  declared explicitly). The floor raise surfaced and forced a real fix: `pydantic-ai-harness` 0.15+ requires the code-mode
  toolset to be entered before `run_code`, which engram's standalone MCP driver now does. The residual rule: a DSN-loaded
  SQLAlchemy driver (`asyncpg`, `aiosqlite`) has no import site and must never be trimmed on import evidence alone.
- (−) The corpus and agent test suites initially carried duplicated hand-rolled fakes of engram's ports; the library now
  ships them once as the `engram.testing` toolkit (engram ADR-0040).
- (−) Without `pydantic-handlebars` installed, the engine's `_worker_document` crashes on a templated-instructions artifact
  instead of collecting the refusal it already drafted — its `try` narrows to `ValidationError` and the ImportError escapes.
  The extras make the dependency present everywhere the engine is installed, so the path is latent, not live; hardening it is
  an engine change, not a packaging one.
- (−) `core.ai.workflows.adapters.workflow_collection` embeds engram's SQL vocabulary (`api.search`, `api.cards`,
  `memory.pages` frontmatter) in the shared library. Import purity holds — the adapter takes injected callables — but a schema
  change in engram now ripples into `core` with no gate on either side to catch the drift.

**References.** Supersedes nothing; [ADR-0007](0007-consolidate-shared-python-into-core.md)'s one-shared-library rule is what makes
`core.ai.workflows` the engine's home.
