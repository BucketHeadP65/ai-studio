---
id: engram-adr-0024-research-profile-privilege
aliases: ["engram-adr-0024-research-profile-privilege"]
type: adr
app: engram
status: superseded
superseded_by: "[[engram-adr-0042-corpus-qualified-identity]]"
tags: []
title: "ADR-0024 — Research-profile privilege on a domain-general spine"
---

# ADR-0024 — Research-profile privilege on a domain-general spine

**Superseded in part by [ADR-0042](0042-corpus-qualified-identity.md) (2026-08-20)** — its schema seam (F3) migrates
away to `frontmatter`/`facets` there; F8 (ubiquitous language) stands, untouched.

**Amended by [root ADR-0018](../../../docs/adr/0018-corpus-bundles-and-provider-packages.md) (2026-09-02)** — the
ingestion seam (F11) is no longer a privilege engram carries. The provider implementations and their converters left
the compiler for the `providers` project, engram keeps only vendor-neutral ports, and a corpus's vocabulary lives in
its own bundle. This ADR's record of what was accepted at the time stands as written.

**Status:** Superseded by [ADR-0042](0042-corpus-qualified-identity.md) on its schema seam (2026-08-20); originally
Accepted (2026-06-30). Delivered in the `engram-domain-remediation` initiative (Slice D,
task D9). **Refines** [ADR-0014](0014-domain-general-vault-spec-and-plugin.md) (it does not overturn
it): ADR-0014's domain-general claim stands; this ADR records, honestly and by name, the three seams
where the research profile remains privileged on that otherwise generic spine. No schema migration, no
`api` SQL surface change, no code behavior change — documentation only. Findings F3, F11, F8.

**Context.** [ADR-0014](0014-domain-general-vault-spec-and-plugin.md) generalized engram from a
research-paper wiki compiler into a domain-general markdown-vault compiler: the page-type ontology
became per-vault data (`VaultSchemaSpec`, `wikinote/spec.py`), engram ships as a Claude Code plugin
installed into any vault, and the storage spine plus the `api.*` agent contract stay frozen across
vaults. The wager held — a cooking, legal, personal, or company-docs vault is a first-class engram
vault. But "domain-general" is not the same as "research-neutral", and three seams still privilege the
research profile in ways the architecture docs glossed over. Naming them is the point of this ADR: an
honest, bounded claim is stronger than an unqualified one.

The three privileged seams:

- **Schema (F3).** `wiki.pages` — and its ORM, `PageORM` in `features/compile/orm.py` (around lines
  128–141) — carries roughly eleven typed, research-specific *nullable* columns on the otherwise generic
  spine: `entity_type`, `paper_type`, `year`, `venue`, `citekey`, `zotero_key`, `source_hash`,
  `maturity`, `question_status`, plus the named facet arrays. These are dead weight for a non-research
  vault — a cooking vault has no `venue` or `citekey` — yet they sit on the shared table. The genuinely
  generic surface, the part that actually carries any vault's structure, is `kind` + the five `text[]`
  facet arrays (`tasks` / `domains` / `stages` / `modalities` / `scale_regimes`) + the `frontmatter`
  jsonb. A generic vault expresses everything through those and simply leaves the typed research columns
  NULL. ADR-0014 noted `wiki.pages` is "keyed by a generic `kind` column with 5 generic `text[]` facet
  columns" but did not name the typed columns riding alongside as a research privilege; F3 makes that
  coupling explicit. The columns cannot be shed without a schema migration (a destructive `ALTER TABLE`
  plus the 21 hand-authored `pages`-column references in the `api.*` SQL of migration 0002).

- **Ingestion (F11).** The `Mirror` / `Sync` / `Conversion` bounded context — `features/sync/`,
  `features/conversion/`, `features/mirror/` — is the **one genuinely profile-coupled boundary** in the
  system. Its definitions are inescapably research-flavored: a `Mirror` is an immutable full-text *paper*
  copy keyed by `zotero_key` + `source_hash`; `Sync` reconciles a local *Zotero* library into `Sources/`;
  `Conversion` is the *PDF/arXiv-HTML* → Markdown step. ADR-0014 already established this path is
  command-gated and optional (`sources.enabled` in the spec — research: on, generic: off), but the
  glossary and docs presented `Mirror`/`Sync`/`Conversion` as if they were spine concerns. They are not:
  a domain-general vault may have no Zotero, no PDFs, and never invoke `engram sync` at all. Ingestion is
  the research profile's boundary, and the rest of the pipeline (compile / serve / wikinote) is the
  domain-general spine.

- **Ubiquitous language (F8).** The §01 C4-context node and parts of the glossary still call the corpus
  "the wiki", a research-era term that predates ADR-0014's domain-general turn. This is mild
  ubiquitous-language drift, not a coupling: "wiki" is the established, well-understood term across the
  codebase (`WikiNote`, `wiki.*` schema, `wiki_reader` role, `wikinote/`), and renaming it would be a
  large, low-value churn. F8 itself frames this as a question — "a future language pass should decide
  whether 'wiki' is the intended ubiquitous term or legacy" — rather than a defect to fix now.

**Options considered.**

- **Migrate the typed research columns off `wiki.pages` into a profile-scoped table (or fold them into
  `frontmatter`).** Rejected. This is the schema-purist path. It demands a destructive migration, a
  rewrite of the 21 `pages`-column references in the `api.*` SQL (migration 0002), invalidates M-Schema
  reflection of those columns, and risks the `incremental == full` byte-identical row guard
  ([ADR-0019](0019-true-incremental-ingestion.md)) — all to remove columns that simply sit NULL for a
  generic vault and cost nothing at query time. For a shipped research-first product the cost/risk vastly
  outweighs the genericity gain. The columns are accepted, documented debt, not a defect to migrate.
- **Rename "wiki" to a domain-neutral term across the codebase and docs.** Rejected for now. A
  large-surface rename (schema, role, package, models, every doc) for a term that is already understood;
  the value is cosmetic. Deferred to a future prose language-sweep, not this task.
- **Document the privilege at its three named seams (chosen).** This ADR, an inline `intentional:` note
  on the `PageORM` research columns, and refined `CONTEXT.md` glosses that mark `Mirror`/`Sync`/
  `Conversion` as research-profile concerns. Records the truth without paying a migration's cost.

**Decision.**

Document the research-profile privilege; do **not** migrate it away.

1. **Schema (F3) — keep the typed columns, mark them as an intentional privilege.** The ~11 typed
   research columns stay on `PageORM` / `wiki.pages`. An inline `intentional:` comment on the promoted
   column block in `features/compile/orm.py` records that they are a deliberate research-profile
   privilege on the domain-general spine: a generic vault leaves them NULL, and the generic surface is
   `kind` + the `text[]` facet arrays + `frontmatter` jsonb. No column is added, removed, or renamed; no
   `api.*` SQL changes.

2. **Ingestion (F11) — name the boundary in the glossary.** The `CONTEXT.md` glosses for `Mirror`,
   `Sync`, and `Conversion` are refined to state that each is a **research-profile** concern and that the
   Ingestion bounded context (`features/sync/` + `features/conversion/` + `features/mirror/`) is the one
   genuinely profile-coupled boundary, while the spine (compile / serve / wikinote) is domain-general.
   The entries are refined, not contradicted, and cross-reference this ADR.

3. **Ubiquitous language (F8) — retain "wiki", note the genericity.** "wiki" stays the established
   ubiquitous term while the docs continue to state that engram is domain-general. A full prose
   language-sweep of the §01 architecture node is a **deferred follow-up**, explicitly out of scope for
   this task — recorded here so the decision is on the record, not silently dropped.

**Consequences.**

- (+) **The domain-general claim is now honest and bounded.** The spine — `kind` + `text[]` facets +
  `frontmatter` jsonb, the `api.*` contract, M-Schema, embed/search, the claim grammar — is genuinely
  generic; the research profile is a *named, documented privilege* at three specific seams, not an
  unstated assumption. ADR-0014 is refined, not weakened.
- (+) **A future generic vault works unchanged.** It expresses its structure through `kind` + facets +
  `frontmatter`, leaves the typed research columns NULL, sets `sources.enabled = false` so it never
  touches Ingestion, and serves over the same frozen `api.*` surface.
- (+) **Zero migration risk taken on.** No schema, `api` SQL, `wiki_reader` role, or HTTP contract
  changed; the `incremental == full` guard and every existing vault are untouched.
- (−) **The schema debt is acknowledged and accepted, not retired.** A non-research vault still carries
  ~11 always-NULL columns on `wiki.pages`. This is a recorded residual: should a future need justify it,
  a profile-scoped-table migration is the path, and this ADR is the rationale it would supersede.
- (−) **"wiki" remains research-flavored language.** The ubiquitous-language drift (F8) is documented but
  not resolved; the prose language-sweep is deferred to a future task.
- **Reversible?** Trivially — nothing executable changed. The inline comment and the glosses can be
  edited or removed at will; this ADR can be superseded by a future migration ADR if the debt is ever
  retired.

**Related.** [ADR-0014](0014-domain-general-vault-spec-and-plugin.md) (the domain-general vault-spec +
plugin decision this ADR refines), [ADR-0003](0003-hybrid-html-first-conversion.md)
(the `Conversion` step F11 names as research-coupled),
[ADR-0019](0019-true-incremental-ingestion.md) (the byte-identical row guard the no-migration decision
preserves), product decision D-25 (the open per-vault spec / `research` default that makes the spine
domain-general). Code: `features/compile/orm.py` (`PageORM`'s promoted research columns — the F3 seam,
carrying the inline `intentional:` note), `features/sync/` + `features/conversion/` + `features/mirror/`
(the Ingestion bounded context — the F11 research-profile boundary). Entry point:
[CONTEXT-MAP.md](../../../CONTEXT-MAP.md). Glossary: [engram/CONTEXT.md](../../CONTEXT.md).
