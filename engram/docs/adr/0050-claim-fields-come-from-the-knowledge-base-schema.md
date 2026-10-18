---
id: engram-adr-0050-claim-fields-come-from-the-knowledge-base-schema
aliases: ["engram-adr-0050-claim-fields-come-from-the-knowledge-base-schema"]
type: adr
app: engram
status: accepted
tags: []
title: "0050: Claim fields come from the knowledge base's schema, and an undeclared one is refused"
---

# 0050: Claim fields come from the knowledge base's schema, and an undeclared one is refused

**Status:** Accepted (2026-10-12). Extends [ADR-0014](0014-domain-general-vault-spec-and-plugin.md): a knowledge base
declares its claim fields the way it declares its page types and relations.

**Context.** A knowledge base's schema already listed its claim field sets in its `claims` block (`multilink_fields`,
`link_scalar_fields`, `text_scalar_fields`, `plain_fields`), but engram's claim model held the research template's
names as attributes of its own: `ClaimBlock` carried `on_dataset`, `on_task`, `metric`, `condition`, `baseline`,
`magnitude`, `eval_setting` and `derived`. `kb.claims` had typed columns for `condition`, `baseline`, `magnitude` and
`eval_setting`, and `api.claims` served `dataset_name`, `task_name`, `metric_name`, `condition`, `magnitude` and
`eval_setting`. The edge qualifiers, the claim's embedding text and the comparative rule named the same fields. Another
knowledge base that declared claim fields of its own lost their values with no error.

**Decision.**

1. Engram owns a small claim grammar: the bullet text, the `^cN` anchor, `loc`, `quote`, `kind`, `subject`, `status`,
   `disputed_by` and the edges named by `edge_relations`. It parses them whether or not a schema lists them, so a
   schema that lists them keeps loading unchanged.
2. Every other claim field is declared. `multilink_fields` and `link_scalar_fields` declare link fields,
   `text_scalar_fields` and `plain_fields` declare text fields, each without engram's own names. `ClaimBlock.fields`
   holds a claim's text fields and `ClaimBlock.links` its link fields, each keyed by field name. The renderer writes
   them in schema order. A link scalar field with more than one target is refused, as before.
3. A claim key that is neither engram's own, nor declared, nor an edge relation is refused by the parser and by the
   authoring payload. The refusal names the key, the built-in fields, the declared fields and the edge relations.
   Nothing is dropped.
4. Migration 0064 adds `kb.claims.fields jsonb NOT NULL DEFAULT '{}'`, fills it from the non-null values of
   `condition`, `baseline`, `magnitude` and `eval_setting`, and drops those four columns. The indexer stores a claim's
   non-empty text fields there. Link field targets stay in `kb.claim_qualifiers`, one row per target, with `qualifier`
   set to the field name and `ord` keeping written order. `api.claims` drops its six research columns and serves
   `fields` and `links` (each link field mapped to its target pages' titles). `api.contradictions` serves `fields_a`
   and `fields_b` in place of `condition_a` and `condition_b`.
5. Each non-empty text field of a claim is copied onto every edge the claim asserts, as that edge's qualifiers. The
   claim's embedding text appends its link targets and its text fields in schema order.
6. A relation's `qualifiers` is a list of groups of claim link field names, and a bare name is a group of one. A claim
   asserting a relation listed in `comparative_relations` must carry at least one field of every group, or validation
   reports `comparative-missing-qualifiers` naming the claim, the relation and the missing fields. A schema does not
   load when a comparative relation names a qualifier that is no declared link field, or when an edge relation takes a
   built-in claim name. A relation outside `comparative_relations` keeps any qualifier names it declares, since no
   rule reads them.
7. `INDEXER_VERSION` is 4, so every knowledge base rebuilds fully on its next index.

**Consequences.**

- (+) Any knowledge base declares its own claim fields, and they are parsed, rendered, stored and served by
  `api.claims`. Engram's claim code names no research field.
- (+) A misspelt claim key is a refusal the writer can act on, never lost data.
- (+) A new claim field is a schema edit, with no migration.
- (−) A writer that emits a key nobody declared now fails where it used to pass. The schema must declare every field
  its writers emit.
- (−) A query over `api.claims` filters on `fields->>'<field>'` or `links->'<field>'` in place of a typed column.
- (−) A flat `qualifiers` list now means every named field is required, and the group form does not load on code
  older than this decision. The research schemas change form together with the code.
- **Reversible?** Partly. 0064's downgrade restores the four columns and both views as they stood at 0063, but every
  key of `fields` other than those four is lost.

**Related.** [ADR-0014](0014-domain-general-vault-spec-and-plugin.md) (the schema-driven ontology this extends),
[ADR-0045](0045-multi-target-claim-links-refuse-or-split.md) (a link scalar field with two targets is refused), root
[ADR-0018](../../../docs/adr/0018-corpus-bundles-and-provider-packages.md) (a knowledge base's vocabulary lives in its
template). Code: `engram/src/engram/kb/domain/note.py` (`ClaimBlock`, `BUILT_IN_CLAIM_FIELDS`),
`engram/src/engram/kb/domain/schema.py` (`ClaimGrammarSpec`, `RelationDecl`), `engram/src/engram/kb/domain/parser.py`,
`engram/src/engram/kb/domain/payload.py`, `engram/src/engram/indexing/domain/validator.py`,
`engram/alembic/versions/0064_claim_fields.py`.
