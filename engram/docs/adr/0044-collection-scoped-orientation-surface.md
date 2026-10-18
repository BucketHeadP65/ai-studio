---
id: engram-adr-0044-collection-scoped-orientation-surface
aliases: ["engram-adr-0044-collection-scoped-orientation-surface"]
type: adr
app: engram
status: accepted
tags: []
title: "0044 — The legacy orientation surface becomes collection-honest"
---

# 0044 — The legacy orientation surface becomes collection-honest

**Status:** Accepted (2026-08-23)

**Context.** `api.about` / `api.schema_guide` / `api.authoring_rules` (migrations 0002, 0008) have always answered from a bare literal id
or an unfiltered vocab scan, with no `collection` column at all. [ADR-0042](0042-corpus-qualified-identity.md)'s migration 0023 gave the
compiler's synthetic orientation pages a collection-scoped id (`orientation_page_id`: the bare literal for exactly one collection,
`research`; `f"{base_id}@{collection}"` for every other), matching `api.collection_about`'s own scoped lookup — but deliberately left the
three legacy views reading the bare, unscoped literal, naming them in its own docstring as "the one `api.about` / `api.authoring_rules` /
`api.schema_guide` still read unscoped." That deferral is now reversed: on a database holding more than one collection, `api.about` /
`api.authoring_rules` answer with `research`'s orientation text alone, whatever collection an agent actually means, and `api.schema_guide`
returns every collection's schema-guide rows conflated into one unlabeled result — silently, with no error and no signal that the answer
is partial.

**Options considered.**

- **Leave the views as research-only compatibility aliases**, documented as such. Rejected: the views are the first read surface an agent
  finds via `describe_schema`, before it ever discovers `api.collection_about` exists; leaving them silently wrong for every non-`research`
  collection means the M-Schema itself misleads an agent working a different corpus, and the wrong answer looks identical to a right one.
- **Drop the three views outright**, pushing every caller onto the scoped functions. Rejected: it breaks every existing single-corpus
  caller (today's actual `research` deployments) for a multi-corpus concern most of them don't have, and it treats
  `api.collection_about`'s own establishment (0018) — a view *alongside* a scoped function, not a replacement for one — as no longer valid
  precedent.
- **Labeled rows plus scoped functions** (chosen). Each view gains a leading `collection` column and answers with one row per collection
  that has actually compiled the surface, instead of one unscoped (or conflated) row for the whole database; two new functions,
  `api.collection_schema_guide` / `api.collection_authoring_rules`, give a caller who wants one collection's rows the same shape
  `api.collection_about` already established for `about`. A single-corpus database still gets exactly one row, byte-identical to today; a
  multi-corpus database gets every collection labeled instead of one silently standing in for all of them.

**Decision.**

1. `api.collection_schema_guide(p_collection)` and `api.collection_authoring_rules(p_collection)` land as functions mirroring
   `api.collection_about`'s shape — `SECURITY DEFINER`, `STABLE`, `EXECUTE` granted to `corpus_reader`, `COMMENT`ed with an example call.
   `api.collection_authoring_rules` mirrors `api.collection_about`'s exact guard (the derived expected id AND `p.collection =
   p_collection`); `api.collection_schema_guide` needs only the collection equality, since `memory.vocab` rows have carried their own
   `collection` column natively since migration 0025 and were never id-scheme-encoded.
2. `api.about` / `api.authoring_rules` / `api.schema_guide` are redefined to select every collection's row, each gaining a leading
   `collection` column. `about` / `authoring_rules` derive the match from the row's own `memory.pages.collection` column, re-deriving the
   expected id with the same bare-vs-`@collection` construction `orientation_page_id` writes — reading the authoritative column directly
   rather than re-parsing it back out of the id string, so a collection name containing `@` can never desync the two (a small, deliberate
   deviation from deriving the label by parsing the id back out, recorded in migration `0027`'s own docstring). `schema_guide` simply
   surfaces `memory.vocab`'s already-correct `collection` column; no id-scheme derivation applies to it at all — it never read pages by id
   in the first place.
3. Every view's `COMMENT` states the single-corpus/multi-corpus row-count contract explicitly and points to its sibling function.
   Migration `0027`'s downgrade restores the three pre-migration view bodies exactly and drops the two new functions.

**Consequences.**

- (+) **Non-breaking for the single-corpus case.** A database with one collection sees the same one row from each view it saw before this
  migration, now carrying a `collection` label it can ignore.
- (+) **Honest for the multi-corpus case.** A caller on a multi-corpus database sees every collection's row, or scopes to its own via the
  new functions or a `WHERE collection = ...` filter — no more silent single-tenant answer standing in for the whole database.
- (+) **Symmetrical with `api.collection_about`.** All three legacy orientation surfaces now share the same view-plus-scoped-function
  shape, rather than `about` alone carrying the multi-corpus escape hatch.
- (−) **Every caller reading `api.about` / `api.schema_guide` / `api.authoring_rules` expecting exactly one row must now handle more than
  one** on a multi-corpus database — acceptable because that expectation was already false there, just silently so.
- **Reversible?** Yes. Migration `0027`'s downgrade restores the three pre-migration view bodies exactly and drops the two new functions.

**Related.** [ADR-0042](0042-corpus-qualified-identity.md) (the identity migration whose own migration `0023` deferred this scoping),
[ADR-0043](0043-age-graph-projection-collection-tag.md) (the migration chain this follows at head). Code:
`engram/alembic/versions/0027_collection_scoped_orientation_views.py`, `engram/src/engram/compile/handlers.py`
(`orientation_page_id`), `engram/alembic/versions/0018_collection_about.py` (the scoped-function precedent this mirrors,
`api.collection_about`).
