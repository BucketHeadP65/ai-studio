---
id: engram-adr-0025-intentional-deviations-register
aliases: ["engram-adr-0025-intentional-deviations-register"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0025 — Intentional deviations register"
---

# ADR-0025 — Intentional deviations register

**Amended (2026-09-02) — where the code sites moved.** The register's judgments stand unchanged, but three of its
findings point at paths that no longer exist in this package. The provider and converter implementations left the
compiler for the top-level `providers/` project
([root ADR-0018](../../../docs/adr/0018-corpus-bundles-and-provider-packages.md)), so read
`engram/sources/conversion/arxiv_html/` (F22) as `providers/src/engram_providers/conversion/arxiv_html/`, and
`engram/sources/zotero/repository.py` (F41 exemplar, F42 residual) as
`providers/src/engram_providers/zotero/repository.py`. What engram still owns at those two seams is the
`DocumentConverter` port (`engram/sources/converter.py`) and the `SourceProvider` port
(`engram/sources/provider.py`). The read-only invariant F41 records is now enforced by that port having no write
method and by the adapter's copy-then-open discipline in its new home. F41's own reference to the research profile
reads as history: engram carries no research profile any more, and a corpus's vocabulary lives in its bundle.

**Status:** Accepted (2026-06-30). Delivered in the `engram-domain-remediation` initiative (Slice E,
task E9). This is the consolidated home (decision D5) for the by-design domain findings the initiative
deliberately did **not** change. The other slices fixed real smells (the composition root,
[ADR-0021](0021-explicit-eager-composition-root.md); the SQL boundary,
[ADR-0023](0023-sql-boundary-type-safety.md); the research-profile privilege,
[ADR-0024](0024-research-profile-privilege.md)); this ADR records the findings that are conscious
trade-offs — correct-by-design or cheaper-to-keep-than-to-fix — so a future reader does not re-flag
them as oversights. Each finding's code site (where it has one) also carries a one-line inline
`intentional:` comment pointing back here. No schema, `api` SQL surface, `wiki_reader` role, or code
behavior change — documentation plus inline comments only. Findings F4, F5, F12, F36, F15, F16, F17,
F18, F22, F23, F31, F41, F42. F43 (the evals late-resolution seam) was added 2026-08-05 alongside
[ADR-0038](0038-dependency-injection-conventions.md); unlike the original subset it landed with a
small code change (the seam narrowed to the `Resolver` port) — the register records what remains
accepted after that narrowing.

**Context.** A critical-lens review of the domain docs produced a ledger of ~70 domain-level findings. The
`engram-domain-remediation` initiative triaged them: factual staleness was fixed in the docs build's
R1 pass, and the DDD / boundary / coupling / schema smells were routed to slices that either fix them
or — for a bounded subset — accept them. This ADR is the register for the accepted subset. Every entry
below is a finding where *changing* the code was judged to cost more than the smell costs
(a destructive migration, a referential-integrity contract the compiler must own anyway, a DI seam that
works), or where the "smell" is in fact a strength. The discipline is the same one
[ADR-0024](0024-research-profile-privilege.md) applies to the research-profile privilege: an honest,
named, bounded acceptance is stronger than silently leaving the finding to be rediscovered.

The positive exemplar these deviations are measured against is **F41** — the Zotero read-only-copy
discipline. Where a deviation accepts a loose-coupling or a boundary gap, F41 is the counter-example of a
boundary made unbreakable at zero cost; it is recorded here as the bar, not as a flaw.

**Options considered.**

- **Fix each finding.** Rejected for this subset. Each carries a cost (migration, broader type surface,
  a DI refactor) that exceeds the smell's cost, or would remove a deliberate property (a representable
  dangling link, a parse-error lint row that predates its page). The other remediation slices fixed the
  findings where the balance ran the other way.
- **Leave them undocumented.** Rejected. An unrecorded by-design deviation reads as an oversight; the
  next critical-lens pass re-flags it, and a well-meaning "fix" can re-break a deliberate property (e.g.
  re-adding a FK that must stay loose, or "deduplicating" a phantom DI subtype that exists for a reason).
- **Record them as accepted intentional deviations, with an inline marker at each site (chosen).**

**Decision.**

Record the following as **accepted** intentional deviations. Where a finding has a code site, that site
carries a one-line `# intentional: <why> (ADR-0025)` comment; where a finding is a doc/framing-level
observation with no single code line, this register is its only home (noted per row). No code behavior
changes.

### Retrieval / serve — read path and DI keys

| # | Finding | Site(s) | Why it's an accepted intentional deviation |
|---|---|---|---|
| F4 | The read path bypasses the CQRS bus — query handlers take a `SqlGateway` directly; only write *intents* are bus-routed. | `engram/retrieval/queries.py:129` (`SearchHandler`), `:157` (`FetchHandler`); `core.cqrs.bus` | This is a clean CQS split, not a defect: commands carry intent and side effects (worth the bus's commit-then-publish machinery), reads are pure functions over the `api` surface that gain nothing from event routing. Forcing queries through the bus would add ceremony with no behavioral payoff. The accepted cost is that "everything goes through the bus" is false and must be stated (the serve docs do); the inline marker keeps a `cqrs/bus.py` reader from assuming queries are routed. |
| F5 | `StatusHandler` is injected an **admin** gateway, deliberately bypassing the `wiki_reader` role (row counts aren't granted to the read-only role). | `engram/retrieval/queries.py:19` (module docstring), `:262-266` (`StatusHandler`) | The one sanctioned exception to "all serve reads go through `wiki_reader`/`api`". Granting `wiki.*` row-count visibility to the reader role to remove this exception would *widen* the read-only surface — the opposite of the least-privilege intent. Accepting one documented, narrow admin read (corpus health) is strictly safer than enlarging the reader grant. The module + class docstrings already document it; this register names it as the deliberate carve-out. |
| F12 + F36 | Phantom DI subtypes: `AdminSqlGateway` (and its sibling `ReaderSqlGateway`) are empty subclasses of `ReadOnlySqlGateway` that exist **only** to give wireup distinct DI keys — identical behavior, no override. `StatusHandler` even types its param as the base. | `engram_app/wiring/serve.py:81-96` (the two subclasses), `engram/retrieval/queries.py:266` (handler types the base, is wired the subtype) | F12 and F36 are the same smell. Wireup keys providers by type, and two structurally identical `ReadOnlySqlGateway` instances (one over the reader engine, one over the admin engine) must resolve unambiguously. The alternative — a keyed/qualified binding — pushes wireup-specific qualifier strings into every injection site and is arguably *more* coupling than two one-line subclasses whose docstrings say exactly why they exist. The accepted cost is two no-op types on the domain surface; the inline markers make their DI-key-only purpose local rather than a three-file hunt. |

### Evals — the one sanctioned late-resolution seam

| # | Finding | Site(s) | Why it's an accepted intentional deviation |
|---|---|---|---|
| F43 | The calibration evaluator resolves its collaborators **late**: `policy_evaluator_factory_provider` injects the `Resolver` port, and the evaluator it builds looks the model + retrieval handlers up from the live container *per calibration round* — so the factory's real dependency set does not appear in its signature and is invisible to the DI graph. | `engram_app/wiring/evals.py` (`policy_evaluator_factory_provider`, `_RunEvalPolicyEvaluator`), `engram_app/container.py` (`resolver_provider`, the port's one registration) | Late resolution is the *requirement*, not a shortcut: the calibration loop's whole point is to vary the retrieval policy per round, and `run_eval_on_dataset` must score each round against the retrieval surface as wired *then* — freezing those lookups at construction time would freeze the knobs the loop exists to turn. The seam is bounded three ways: the slice depends only on the framework-free `Resolver` port (`core.resolver`), never on the container type or its adapter; the port is registered once, at the composition root (`resolver_provider`); and this row is the register's **only** sanctioned late-resolution consumer — a second `Resolver` injection must argue against this row and [ADR-0038](0038-dependency-injection-conventions.md)'s conventions rather than cite this one as precedent. The accepted cost stays real: this is the one provider whose dependencies cannot be read off its signature. |

### Knowledge / schema — loose-by-design references

These three accept the absence of a database-level foreign key. In each case the reference is
**polymorphic, dangling-permitting, or lifecycle-ordered** in a way a FK cannot express; referential
integrity is owned by the compiler (resolve-at-compile), which is the correct home for it given the
corpus is a full rebuild pruned per collection. The accepted cost is that a dangling row is not caught
at the DB layer — named here and in the serve/graph docs so a reader does not assume the FK exists.

| # | Finding | Site(s) | Why it's an accepted intentional deviation |
|---|---|---|---|
| F15 | `wiki.edges` has no FK on `src_id`/`dst_id` (plain `text`, id-keyed, polymorphic over pages **and** claims); the AGE projection derives `wiki_kg` from it. | `alembic/versions/0001_wiki_schema.py:205-216` | A FK cannot point at two tables (pages *or* claims) at once, and edges are resolved-only — the compiler guarantees both endpoints exist before writing the row. A polymorphic FK would mean splitting `edges` per endpoint type or adding a discriminator-constrained check, complicating the one table the graph backends read. Integrity is enforced at compile, by design. |
| F16 | `lint_store.page_id` is deliberately not a FK — parse-error issues predate any page row. | `alembic/versions/0006_compile_state.py:94-105` | A lint issue for a note that failed to parse has no `wiki.pages` row to reference — a FK would reject exactly the rows the lint store exists to record. The orphan-row risk is managed by the incremental lifecycle's explicit deletion, not the DB. |
| F17 | `comparison_cells.claim_refs` (`text[]`) and `link_refs.dst_id` (nullable `text`) store logical references with no FK. | `alembic/versions/0001_wiki_schema.py:225`, `0006_compile_state.py:85` | A dangling link must be *representable* (an unresolved `[[target]]` is real corpus data, not an error). `dst_id` is nullable precisely so an unresolved target round-trips; `claim_refs` is an array (un-FK-able). Loose-coupling-by-design. |

### Cross-context — the shared `wiki` schema (doc/framing only)

| # | Finding | Site(s) | Why it's an accepted intentional deviation |
|---|---|---|---|
| F18 | `eval_runs` / `eval_case_results` (Evaluation) and `query_log` (Ops) physically live in the same `wiki` schema as the Knowledge corpus (Decision P2), so the schema boundary does not mirror the bounded-context boundary. | `engram/evals/shared/orm.py:29`, `alembic/versions/0001_wiki_schema.py` | One Postgres schema serving five bounded contexts is a deliberate operational simplification (one migration chain, one grant surface, one `DeclarativeBase`). The context fence is the `api.*` grant boundary + the compile-private convention, not the SQL `schema` namespace. Splitting into five schemas would multiply migration and grant management for no isolation the `api` boundary doesn't already provide. **Doc/framing-level — no single code line; this register is its home** (the serve/ERD docs also state it). |

### Knowledge / wikinote + compile — deferred-import cycle breaks

These accept a real import cycle broken by a **deferred (function-local) import**, with acyclicity held
by convention (a docstring) rather than structurally. In both cases the cycle exists for a good reason
(DRY across two code paths; a sibling-module reference Python resolves only after package init), and the
deferred import is the standard, least-invasive break. The accepted cost is that a careless top-level
`import` would harden the latent coupling — named here and well-commented at the site so the structural
view shows the cycle rather than hiding it.

| # | Finding | Site(s) | Why it's an accepted intentional deviation |
|---|---|---|---|
| F23 | `parse_frontmatter` uses a deferred `import engram.memory.spec` inside the function to break a domain↔spec circular reference; acyclicity is convention-enforced (docstring), not structural. | `engram/memory/domain.py:234-259` | `memory/domain.py` and `memory/spec.py` are siblings in one package and `spec.py` must reference `domain.py` types, so a top-level `domain → spec` import would cycle at package-init time. `spec.py` has *no* import back to `domain.py`, so the graph is genuinely acyclic; the function-local import is the minimal, idiomatic break. The site already carries an explanatory comment; the marker augments it. |
| F31 | `handlers.py` exports row builders **up** to `incremental.py` (to keep full/incremental byte-identical) while importing `incremental` symbols only locally — a real handlers↔incremental cycle broken by deferred imports. | `engram/compile/handlers.py:937-1024`, `engram/compile/incremental.py:47-49` | The shared row builders (`link_ref_rows`, `lint_row`, `_RowBuilder`, …) must live in the lower module so both compile paths write byte-identical `link_refs`/`lint_store` rows — the `incremental == full` property-test invariant. That is intentional DRY, not accidental coupling; the deferred import in `handlers` (not `incremental`) keeps the direction sane. Already well-commented (the "DRY" block); the marker names the accepted cycle. |

### Ingestion / conversion — cohesive intra-package coupling

| # | Finding | Site(s) | Why it's an accepted intentional deviation |
|---|---|---|---|
| F22 | The `arxiv_html` sub-package shares private helpers across modules (`svg` imports `_SENTINEL_RE` from `markdown`; `tables`/`svg` import `_break_paragraph_spans` from `spans`; `converter` reaches into `fetch`/`svg` privates). | `engram/sources/conversion/arxiv_html/{converter,markdown,svg,tables,spans}.py` (e.g. `converter.py:24`, `svg.py:26-27`, `tables.py:13`) | This is **one cohesive converter** split into modules for readability, not five independent units that happen to couple. The shared helpers (`_SENTINEL_RE`, `_break_paragraph_spans`, the asset utilities) are implementation details of *the same* arXiv-HTML conversion, so cross-module private use is internal cohesion, not a leaked boundary. Promoting every shared helper to a public `_internal` module would be churn that obscures, not clarifies, the single-converter intent. Recorded as the sub-package's coupling surface, accepted. |

### Ingestion / zotero — the exemplar and the recorded residual

| # | Finding | Site(s) | Why it's recorded here |
|---|---|---|---|
| F41 (positive — the exemplar) | The Zotero read-only invariant is enforced **structurally on two layers**: the `ZoteroItemRepository` port has no write method, and `SqliteZoteroRepository` copies `zotero.sqlite` (+`-wal`/`-shm`) into a `TemporaryDirectory` and opens the **copy** via `?mode=ro`. The invariant cannot be violated even in principle, and the copy sidesteps Zotero's live write-lock. | `engram/sources/zotero/repository.py:180-182` (the copy-and-open-read-only path), `engram/sources/zotero/domain.py:52` (the write-free port) | **Not a flaw — the positive counter-example** the other deviations are measured against. It costs nothing and makes a boundary unbreakable at two layers; it is the model the conversion boundary (F21, fixed in E2) and the mirror should imitate. Recorded as the bar, not as debt. No inline marker (there is nothing to flag — the discipline is already self-evident in the code + module docstring). |
| F42 (residual) | Deep coupling to Zotero's internal SQLite schema: raw SQL hard-codes table/column names across ~10 Zotero tables, and rows map **straight to pydantic with no validated boundary model**. | `engram/sources/zotero/repository.py:39-104` | **Partially addressed in E3** (commit `39ff943`): the magic `linkMode`/`contentType` ints became `_LOCAL_FILE_LINK_MODES: tuple[Literal[0,1,2], …]` / `_PDF_CONTENT_TYPE: Final` constants with a prose rationale (`repository.py:39-45`). The **recorded residual** is the rest: the ~10-table raw-SQL coupling and the unvalidated row→pydantic mapping. Adding a frozen boundary model over Zotero's internal schema is accepted as not worth it — this is a *research-profile* adapter ([ADR-0024](0024-research-profile-privilege.md)) over a *local read-only copy* (F41), invoked only on the optional `engram sync` path; a Zotero schema change is rare and surfaces in one adapter, and the read-only copy means the blast radius is a failed sync, not corruption. The E3 constants already carry the only marker; this register records the residual against the ADR rather than re-commenting the SQL. |

**Consequences.**

- (+) **The by-design deviations have one named home.** A future critical-lens pass (or a reviewer)
  finds them recorded as accepted trade-offs with rationale, not as fresh smells; the inline markers
  point back here from each code site.
- (+) **Deliberate properties are protected from well-meaning "fixes".** The loose FKs (F15/F16/F17),
  the phantom DI subtypes (F12/F36), and the deferred-import cycles (F23/F31) each guard a real property;
  the marker + this register stop a refactor from silently removing it.
- (+) **F41 is on the record as the exemplar.** The read-only-copy discipline is named as the bar the
  estate's other boundaries should reach.
- (−) **The accepted costs remain.** Dangling rows aren't caught at the DB layer (F15/F16/F17), two no-op
  DI types sit on the serve surface (F12/F36), one admin read bypasses `wiki_reader` (F5), one provider's
  dependencies cannot be read off its signature (F43), and Zotero's
  internal schema is still coupled to without a validated boundary model (F42 residual). Each is a
  recorded residual, not a defect — should the balance ever change, the finding row is the rationale a
  future fix would supersede.
- **Reversible?** Trivially — nothing executable changed. The inline comments and this ADR can be edited
  or removed at will, and any individual deviation can be promoted to a fix by a future slice that
  judges the cost worth paying.

**Related.** [ADR-0021](0021-explicit-eager-composition-root.md) (the composition-root smells this
initiative *did* fix), [ADR-0023](0023-sql-boundary-type-safety.md) (the `api` SQL-boundary type safety,
the read-path locus F4 touches), [ADR-0024](0024-research-profile-privilege.md) (the research-profile
privilege — F42's residual and F41's adapter are research-profile concerns). Code sites carrying the
inline `intentional:` markers: `engram/retrieval/queries.py` (F4, F5, F12), `engram_app/wiring/serve.py`
(F12/F36), `engram_app/wiring/evals.py` (F43), `alembic/versions/0001_wiki_schema.py` (F15, F17), `alembic/versions/0006_compile_state.py`
(F16, F17), `engram/memory/domain.py` (F23), `engram/compile/handlers.py` +
`engram/compile/incremental.py` (F31), `engram/sources/conversion/arxiv_html/` (F22),
`engram/sources/zotero/repository.py` (F42 residual; F41 exemplar). Entry point:
[CONTEXT-MAP.md](../../../CONTEXT-MAP.md). Glossary: [engram/CONTEXT.md](../../CONTEXT.md).
