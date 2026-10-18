---
id: meta.registry-relation-types
aliases: ["meta.registry-relation-types"]
type: meta
title: "Vocabulary: relation types"
summary: The typed-link vocabulary for the repo-docs estate knowledge base, every frontmatter edge key, with domain to range.
status: verified
created: 2026-08-09
updated: 2026-08-19
---

# Vocabulary: relation types

Edges are written as **frontmatter keys**, page-level, durable facts about the page's
subject. Unregistered keys fail validation. The indexer materializes every edge into the `edges`
table with src/dst resolved to ids.

## Disclosure lifecycle

| Relation | Domain to Range | Meaning |
|---|---|---|
| `depends_on` | memory-unit to memory-unit | Invalidation edge: if target falls, source needs review |
| `refines` | memory-unit to hub | Unit refines a hub topic |
| `members` | hub to memory-unit | Hub groups these units |
| `emits` | brainstorming-doc \| spec-doc to memory-unit | Doc factored this unit out |

## Promotion and module governance

| Relation | Domain to Range | Meaning |
|---|---|---|
| `absorbed_by` | memory-unit to adr | Promotion: the governed doc that absorbed this unit |
| `governed_by` | module to adr | The decisions this package is built under |
| `applies` | module to memory-unit | Doctrine/patterns this package applies |

## Adding a relation

Add a row with a one-line definition and domain to range. If it overlaps an existing relation,
sharpen both definitions or don't add it, overlapping relations make agents (and validation) guess.
