---
id: engram-adr-0005-zotero-linked-files
aliases: ["engram-adr-0005-zotero-linked-files"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0005 — Resolve Zotero linked-file attachments; optional collection filter"
---

# ADR-0005 — Resolve Zotero linked-file attachments; optional collection filter

> Recorded for the Zotero sync tool before it became an engram slice (root ADR-0003).

**Status:** Accepted (2026-06-09).

**Context.** The original repository only saw *imported* PDFs stored under Zotero's `storage/` tree (attachment `path LIKE 'storage:%'`). A real-library audit found that **~87%** of attachments are **linked files** (`linkMode = 2`) living under a user-configured base directory — so the tool saw under 10% of the actual library. Separately, the library is broad and cross-domain (hundreds of papers); a full backfill is costly (Azure spend + vault notes), so scoping a run is valuable.

**Decision.**
- The repository selects attachments with `linkMode IN (0, 1, 2)` and resolves the path totally: `storage:<key>/…` → the storage tree; `attachments:<rel>` → `ZOTERO_BASE_ATTACHMENT_DIR/<rel>` (or a bare relative path if the base is unset); an absolute path → as-is. `linkMode = 3` (`linked_url`) and NULL paths are excluded. Resolution never raises — an unresolvable/stale path surfaces as a per-item error during a run, not a crash.
- An optional **single-collection filter** (`ZOTERO_COLLECTION`, empty = whole library) bounds which items a `sync`/`status` run considers, capping the scope and Azure cost of a first backfill.

**Consequences.**
- (+) The tool sees the real, mostly-linked library; the collection filter makes an incremental, cost-bounded backfill practical.
- (−) Linked files with absolute paths from another machine (e.g. a different OS home) are unresolvable on this machine — recorded as per-item errors, not silently dropped.
- (−) Linked-file resolution depends on a correctly-set base directory (read from the user's Zotero profile `prefs.js`).
- **Reversible?** Additive to the SQL filter and the settings surface; trivially reversible.
