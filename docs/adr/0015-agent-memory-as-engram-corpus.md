---
id: adr-0015-agent-memory-as-engram-corpus
type: adr
title: Agent memory is an engram corpus behind the vendor Memory capability
summary: The studio agent's semantic memory is an engram corpus behind the vendored pydantic-ai-harness Memory capability. Engram ships a MemoryStore adapter over its own doors, harness mounts global, project,
status: proposed
aliases:
- adr-0015-agent-memory-as-engram-corpus
app: root
absorbs:
- '[[spec-2026-08-24-agent-memory-on-engram-design]]'
created: '2026-08-29'
---

## Context

The studio agent has verbatim episodic memory (the session journal behind the recall door) but no semantic memory: nothing standing that says what the agent believes about this machine or this project. The owner mandated (2026-08-24) giving the agent persistent memory shaped like the vendored `pydantic_ai_harness.memory` capability, with engram underneath instead of files or sqlite.

The conceptual ground the decision rests on: memory is a function in the agent's loop, not a body of documents. Every corpus compiled so far is a library whose pages mirror documents that exist outside the agent. A memory note has no source document in the world. Its provenance is an experience, its content is a distilled belief, and revising it means the agent changed its mind. The thesis: memory is a corpus whose spec opens the write door to the agent and injects its orientation surface. Same shelving as a library corpus, a different thing on the shelves, and the corpusspec is where the role is declared.

Two constraints shaped everything below. First, the vendor capability already fixes the interaction shape: `Memory` injects an index plus sibling listing as user-role context (deliberately never the instruction channel) and mounts `write_memory`, `read_memory`, and `search_memory` over a `MemoryStore` protocol with compare-and-set versions and idempotent operation receipts. The build is an adapter, not a fork. Second, the search of the base found no accepted ADR deciding this. ADR-0013 (agent-authored harness artifacts stay pending until the owner activates them) covers capabilities, leaves, agents, and profiles, but it does not reach memory notes, and this ADR deliberately carves memory out of its per-artifact owner-activation rule. This ADR is the decision of record for memory.

## Decision

The decisions below absorb the design of 2026-08-24 (initiative folder `agent-memory-on-engram`, the design, plan, and brainstorming docs that share it).

**D1. Two standing memory mounts, global switchable.** A global memory corpus for cross-project beliefs (the machine, local ports, shared tooling) and a per-project corpus, each its own `Memory` instance with a distinct heading. A third mount, the scratchpad, holds per-session working notes in a session-id namespace over a throwaway store. The global mount can be switched off per project or everywhere via config, riding the `Switched` door and `project_config` machinery. The `memory` door sits beside, never replaces, `recall`: recall answers what happened in past turns, memory answers what I believe.

**D2. Write-through capture.** Every `write_memory` ingests, compiles, and embeds that one note synchronously, so read-your-writes and the protocol's compare-and-set versions hold naturally. Capture and compounding are different verbs. Consolidation is separate and scheduled (D7).

**D3. The injected index is derived.** The adapter answers `read({scope}/MEMORY.md)` with engram's compiled orientation surface, one line per note. Nobody hand-writes the map, so the map cannot drift from the pages. Writes to the index path are rejected with an error that names the right move: write a note under a topic path instead. The review of the plan confirmed this needs harness-side toolset work too: the stock toolset cannot express `{category}/{slug}.md` paths, so the mount ships a toolset adaptation that takes an explicit category and slug and turns grammar refusals into `ModelRetry` so refusals teach instead of abort.

**D4. Live writes, lint reviews.** Agent writes land immediately. Standing corpus lint is the reviewer (a dangling link is a memory worth writing next). The owner inspects and edits on demand through the dashboard and never blesses per note, because per-note blessing does not scale. Deletes are soft: supersession keeps the history, so nothing is silently lost.

**D5. The three voices stay separate.** Friction flags never promote to memory. They are work items other agents resolve, and nothing standing remains once fixed. Owner margin notes promote once, explicitly, source-stamped: the resulting note's frontmatter records its source kind (`owner-note | session | dream`).

**D6. Preferences are not memory.** Prescriptive owner rules go to preferences, which are owner-authored, always preloaded to the instruction channel (a generic user-preferences entry and a per-project entry beside `project_config`). Descriptive beliefs go to memory, which stays agent-authored in user-role context. The channels never blur: "wrap markdown at 140" is a preference, "the studio API answers on 8100" is a memory.

**D7. Dreaming is a scheduled workflow, not a subsystem.** A harness workflow on the scheduler reads the session journal since the last dream plus the memory corpus and consolidates: merge fragmented notes, dedup, prune what proved wrong, promote what margins and sessions taught. Output is ordinary memory writes through the same adapter, each carrying supersession provenance. The shape follows Letta's sleep-time agents (arXiv:2504.13171) and OpenClaw's dreaming.

**D8. Engram ships the adapter, harness mounts it.** `EngramMemoryStore` implements the vendor `MemoryStore` protocol (and the `SearchableMemoryStore` extension) over a dedicated memory collection through engram's own ingest and read doors, so engram owns it as part of its corpus surface. Harness owns everything agent-side: the mounts, the `memory` door, the config, the preferences wiring, and the dream workflow. Engram already depends on the vendored `pydantic-ai-harness`, so no new dependency edge.

## Consequences

Easier: the agent gains standing semantic memory without forking the vendor capability, every engram corpus gets a write-door pattern it can reuse, and consolidation (dreaming) has a clean seam because capture and compounding never shared a code path.

Harder: the corpus surface now has a writer that is not a human. The corpusspec, the lint rules, and the soft-delete machinery must stay trustworthy because agent-written notes are only reviewed by lint, not by a person per note. The adapter must honor protocol semantics the happy path does not exercise: compare-and-set conflicts, receipt replays, and bounded search truncation are the integration test's contract, not storage of text. Grammar refusals must teach (translate to `ModelRetry`) rather than abort the run.

To honor this decision the repo must keep: the index path write-refusal in the adapter, the source-stamping rule on promoted notes, the channel split between preferences (instruction) and memory (user-role context), the derived index over any hand-written one, and the boundaries declared out of scope: the auto retrieval ladder (its own brainstorming doc), dreams that write skills, multi-agent memory, and any memory-health dashboard beyond what corpus views already show. The dream workflow and the retrieval-ladder upgrade are follow-up plans, not parts of this decision.

## Options considered

**Engram adapter versus files or sqlite.** The owner mandate named engram as the substrate. Files and sqlite would work for the vendor protocol's letter, but engram's note grammar (one fact per page, typed frontmatter, `[[wikilinks]]`, supersession) is exactly the shape beliefs should take, and the corpus machinery (compile, orientation surface, retrieval, dashboard) comes for free. Won on fit, not on protocol compliance.

**Batched compile on a cadence versus write-through.** The scheduler could own a batch compile. Rejected: compare-and-set versions and read-your-writes fall apart when the store lags the write, and the scheduler's real job is consolidation, not capture.

**Candidate store, then owner adoption, versus live mutation.** Letta and OpenClaw offer a variant where consolidated candidates wait for blessing. Rejected: it reimports the per-note blessing that does not scale, and lint plus supersession are the safety net that costs nothing per note.

**Preferences on the memory capability versus a separate channel.** Building preferences as another `Memory` mount would have been less code. Rejected: preferences are prescriptive and belong to the instruction channel, and model-written content must not speak with instruction authority. The split is the whole point of D6.

**One shared collection per agent identity versus global plus per-project.** One shared collection would simplify the adapter. Rejected: cross-project beliefs and project beliefs have different lifetimes and different switch rules, and the global switch was the owner's explicit ask.

**Hand-maintained MEMORY.md versus a derived index.** The vendor stock toolset appends bullets to MEMORY.md. Rejected: a hand-written map drifts from the pages by construction. The derived orientation surface makes drift impossible, at the cost of overriding the toolset's defaults (D3).
