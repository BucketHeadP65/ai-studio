---
id: harness-agent-memory
aliases: ["harness-agent-memory"]
type: reference
app: harness
status: current
tags: [memory, capability, harness, agent, engram, preferences]
title: "agent-memory, the plugin, the three mounts, and the engram knowledge base underneath"
---

# agent-memory, the plugin, the three mounts, and the engram knowledge base underneath

The `memory` plugin (`harness/src/harness/plugins.py`, on by default) mounts three standing-memory scopes as one
`CombinedCapability` behind a single `Switched(..., plugin="memory")` (`composition.py`). `Switched.id` is
`f"plugin-{plugin}"`, so
three separate wrappers on the same plugin name would mint the same capability id three times and `Agent.__init__` refuses
that as a duplicate. Each scope is a `NoteMemory` capability (`harness/src/harness/features/projects/memory.py`), an
adaptation of pydantic-ai-harness's `pydantic_ai_harness.memory.Memory` whose stock `MemoryToolset` cannot address this store's paths.
It normalizes every filename through a flat, dot-only pattern that rejects slashes and defaults an omitted filename to
`MEMORY.md`, the derived index this store refuses to accept as a write target. This page is the contract.

## The three mounts

| Mount | Tool prefix | Namespace | Gate |
|---|---|---|---|
| Global | `global_` | `global` | the plugin, and `ProjectConfig.memory.global_memory` (`.aifactory/config.json`), read fresh per turn |
| Project | `project_` | `project-<sanitized project id>` | the plugin only |
| Scratchpad | `scratchpad_` | `session-<sanitized session id>` (`session-main` when unset) | the plugin only |

Global and project share one backing `EngramMemoryStore`. The scratchpad gets its own fresh, in-process `InMemoryStore`.
Every mount registers the same four tools under its own prefix (`global_write_memory`, `project_read_memory`,
`scratchpad_search_memory`, and so on), so three mounts on one agent never collide on a bare tool name, and every guidance
line or `ModelRetry` message names the exact tool the model can call. The global mount is the only one gated per turn: a
`DynamicCapability` resolves to nothing on a turn where `global_memory_enabled()` reads false, so a project can opt out of
world-wide facts without touching the plugin.

## The path and category grammar

A note lives at `{namespace}/{agent}/{category}/{slug}.md` (`agent` is always `main`, the vendor `Memory.agent_name` default,
unset by every mount here). `category` is one of `world`, `project`, or `reference`, the note's type, never which mount holds
it. A model names a note either with `category` + `slug`, or with the relative `<category>/<slug>.md` path a prior listing or
`search_memory` already returned, never both. Path segments may not contain a literal dot: dots are reserved as the page-id
separator (`engram.agent_memory.contracts.page_id_of`/`engram.agent_memory.domain.path_of`, slashes to dots under a `mem.` prefix), and a dotted segment would
round-trip to a different path. Every grammar violation (an unknown category, a dotted segment, a missing note, ambiguous
`old_text`, an over-size note) raises a store-side `ValueError` that the toolset re-raises as `ModelRetry`: a teaching retry
naming the fix, never a bare abort.

## The derived index

`MEMORY.md` is never stored. It is assembled on every read from the scope's live notes and refuses every write
(`EngramMemoryStore.write`/`delete` raise before either inner store or projection is touched). The global and project mounts
render it as the store's own scope-relative listing. The scratchpad's `InMemoryStore` derives no index of its own, so
`_NoteIndexStore` wraps it to strip the scope prefix from an inner-rendered index, or derive a plain listing from
`list_paths` when there is none, one map, never three spellings of it.

## Delete is retire, not erase

`delete_memory` removes the note from the authority store and re-projects it into the knowledge base with `status: retired`
rather than removing the page outright. The note stops answering `read_memory`/`search_memory` and drops out of the index,
but its history is not destroyed. A projection failure after the authoritative write raises, and every mutation is keyed by
an idempotent operation receipt (`tool_call_id` + `run_id`), so a retried write or delete replays the stored result instead
of double-applying.

## Preferences: standing voice, not memory

`Preferences` (`harness/src/harness/features/projects/preferences.py`) is mounted always-on beside `NoteOverlay`, not behind a
plugin: two files, `~/.aifactory/preferences.md` (global) and `<project>/.aifactory/preferences.md`, read fresh on every
request and rendered into the instruction channel under `## Owner preferences`. It is not memory because it answers a
different question. Memory is what the agent believes, agent-authored, riding user-role context. Preferences are what the
owner wants, owner-authored, prescriptive, and never written by the agent.

## The engram knowledge base underneath

Every note write lands in two places in one call: the vendor `PostgresMemoryStore` (migration 0031, `public.agent_memory*`)
owns the bytes, the compare-and-set versions, and the operation receipts. `EngramMemoryStore`
(`engram/src/engram/agent_memory/`) projects the same note into the `agent-memory` knowledge base as an indexed, embedded
page through `embedded_authoring`, the same validated write path `studio ingest` runs for every other knowledge base.
`embedded_memory_store` (`_factory.py`) opens both over one writer role and pins `search_path=public` on every connection,
since the vendor store's own DDL and queries are unqualified.

## Two caveats

- **The scratchpad is process-local.** Its `InMemoryStore` lives for as long as the server process does, not strictly for
  the session. A restart loses every scratchpad note regardless of whether the session that wrote it is still open.
- **The memory knowledge base is fixed-mode today.** `agent-memory.json`'s `retrieval` block declares no `mode`, so it
  defaults to `KnowledgeBaseSchema`'s `"fixed"`: hybrid search is always on when this deployment wires an `embed_query`
  (`knowledge_base_embedder()`), and falls back to the inner store's lexical search otherwise. Retrieval mode becoming a
  per-knowledge-base, declared choice is the auto-retrieval-mode build's concern, not this one's.
