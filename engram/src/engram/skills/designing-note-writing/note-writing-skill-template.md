# Ingest-skill template

Fill the `{{PLACEHOLDERS}}` from the `designing-note-writing` Phase-1 interview, drop the variant lines you don't use, and write the result to `<project_root>/.agents/skills/{{SKILL_NAME}}/SKILL.md` for Codex or `<project_root>/.claude/skills/{{SKILL_NAME}}/SKILL.md` for Claude Code. For Codex, add `agents/openai.yaml` with `policy.allow_implicit_invocation: false`. Name the actual knowledge-base working directory in the generated skill when it differs from the project root. Keep the four laws verbatim. They are the contract, not boilerplate.

A note on **modes**:

- **mirror** authors fresh canonical notes. The original is never touched. For bulk, write a manifest of `{out, payload}` entries and feed it to `studio note build-batch` (one call builds + validations + writes them all, reporting per-note failures). Use `studio note build --payload P.json --out …` for a single note. **Never loop the binary from a shell script or package a `write_notes.py`. The batch command *is* the loop.**
- **in-place** preserves the original's body and only adds/repairs engram frontmatter, so it uses `studio note normalize` (the in-place frontmatter-merge primitive) rather than `note build`. Do not approximate in-place by re-rendering with `note build --out <original> --force`. That would rewrite the user's body.

---

The scaffolded `SKILL.md` (everything below the line):

```markdown
---
name: {{SKILL_NAME}}                      # e.g. write-notes
description: Use when writing {{KNOWLEDGE_BASE_NOUN}} from {{FOLDERS}} into this knowledge base as engram notes, single note or batch. {{ONE_LINE_WHAT_IT_DOES}}. Triggers: /{{SKILL_NAME}}, "write {{KNOWLEDGE_BASE_NOUN}} notes".
tags: [workflow]
disable-model-invocation: true
argument-hint: "[filenames | 'all pending']"
---

# Ingest {{KNOWLEDGE_BASE_NOUN}}

## Laws (do not edit)
1. Capture, don't fabricate — make explicit only the structure these notes already have.
2. One writer per note{{; shared surfaces merge serially — KEEP ONLY IF MERGE PHASE}}.
3. Build → validate → (defer) index through engram's own commands (`note build-batch`/`note build`/`note normalize`, then `index`) — never a side-script; a failed build wrote nothing; never hand-edit a rendered note.
4. Ids are minted up front, unique and collision-proof, deduped by {{DEDUP_KEY}} — never by title or filename.

## Mode: {{in-place | mirror}}
- in-place → `studio note normalize <note> --knowledge-base .` adds/repairs frontmatter (id/type/status/facets), body preserved; its `[[links]]` become edges when index parses the body.
- mirror   → `studio note build --payload P.json --out {{TARGET_FOLDER}}/<slug>.md --knowledge-base .`, original untouched; stamp `source_path:` on the mirror so re-runs dedup.

## Phase 0 — Pre-flight (serialized)
- Inventory the **source** folders {{SOURCE_FOLDERS}} (usually NOT the spec's page-type folders). Apply {{SKIP_RULE}} — always skip the mirror target, `_meta/`, templates, attachments, editor/agent meta-files, dotfiles, `<% … %>` template junk, and empty notes.
- **Classify** each surviving note into a page_type ({{CLASSIFICATION_RULE}}): folder→page_type where a source folder is single-page_type; read-the-note judgment where it is mixed.
- Mint a stable, unique `id` ({{ID_CONVENTION}} — transliterate non-Latin titles, never `untitled`); collision-check (grep the target for the id); dedup against {{DEDUP_KEY}} (e.g. `source_path`).
- Propose the batch to the user. Note writing is editorial, not "write everything".

## Phase 1 — Author (the judgment; the build loop is engram's)
Author each classified note as an AuthoringPayload — this is the part only you can do:
1. `studio note schema --type <page_type> --knowledge-base .` for the authoritative field list.
2. Build the payload:
   - frontmatter: the minted `id`, the `page_type`, `title`, `status`, + page_type-specific fields;
   - edges (mirror only — in-place leaves links in the body for index): lift `[[links]]` into {{EDGE_FIELDS}};
   - facets ({{FACET_RULES}}) — only when a real signal is present (capture, don't fabricate — a keyword in the title is a guess, not a captured tag);
   - sections: map the body into {{SECTIONS}} (mirror) or leave it verbatim (in-place). **Leave the source headings as written** — index makes section anchors collision-proof (transliterates non-ASCII, disambiguates duplicates), so do NOT bold/flatten headings to dodge a crash;
   - {{claims: ...  — KEEP ONLY IF claims_enabled FOR THIS KIND}}.
3. Hand the whole batch to engram in **one** call — no shell loop, no script:
   - mirror → write a manifest `[{"out": "{{TARGET_FOLDER}}/<slug>.md", "payload": {…}}, …]` and run `studio note build-batch --manifest batch.json --knowledge-base .` (builds + fully validations + writes every entry; a per-note failure is reported and skips that note, the rest still build; exit 1 if any failed).
   - in-place → `studio note normalize <note> --frontmatter f.json --knowledge-base .` per note.
   `dangling-link` warnings (links to notes not indexed yet) are expected pre-index and do not gate.

## {{Phase 2 — Merge (serialized; ONE writer) — KEEP ONLY IF SHARED SURFACES}}
{{Update each shared/aggregate surface (index, entity hubs, registries) in one pass, re-reading each file before writing it. No Phase-1 author touches these.}}

## Phase 3 — Index (once, deferred)
`studio index --knowledge-base . --collection {{COLLECTION}}`  (always index with embeddings, so retrieval picks up what you wrote). No `export APP_KNOWLEDGE_BASE__PATH` ritual, the settings carry no knowledge base field: `--knowledge-base .` resolves the knowledge base, and `--collection {{COLLECTION}}` (your `knowledge-base-schema.json` `slug`) keeps this knowledge base's rows in their own partition.

## Phase 4 — Close
Verify the batch, append one log line, print a completion banner, and stop — never auto-advance to the next batch.
```
