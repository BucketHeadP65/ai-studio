---
id: engram-adr-0012-retrieval-strategy-and-code-mode-axes
aliases: ["engram-adr-0012-retrieval-strategy-and-code-mode-axes"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0012 — Retrieval strategy and code mode as per-run eval experiment axes"
summary: 'Keep retrieval strategy and code mode explicit experiment axes with toolsets whose discovery and evidence-verification dependencies are viable.'
---

# ADR-0012 — Retrieval strategy and code mode as per-run eval experiment axes

**Status:** Accepted (2026-06-19). Extends the `evals-and-eval-driven-self-optimization`
work with two new per-run experiment axes on top of the single-source toolset
([ADR-0011](0011-single-source-retrieval-toolset.md)): a **retrieval strategy** (which
retrieval tools the agent-under-test is given, and in which search leg) and **code mode**
(whether the agent orchestrates the tools by writing sandboxed Python). Scope: the toolset
*builder* gains a tool-subset + forced-search-leg seam; a new `features/evals/strategy.py`
owns the named strategies; `run_eval` builds the toolset per run; `build_agent` gains an
optional `CodeMode` capability. The CQRS handlers, the `api` SQL surface, and the metrics
are unchanged.

**Context.** The whole point of the eval loop is to learn *which retrieval the corpus and a
given model actually need*. The full suite — hybrid search + fetch + graph traversal — is not
free: more tools is more token cost, more latency, and more room for a cheaper model to get
distracted, and a client's constraints (cost ceiling, no graph backend) may rule a leg out
entirely. To answer "is the graph leg earning its keep on this dataset?" or "does keyword-only
retrieval suffice for this model?" the harness must be able to **hold the retrieval surface
constant as a controlled variable** and vary it across runs — a factorial axis, recorded so runs
are comparable. Two things blocked that:

1. The toolset was built **once** as an all-tools wireup singleton (`retrieval_toolset_provider`)
   and resolved by `run_eval`; there was no way to expose a subset, and the `search` tool let the
   *model* choose `bm25_only` per call — so the search leg was not a controllable variable.
2. There was no toggle for [code mode](https://pydantic.dev/docs/ai/harness/code-mode/) — the
   `pydantic-ai-harness` capability that collapses the tools into a single `run_code` tool the model
   drives by writing Python (loops / `asyncio.gather` over the tools in one turn), trading model
   round-trips for a sandbox. Whether that helps is exactly an empirical question the evals exist to
   answer, so it belongs as an axis, not a global default.

**Options considered.**

- **Leave the surface fixed; compare whole separate codebases/configs.** Rejected: makes the most
  interesting question (tool loadout per dataset/model) unanswerable without forking the wiring.
- **Let the model keep choosing the search leg / which tools to call.** Rejected: that measures the
  model's tool *judgement*, not the *strategy* — you can't attribute a recall delta to "keyword vs
  hybrid" if the model silently mixed them. A controlled axis must force the leg.
- **Add a pure vector-only leg now.** Deferred: the search SQL has only `hybrid` (vector+BM25) and
  `bm25_only` (keyword) legs — a true vector-only leg is net-new SQL across both dialects
  (paradedb + pg_textsearch). The achievable-now set ships without it; vector-only maps to
  `search-hybrid` until that SQL exists.
- **Make code mode a global capability.** Rejected: it changes the tool surface fundamentally; it
  must be a per-run axis you can compare against a non-code-mode baseline.
- **Per-run strategy + forced legs + optional code-mode, recorded on the variant (chosen).**

**Decision.**

- **The toolset builder takes primitives, not an evals type.** `build_retrieval_toolset(handlers, *,
  only=None, search_hybrid=None)` in `features/serve/tools.py` registers only the named subset
  (`only`, mirroring `register_mcp_tools`) and, when `search_hybrid` is set, registers a
  *forced-leg* `search` tool that drops the agent-chosen `bm25_only` arg and runs hybrid
  (`True`) or BM25-only (`False`). `search_hybrid=None` keeps the legacy agent-controlled `search`,
  so existing callers (the default singleton, the MCP path, the unit tests) are unchanged. The tool
  names are the canonical `SEARCH`/`FETCH`/`GRAPH_EXPAND`/`FIND_PATHS` constants exported from the
  same module — one source of truth. `serve.tools` stays **evals-agnostic** (it only knows tool
  names + a hybrid flag).
- **The evals slice owns the named strategies.** `features/evals/strategy.py` defines a
  `RetrievalStrategy` `StrEnum` and a `_STRATEGIES` registry mapping each to a frozen
  `RetrievalProfile(tools, search_hybrid)` — the project's closed-set dispatch shape. The
  achievable-now set: `full` (all tools, hybrid — the default), `search-hybrid` (search+fetch,
  hybrid), `search-keyword` (search+fetch, BM25), `graph-only` (graph tools plus fetch, no search —
  a traversal an agent can also read), and `keyword-graph` (all tools, BM25). `resolve_strategy(name)`
  returns the profile (`None` → the default `full`); an unknown name raises `ValueError` (the
  HTTP/CLI surfaces constrain it to the registry-owned `STRATEGY_NAMES`). Strategies are
  **code-owned, not config** — a name outside the registry would break, so the options endpoint
  serves them directly.
- **`run_eval` builds the toolset per run.** It now resolves the `RetrievalHandlers` bundle (not the
  all-tools singleton) and calls `build_retrieval_toolset(handlers, only=profile.tools,
  search_hybrid=profile.search_hybrid)`, so each run gets exactly its strategy's surface.
- **Code mode is a lazy, opt-in capability.** `build_agent(..., code_mode=False)` adds a
  `pydantic_ai_harness.CodeMode()` capability alongside the existing `Instrumentation()` when set.
  The package + its Monty sandbox (`pydantic-ai-harness[code-mode]`) are declared in
  `pyproject.toml` and imported **lazily** only on the code-mode path.
- **A parameter object keeps the call sites tidy.** The growing set of per-run axes (model,
  instructions, bridge model, retrieval strategy, code mode) is bundled into a frozen
  `EvalRunConfig` (`runner.py`); `run_eval(container, *, config, dataset_path)` and its two callers
  (the CLI, the HTTP job) pass it, staying under the project's arg-count limit.
- **Both axes are recorded on the run's `variant`.** `_run_variant` folds `retrieval_strategy` (when
  set) and `code_mode` (when on) into the variant, so runs are comparable across strategies/code-mode
  in the history and Logfire. The dashboard run form (always-shown strategy dropdown + code-mode
  toggle) and the CLI (`--retrieval-strategy`, `--code-mode`) drive them; the options endpoint serves
  `retrieval_strategies`.

**Consequences.**

- (+) **The core question is answerable.** A dataset/model can be scored under each strategy and
  compared directly — "graph-only loses 20% recall here", "keyword suffices for the cheap model" —
  with the surface held as a controlled variable.
- (+) **Scoring survives both axes (verified).** Recall@k / precision@k / MRR score off the agent's
  structured `cited_source_ids` (output), so they are strategy- and code-mode-agnostic. Only
  `n_tool_calls` reads the span tree; the harness runs sandboxed inner calls through a `ToolManager`
  over the real toolset inheriting the agent's instrumentation, so each inner call **and** the
  `run_code` wrapper emit `gen_ai.operation.name="execute_tool"` spans — a code-mode run's
  `n_tool_calls` therefore reads ≈ +1 (the wrapper) vs the same calls made directly. Documented so
  cross-mode comparisons account for it; not a bug.
- (+) **Additive and backward-compatible.** A bare run (`retrieval_strategy=None`, `code_mode=False`)
  behaves as before; the default singleton, the MCP path, and existing tests are untouched.
- (−) **A new heavy dependency.** Code mode pulls `pydantic-ai-harness` + the Monty sandbox; it is an
  extra and a lazy import, but it is real surface to maintain and a real runtime requirement for
  code-mode runs.
- (−) **No pure vector-only leg yet.** The factorial is incomplete until the vector-only SQL exists;
  `search-hybrid` is the closest available proxy.
- (−) **The all-tools `retrieval_toolset_provider` singleton is no longer used by `run_eval`.** It
  remains as the canonical default-surface DI artifact (still provided and tested); a future cleanup
  may retire it.
- **Reversible?** Yes — additively. The strategy module + the two builder kwargs + the `EvalRunConfig`
  field + the `CodeMode` branch are removable independently; reverting leaves the single-source
  toolset ([ADR-0011](0011-single-source-retrieval-toolset.md)) intact.

**Related.**
[ADR-0011](0011-single-source-retrieval-toolset.md) (the single-source toolset these axes build on).
Code: `features/serve/tools.py` (the subset + forced-leg seam),
`features/evals/strategy.py` (the strategy registry), `features/evals/runner.py` (`EvalRunConfig`,
per-run toolset build), `features/evals/harness.py` (the `CodeMode` branch),
`features/evals/http.py` + `cli.py` (the surfaces). External:
[pydantic-ai code mode](https://pydantic.dev/docs/ai/harness/code-mode/). Patterns:
[Strategy registry](../../../docs/explanation/design-patterns.md#strategy-registry)
(the `RetrievalStrategy` → `RetrievalProfile` registry).
