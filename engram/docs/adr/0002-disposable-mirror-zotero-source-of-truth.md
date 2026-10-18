---
id: engram-adr-0002-disposable-mirror
aliases: ["engram-adr-0002-disposable-mirror"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0002 — Disposable mirror, Zotero as source of truth, state in frontmatter"
---

# ADR-0002 — Disposable mirror, Zotero as source of truth, state in frontmatter

> Recorded for the Zotero sync tool before it became an engram slice (root ADR-0003).

**Status:** Accepted. The *filename* decision below (flat `Sources/<key>.md`) is superseded by [ADR-0004](0004-title-named-mirrors.md) — active mirrors are now title-named, with the `zotero_key` kept in frontmatter as the identity. The disposable-mirror and state-in-frontmatter decisions stand.

**Context.** The vault note for a paper can be (a) a disposable mirror regenerated from Zotero, (b) a one-time seed the user then owns, or (c) a hybrid that preserves a hand-edited zone. The user will keep wiki pages *separate* from raw sources.

**Decision.** Mirrors are **disposable** (option a). Zotero is the single source of truth; a mirror is fully regenerated when its item changes and soft-deleted when its item is removed. Identity is the Zotero **item key** → stable flat path `Sources/<key>.md`. The only mutable state is the note's YAML frontmatter (`zotero-version`, `source-hash`, `status`, `synced`) — there is no database of our own.

**Consequences.**
- (+) Reconcile is trivially idempotent; no merge/conflict logic; no clobber risk (nothing hand-edited to clobber).
- (+) State is human-readable and Git/Obsidian-native; the system can be rebuilt from the vault + Zotero alone.
- (−) Hand edits to a mirror are lost on next reconcile (by design — edits belong in separate wiki pages).
- **Reversible?** Switching to seed/hybrid later would change the reconcile contract and the meaning of every existing note. Hence an ADR.
