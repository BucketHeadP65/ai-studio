---
id: engram-adr-0014-domain-general-vault-spec-and-plugin
aliases: ["engram-adr-0014-domain-general-vault-spec-and-plugin"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0014 — Domain-general vaults via a spec-driven ontology; engram as a Claude Code plugin"
---

# ADR-0014 — Domain-general vaults via a spec-driven ontology; engram as a Claude Code plugin

**Status:** Accepted (2026-06-20). Generalizes engram from a research-paper wiki compiler into a
domain-general markdown-vault compiler, shipped as a Claude Code plugin. The change has three
parts: (1) the page-type ontology becomes per-vault data — a `VaultSchemaSpec` validated like
any other boundary object — amending D-1's "7 page types frozen across domains" clause; (2) engram
is distributed as a Claude Code plugin installed into any vault, removing all hardcoded path
couplings; (3) `engram init` configures (not provisions) — scaffolds `_meta/`, migrates the DB
schema, loads vocab — assuming the CLI and a Postgres are already reachable.

**Context.** D-1 settled the foundational tension between agent legibility and per-domain
flexibility by fixing the structure (3 layers, 7 page types, sections, claims, edges) and leaving
vocabulary open (registry rows). That delivered a corpus the agent can navigate without re-learning
schemas on every vault. What it did not do is let engram compile any vault other than the one
research ontology it was built for.

The hardcoding showed up in five places: eight typed `FRONTMATTER_BY_TYPE` subclasses in
`wikinote/`; module-level lookup tables (`_PREFIX_BY_TYPE`, `_CANON_H2_BY_TYPE`,
`_CANON_H2_ORDER`, `_EDGE_RELATIONS`, claim-field frozensets, `_QUOTE_REQUIRED_KINDS`,
`_COMPARATIVE_RELATIONS`); `compile/validator.py` branching on page type; the vault's `.mcp.json`
pointing at a hardcoded engram path; and skills that hardcoded the engram CLI path. Everything
else — the storage spine, the `api.*` SQL contract, M-Schema reflection, embed/search, the
claim-grammar mechanics, the `BaseFrontmatter` shared fields — was already generic. A careful audit
found engram was approximately 95% domain-agnostic: the research ingest pipeline
(Zotero/PDF/Sources/quote-verify) is already command-gated and optional; `wiki.pages` is keyed by
a generic `kind` column with 5 generic `text[]` facet columns; the `api.*` views degrade to empty
(not broken) when pages of unknown types are absent; M-Schema reflects the live schema. The
page-type layer was the only real coupling, and it was a coupling by choice, not by necessity.

The trigger for removing it was a concrete use-case: installing engram into a non-research vault
(cooking, legal, personal notes, company docs) requires only that the vault agree on a type
ontology with the compiler. A user-facing onboarding interview to co-design that ontology, captured
as a versioned `vaultspec.json`, is the right shape — identical to how the research vault's
`_meta/registry/*` captures vocabulary today, but one level higher (schema, not vocabulary).

**Options considered.**

- **Runtime dynamic Pydantic (`create_model`) + per-vault Postgres tables/columns.** Rejected.
  This is the XL generalization path — it reopens D-1's rejected "per-domain page schemas"
  (ORKG-style), forces a rewrite of the 21 hand-authored `pages`-column references in the `api.*`
  SQL (migration 0002), and invalidates M-Schema reflection. High cost for a fixed contract that
  loses the single agent surface. The spec-as-data path achieves the same generality at S–M cost
  confined to `wikinote/`, leaving the storage spine and agent contract intact.
- **Auto-provisioning a DB / packaging engram for pip install.** Deferred (configure-only, Part 3
  below). A managed DB ([ADR-0006](0006-dual-dialect-bm25-seam.md),
  [ADR-0008](0008-configuration-via-application-settings-json.md)) cannot be auto-provisioned by a
  plugin anyway; the user must supply the connection.
- **Promoting open facet keys to JSONB-native columns.** Captured in the spec
  (`facet.slot = null` → rides the `frontmatter` JSONB column) but storage is unchanged this
  round. Its own future ADR when column promotion is worth the migration cost.
- **A SessionStart auto-onboard hook.** Rejected as the onboarding trigger. Claude Code has no
  first-install hook. Onboarding is a user-invoked `/engram:init` skill, with an optional
  SessionStart *nudge* (a brief prompt) if the vault contains no `vaultspec.json` — but the
  interview itself must be explicit and acknowledged.

**Decision.**

The decision has three parts.

**Part 1 — Spec-driven page-type layer, validated as data (amends D-1).**

A `VaultSchemaSpec` model in `wikinote/spec.py` replaces the eight hardcoded `FRONTMATTER_BY_TYPE`
subclasses and the associated module-level lookup tables. It declares: `page_types[]` (kind,
id_prefix, folder, frontmatter_fields[], canonical_sections[], edge_fields[], claims_enabled);
`relations[]` (slug, domain→range, symmetric, qualifiers); `entity_types[]` and `claim_kinds[]`
(vocabulary); `facets{}` (key → slot mapping onto the 5 generic text[] columns, or null ⇒ JSONB);
`claims` (the claim grammar field-sets + rules, present if any type has claims_enabled); `sources`
({enabled, quote_verify} — research: on; generic: off); `retrieval` (profile ∈ the strategy names
from [ADR-0012](0012-retrieval-strategy-and-code-mode-axes.md)); plus `purpose` and
`slug_conventions` prose. The model lives in `wikinote/` because the kernel owns its contract, like
`RegistryIndex`. engram can export per-page-type authoring JSON Schemas from it — the forward
direction of today's `payload_json_schema` (D-22).

The spec is stored as `vaultspec.json` at the vault root: version-controlled, the auditable record
of the onboarding interview, re-runnable. The compile *validate* stage loads it and validates each
note against its spec entry exactly as it already validates vocabulary against `RegistryIndex`. The
research 7+1 page types become a shipped default `research` spec; the live research vault falls
back to it when no `vaultspec.json` is present (backward-compatible). The spec is parity-gated
against the live research vault (identical RegistryIndex/lint/compiled rows) before it ships.

What this does NOT change: the storage spine (one generic `wiki.pages` table, 5 generic `text[]`
facet columns); the curated `api.*` SQL contract and M-Schema reflection (D-11); the
`BaseFrontmatter` universal fields; the claim-grammar mechanics (D-2, D-4); the embed/search path.
There is no runtime Pydantic class generation, no per-vault Pydantic subclasses, no per-vault
Postgres tables or columns. Extra or unmodelled frontmatter keys ride the existing `frontmatter`
JSONB column unchanged.

This amends D-1's "7 page types frozen across domains" clause. The *types* are now per-vault data;
the *machinery and the single `api.*` contract* remain frozen. D-1's core wager — agent legibility
from a fixed, discoverable structure — is preserved: the agent still reads one `api.schema_guide`
and one M-Schema regardless of what the vault's ontology contains.

**Part 2 — engram as a Claude Code plugin (distribution).**

engram's skills, `engram init` scaffolder, schema-design process, and retrieval MCP server are
packaged as a Claude Code plugin installed into any vault. The vault knows nothing about engram: no
hardcoded `.mcp.json` path, no hardcoded engram CLI path in skills.

The plugin lives in-monorepo at `engram/plugin/` with a `plugin.json` manifest. The manifest
registers the retrieval MCP server via its `mcpServers` entry, resolving the binary path as
`${ENGRAM_BIN}` (env override) or `engram` on `PATH`, using `${CLAUDE_PLUGIN_ROOT}` for the
plugin root. Skills pass `--vault .` so the vault's CWD is always the working directory. Because
the plugin lives in the monorepo alongside the CLI it calls, they version together — no
out-of-band sync.

The monorepo root is a single-plugin marketplace. Per-project install is via
`.claude/settings.json` (a local plugins entry), not a global install. On first use, the user
invokes `/engram:init` — a skill that co-designs the vault's `VaultSchemaSpec` through an
interview, then calls `engram init` to scaffold `_meta/`, apply the DB migration, and load the
vocabulary. Subsequent `/engram:*` skills and the MCP server route through the same plugin, with no
configuration beyond the initial `engram init` step.

**Part 3 — Configure-only infra (scope choice).**

`engram init` configures, it does not provision. Given a reachable `engram` CLI and a reachable
Postgres instance (local ParadeDB, port 5433 by default, or a managed Postgres per
[ADR-0006](0006-dual-dialect-bm25-seam.md) / [ADR-0008](0008-configuration-via-application-settings-json.md)),
`engram init` scaffolds `_meta/` (purpose, schema, retrieval-policy stubs), runs `alembic upgrade
head`, and loads the vocabulary from the spec. It does not bring up Docker, does not package engram
for `pip install`, and does not auto-provision hosted resources. A managed DB cannot be provisioned
by a plugin. The user supplies the DB connection (via `application_settings.json` or env vars). A
future ADR may address packaging and provisioning when the scope demands it.

**Consequences.**

- (+) **The live research vault keeps working byte-for-byte.** When `vaultspec.json` is absent,
  engram falls back to the bundled `research` default spec. The parity gate verifies identical
  RegistryIndex validation, lint results, and compiled rows against the current vault. No migration
  needed for existing users.
- (+) **Any markdown vault is supported.** Install the plugin → invoke `/engram:init` → co-design
  `vaultspec.json` → `engram init` scaffolds + migrates + loads vocab → bundled skills and MCP
  server work with no hardcoded paths. A cooking vault, a legal notes vault, a company wiki — any
  domain with a coherent type ontology is a first-class engram vault.
- (+) **No hardcoded couplings.** The plugin resolves its own binary path; vaults contain no
  engram-specific `.mcp.json` entries or hardcoded skill paths. Moving the engram CLI install
  location requires only an `ENGRAM_BIN` override.
- (+) **S–M cost, not XL.** The spec-as-data generalization is confined to `wikinote/`; the
  storage spine, the `api.*` SQL, M-Schema, and the agent contract are untouched. The compiler
  validate stage gains one new input (the spec) without changing its structure.
- (−) **Generic-frontmatter ergonomics.** Losing the 8 typed Pydantic subclasses costs some
  internal static IDE completions for page-type-specific fields. This is mitigated because
  production code already reads type-specific fields via `getattr` against `BaseFrontmatter` — the
  typed subclasses were almost exclusively used in tests. Test fakes update to work against the
  spec's field lists.
- (−) **Spec/CLI version skew is a managed risk.** A `vaultspec.json` designed against engram 0.9
  may reference a page-type feature removed in engram 1.0. Mitigation: stamp the generating engram
  version (or a spine-compatibility hash) into the spec; compile lint surfaces schema drift as an
  explicit error, not silent data corruption.
- (−) **Configure-only init means two prerequisites.** The user must have the `engram` CLI
  installed and a Postgres reachable before `/engram:init` can complete. The plugin cannot satisfy
  either requirement itself. This is an explicit scope choice (Part 3), not an oversight.
- **Reversible?** Partially. The plugin manifest and `engram init` scaffolding are additive and
  removable. The spec-driven kernel requires re-hardcoding the lookup tables to undo; the parity
  gate and the bundled `research` default mean the rollback path keeps the research vault intact.

**Related.**
[ADR-0006](0006-dual-dialect-bm25-seam.md) + [ADR-0008](0008-configuration-via-application-settings-json.md)
(the dual-dialect BM25 seam and its config, the DB backends `engram init` targets),
[ADR-0007](0007-age-multihop-dual-graph-backend.md) (AGE backend — spec's `retrieval` profile
selects graph backend per vault), [ADR-0009](0009-okf-export-interchange.md) (OKF export — the
projector is spec-driven: it exports every page type the vault's spec declares (research kinds keep
their `papers/`…`maps/` subdirs; a generic vault's kinds export to their own spec-folder subdirs),
so a non-research vault exports a real bundle), [ADR-0012](0012-retrieval-strategy-and-code-mode-axes.md)
(retrieval strategy names the spec's `retrieval.profile` references),
[ADR-0013](0013-design-and-vision-docs-in-repo.md) (the explanation docs this ADR adds to).
Product decisions: D-1 (amended by D-25), D-11 (fixed `api.*` contract preserved), D-22 (the
note-build/validate/verify path — spec supplies the schema the kernel validates against).
Code: `wikinote/spec.py` (`VaultSchemaSpec`), `engram/plugin/plugin.json` (the plugin manifest),
`engram/plugin/skills/` (the `/engram:init` and related skills), `compile/validator.py`
(spec-driven validate stage).
