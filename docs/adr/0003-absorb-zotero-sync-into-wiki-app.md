---
id: adr-0003-absorb-zotero-sync
aliases: ["adr-0003-absorb-zotero-sync"]
type: adr
app: root
status: accepted
tags: []
title: "0003: Absorb the Zotero sync tool into the wiki app as feature slices"
---

# 0003: Absorb the Zotero sync tool into the wiki app as feature slices

- **Status:** accepted
- **Date:** 2026-06-11
- **Deciders:** repository maintainers (ui-skeleton feature)
- **Supersedes / Amends:** amends the earlier "out of scope: absorbing the sync tool" deferral
- **Related:** `docs/adr/0001` (wiki app placement + `core`), `docs/adr/0002` (the CQRS bus the sync command rides), the sync tool's ADRs 0001 to 0005 (moved verbatim to `engram/docs/adr/`)

## Context

The LLM wiki's Layer-1 sync tool, the Zotero-to-`Sources/` mirror reconciler, ran as a separate tool with its own app shell. The wiki-rag build deliberately deferred absorbing it, but mirrored its patterns (vertical slices, `ctx.obj` CLI seam, settings idiom) precisely so a later move would be cheap, and lifted its generic file/markdown utilities into `core.files` / `core.markdown`, leaving the sync tool's own copies as live duplication. Two code bases now hold one pipeline (sync, author, compile, serve) with duplicated app shell (settings composition, CLI wiring, test infrastructure, lint/typecheck config), dispersed docs, and no way for the new dashboard to trigger a sync.

## Decision

Move the sync tool into `engram/` as feature slices:

- `src/features/{zotero,conversion,mirror}/` move nearly verbatim (they are already clean vertical slices); `mirror/store.py` re-points onto `core.files` / `core.markdown`, deleting the duplicated utilities.
- The reconcile loop becomes a `SyncMirrors` command + handler on the existing message bus; CLI surface: `engram sync` (apply) and `engram sync --dry-run` (the sync tool's old `status` drift plan, renamed because `engram status` already reports DB state).
- `ZoteroSettings` (`ZOTERO_*`) and Document Intelligence settings (`AZURE_DI_*`) merge into engram's `settings.py`; the two `VaultSettings` unify (same `VAULT_` prefix, shape-compatible).
- Tests fold into engram's `tests/` tree. The sync tool's ADRs 0001 to 0005 move verbatim to `engram/docs/adr/`.

## Options considered

- **Keep the separate tool (status quo)**: rejected. It means permanent app-shell duplication, duplicated utilities already extracted to `core`, dispersed documentation for one pipeline, and the dashboard cannot trigger sync.
- **Separate top-level monorepo project**: rejected. It keeps all the shell duplication. Its only benefit (isolating the Azure Document Intelligence dependency from engram's lockfile) does not outweigh maintaining a second app skeleton.

## Consequences

**Good:** one app owns the whole pipeline end-to-end; the dashboard's job runner triggers sync exactly like compile; the `core` duplication is resolved; one settings file, one test suite, one CLI binary.

**Bad:** engram's dependency set grows (Azure DI SDK, Zotero SQLite access); the app is no longer "serving layer only" (its docs and `CLAUDE.md` description are updated in the same change); reversing means re-extracting.

**Mitigation:** the absorbed slices remain vertically isolated with their own ports — a future re-extraction is a directory move plus a settings split, the same cheapness the original deferral relied on.
