---
id: meta.vocabulary-relation-types
type: meta
title: "Vocabulary: relation types & claim qualifiers"
summary: The typed-link vocabulary — every frontmatter edge key and claim-level edge key, with domain → range, plus the qualifier fields that scope evidential edges.
status: verified
created: 2026-06-10
updated: 2026-06-10
---

# Vocabulary: relation types

Edges are written as **frontmatter keys** (page-level, durable facts about the page's subject) or **inline fields on claim blocks** (claim-level, evidence-bearing, with the block as provenance). Unregistered keys fail validation. The indexer materializes every edge into the `edges` table with src/dst resolved to ids.

## Conceptual (entity-page frontmatter)

| Relation | Domain → Range | Meaning |
|---|---|---|
| `is_a` | entity → entity | Subtype of a more general technique/task/family |
| `variant_of` | entity → entity | Same core idea, different formulation |
| `extends` | entity → entity | Builds on it and adds capability |
| `composes_with` | entity ↔ entity | Combinable in one pipeline (symmetric) |
| `alternative_to` | entity ↔ entity | Substitutable in the same role (symmetric) |
| `requires` | entity → entity | Hard dependency |
| `addresses` | technique → problem | Mitigates this failure mode |
| `applies_to` | technique → task | Useful for this task |
| `includes` | benchmark → dataset | Suite membership |

## Paper-level (paper-note frontmatter)

| Relation | Domain → Range | Meaning |
|---|---|---|
| `proposes` | paper → entity | The paper introduces it (first one = default claim subject) |
| `evaluates` | paper → entity | The paper measures it (incl. baselines) |
| `uses_datasets` | paper → dataset | Evaluation corpora |
| `builds_on` | paper → paper | Curated intellectual lineage (NOT the full bibliography) |

## Synthesis-level (concept/comparison/playbook frontmatter)

| Relation | Domain → Range | Meaning |
|---|---|---|
| `synthesizes` | page → paper | Sources this page digests (drives computed staleness) |
| `maps` | moc → any page | Navigation membership — derived from links in the moc's `## Map` section, not frontmatter (D-18) |
| `compares` | comparison → entity | What the matrix covers |
| `recommends` | playbook → entity | Positive guidance |
| `advises_against` | playbook → entity | Negative guidance (cite evidence) |

## Evidential (claim blocks ONLY — inline fields)

| Relation | Domain → Range | Meaning | Qualifiers |
|---|---|---|---|
| `supports` | claim → entity \| claim | Evidence in favor | recommended |
| `refutes` | claim → entity \| claim | Evidence against | recommended |
| `replicates` | claim → claim | Independent reproduction of a prior claim | recommended |
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
| `on_task::` | task link | Task setting |
| `metric::` | metric link | What was measured |
| `condition::` | free text | Setup that bounds validity (mode, judge model, k, hardware…) |
| `baseline::` | entity link | Comparison anchor when not an `outperforms` edge |
| `magnitude::` | free text | Effect size verbatim (`+4.3 Recall@5`, `0.85 → 0.92`) |
| `eval_setting::` | `zero-shot \| few-shot \| fine-tuned` | Adaptation regime |

## Adding a relation

Add a row with a one-line definition and domain → range. If it overlaps an existing relation, sharpen both definitions or don't add it — overlapping relations make agents (and validation) guess.
