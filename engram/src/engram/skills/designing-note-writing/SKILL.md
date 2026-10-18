---
name: designing-note-writing
description: "Use when a knowledge base needs its own note-writing logic, co-designs and scaffolds a custom, per-knowledge-base skill that turns this knowledge base's raw markdown notes into validated engram notes and indexes them. Triggers: /knowledge-base:design-note-writing, 'design note writing', 'build a note-writing skill for this knowledge base', and at the end of /knowledge-base:init."
tags: [workflow]
disable-model-invocation: true
argument-hint: "[redesign | sample <folder>]"
---

# Designing Note Writing

engram ships **primitives** (`note build`, `note build-batch`, `note normalize`, `note validate`, `index`) and **this guidance**, it does *not* ship one fixed note-writing workflow. How a knowledge base turns raw notes into engram notes is inherently knowledge-base-specific: a research knowledge base mines papers for claims and entities. A personal knowledge base lifts the `[[links]]` and tags it already has. A cookbook pulls ingredients and techniques. This skill co-designs that logic with the user and **scaffolds a knowledge-base-local note-writing skill**, which the user then owns and refines.

Use `<project_root>/.agents/skills/` for Codex and `<project_root>/.claude/skills/` for Claude Code. The project root is the folder opened in the client. It can contain the knowledge base under `docs/`. Keep generated skills under the project root so the client discovers them, and give them the actual knowledge-base path. Resolve bundled references relative to this `SKILL.md`. In Codex, select `designing-note-writing` or `engram-init` by name. `/knowledge-base:design-note-writing` and `/knowledge-base:init` below are their Claude Code command aliases.

This skill covers the invariants every note-writing skill keeps. Machinery for outside material (a bibliographic manager, `Sources/` mirrors, quote verification, claim and entity synthesis) belongs to a knowledge base only if its schema turns it on.

## The deliverable is a skill, not a script

The scaffolded result is a **`SKILL.md`** (plus an optional worker brief for a merge phase) the agent runs by calling engram primitives, **never a bundled side-script** (`ingest.py`, a migrate/neutralize one-off, a "fast path"). Bulk is *not* a reason to write code: the **judgment** (classify a note, lift its real links/facets, decide which notes to write) is the agent's and lives in the skill. The **mechanical loop** (build + validate + write many notes, then index) is engram's and is one call to **`note build-batch --manifest`**. A knowledge-base-resident Python writer re-implements engram's own frontmatter parsing, slugging, and section handling. It freezes judgment into brittle regexes (keyword-matching a title into a facet *is* fabrication, Law 1). It leaks engram internals into the knowledge base. And it rots the next time engram changes. If you find yourself reaching for a script, you've found a missing primitive. **Report it upstream, don't paper over it in the knowledge base.**

## The four laws every generated note-writing skill must obey

Copy these verbatim into whatever skill you scaffold. They are the contract.

1. **Capture, don't fabricate.** A note-writing skill makes explicit the structure the knowledge base *already has*, its `[[links]]`, tags, folders, frontmatter. It never invents structure the notes don't contain (no synthesized claims or edges on a note that has none). Richness comes from the source's real density, not from the model.
2. **One writer per note. Shared surfaces merge serially.** Each note has exactly one writer. Any *aggregate* surface (an index, an entity hub, a registry) is written by exactly one serialized pass. Partition by ownership: no locks needed. Omit the merge phase entirely when the knowledge base produces no shared surfaces.
3. **Validated files, not trusted prose.** Author payloads, then build through engram's own commands, never a side-script: bulk mirror via one `studio note build-batch --manifest` (renders + validations + writes every entry, a per-note failure is reported and skips just that note), a single note via `studio note build`, in-place via `studio note normalize`. A non-zero build exit means *that note* was not written. Fix the payload, never hand-edit the rendered `.md`. **Indexing is deferred and batched**: author + validate many notes, then `studio index --knowledge-base . --collection <slug>` once.
4. **Stable, collision-proof ids, minted up front, deduped by a stable key.** Mint each note's `id` during pre-flight from the knowledge base's convention and **guarantee uniqueness**, transliterate or hash non-ASCII, never let two notes collapse to the same slug (a real knowledge base has Greek/emoji/punctuation filenames, a naive ASCII slug sends them all to `untitled` and collides on index). Dedup against an explicit key, **never by title or filename**. `index` ingests only notes carrying an explicit `id`. Minting those ids is *this skill's* job, not the indexer's.

## When NOT to use

- Editing or curating one existing engram-format note: ordinary editing, no orchestration.
- The knowledge base has no `knowledge-base-schema.json` yet: run `/knowledge-base:init` first. This skill reads the schema.
- A genuine one-off single note: use `documenting` directly. This skill designs the repeatable *bulk* logic.

## Phase 0: Pre-flight

- **0a** Confirm CWD is the knowledge base root (markdown + `_meta/` present) and `./knowledge-base-schema.json` exists. If not, send the user to `/knowledge-base:init`.
- **0b** Verify the CLI is reachable: `"${STUDIO_BIN:-studio}" --version`.
- **0c** Read `./knowledge-base-schema.json`. It already answers much of Phase 1: `page_types` (with each type's `folder`, `canonical_sections`, `edge_fields`, `claims_enabled`), `facets`, `relations`, `sources.enabled`, `slug_conventions`. Surface what the schema implies and ask the user only what the schema does not settle.

## Phase 1: Interview the per-knowledge-base note-writing decisions

Seeded from the schema, settle and write down each decision. These become the template's parameters. A real knowledge base is messier than its schema implies. These decisions are where that mess is handled.

1. **Source folders vs. page-type folders + classification.** The *source* folders you scan are usually NOT the schema's `page_type.folder` values, and a source folder is often **multi-type** (one `notes/` holding plain notes, meeting notes, and place notes). Capture both: which folders to scan, and a **classification rule**, folder to type where single-type, a read-the-note content judgment where mixed.
2. **Mode: in-place or mirror** (offer both, a knowledge base may mix them per folder):
   - *in-place*: add/repair engram frontmatter on the original, body left untouched (its `[[links]]` become edges once indexed).
   - *mirror*: author a fresh canonical note under a target folder (e.g. `Wiki/`). The original is never touched.
   The mode also fixes the schema's `page_type.folder` values: **mirror** means a dedicated target per type that the indexer scans. **in-place** means the source folders themselves (the indexer reads each note's own `type` field to tell them apart). Keep `knowledge-base-schema.json` consistent with the chosen mode.
3. **Id convention + dedup key.** `id = prefix + slug(title)`, the slug **transliterated** (non-Latin titles, e.g. Greek or CJK, must never collapse to `untitled`) and made unique. Dedup by a stable key, **never the title**: a knowledge base that mirrors external material declares that key as its `sources.key_field`, and one with no natural key (most personal knowledge bases) uses **`source_path`** (stamped on a mirror) or a content hash.
4. **Links to edges.** Which `[[links]]` map to which `edge_fields` (default: all becomes `relates_to`, citations become `references`). Mode asymmetry: **in-place** leaves links in the body for the indexer to extract (payload carries none). **mirror** re-renders, so the payload must carry the edges.
5. **Facets to lift.** Which recurring referents (project, person, place, tag) map to which facet key, and from what signal, and *only when present* (capture, don't fabricate).
6. **Claims?** Only if `claims_enabled` for some type, otherwise skip the claim layer entirely (the usual case for a personal knowledge base).
7. **Skip rule (concrete, first-class, load-bearing on a real knowledge base).** Always skip: the mirror **target** folder, `_meta/`, template/snippet folders, attachments/drawings, editor/agent meta-files (`AGENTS.md`, `CLAUDE.md`, READMEs), dotfiles, files with unrendered `<% … %>` placeholders, and empty notes, then the user's editorial gate (drafts/stubs) on top.
8. **Shared surfaces / merge.** Does note writing produce any aggregate file (an index, entity hubs)? Yes: a serialized merge phase. No: omit it (most knowledge bases).

## Phase 2: Choose a template and specialize

Start from the closest shape in [note-writing-skill-template.md](note-writing-skill-template.md), beside this skill, and fill the Phase-1 parameters:

- **capture** (in-place/mirror, no merge), the common case: classify, then author payloads, then mirror via one `note build-batch --manifest` (or in-place via `note normalize`), then index once. (The personal-knowledge-base shape.)
- **extract-and-merge**, when notes feed shared surfaces: parallel per-note authors + one serialized merge pass.

## Phase 3: Scaffold the knowledge-base-local note-writing skill

Write `<project_root>/.agents/skills/<name>/SKILL.md` in Codex, or `<project_root>/.claude/skills/<name>/SKILL.md` in Claude Code (and a worker brief beside it *only* if Phase 1 needs a merge). The generated skill:

- carries workflow frontmatter (`tags: [workflow]`, `disable-model-invocation: true`, an `argument-hint`, a triggers-led `description`).
- for Codex, also carries `agents/openai.yaml` with `policy.allow_implicit_invocation: false` to preserve explicit invocation.
- invokes the binary as `"${STUDIO_BIN:-studio}"` with the knowledge base as CWD and `--knowledge-base .`, **no absolute paths, ever**.
- copies the four laws verbatim.

## Phase 4: Dry-run on a small sample

Run the generated skill on 2-3 representative notes (in each chosen mode). Confirm `note build-batch` (or `note normalize`) + `index --knowledge-base .` are clean and that edges/facets were actually captured (`studio fetch <id>`, `studio search`). Iterate the *skill*, never the rendered notes. If a note crashes indexing (e.g. a non-ASCII heading), that's an engram bug to report, not a reason to mangle the note.

## Phase 5: Hand off

The project now has the named skill (Codex: select it by name, Claude Code: `/<name>`). Tell the user to run it across the full knowledge base, version-control the skill, and re-run `designing-note-writing` (or edit the skill directly) as the knowledge base evolves. The skill is theirs.
