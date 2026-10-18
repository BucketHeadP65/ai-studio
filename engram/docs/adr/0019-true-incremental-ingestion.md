---
id: engram-adr-0019-true-incremental-ingestion
aliases: ["engram-adr-0019-true-incremental-ingestion"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0019 — True incremental ingestion (Vibe RAG Phase 3)"
---

# ADR-0019 — True incremental ingestion (Vibe RAG Phase 3)

**Status:** Accepted (2026-06-28). Built on `feat/vibe-rag-phase3-incremental-ingestion` (Vibe RAG
**Phase 3**, true incremental ingestion). Builds on the compile slice
([indexing.md](../reference/indexing.md), [ADR-0001](../../../docs/adr/0001-wiki-app-placement-and-core.md)),
runs the correctness oracle on the [ADR-0017](0017-local-postgres-primary-testbed.md) local Postgres bed by
default, gates the [ADR-0007](0007-age-multihop-dual-graph-backend.md) AGE graph projection on an
edge change, and emits the precise changed-set that Vibe RAG Phase 6 (freshness) consumes. Scope: a
new compile-state schema (`alembic/versions/0006_compile_state.py` — `wiki.compile_manifest`,
`wiki.link_refs`, `wiki.lint_store`, `wiki.compile_fingerprint`), the pure `features/compile/manifest.py`
(diff + closure + `identity_hash`), four repositories and ORM models, the incremental orchestration in
`features/compile/incremental.py`, a `full` and `dry_run` flag on `CompileVault`, the `--full` and
`--dry-run` CLI flags, `engram status` drift reporting, the HTTP `full` toggle on
`POST /api/jobs/compile`, and a narrowed `PapersChanged`. The read path, the `api` SQL surface, and the
`wiki_reader` role are unchanged, and the new tables are deliberately omitted from both.

**Context.** `engram compile` is idempotent but O(corpus): only the embed stage is incremental (a
hash-gated upsert NULLs a row's `embedding` when its embed-input hash changed, so only changed units
re-embed), while parse, resolve, validate, and load re-process the whole vault on every run and the
prune is a whole-set `DELETE … WHERE id NOT IN <full keep-set>`. As corpora grow (Phase 5) and the
calibration loop (Phase 2) recompiles often, the full rebuild is the bottleneck, and there is no
machine-readable "what changed" signal for the Phase 6 freshness detector to consume. The hard part is
that resolve is **global**: `resolver.py::build_link_index` keys each page by filename stem, `id`,
`title`, and `aliases` (lower-cased), so a one-file edit can flip the resolution of *unchanged* files.
The dirty set is therefore never "files that changed" but "files whose compiled output changed," which
requires link-graph reasoning. The driving constraint, confirmed with the user, is **all four** of
compile wall-clock latency, a precise machine-readable changed-set, Phase 5 scale headroom, and
DB/graph write-amplification — which rules out a point optimization and demands a correctness-first
re-architecture.

**Options considered.**

- **Make only load and embed incremental; keep parse, resolve, and validate global.** Rejected. It
  gives up most of the wall-clock win and still re-reads and re-parses the whole corpus every run, so
  it does not address the latency or the scale driver.
- **mtime / git-diff change detection.** Rejected. mtime is unreliable across checkouts, copies, and
  clock skew, and a git-diff source couples compile to VCS state and breaks for a dirty working tree.
  A content hash is robust and self-correcting: a missing or empty manifest simply degenerates to
  "everything changed," i.e. a full rebuild.
- **Local-fast with a full-rebuild fallback for any structural change.** Rejected as the end state
  (though it is a strict subset of the chosen design). Structural edits — rename, add, delete — are
  common at scale and would pay the full price every time, defeating the churn driver.
- **A precise closure via a persisted link-reference graph plus a content/identity-hash split
  (chosen).**

**Decision.**

Seven load-bearing choices carry the re-architecture.

**1. A content-hash manifest detects changes without parsing.** `wiki.compile_manifest` is a
per-collection, compile-private table mapping each compiled file `path` to its `content_hash` (the
raw file text) and `identity_hash`, plus the `page_id` or `zotero_key` it produced. Each run re-hashes
every file and diffs against the stored manifest, yielding the raw `{added, removed, content_changed}`
partition without parsing. The detection is robust and self-correcting: an empty manifest (a cold
start) degenerates to a full rebuild.

**2. An `identity_hash` split distinguishes local edits from structural ones.** `content_hash` answers
"did anything in this file change?" (gating whether the file is re-parsed at all); `identity_hash` is
`card.content_hash` of the normalized, sorted resolution-key set `{filename_stem, id, title, aliases}`
and answers "did this file's effect on how *other* files resolve change?". A body-only edit changes
`content_hash` but not `identity_hash`, so it re-does that one file and nothing else. Only an
identity change (or an add/remove) triggers the inbound closure. The `identity_hash` of a changed note
is computed from a frontmatter-only read (`core.markdown.split_frontmatter`), so a full
`wikinote.parser.parse` runs only for files that land in the dirty set.

**3. A persisted link-reference graph makes the inbound closure a name query.** `wiki.link_refs`
stores **every** outbound link target a page declares — resolved **and** dangling — with the raw,
lower-cased target string and the resolved `dst_id` (NULL if dangling). `wiki.edges` is resolved-only,
id-keyed, and drops the raw target, so it cannot answer the name-based closure (for example a title
change that turns a resolved link dangling, or an add that satisfies a prior dangling `[[X]]`). The
inbound closure is then `WHERE raw_target IN <affected names>`. The duplication of resolved links
across `link_refs` and `edges` is bounded and intentional; `edges` stays the clean, serving-facing
resolved graph.

**4. Lint is persisted per page so the CI gate stays bit-for-bit identical.** `wiki.lint_store` holds
each page's lint issues, so the whole-vault report is reassembled as `stored(clean pages) ∪
recomputed(dirty pages)` and the non-zero-`n_errors`-fails-CI contract is preserved exactly. A clean
page cannot be re-linted without re-parsing it, which is the whole point of not re-parsing it.

**5. Load is targeted, and a rebuilt page's own children are reconciled with it.** The incremental
path upserts only the dirty pages, deletes by vanished id
(`DELETE FROM wiki.pages WHERE id IN <removed ∪ renamed-old ids>`, cascading `sources_raw`), replaces
edges and link-refs and lint per source, and re-projects the AGE graph **only when an edge changed**
(Decision D6 — the projection is cheap SQL with no LLM or network, and an incremental run skips it
entirely when no edge moved). `full` keeps today's whole-set `NOT IN` prune as the belt-and-braces
baseline.

An upsert can only add or update, so a page that stops producing a child row would keep that row
forever, and the whole-set `NOT IN` prune is not available to a run that re-read part of the
knowledge base: its keep-set names only part of the collection. So the targeted load reconciles
**per page**. For each page it actually rebuilt, it deletes that page's sections, claims and
comparison cells the fresh load did not re-produce, and the unit-summary rows describing a section
that went away or a page that was deleted. Every one of those `DELETE`s carries three predicates —
the row's own `collection`, the owning page set, and the keep-set — so a neighbouring collection
holding the same child id is never a candidate, and a page nobody re-read keeps everything it had.
A dirty note that failed to parse produces no rows and is therefore left out of the owning page set,
so it keeps its children rather than having them reconciled against an empty load. Claim qualifiers
need no reconcile of their own: they cascade from the claim row and are already replaced per owning
page. The reconcile rides the one load transaction, so a failure mid-way rolls its deletes back with
the upserts, and the rows it removed are the run's reported prune count. The full rebuild gains the
matching whole-collection unit-summary prune, keeping the summaries of its own pages and sections
and dropping the rest — a summary table has no foreign key to the units it describes, so nothing
else would.

Reconciling only fixes what a *later* run finds stale. A row already orphaned by an earlier run is
never named by any page set again, so the supported repair for a database indexed before this
decision took effect is a `--full` rebuild of the affected collection.

**6. Incremental by default, `--full` is the escape hatch, with global invalidators.** A
per-collection `wiki.compile_fingerprint` single row holds `(compiler_version, spec_hash,
registry_hash, embed_model)`. A mismatch is a global invalidator that auto-forces a full rebuild,
because a spec or registry change moves every note's parse and validate and an embed-model change
invalidates every vector. `engram compile` is incremental by default; `--full` forces the whole-corpus
rebuild; `--dry-run` previews the dirty set and writes nothing; CI runs `--full`.

**7. Correctness is proven by an `incremental == full` property test.** The completeness lemma — the
inbound closure is single-level — is what makes incremental provably equal to full: a closure page is
included only because it references an affected name, and re-resolving it updates its edges but cannot
change its own identity (only added/content-changed files change identities, and those are already in
the dirty set), so no second-level expansion is needed. The property test
(`tests/integration/compile/test_incremental_equals_full.py`) replays a scenario matrix end to end and
asserts incremental-on-A equals full-on-fresh-B over every `wiki.*` data table plus `lint_store`,
claim qualifiers, comparison cells and unit summaries included. The matrix covers the shrinking
edits a per-page reconcile exists for — a removed section, a renamed heading, a removed claim
anchor, a removed comparison cell, a page stripped of every child, and removed introduction prose —
each declaring the exact keys that must be gone, and it holds a second collection with colliding ids
beside the one being reindexed to prove no `DELETE` reaches across.

**Consequences.**

- (+) **Edit-to-queryable in seconds on a large vault.** Only the dirty set is parsed, resolved,
  validated, and loaded; the AGE graph is re-projected only when an edge moved; the prune is targeted
  rather than a whole-set `NOT IN`.
- (+) **A precise machine-readable changed-set.** `PapersChanged` is narrowed to only the paper ids
  whose compiled output changed this run (a full rebuild or a cold start still emits all papers,
  consistent with today). This is the signal the Phase 6 freshness detector consumes.
- (+) **A page can shrink.** Removing a section, a heading, a claim anchor or a comparison row takes
  its indexed rows with it on an incremental run, so what retrieval serves matches what the Markdown
  says rather than accumulating everything the page ever held.
- (+) **The CI cleanliness contract is preserved bit-for-bit.** The whole-vault `n_errors` gate is
  reassembled from `lint_store(clean pages) ∪ recomputed(dirty pages)`, and the `incremental == full`
  property test is the oracle that holds it true.
- (+) **A pre-existing full-compile non-idempotency was found and fixed.** The property test surfaced
  a latent bug independent of the incremental path: `EdgeRepository` scoped the edge `DELETE` on
  `src_id IN (SELECT id FROM wiki.pages …)`, but claim and section edges carry
  `src_id = <page_id>#<anchor>`, so they were **never** deleted and duplicated on every recompile —
  under both the full and the incremental path; the e2e idempotency test missed it because it checked
  only page counts. The fix scopes the `DELETE` on the owning page id via
  `split_part(src_id, '#', 1)` in both `delete_collection_statement` (full) and
  `delete_for_sources_statement` (incremental), making the full rebuild itself idempotent at the edge
  level.
- (−) **Four new compile-private tables to maintain and migrate.** `compile_manifest`, `link_refs`,
  `lint_store`, and `compile_fingerprint` are written inside the single load transaction so the
  manifest can never desync from the rows it describes (Decision D7).
- (−) **`wiki.link_refs` duplicates resolved links already in `wiki.edges`.** The duplication is
  bounded and intentional: `edges` cannot answer the name-based closure, so the raw-target graph is
  kept alongside it.
- **Reversible?** Yes, additively. Dropping the four tables and forcing `--full` returns to today's
  behavior; the read path, the `api` surface, and the `wiki_reader` role were never touched.

**Related.** [ADR-0017](0017-local-postgres-primary-testbed.md) (the local Postgres bed the
`incremental == full` property test runs on), [ADR-0007](0007-age-multihop-dual-graph-backend.md) (the
AGE graph backend whose `wiki_kg` projection is gated on an edge change),
[ADR-0016](0016-structural-eval-generation.md) / [ADR-0018](0018-calibration-loop.md) (Vibe RAG Phases
1–2, the program this phase continues), [indexing.md](../reference/indexing.md) (the compile slice
reference this phase re-architects), and [vibe-rag.md](../explanation/vibe-rag.md) (the program
roadmap, where Phase 3 is now *Shipped*). Code: `alembic/versions/0006_compile_state.py` (the four
tables), `features/compile/manifest.py` (`identity_hash`, `compute_diff`, `compute_closure`, the
`ManifestEntry` / `HashedFile` / `ManifestDiff` / `DirtySet` models), `features/compile/incremental.py`
(`run_incremental`, `preview_dirty_set`), `features/compile/handlers.py` (the full-vs-incremental
branch, the `COMPILER_VERSION` fingerprint, the full path's state population), `features/compile/repository.py`
(the four repositories and the page-prefix edge-delete fix), `features/compile/resolver.py`
(`build_link_index_from_pages`, `resolve_with_index`, `LinkIndex.merge`), `features/compile/domain.py`
(`CompileVault.full` / `dry_run`, `CompileReport.mode` / `skipped`, `DryRunPreview`, the narrowed
`PapersChanged`), `src/cli.py` (the `--full` / `--dry-run` flags and `status` drift reporting), and
`features/serve/http.py` (the `full` toggle). Patterns:
[CQRS message bus](../../../docs/explanation/design-patterns.md#cqrs-message-bus) (the `CompileVault`
command).
