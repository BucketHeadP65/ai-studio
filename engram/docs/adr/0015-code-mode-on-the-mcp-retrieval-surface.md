---
id: engram-adr-0015-code-mode-on-the-mcp-retrieval-surface
aliases: ["engram-adr-0015-code-mode-on-the-mcp-retrieval-surface"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0015 — Code mode as an opt-in `run_code` tool on the MCP retrieval surface"
---

# ADR-0015 — Code mode as an opt-in `run_code` tool on the MCP retrieval surface

**Status:** Accepted (2026-06-23). Adds a `run_code(code: str)` tool to engram's FastMCP
stdio retrieval server (`engram mcp`): the external MCP client's model writes Python that
the engram server executes in a [Monty](https://pydantic.dev/docs/ai/harness/code-mode/)
sandbox where engram's retrieval functions are bound as callables, collapsing N sequential
tool round-trips into one. Builds on the single-source toolset ([ADR-0011](0011-single-source-retrieval-toolset.md))
and reuses the same `pydantic-ai-harness` code-mode engine the eval axis added
([ADR-0012](0012-retrieval-strategy-and-code-mode-axes.md)). Scope: `features/serve/tools.py`
(a `RUN_CODE` name constant), `features/evals/strategy.py` (a new `code-mode` profile), and
`features/serve/mcp_server.py` (the tool + the standalone sandbox driver). The CQRS handlers,
the `api` SQL surface, the `wiki_reader` role, and the eval code-mode path are unchanged.

**Context.** An MCP client doing real retrieval work climbs a ladder — search, fetch a hit,
expand the graph, fetch another, run a scoping SQL — each step a separate model turn and MCP
round-trip. [Code mode](https://pydantic.dev/docs/ai/harness/code-mode/) is the
`pydantic-ai-harness` capability that collapses a toolset into a single `run_code` tool the
model drives by writing Python (loops, conditionals, `asyncio.gather` over the tools in one
turn), trading round-trips for a sandbox. engram already depends on it for the eval axis
(`build_agent(..., code_mode=True)`), but that wiring is *inside* an in-process `Agent`. The
ask here is to expose the same capability on the **external MCP surface**, so a client like
Claude Code can orchestrate engram's retrieval in one call — while engram itself **stays
LLM-free**: the client's model writes the code; engram only sandboxes and runs it.

Two product constraints framed the design: (1) it must be **opt-in** — `run_code` must not
appear on the default surface; only a server configured with a profile that names it exposes
it; (2) **all** retrieval tools — including `run_sql` — are callable inside the sandbox, so a
program can mix the structured tools with raw read-only SQL in one turn.

**Options considered.**

- **Always-on `run_code`.** Rejected: code execution on the serve surface is a capability a
  deployment should *choose*, not inherit. The existing retrieval-profile gate
  ([ADR-0012](0012-retrieval-strategy-and-code-mode-axes.md)) is exactly the opt-in seam.
- **A new `Agent` inside `engram mcp` to host `CodeMode`.** Rejected: it would put a model in
  engram's read path — the opposite of the LLM-free design — and re-introduce the agent/MCP
  coupling [ADR-0011](0011-single-source-retrieval-toolset.md) avoids. The client owns the model.
- **Bind the retrieval functions into a Monty sandbox directly (bypass `CodeModeToolset`).**
  Rejected: it would re-implement the dispatch / serialization / sequential-vs-parallel /
  error-mapping logic `CodeModeToolset` already owns — guaranteed drift from the eval
  code-mode path and a large maintenance surface for no benefit.
- **Reuse `CodeModeToolset` standalone, gated by a new `code-mode` profile (chosen).**

**Decision.**

- **`run_code` is gated by an opt-in `code-mode` retrieval profile.** A new
  `RetrievalStrategy.CODE_MODE = "code-mode"` resolves to `RetrievalProfile(tools=_ALL_TOOLS |
  {RUN_CODE}, search_hybrid=True)` — **additive** over `full`: the client gets the structured
  tools top-level *and* `run_code`. `RUN_CODE = "run_code"` is a name constant in
  `features/serve/tools.py` (one source, like `SEARCH`/`FETCH`), deliberately **not** in
  `TOOL_NAMES` (the closed set the retrieval-toolset subset filters iterate). The default
  `full` profile stays `run_code`-free, so the opt-in is preserved; `APP_RETRIEVAL__RETRIEVAL_PROFILE=code-mode`
  turns it on, flowing through the existing `only=profile.tools` wiring with no CLI change.
- **The sandbox binds the whole read-only surface.** `create_mcp_server` builds a deps-agnostic
  `FunctionToolset` of the four structured tools (via the single-source `build_retrieval_toolset`,
  so the sandbox runs the *same* helpers the structured MCP tools and the eval harness do) **plus**
  `run_sql` and `describe_schema`. Those two are now produced by factory functions
  (`_make_run_sql` / `_make_describe_schema`) so the *same* callables back both the top-level
  tools and the sandbox — the D-20 row cap, the `vector_query => '<text>'` embed shim, and the
  actionable read-only error message are defined exactly once and cannot drift.
- **`CodeModeToolset` is driven standalone, outside an `Agent`.** `run_code`'s FastMCP closure
  wraps the bound toolset in a `pydantic_ai_harness.code_mode.CodeModeToolset` (the same engine
  the eval path uses) and invokes its `run_code` tool directly: build a per-call `RunContext`
  (each call gets isolated REPL state via `for_run`), set its `tool_manager` (the only field
  `call_tool` reads), resolve the tool from `get_tools`, and call it. The harness + Monty are
  **lazy-imported** only on this path (the sanctioned `# noqa: PLC0415` pattern, mirroring
  `features/evals/harness.py`). The result is a `ToolReturn` whose `return_value` is the last
  expression's value (already serialized to plain JSON-shaped data by the sandbox); the closure
  dumps it to a JSON string to match the other MCP tools' wire shape. A sandbox error (syntax /
  type / runtime / tool failure) surfaces as a `ModelRetry` whose rendered message is returned as
  the tool result — an actionable error, never a torn-down session (the same defensive contract
  `run_sql` and `fetch` keep).
- **The `RunContext` needs a `model`, but engram never invokes one.** A module-private
  `_NoModel(Model)` sentinel satisfies the type contract without an ignore; its `request` raises
  to assert the code-mode path is genuinely LLM-free (reaching it would be a wiring bug).

**Consequences.**

- (+) **One round-trip retrieval for capable clients.** A client model can search, fetch,
  expand the graph, and run a scoping SQL in a single `run_code` call instead of a turn each —
  fewer round-trips, less context churn, `asyncio.gather` for concurrent reads.
- (+) **No drift, three ways.** The sandboxed structured tools are the single-source helpers
  ([ADR-0011](0011-single-source-retrieval-toolset.md)); the sandboxed `run_sql` / `describe_schema`
  are the *same* callables as the top-level tools (shared factories); and the code-mode engine is
  the *same* `CodeModeToolset` the eval axis uses. A change to any of them lands everywhere at once.
- (+) **Opt-in and backward-compatible.** Every existing profile (incl. the `full` default) is
  `run_code`-free and unchanged; the four-tool gating, the eval code-mode toggle, and existing
  tests are untouched.
- **Safety posture.** Code execution on a read surface is real surface area; three mitigations
  bound it. (1) `engram mcp` is **local-only** stdio — the same trust boundary as the existing
  `run_sql` tool; it is not a network endpoint. (2) The sandbox is **Monty** — a restricted Python
  subset with no host filesystem / environment / clock unless explicitly granted (engram grants
  none). (3) The sandboxed `run_sql` is the *same* bounded surface as the direct tool: every
  statement still runs through the read-only `wiki_reader` DB role and the 50-row cap, so binding
  it adds **no DB risk** beyond what the always-on `run_sql` tool already exposes. The capability
  is, by design, off unless a deployment sets the `code-mode` profile.
- (−) **A return-shape ergonomics note for clients.** Tool results inside the sandbox are plain
  JSON-shaped data (dicts/lists), not the typed result models — so a program indexes a search hit
  as `hit['id']`, not `hit.id`. The `run_code` description states this and shows the `gather` idiom;
  it is the standard `pydantic-ai-harness` contract, not engram-specific.
- (−) **The heavy code-mode dependency is now reachable from the serve path too** (previously only
  the eval path). It remains a lazy import behind the opt-in profile, but it is real runtime surface
  whenever `code-mode` is configured.
- **Reversible?** Yes — additively. The `code-mode` profile entry, the `RUN_CODE` constant, and the
  `_add_run_code` / `_run_code` / `_build_sandbox_toolset` block are removable independently; the
  `run_sql` / `describe_schema` factory extraction is a harmless refactor that stands on its own.
  Reverting leaves the structured surface ([ADR-0011](0011-single-source-retrieval-toolset.md)) and
  the eval code-mode axis ([ADR-0012](0012-retrieval-strategy-and-code-mode-axes.md)) intact.

**Related.**
[ADR-0011](0011-single-source-retrieval-toolset.md) (the single-source retrieval helpers the
sandbox binds), [ADR-0012](0012-retrieval-strategy-and-code-mode-axes.md) (the retrieval-profile
gate this reuses + the eval-side code-mode axis that shares the `CodeModeToolset` engine),
[ADR-0008](0008-configuration-via-application-settings-json.md) (how `APP_RETRIEVAL__RETRIEVAL_PROFILE`
is resolved). Code: `features/serve/tools.py` (the `RUN_CODE` constant), `features/evals/strategy.py`
(the `code-mode` profile), `features/serve/mcp_server.py` (`_add_run_code`, `_build_sandbox_toolset`,
the standalone `_run_code` driver, the `_NoModel` sentinel, and the `run_sql` / `describe_schema`
factories). External: [pydantic-ai code mode](https://pydantic.dev/docs/ai/harness/code-mode/),
[Monty](https://pydantic.dev/docs/ai/harness/code-mode/). Patterns:
[Strategy registry](../../../docs/explanation/design-patterns.md#strategy-registry)
(the `RetrievalStrategy` → `RetrievalProfile` registry the `code-mode` entry extends).
