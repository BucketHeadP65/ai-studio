---
id: engram-adr-0008-configuration-via-application-settings-json
aliases: ["engram-adr-0008-configuration-via-application-settings-json"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0008 — Configuration via a single `application_settings.json` (capability-grouped, env overrides)"
---

# ADR-0008 — Configuration via a single `application_settings.json` (capability-grouped, env overrides)

**Status:** Accepted (2026-06-17; design refined 2026-06-18). Implements Slice A of
the full-mode test plan. Scope: the **engram** app only (a sibling app keeps its
current setup, and copying the pattern later is trivial).

**Context.** engram's configuration was five per-concern `BaseSettings` subclasses
(`VaultSettings`, `WikiDatabaseSettings`, `EmbeddingProviderSettings`,
`ZoteroSettings`, `DocumentIntelligenceSettings`), each with its own `env_prefix`,
composed into `ApplicationSettings` via per-field `default_factory`, all sourced from
a `.env` file. Three problems: (1) config scattered across opaque
`WIKI_DB_`/`VAULT_`/`WIKI_EMBED_`/… prefixes in a flat dotenv with no structure or
types; (2) the grouping was by **technical concern** (a naked `database` blob), not by
the **domain capability** the rest of the codebase (and the earlier internal product it mirrors)
organizes around; (3) two parallel override vocabularies coexisted — `WIKI_DB_*` for
the migration CLI and `APP_DATABASE__*` for the serving path — which is exactly the
"why are there two settings systems?" confusion the refactor set out to remove.

**Options considered.**

- **Keep `.env` + per-concern `env_prefix` (status quo).** Rejected: flat, untyped,
  inconsistent with the estate; two override vocabularies.
- **One JSON source, but keep technical grouping (`database`/`embedding`/…).**
  Rejected: doesn't fix the DDD-alignment problem; `database` is infra, not a domain.
- **One JSON source + committed defaults layered under a gitignored local-overlay
  (a two-file merge).** Rejected during implementation: two files whose values must be
  mentally merged is confusing; not how the reference does it.
- **One JSON source, capability-grouped, with a committed `.example.json` template
  (chosen).** Mirrors an earlier internal product's `server/core`: infra quarantined into one
  `resources` bag, every other group named by domain capability; one real (gitignored)
  config file plus a committed sample.
- **Same, plus an Azure App Configuration source.** Out of scope (user directed: ignore
  App Config). `settings_customise_sources` leaves a clean seam to add it later.

**Decision.**

- One `ApplicationSettings(BaseSettings)` composing **domain-capability** sub-models —
  `vault`, `retrieval` (BM25 dialect + graph backend *policy*), `ingestion` (embedding
  policy), `library` (Zotero) — plus a single **`resources`** infrastructure bag holding
  `database` (Postgres URLs), `openai` (Azure OpenAI endpoint/deployment), and
  `doc_intel` (Azure Document Intelligence endpoint). Sub-models are plain `BaseModel`;
  the root owns all sourcing. Domain groups carry policy; `resources` carries credentials.
- `SettingsConfigDict(env_prefix="APP_", env_nested_delimiter="__",
  nested_model_default_partial_update=True, json_file=<engram>/config/application_settings.json)`.
  `settings_customise_sources` orders: init kwargs → `APP_*` env → the single
  `JsonConfigSettingsSource` (read at call time so the zero-arg accessor stays
  monkeypatchable). **No `.env` source** — it is retired.
- **One real config file**, `config/application_settings.json` — **gitignored**, holding
  machine paths + secrets (cloud DB URLs, Azure endpoints). The corpus is never sourced,
  a command names it with `--corpus` or runs inside one, and the registry `corpora` maps
  names to roots. A committed `config/application_settings.example.json` documents the
  full shape; copy it to create the real file. No defaults-plus-overlay merge — exactly
  one file the app reads.
- **The migration CLI shares the same system.** `alembic/env.py` reads the *same*
  `application_settings.json` (only the DB URL + `retrieval.*` subset, directly, so a
  migration never requires a `vault`) and honours the *same* `APP_*` overrides
  (`APP_RESOURCES__DATABASE__URL`, `APP_RETRIEVAL__DIALECT`,
  `APP_RETRIEVAL__GRAPH_BACKEND`). The `WIKI_DB_*` / `WIKI_GRAPH_BACKEND` env vocabulary
  is **deleted** — one configuration language across app and migrations.
- The `wiki_reader` login password is **derived from** `resources.database.reader_url`
  (env.py parses it and stashes it on `config.attributes["reader_password"]`), so the
  role's password is always the same one the serving layer connects with — the
  reader-credential-mismatch class of bug is structurally eliminated. The integration
  harness falls back to a deterministic `wiki_reader_dev`.
- Expose `@lru_cache fetch_application_settings()` as the single accessor; `container.py`,
  `alembic/env.py`, and `cli.py` resolve through it.

**Consequences.**

- (+) One structured, typed, capability-grouped config; one override vocabulary (`APP_*`)
  everywhere; consistent with the earlier internal product. Secrets stay out of git (the real file
  is gitignored; the committed file is a placeholder template).
- (+) Reader-password mismatches can't recur (single source of truth = `reader_url`).
- (+) Clean seam to add Azure App Configuration later without touching call sites.
- (−) A migration moment: every deployment/dev env moves its `.env` values into
  `application_settings.json` (+ `APP_*` for CI/cloud); `.example.json` is the template.
- (−) A fresh checkout has no `application_settings.json` until copied from the example —
  the same shape as `.env` + `.env.example`.
- **Reversible?** Yes but unattractive — reverting means restoring the per-concern
  `env_prefix` classes + the `WIKI_DB_*` migration interface. The blast radius is
  `settings.py` + `alembic/env.py` + the DI/consumer call sites.

**Related.**
the earlier internal product's `server/core` (the reference: infra in `resources`, domain by
capability). ADR-0006 (the BM25 dialect seam — the dialect selector now lives in
`retrieval.dialect`). ADR-0007 (the AGE dual-graph backend — the backend selector now
lives in `retrieval.graph_backend`). Supersedes the per-concern `default_factory`
composition this refactor replaced.
