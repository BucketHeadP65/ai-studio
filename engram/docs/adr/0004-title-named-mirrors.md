---
id: engram-adr-0004-title-named-mirrors
aliases: ["engram-adr-0004-title-named-mirrors"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0004 — Human-readable (title-named) mirror filenames"
---

# ADR-0004 — Human-readable (title-named) mirror filenames

> Recorded for the Zotero sync tool before it became an engram slice (root ADR-0003).

**Status:** Accepted (2026-06-09). Supersedes the *filename* decision of ADR-0002 (flat `Sources/<key>.md`); the disposable-mirror and state-in-frontmatter decisions of ADR-0002 stand.

**Context.** ADR-0002 named each active mirror `Sources/<zotero-item-key>.md` (e.g. `MZV6IKR7.md`). That is stable but unreadable in Obsidian, where the filename is the note title shown everywhere. We want human-readable notes without losing the stable identity that makes reconcile idempotent.

**Decision.** Active mirrors live at `Sources/<sanitised title>.md`. The **permanent identity stays the `zotero_key`** carried in frontmatter, so the store resolves key→path by **reading frontmatter**, never by parsing the filename. A title change renames the note; a title collision across two different items disambiguates by appending the key (`<title> (KEY).md`). **Archived** notes remain key-named (`_archive/<key>.md`) — they sit outside the Obsidian link graph, so collision-proofing beats readability there.

Sanitisation (`_slug`): `:` → ` - `, OS/Obsidian-illegal characters (`\ / * ? " < > | # ^ [ ]`) → `-`, whitespace collapsed, stem capped at 150 chars (room for a ` (KEY).md` suffix), empty → `untitled`. This also neutralises `../` path-traversal in a title.

**Consequences.**
- (+) Notes are human-readable and `[[wikilinks]]` resolve naturally; identity is preserved (reconcile matches on the frontmatter `zotero_key`, not the path).
- (+) A renamed/retitled paper is handled (old note removed, new written) without orphaning.
- (−) Resolving key→path now globs and parses `Sources/` frontmatter (O(N) per lookup) instead of an O(1) path join — acceptable at vault scale, revisit if the corpus grows large.
- (−) Filesystem renames of mirror notes still bypass Obsidian's link integrity (see vault `CLAUDE.md`); the stable key in frontmatter is precisely what limits the blast radius.
- **Reversible?** Changes the on-disk filename convention and the resolution path; existing notes were migrated key→title via `os.rename` (identity preserved). Moderately reversible, hence an ADR.
