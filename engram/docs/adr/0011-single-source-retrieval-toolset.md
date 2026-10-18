---
id: engram-adr-0011-single-source-retrieval-toolset
aliases: ["engram-adr-0011-single-source-retrieval-toolset"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0011 — A single-source retrieval toolset for the eval harness and MCP"
---

# ADR-0011 — A single-source retrieval toolset for the eval harness and MCP

**Status:** Accepted (2026-06-19). Implements decision **D1** (one retrieval
toolset, two surfaces) of the `evals-and-eval-driven-self-optimization` plan.
Scope: the retrieval tool *functions* (`search` / `fetch` / `graph_expand` /
`find_paths`) are defined once in `features/serve/tools.py` and registered into
both the eval harness's pydantic-ai `FunctionToolset` and the FastMCP server. The
underlying CQRS query handlers and the `api` SQL surface are unchanged.

**Context.** The evals ([ADR-0010](0010-claude-code-event-bridge-model.md)) are
only trustworthy if the agent-under-test exercises the **same** retrieval surface
real clients use. Real clients reach retrieval over the FastMCP stdio server
(`features/serve/mcp_server.py`); the eval harness reaches it as a pydantic-ai
`FunctionToolset` bound to an in-process `Agent`. pydantic-ai tools are **not**
exposed as MCP automatically and MCP tools are not pydantic-ai tools, so the naive
design is two parallel definitions of the same four tools — one for the harness,
one for MCP — which will drift: a fix or a description tweak applied to one and
forgotten on the other silently makes the eval measure something the real surface
no longer does. "We evaluated the agent and it's great" then says nothing about the
MCP clients in production.

The retrieval *logic* already exists once: the serve slice's CQRS query handlers
(`SearchHandler` / `FetchHandler` / `GraphExpandHandler` / `GraphPathsHandler`)
wrap the `api.*` gateway. The only thing duplicated by a naive design is the *tool
wrapper* around those handlers — the part that names the tool, documents its args
for the model, and projects the handler's rows into a typed result. So the fix is
to define that wrapper exactly once and register it into both surfaces.

**Options considered.**

- **Two independent tool definitions (harness vs MCP).** Rejected: guaranteed
  drift; the eval stops being evidence about the real surface the moment one side
  changes and the other does not.
- **Generate the MCP tools from the pydantic-ai toolset (or vice versa) at
  runtime.** Rejected: pydantic-ai and FastMCP have no such adapter, and a bespoke
  reflection bridge is more machinery (and more failure surface) than a shared
  function is worth.
- **Make the harness call the MCP server as a client.** Rejected: it would re-add
  a transport hop, MCP's per-turn limits, and JSON-string round-tripping inside the
  eval loop — measuring the MCP *client path* rather than the in-process tools, and
  re-opening exactly the coupling [ADR-0010](0010-claude-code-event-bridge-model.md)
  works to avoid.
- **One source of truth, two thin registration adapters (chosen).** Define each
  tool's logic once as a module-private core helper; register those helpers into a
  `FunctionToolset` and into FastMCP through two thin adapters that differ only in
  their I/O envelope (typed models for pydantic-ai; JSON strings for MCP).

**Decision.**

- **One source of truth per tool.** The retrieval logic lives in the
  module-private helpers `_search` / `_fetch` / `_graph_expand` / `_find_paths` in
  `features/serve/tools.py`. Each builds the frozen CQRS query message, delegates to
  the injected handler (it does **not** re-implement retrieval), and projects the
  handler's rows into the canonical `serve.domain` shape (`list[SearchHit]` /
  `FetchResult` / `GraphResult` / `list[GraphPath]`). The tool descriptions — the
  model's orientation — are module constants shared by both surfaces.
- **Two thin registration adapters over those helpers.** `build_retrieval_toolset`
  registers them as a pydantic-ai `FunctionToolset` (the closures return the typed
  models; pydantic-ai serialises them; `fetch` raises `ModelRetry` on an unknown id
  so the agent re-grounds). `register_mcp_tools` registers the **same** helpers on a
  FastMCP server (the closures dump the same models to JSON, matching the MCP wire
  shape; an unknown `fetch` id returns an actionable `{"error": …}` object). The
  adapters differ only in envelope.
- **A tight DI seam.** The helpers depend on a `RetrievalHandlers` bundle — a
  frozen dataclass wrapping four single-method `Protocol` ports
  (`SupportsSearch` / `SupportsFetch` / `SupportsGraphExpand` / `SupportsFindPaths`),
  one per handler (Interface Segregation, CLAUDE.md). The concrete serve handlers
  satisfy them structurally (each keyed by its `__call__`), so there is no adapter
  class; a test passes a `FakeRetrievalHandlers` of scriptable stubs without a
  database. The bundle is constructed once — by the eval slice's wireup providers
  (`features/evals/deps.py`: `retrieval_handlers_provider` reuses the *same*
  singleton serve handlers the dashboard/MCP use, `retrieval_toolset_provider`
  builds the toolset) — and both registration paths close over it.
- **The harness binds the shared toolset.** `features/evals/harness.py`'s
  `build_agent` assembles the agent-under-test as *(model × instructions × the
  single-source toolset)* returning a structured `RetrievalAnswer`, built
  instrumented so every tool call / token / latency lands in the span tree the
  evaluators score. Because the toolset is the same one the MCP surface exposes,
  **evaluating this in-process agent is evaluating the real MCP retrieval surface.**
- **Graph tools: shared `search`/`fetch`, server-owned graph tools (first-wins).**
  `register_mcp_tools` also defines `graph_expand` / `find_paths`, but
  `mcp_server.py` already registers **richer** graph tools (with `rel_types` / `k` /
  `max_hops` arg surfaces) *first*, and FastMCP registration is **first-wins**, so
  the rich server versions survive and only the shared `search` / `fetch` are added.
  The single-source pair is `search`/`fetch`; the graph tools differ by design and
  the richer ones win on the MCP surface (the reconciliation is gated on the graph
  handlers being present, so the thinner single-source graph closures are always
  deduped, never left live behind a smaller signature).

**Consequences.**

- (+) **No harness/MCP drift.** There is exactly one definition of each shared
  tool's logic and description; the eval is, by construction, evidence about the
  surface real clients use.
- (+) **Adding or changing a tool is a one-place change.** A new shared retrieval
  tool is one helper plus two one-line registrations; a description or
  arg-surface tweak lands in both surfaces at once.
- (+) **A clean DI seam.** The `RetrievalHandlers` bundle of single-method ports
  makes the tools trivially testable (scriptable fakes, no DB) and is constructed
  once from the composition root / eval `deps`, reusing the serve slice's singleton
  handlers rather than a parallel set.
- (−) **The first-wins graph reconciliation is a subtlety to hold in mind.** The
  MCP server intentionally keeps its richer `graph_expand` / `find_paths` and
  suppresses the duplicate-registration warning for the two superseded single-source
  graph closures; that is deliberate, not a wiring bug, and is gated on the graph
  handlers being present so the dedup is guaranteed.
- (−) **A small registration-order contract.** The reconciliation relies on FastMCP
  being first-wins and on the rich graph tools being registered before the
  single-source set — a property pinned by tests rather than by the type system.
- **Reversible?** Yes — additively. The shared toolset is one module plus two
  registration functions and the eval `deps`/`harness` that consume it; reverting
  means inlining the tool wrappers back into each surface (re-accepting the drift
  risk). The serve handlers, the `api` SQL surface, and the MCP server's own graph
  tools are unaffected.

**Related.**
Code:
`features/serve/tools.py` (the single source), `features/serve/mcp_server.py` (the
MCP registration + the first-wins graph reconciliation),
`features/evals/{harness,deps}.py` (the harness binding + the wireup providers).
Patterns:
[Strategy registry](../../../docs/explanation/design-patterns.md#strategy-registry)
(the frozen `RetrievalHandlers` bundle of ports),
[DI seams / fakes over mocks](../../../docs/explanation/design-patterns.md#di-seams-and-fakes-over-mocks)
(the single-method handler Protocols),
[CQRS message bus](../../../docs/explanation/design-patterns.md#cqrs-message-bus)
(the query handlers the helpers wrap).
