---
id: engram-init
aliases: ["engram-init"]
type: reference
app: engram
status: current
tags: []
title: "init, knowledge base initialisation"
---

# init, knowledge base initialisation

## Purpose

The `init` slice configures a knowledge base for engram. It does **not** provision infrastructure.
It scaffolds the `_meta/` directory layout, migrates the Postgres schema, and seeds the
vocabulary. All three operations assume the studio CLI is already reachable on
`PATH` (or via `STUDIO_BIN`) and a Postgres instance is already running (configured via
`application_settings.json` or `APP_INFRASTRUCTURE__DATABASE__URL`).

This is the **configure-only model** of [ADR-0014](../adr/0014-domain-general-vault-spec-and-plugin.md):
`init` is idempotent on a live knowledge base (alembic's `upgrade head` is a no-op when already
current, scaffolded files are skipped if present) and chicken-egg safe. It does not
require an indexed knowledge base, only a writable Postgres URL.

## Entry points

| Command | What it does |
|---|---|
| `studio init scaffold [--spec PATH]` | Scaffold the `_meta/` directory tree (page-type stubs, vocab stubs, indexing config) from a `KnowledgeBaseSchema`. `--spec` defaults to `<knowledge_base>/knowledge-base-schema.json`. Skips files that already exist. |
| `studio init db` | Apply the schema migrations (`alembic upgrade head`) and load vocab from `_meta/vocab.yaml`. Requires a reachable Postgres. |
| `studio init spec` | Write (or overwrite) `knowledge-base-schema.json` from the knowledge base's `_meta/` data. The schema is the machine-readable contract the plugin reads at session start. |
| `studio init all [--spec PATH]` | Run `scaffold`, then `db`, then `spec` in order, the full one-shot initialisation. `--spec` defaults to `<knowledge_base>/knowledge-base-schema.json`. |

The `/knowledge-base:init` skill (bundled with the plugin) interviews the user, writes a draft
`knowledge-base-schema.json`, and then calls these commands. Use it for first-time setup of a new knowledge base.

## Public contract

- **Inputs:** knowledge base path (resolved from `KnowledgeBaseSettings.path`, default from
  `application_settings.json` or `APP_VAULT__PATH`). Postgres URL from
  `DatabaseSettings.url` (`APP_INFRASTRUCTURE__DATABASE__URL`).
- **Outputs:** scaffolded files under `_meta/`, migrated DB schema, `knowledge-base-schema.json` at
  the knowledge base root.
- **Idempotent:** running `init all` twice on the same knowledge base is safe. Alembic is a no-op
  when the schema is current, scaffold skips existing files, and the schema write overwrites only
  when explicitly run.

## Chicken-egg avoidance

The init slice depends on **nothing** in the indexed knowledge base. It does not call `index`,
read `memory.*` tables, or require any embeddings. It only needs:

1. A reachable Postgres accepting connections from `DatabaseSettings.url`.
2. The studio CLI installed (`uv tool install ./studio` or `STUDIO_BIN=uv run studio`).
3. A knowledge base directory with the note folders its schema declares and `_meta/` (or you run `scaffold` first to create them).

## Retrieval profile

After `init all`, the bundled MCP server reads `APP_RETRIEVAL__RETRIEVAL_PROFILE`
(default `"full"`) to decide which tools to expose. Valid values: `full`,
`search-hybrid`, `search-keyword`, `graph-only`, `keyword-graph`. See
[serve](serve.md) and [ADR-0012](../adr/0012-retrieval-strategy-and-code-mode-axes.md).

## Disclosure declarations

Two optional `knowledge-base-schema.json` keys govern the progressive-disclosure surface (defaults preserve
existing behavior exactly):

- per page type, `"disclosed": true|false` (default `true`). A page type marked `false` never
  appears as a collection child in a disclosure envelope.
- under `"retrieval"`, `"disclosure_mode": "strict"|"search_only"` (default `"strict"`).
  `search_only` withholds the collection root map. Search hits still re-enter as disclosable
  `page:`/`section:` refs.

Enforcement lives in the serve slice's `EnvelopeHandler`. The agent-facing surface is the ordinary
`fetch` tool, which becomes this leveled read once the handler is wired. There is no separate
`disclose` tool. See [serve](serve.md).
