---
id: engram-plugin
aliases: ["engram-plugin"]
type: reference
app: engram
status: current
tags: []
title: "plugin, Studio for Codex and Claude Code"
---

# plugin, Studio for Codex and Claude Code

## Purpose

engram ships as a **Codex and Claude Code plugin** ([ADR-0014](../adr/0014-domain-general-vault-spec-and-plugin.md)):
a shared package installed through the client's plugin marketplace that delivers the MCP
server, the `engram-init` setup skill, the note-writing workflows,
and a `SessionStart` nudge hook. Once installed, any knowledge base can be indexed and queried
without hardcoded paths.

For Codex, build a standalone export directory with `python3 engram/scripts/package_plugin.py ~/plugins/knowledge-base` from the monorepo root and register it in a local Codex marketplace. The export directory includes both client manifests and the real skill files. See the [plugin installation guide](../../plugin/README.md#install-in-codex) for installation, updates, executable configuration, and hook trust.

## Installation

1. Install the studio CLI (once per machine):

   ```bash
   uv tool install ./studio          # or: pip install ./studio
   # verify:
   studio --version
   ```

   Or set `STUDIO_BIN` to the full path to the CLI executable. It cannot contain a shell command such as `uv run studio`.

2. Install the plugin into the knowledge base:

   ```bash
   # from the knowledge base root:
   claude plugin marketplace add ./ --scope project
   claude plugin install knowledge-base@ai-studio --scope project
   ```

   The plugin source is `./engram/plugin` (relative to the repo root, per
   `.claude-plugin/marketplace.json`).

## Bundled MCP server

The plugin registers one MCP server named `studio`:

```json
{
  "studio": {
    "command": "${CLAUDE_PLUGIN_ROOT}/bin/knowledge-base-mcp",
    "args": ["${CLAUDE_PROJECT_DIR}"]
  }
}
```

This is the Claude declaration. Codex uses an inline Bash launcher in `plugin/.codex-plugin/plugin.json`, resolving the project from the session working directory. Both launchers find a schema at the project root, under `docs/` or under `knowledge-base/`, then pass the selected path to Studio. Both resolve `${STUDIO_BIN:-studio}` so a plain `studio` on `PATH` works.

### Retrieval profile

The `retrieval_profile` setting (env `APP_RETRIEVAL__RETRIEVAL_PROFILE`, default `"full"`)
gates which of the four structured tools the MCP server exposes:

| Profile | Exposed tools |
|---|---|
| `full` | `search`, `fetch`, `graph_expand`, `find_paths` |
| `search-hybrid` | `search` (hybrid), `fetch` |
| `search-keyword` | `search` (BM25-only), `fetch` |
| `graph-only` | `fetch`, `graph_expand`, `find_paths` (no `search`, so an id comes from the task or a prior traversal) |
| `keyword-graph` | `search` (BM25-only), `fetch`, `graph_expand`, `find_paths` |

`describe_schema` and `run_sql` are always present regardless of profile. They give the
agent raw SQL access to the `api.*` surface. `run_sql` checks each statement before it runs and stops
it at the row cap, as [serve.md](serve.md) describes (engram ADR-0047).

Set `APP_RETRIEVAL__RETRIEVAL_PROFILE=search-hybrid` in the knowledge base's
`application_settings.json` or environment to restrict the surface:

```json
{
  "retrieval": { "retrieval_profile": "search-hybrid" }
}
```

## SessionStart nudge hook

The plugin ships a `SessionStart` hook (`plugin/hooks/hooks.json` +
`plugin/hooks/nudge.sh`). On every session start, if `./knowledge-base-schema.json` is **absent**
from the knowledge base root, the hook prints:

```
studio: this knowledge base is not initialized, run /knowledge-base:init to set up the schema and DB.
```

If `knowledge-base-schema.json` is present, the hook also runs `studio decisions --count --knowledge-base .` and
counts the pages awaiting a decision across every one of the knowledge base's declared decision
page types (`KnowledgeBaseSchema.decision_page_types`), not one hardcoded folder. When the count is above zero it prints:

```
studio: N pages await a decision in this knowledge base, run 'studio decisions' to read them.
```

(singular: `studio: 1 page awaits a decision in this knowledge base, run 'studio decisions' to
read them.`). Otherwise (no pages awaiting a decision, or the command fails) the hook prints
nothing. The hook never non-zero-exits. A failure never tears down the session.

## /knowledge-base:init flow

The `/knowledge-base:init` skill (in `plugin/skills/engram-init/`) guides first-time knowledge base setup:

1. Interviews the user about the knowledge base's domain, page types, and vocabulary.
2. Writes `knowledge-base-schema.json` with the agreed schema.
3. Calls `studio init scaffold`, then `studio init db`, then `studio init spec` in sequence.

After a successful `/knowledge-base:init`, the SessionStart nudge is silent and the MCP tools
are live. See [init](init.md) for command-level reference.

## Plugin file layout

```
engram/plugin/
  .codex-plugin/plugin.json    - Codex manifest, skills, and MCP launch configuration
  .claude-plugin/plugin.json   — plugin manifest (name, version, mcpServers)
  bin/studio                   — wrapper: resolves STUDIO_BIN or PATH
  commands/init.md             — /knowledge-base:init command stub
  commands/design-note-writing.md    /knowledge-base:design-note-writing command stub
  hooks/hooks.json             — SessionStart hook declaration
  hooks/nudge.sh               — init-nudge script (mode 0755)
  skills/engram-init/          — /knowledge-base:init interview + spec-write skill
  skills/designing-note-writing/     /knowledge-base:design-note-writing skill
  skills/documenting/          — /knowledge-base:documenting note-writing skill
  skills/searching-the-knowledge-base/   — /knowledge-base:searching-the-knowledge-base skill
  README.md                    — installation guide
```
