---
id: meta.vocabulary-relation-types
type: meta
title: "Vocabulary: relation types & claim qualifiers"
summary: The typed-link vocabulary — every frontmatter edge key and claim-level edge key, with domain → range, plus the qualifier fields that scope evidential edges.
status: verified
---

# Vocabulary: relation types

Edges are written as **frontmatter keys** or **inline fields on claim blocks**. Unregistered keys fail validation.

## Conceptual (entity-page frontmatter)

| Relation | Domain → Range | Meaning |
|---|---|---|
| `is_a` | entity → entity | Subtype of a more general technique/task/family |
| `composes_with` | entity ↔ entity | Combinable in one pipeline (symmetric) |
| `alternative_to` | entity ↔ entity | Substitutable in the same role (symmetric) |
| `addresses` | technique → problem | Mitigates this failure mode |
| `includes` | benchmark → dataset | Suite membership |

## Paper-level (paper-note frontmatter)

| Relation | Domain → Range | Meaning |
|---|---|---|
| `proposes` | paper → entity | The paper introduces it (first one = default claim subject) |
| `builds_on` | paper → paper | Curated intellectual lineage (NOT the full bibliography) |

## Evidential (claim blocks ONLY — inline fields)

| Relation | Domain → Range | Meaning | Qualifiers |
|---|---|---|---|
| `supports` | claim → entity \| claim | Evidence in favor | recommended |
| `outperforms` | subject entity → object entity | Comparative win | **REQUIRED: `on_dataset` (or `on_task`) + `metric`** |
| `comparable_to` | subject entity → object entity | No meaningful difference (null result) | **REQUIRED: same as outperforms** |

## Lifecycle (any page/claim)

| Relation | Meaning |
|---|---|
| `superseded_by` | Points to the replacement; the old item is kept and filtered from default queries |
| `same_as` | Duplicate marker pending merge (validation surfaces it) |

## Claim qualifier fields

Not relations — scoping fields. Qualifiers on a claim block attach to **every** edge of that block.

| Field | Value | Meaning |
|---|---|---|
| `subject::` | entity link | Claim's subject (defaults to paper's first `proposes`) |
| `on_dataset::` | dataset/benchmark link | Where measured |
| `metric::` | metric link | What was measured |
