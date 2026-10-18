---
id: engram-adr-0042-corpus-qualified-identity
aliases: ["engram-adr-0042-corpus-qualified-identity"]
type: adr
app: engram
status: accepted
tags: []
title: "0042 — Corpus-qualified identity on a matured spine"
summary: 'Use collection plus unit id as retrieval identity so shared stores cannot cross-wire knowledge-base rows, refs or provenance.'
---

# 0042 — Corpus-qualified identity on a matured spine

**Status:** Accepted (2026-08-20).

**Context.** `memory.pages.id` (and its analogues on `sections`, `claims`, `unit_summaries`, `vocab`, `sources_raw`) was the sole primary
key shared by every collection compiled into the same database; `collection` rode beside it as an ordinary column. Every read path was
collection-aware — the `api` functions filtered by it, retrieval scoped to it — but the write path was not: two corpora claiming the same id
upserted into one row instead of failing or coexisting. This was not hypothetical. Two knowledge bases in one database, `repo-docs` and `field-notes`, each compiled a
synthetic orientation page under the literal id `meta.about`; the later compile's row survived, `api.collection_about('repo-docs')` began
returning nothing, and neither the compile nor the read surfaced an error. The compiler upserts by natural key at five levels (pages,
sections, claims, unit_summaries, vocab), and section/claim ids embed their page id, so a page-id collision was automatically a collision
one level down — fixing `pages` alone would not have fixed the incident.

The same storage spine still privileged its founding tenant in the way [ADR-0024](0024-research-profile-privilege.md) named and chose not to
migrate: `pages` carried seven typed research-only scalars (`entity_type`, `paper_type`, `year`, `venue`, `citekey`, `maturity`,
`question_status`) plus a vendor key (`zotero_key`) no generic corpus could ever populate, five fixed-slot facet arrays
(`tasks`/`domains`/`stages`/`modalities`/`scale_regimes`) baked into a `FacetSlot` literal and into `api.search`'s signature, and
`sources_raw` keyed by that same vendor's identifier as its own primary key. ADR-0024's call was correct for a single-corpus, research-first
product — migrating cost a destructive schema rewrite plus a hand-authored `api` SQL rewrite, to remove columns that simply sat NULL for a
generic corpus. The multi-corpus requirement (many independent knowledge bases in one database, referencing each other) invalidates that
premise: the identity fix has to rewrite the same keys, FKs, and `api` SQL regardless, so the marginal cost ADR-0024 was avoiding collapses
to zero.

**Options considered.**

- **A surrogate `bigint` PK plus `UNIQUE (collection, id)`.** Rejected: the text id is the address everywhere the system speaks — wiki-style
links, MCP fetch refs, `edges.src_id`/`dst_id`, export back to markdown — so every read would immediately join back to `(collection, id)`
regardless, the bulk compile writer would need an id-to-surrogate resolution pass per batch, and the citation columns below would still be
required. The surrogate saves DDL once and costs a join forever.
- **A schema or database per corpus.** Rejected: it seals corpora off from each other, and the requirement is that they reference each other
— a derived corpus citing its primaries.
- **Migrate the research columns off `pages` without also touching identity.** Rejected as its own slice: the maturation and the identity
fix rewrite the same joints (`pages`, `claims`, the sources tables), so sequencing them apart pays the migration tax twice and the identity
slice would build citation columns the maturation immediately deletes.

**Decision.**

1. **Every corpus-owned table's primary key becomes the composite `(collection, id)`** — or the table's own natural-key analogue:
`(collection, source_id)` on `sources_raw`, `(collection, vocab, value)` on `vocab`, `(collection, unit_id)` on `unit_summaries`,
`(collection, comparison_id, row_entity, dimension)` on `comparison_cells`. `collection` is the tenancy axis; a bare `id` is unique only
within one collection, and post-migration `0025` no corpus-owned read or write may address a row by bare id. The `server_default='research'`
fallbacks on `pages.collection` and `sources_raw.collection` are dropped — an uncollectioned row is now an error, never a silent
file-under-the-founding-corpus.
2. **Every reference is classified containment or citation, and takes exactly the shape its kind dictates.** A **containment** reference — a
section to its page, a claim to its page, a claim-qualifier to its claim, a raw chunk to its mirror, a page's own supersession — shares the
row's single `collection` column, takes a composite FK `(collection, owner_id)`, and keeps `ON DELETE CASCADE` where it had one; containment
crossing corpora is unrepresentable by construction. A **citation** — a claim qualifier's target, a comparison row's entity, an edge's
endpoint, a link's resolution target — carries its **own** target-collection column beside the id, takes a composite FK on the pair, and
never cascades; a nullable citation gets a `CHECK ((x_id IS NULL) = (x_collection IS NULL))`, since Postgres does not enforce a composite FK
when either column is NULL. Citation columns land now, backfilled to the row's own collection, even though the resolver cannot yet produce a
genuine cross-corpus reference — the day the qualified-link syntax lands, no further identity migration is needed.
3. **The seven typed research scalars demote to declared typed frontmatter fields**, landing in `pages.frontmatter` jsonb (validated by the
research `CorpusSpec`, the grammar already does this); `source_hash` stays typed on `pages` — mirror-freshness invalidation is spine
machinery, not profile vocabulary.
4. **The five fixed-slot facet arrays collapse into one declared, GIN-indexed `pages.facets` jsonb** (facet name → `text[]`), and the
`FacetSlot` literal plus the slot indirection are deleted from the spec model. Every facet a corpus declares is search-narrowable through
`api.search`'s one `facets` parameter — the privilege gap between a slotted research facet and a JSONB-only generic-corpus facet closes from
both sides.
5. **`sources_raw` gets vendor-neutral, corpus-scoped identity**: `(collection, source_id)` where `source_id` is ours; `provider` +
`provider_key` name the adapter and its native key under `UNIQUE (collection, provider, provider_key)`. The Zotero sync becomes the first
adapter behind a `SourceProvider` port. `doi`/`arxiv_id`/`venue`/`year` move into `sources_raw.meta` jsonb; `pages.zotero_key` becomes
`pages.source_id`, a same-collection containment FK. `claims.on_dataset`/`on_task`/`metric` are deleted; their targets move to the new
`memory.claim_qualifiers` table, one row per target named by the claim-grammar's declared qualifier field, fixing the standing data loss
where a multilink qualifier kept only element zero.
6. **Cross-corpus equivalence and hierarchy assertions speak SKOS's mapping vocabulary** — `exactMatch`, `closeMatch`, `broadMatch`,
`narrowMatch`, `relatedMatch` — seeded as ordinary declared relations, deliberately never `owl:sameAs`: SKOS's calibrated claims are what a
mapping between independently owned corpora can honestly assert, where `owl:sameAs` would fuse two identifiers totally. An ordinary declared
relation crossing corpora is just a citation wearing its relation name; the mapping set is reserved for "that concept there is this one
here."
7. **A collection's name is its namespace** — unique per database, chosen at creation, renamed on collision only at import (before anything
can cite the newcomer); once another corpus cites a collection, the name is permanent.

Two alembic revisions carry this, applied together in one release train: `0024` (spine maturation — folds the research columns and facet
arrays, creates `claim_qualifiers`, moves `sources_raw` to vendor-neutral identity, ids still global) first, `0025` (corpus-qualified
identity — swaps every PK/FK to the composite pair, backfills containment from parent rows and citations to the row's own collection,
asserts zero unresolved edge endpoints and zero cross-collection id collisions before dropping the old keys) second — sequenced this way so
`0025` only swaps keys rather than restructuring what `0024` just built.

**Supersedes** [ADR-0024](0024-research-profile-privilege.md)'s decision on its **schema** seam (F3): the typed research columns on `pages`
are no longer kept and marked as an intentional, undisturbed privilege (ADR-0024's decision 1) — they migrate to `frontmatter`/`facets` by
decisions 3–4 above, and `sources_raw`'s vendor-keyed identity closes the same way by decision 5. ADR-0024's **ingestion** finding (F11 —
Mirror/Sync/Conversion as the one genuinely profile-coupled boundary) stands unreversed; the `SourceProvider` port (decision 5) makes that
boundary's *identity* vendor-neutral without disputing that ingestion itself stays research-profile business. ADR-0024's **language**
finding (F8 — "wiki" retained as ubiquitous language) is **untouched**: this ADR renames nothing, and that prose sweep stays deferred.
ADR-0024's analysis is vindicated, not overturned — it priced a migration correctly against a single-corpus product; the multi-corpus
requirement re-prices it, and the identity migration puts the same schema on the operating table regardless.

**Consequences.**

- (+) **The silent-upsert-theft failure class is structurally impossible**, not procedurally avoided: a same-id write from another corpus
becomes a second row at every level (pages, sections, claims, qualifiers, summaries, vocab), not an overwrite. The incident this ADR
responds to cannot recur.
- (+) **The domain-general claim ADR-0024 bounded is now schema-true, not just spec-true.** A generic corpus declares its frontmatter fields
and facets exactly like the research profile does; nothing on the spine is left as a domain-named column, parameter, or vendor key.
- (+) **Multilink claim qualifiers stop losing data.** `claim_qualifiers` stores every resolved target with its authored order, not element
zero.
- (+) **The cross-corpus addressing a derived knowledge base needs is storage-ready.** Citation columns exist and are populated (to the
row's own collection) before the resolver can write a genuine cross-corpus reference, so the qualified-link syntax fork lands with zero
further schema migration.
- (−) **Corpus-qualified identity currently runs only on a ParadeDB/CTE bed.** Apache AGE's projected property graph carries no
per-collection partition tag, so it cannot express a collection-scoped traversal at all —
`core.ai.retrieval.graph_builder.AgeGraphTraversalBuilder` refuses a partitioned `GraphSchema` with `ValueError`, by design, pending the AGE
projection fork, and migration `0025` re-raises that refusal as its own `RuntimeError`, naming the limitation and the portable recursive-CTE
alternative (`CteGraphTraversalBuilder`), rather than let a bare `ValueError` reach the operator. A database configured for the Apache AGE
graph backend cannot apply revision `0025` at all until that fork lands. **This is not a finished resting state.** The managed Postgres
target is AGE-backed, so teaching the AGE projection to carry the collection tag is **required follow-up roadmap, not optional**: it is
the prerequisite for running corpus-qualified identity on the managed target, not a someday nice-to-have.

  **AGE projection fork landed** ([ADR-0043](0043-age-graph-projection-collection-tag.md), 2026-08-23): the projection now carries a
  `collection` property on every vertex and edge, vertex identity is the composite `(id, collection)`, and a partitioned walk isolates
  per hop via an `UNWIND nodes(p)` aggregate (the only form Apache AGE 1.6.0's cypher dialect accepts for this). Migration `0026`
  regenerates `api.graph_expand`/`api.graph_paths`/`memory.refresh_graph()` and re-projects existing AGE graphs once. The Apache AGE
  backend reaches `alembic upgrade head` again; this Consequence's limitation no longer holds.
- (−) **Every hand-authored `api` SQL function and view that touches an identity column changed signature** (`fetch`, `envelope_page`,
`envelope_section`, `lookup_value`, `graph_expand`, `graph_paths`, `search`'s facet parameter, plus the spine and research-projection views)
— every caller (retrieval queries, envelope handlers, MCP tools, the OKF exporter, the eval SQL descriptors) moved in the same change, and
any future `api` addition must carry `collection` from day one or repeat this migration's shape.
- (−) **The qualified cross-corpus link syntax is still an open fork.** The storage and resolution semantics are fixed here (decisions 2 and
6); the concrete spelling that survives Obsidian's filename-based resolution is not.
- **Reversible?** Only partially. Downgrading `0025` is honest about what it cannot reverse: if two corpora took the same id while the
composite key was in force, collapsing back to a global id would lose one of them — the downgrade counts cross-collection duplicates first
and fails loudly rather than silently dropping rows.

**Related.** [ADR-0024](0024-research-profile-privilege.md) (superseded in part, per above),
[ADR-0007](0007-age-multihop-dual-graph-backend.md) (the dual-graph-backend seam the AGE fail-closed ruling lands on),
[ADR-0014](0014-domain-general-vault-spec-and-plugin.md) (the domain-general vault-spec this ADR makes schema-true),
[ADR-0017](0017-local-postgres-primary-testbed.md) (the local Postgres default test bed the AGE fail-closed ruling affects). Code:
`src/engram/compile/orm.py` (the composite mappings), `alembic/versions/0024_spine_maturation.py` +
`alembic/versions/0025_corpus_qualified_identity.py` (the two revisions), `src/engram/memory/spec.py` (`FrontmatterFieldSpec`, `FacetSpec`,
the declared fields the spine now defers to).
