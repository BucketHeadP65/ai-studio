---
id: meta.registry-entity-types
aliases: ["meta.registry-entity-types"]
type: meta
title: "Vocabulary: entity types"
summary: Controlled vocabulary for `entity_type` on entity pages. Add rows freely (with a definition). Adding obligations or removing types needs an ADR.
status: verified
created: 2026-08-09
updated: 2026-08-09
---

# Vocabulary: entity types

The value of `entity_type` in entity-page frontmatter. Seeded for the working-docs memory
knowledge base. **Rule:** before minting an entity, search existing pages *and aliases*. When fuzzy
between two types, prefer the more specific one and note the doubt in the page.

| entity_type | Definition |
|---|---|
| `doctrine` | A standing rule or policy the project has adopted. |
| `model` | A conceptual model or framing. |
| `decision` | A dated decision with alternatives considered. |
