---
id: adr-0010-harness-extraction
aliases: ["adr-0010-harness-extraction"]
type: adr
app: root
status: accepted
absorbs: ["[[spec-2026-08-22-harness-package-design]]"]
tags: []
title: "ADR-0010 — The harness extraction: the coding agent is its own application"
---

# ADR-0010 — The harness extraction: the coding agent is its own application

**Status:** Accepted (2026-08-22). Executes
the harness package design; amends the "one app" clause of
[ADR-0009](0009-ai-factory-studio-consolidation.md), whose other decisions stand.

## Context

ADR-0009 consolidated everything into one application, `studio`, with two halves behind two ports. The halves then grew apart:
the agent half gained doors, profiles, self-authoring, and a terminal, while the corpus half stayed the CLI-and-registry app —
and the import seam between them was already clean (one CLI mount was the whole coupling). The owner's station-reset mandate
(P5) ordered the split: build the agent "like a real system".

## Decision

- **Two applications.** `studio/` keeps the corpus half only (package `studio`, console script `studio`, :8100). The agent half
  becomes the top-level uv project **`harness/`** (package `harness`, console script `harness` — `serve` plus the
  review-and-activate verbs — :8770). They share nothing at import level but `engram` and `core`.
- **The wire is frozen through the move.** Same port, same routes, same payloads; `studio-client` ships unchanged.
- **Layers, enforced.** Inside harness the tiers are domain < settings < kernel < adapters < features < composition < entries,
  held by `harness/tests/unit/test_layering.py`: every module classifies, every import edge points inward, every sanctioned
  exception is pinned exactly and can only shrink.
- **Runtime state moves with the app.** `.state/`, `creds.json`, and the authored root live under `harness/`; the alembic chain
  stays engram's alone.
- **The two remaining runtime couplings are named, not hidden.** Harness reads engram's settings file (its resolution rule now
  lives once, in `engram.config.resolve_active_corpus_path`), and the external-clients corpus door spawns `uv run studio mcp`
  from the sibling studio checkout — both sibling paths overridable (`APP_ENGRAM_REPO`, `APP_STUDIO_REPO`).
- **The separate start-up scripts retired with the split.** The settings file itself carries `?sslmode=disable` for a local
  Postgres, and `reader_dsn` preserves exactly that term, so a plain `uv run harness serve` boots with nothing sourced.

## Consequences

- Each app runs the full gate independently; a shared-library change must keep both green (the standing ripple rule).
- `studio harness ...` commands became `harness ...`; identity strings inside the harness (Logfire service, titles, provenance
  stamps for future packs) say `harness`, while previously recorded artifacts keep their recorded values.
- ADR-0009's "one app, one name" reads as "one product, two applications" from here; its engine placement (`core.ai.workflows`),
  client consolidation, and naming decisions are untouched.
- Harness carries its own ADR chain from this date (`harness/docs/adr/`).
