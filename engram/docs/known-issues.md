---
id: engram-known-issues
aliases: ["engram-known-issues"]
type: reference
app: engram
status: current
tags: [defects, gaps]
title: "Known issues"
---

# Known issues

Confirmed defects or design gaps awaiting a fix or an ADR. One `##` entry per issue, newest first. Remove entries when resolved (link the fixing PR/ADR in the commit that removes them).

## 2026-07-15: null-slot (JSONB) facets are registered vocabulary but not queryable via the api surface

A facet with `slot: null` rides the pages' frontmatter JSONB per the schema, and its seed values land in `kb.vocab`, but `api.lookup_value` reports `n_pages = 0` for every value (observed with a `priority` facet: `mvp`/`phase-2`/`future` seeded, 62 pages carrying real values, all counted 0), and no `api.*` view exposes the frontmatter JSONB, so a read-only agent cannot filter or count by these facets at all (`run_sql` is restricted to `api.*`). Either surface a JSONB-path accessor on `api.cards` (and count JSONB facets in `api.coverage`/`lookup_value`), or document that filterable facets must occupy one of the 5 slot columns so knowledge base designers choose slots accordingly.

## 2026-07-15: an indexing prune can delete another knowledge base's collection on a shared database

**Severity:** data loss (near miss, blocked only by an FK constraint)

**What happened.** Initializing a new knowledge base (`field-notes`) against a database that already held a different knowledge base's index (`research` collection, ~486 pages, `wiki` DB on the local Postgres test bed) ran the `init all` index smoke, whose prune step issued:

```sql
DELETE FROM kb.pages
WHERE kb.pages.collection = 'research'
  AND (kb.pages.id NOT IN ('meta.about'))
RETURNING kb.pages.id
```

i.e. it targeted the *foreign* knowledge base's collection and tried to prune it down to the new knowledge base's only page. The delete aborted solely because `claims.claims_on_dataset_fkey` still referenced `ent.2wikimultihopqa`. Without claim rows the research collection would have been silently destroyed.

**Root cause (confirmed).** With `library.collection` unset (`""`) and no `--collection` flag, indexing falls back to the default collection constant **`research`** (the ingest-skill template itself documents: "`--collection <slug>` … keeps this knowledge base's rows in their own partition instead of the default `research`"). The `init all` index smoke passes no `--collection`, so on a shared DB it scoped its prune to the `research` partition, another knowledge base's data. The default should be derived from `knowledge_base_schema.slug`, not a hardcoded research constant.

**Workaround.** One database per knowledge base, for example a dedicated `field_notes` database in the same container (`CREATE DATABASE field_notes OWNER wiki; GRANT CONNECT ON DATABASE field_notes TO kb_reader;`, migrations handle the rest).

**Proposed guards (any subset):**

1. Derive the prune's collection filter strictly from the current knowledge base (`knowledge_base_schema.slug` or explicit `library.collection`), never from database contents.
2. Refuse to prune when the target collection does not match the current knowledge base's slug/configured collection. Require an explicit `--collection`/`--allow-prune` to override.
3. `init all` should fail fast with a clear message when pointed at a database containing foreign collections, recommending a dedicated database or explicit collection config.
4. Belt-and-braces: cap the prune (refuse when it would delete >N% of a collection) unless explicitly forced.
