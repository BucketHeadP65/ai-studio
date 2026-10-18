# Studio plugin for Codex and Claude Code

engram is a domain-general markdown-knowledge-base indexer. It indexes any Obsidian-style knowledge base into Postgres (the local Postgres with Apache AGE, BM25 and pgvector by default, ParadeDB as an option, or a managed Postgres with the same extensions), powering full-text search, vector similarity, and a typed knowledge graph, accessible to Codex and Claude Code via bundled MCP tools.

This plugin:
- Ships the `studio mcp` retrieval server (auto-registers when the plugin is enabled, no hand-edited `.mcp.json` needed)
- Ships the `/knowledge-base:init` onboarding skill to co-design your knowledge base's schema and bootstrap the database
- Indexes the knowledge base again at session start (a `SessionStart` hook), so retrieval always reads what is on disk
- Ships the `agent-threads` skill, the note threads' verbs for an agent (the public Agent Threads Obsidian plugin at https://github.com/BucketHeadP65/agent-threads writes the same skill into the knowledge base folder)

---

## Prerequisites (configure-only)

engram is a configure-only dependency, the plugin does not install it for you.

1. **Install studio** from this repository and make the `studio` command reachable:
   ```bash
   cd studio && uv sync   # the executable is studio/.venv/bin/studio
   ```
   Put that executable on your PATH, or set `STUDIO_BIN=/absolute/path/to/studio/.venv/bin/studio` in your shell
   environment. This repository does not publish `studio` to PyPI.

2. **A reachable Postgres instance.** For local development, `docker compose up -d` starts the
   local Postgres (the default, host port 5434, `pg_textsearch` BM25, Apache AGE and pgvector). ParadeDB is the opt-in
   database behind a profile (`docker compose --profile paradedb up paradedb`, host port 5433).
   ```bash
   docker compose up -d   # from the engram source directory (the local Postgres on :5434)
   ```
   Default local URL: `postgresql+asyncpg://wiki:wiki@localhost:5434/knowledge_bases` (ParadeDB opt-in: `:5433`).

   For a managed Postgres with the same extensions, supply its connection strings via the environment,
   `APP_INFRASTRUCTURE__DATABASE__URL` and `APP_INFRASTRUCTURE__DATABASE__READER_URL` (plus
   `APP_RETRIEVAL__DIALECT=pg_textsearch` and `APP_RETRIEVAL__GRAPH_BACKEND=age`). The MCP server
   reads these from its environment. No engram config file is required on your side.

---

## Install in Codex

The source includes `.codex-plugin/plugin.json` alongside the Claude manifest. Both clients share the same skills and hooks, with separate MCP launch declarations in their manifests. Codex's inline Bash launcher finds `knowledge-base-schema.json` in the session's working directory, under `docs/` or under `knowledge-base/`, then invokes the installed Studio CLI. Open the project or knowledge-base root in Codex. This launcher avoids plugin-root placeholders, which Codex's legacy MCP loader does not expand.

Build a standalone copy before registering it in a local marketplace. The source skill directories are symlinks into the Python package. The packaging command includes their actual files, including scripts, schemas, and examples:

```bash
# From the monorepo root. The destination must be outside this checkout.
python3 engram/scripts/package_plugin.py ~/plugins/knowledge-base
```

Use Codex's `plugin-creator` skill to register the resulting directory in the personal marketplace at `~/.agents/plugins/marketplace.json`. Preserve this export directory's manifest when registering it. Once the `studio` entry is registered under the `personal` marketplace:

```bash
codex plugin add studio@personal
```

Ensure `studio` is on the PATH inherited by Codex, or launch Codex with `STUDIO_BIN=/absolute/path/to/studio`. In this checkout, `studio/.venv/bin/studio` is the executable after the Studio environment has been installed. `STUDIO_BIN` must name one executable, not a shell command such as `uv run studio`. The Codex manifest forwards the listed database, embedding, retrieval, and Azure identity variables through `env_vars`. Add other environment overrides there when needed. Existing Studio configuration files still apply.

Start a new session after installation, then check `/mcp` for Studio. Select the `engram-init` skill or ask to set up the knowledge base. The Claude `/knowledge-base:init` command stub is not needed in Codex. For note-writing setup, select `designing-note-writing`. It creates local skills under the project's `.agents/skills/` in Codex and `.claude/skills/` in Claude Code.

Review and trust the plugin's `SessionStart` hooks in Codex before expecting automatic indexing and status nudges. Installing the plugin does not automatically trust hooks.

To update the local copy, rebuild with `--force` (replaces only the chosen generated export directory), then use `plugin-creator`'s cachebuster/reinstall workflow and start a new session:

```bash
python3 engram/scripts/package_plugin.py ~/plugins/knowledge-base --force
```

## Install in Claude Code (per-project)

Add the plugin to your knowledge base's `.claude/settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "ai-studio": {
      "source": {
        "source": "directory",
        "path": "<ai-studio-checkout>/.claude-plugin/marketplace.json"
      }
    }
  },
  "enabledPlugins": {
    "knowledge-base@ai-studio": true
  }
}
```

Replace `<ai-studio-checkout>` with the path to this repository, which holds `.claude-plugin/marketplace.json`.

Or use the Claude Code CLI:
```bash
claude plugin marketplace add ./ --scope project
claude plugin install knowledge-base@ai-studio --scope project
```

---

## Usage

### First-time setup
Run the onboarding skill to co-design your knowledge base's schema and bootstrap the database:
```
/knowledge-base:init
```

This will:
1. Check whether the studio CLI is installed and reachable
2. Interview you to design a `KnowledgeBaseSchema` for your domain
3. Write `./knowledge-base-schema.json` (version-control this file)
4. Run `studio init all` to scaffold `_meta/`, run migrations, and load vocabulary

After the skill completes, **reload Claude Code** so the bundled MCP server attaches.

### Index notes
After authoring or editing notes in your knowledge base:
```bash
studio index
```

### Search
Ask Claude directly, or use the `searching-the-knowledge-base` skill.

---

## Bundled MCP server

The Claude plugin registers a `knowledge-base` MCP server through `bin/knowledge-base-mcp`. Codex uses the inline launcher in its manifest. Both find the knowledge base at the project root, under `docs/` or under `knowledge-base/`, then run `studio mcp --knowledge-base <resolved-path>`, resolving the binary via `$STUDIO_BIN` (if set) or `studio` on PATH.

Claude Code passes `CLAUDE_PROJECT_DIR` (the directory you opened) to the wrapper. Codex uses the session's working directory. Its MCP configuration deliberately omits the Claude project placeholder. Both clients pass the resolved knowledge-base path explicitly to Studio. The knowledge base is self-contained: its tool surface comes from its own `knowledge-base-schema.json` (below), its database + dialect from the environment (above), and **no engram config file is needed**.

### Identity guard

The injected `--knowledge-base` is **authoritative**, and it is the only thing that names the knowledge base apart from the working directory. The settings carry no knowledge base field at all, so an `APP_KNOWLEDGE_BASE__PATH` in the environment does nothing. Only a command names the knowledge base, with `--knowledge-base` or, absent that flag, the working directory. On startup, the server prints a one-line banner to stderr that surfaces the resolved **knowledge base**, **db**, and **binary** it is serving:

```
studio mcp serving: knowledge_base=/path/to/knowledge-base db=host:5434/knowledge bases bin=/path/to/studio dialect=… graph=… profile=…
```

The `bin=` field is the running studio entrypoint the wrapper resolved via `${STUDIO_BIN:-studio}`. This makes a wrong-knowledge-base situation diagnosable directly from the banner: a PATH-shadowing `studio` (or a `STUDIO_BIN`) wrapper that exports its own `APP_*` and serves a different database shows up as a `bin=` / `db=` you did not expect, instead of merely surfacing as puzzling search results.

> **Relaunch caveat.** The MCP server launches **once** at session start, so a plain relaunch inherits the same `${STUDIO_BIN:-studio}` resolution. Fixing a bad wrapper means correcting the wrapper (or `STUDIO_BIN`) **and then** running `/mcp` to reconnect, a relaunch alone re-runs the same resolution and serves the same wrong knowledge base.

The tools available depend on the `retrieval.profile` in your `knowledge-base-schema.json`:

| Profile | Tools |
|---|---|
| `full` (default) | `search`, `fetch`, `graph_expand`, `find_paths`, `run_sql`, `describe_schema` |
| `search-hybrid` | `search`, `fetch`, `run_sql`, `describe_schema` |
| `search-keyword` | `search` (BM25 only), `fetch`, `run_sql`, `describe_schema` |
| `graph-only` | `fetch`, `graph_expand`, `find_paths`, `run_sql`, `describe_schema` (no `search`, so an id comes from the task or a prior traversal) |
| `keyword-graph` | `search`, `fetch`, `graph_expand`, `find_paths`, `run_sql`, `describe_schema` |
| `code-mode` | the `full` set **plus** `run_code`, a Monty sandbox that orchestrates the retrieval functions in one round-trip (opt-in, see the `searching-the-knowledge-base` skill) |

---

## Regenerate knowledge-base-schema.schema.json

`skills/engram-init/knowledge-base-schema.schema.json` is generated from `KnowledgeBaseSchema.model_json_schema()`, plus the one repair the
generator cannot express (`canonical_sections` accepts a bare slug string as well as a full object). If you upgrade
engram and the spec model changes, regenerate it from the engram source directory:

```bash
cd <engram-source>
uv run python -m engram.skills.engram_init_schema > src/engram/skills/engram-init/knowledge-base-schema.schema.json
```

`skills/` here symlinks into `src/engram/skills/`, so that one write updates both.
`tests/unit/engram/test_skills_shipped.py` fails if the shipped file drifts from the generator's output.

---

## Version sync

Both `plugin/.claude-plugin/plugin.json` and `plugin/.codex-plugin/plugin.json` list `"version": "0.0.1"` to mirror `engram/pyproject.toml`. Keep these in sync when releasing. A local Codex install may add a cachebuster suffix to its generated copy. It does not change the source release version.

### Validation

Run `engram/.venv/bin/pytest` from the monorepo root with `-c engram/pyproject.toml engram/tests/unit/engram/plugin engram/tests/unit/engram/test_skills_shipped.py`, or run the same tests from the `engram/` project. Verify the installed copy with Codex's `/mcp`, skill picker, and `/hooks` as well.

The generic Codex plugin/skill validators flag the existing Claude-only frontmatter on `designing-note-writing` (`disable-model-invocation`, `tags`, and `argument-hint`). These fields are retained for Claude compatibility. Codex 0.154 loads the skill successfully and uses its `agents/openai.yaml` invocation policy. This local export directory is runtime-tested, not a claim of public-directory submission validation.

---

## Worked examples

Two example `knowledge-base-schema.json` files are bundled under `skills/engram-init/examples/`:

- `research.json`, the canonical research knowledge base spec (papers, entities, claims, Zotero sources)
- `cooking.json`, a minimal non-research spec (recipes, ingredients, cuisine facets, no claims)

Use them as starting points when running `/knowledge-base:init`, or inspect them to understand the spec format.
