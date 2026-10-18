---
id: engram-adr-0010-claude-code-event-bridge-model
aliases: ["engram-adr-0010-claude-code-event-bridge-model"]
type: adr
app: engram
status: superseded
tags: []
title: "ADR-0010 — A Claude-Code event bridge as a pydantic-ai model backend"
---

# ADR-0010 — A Claude-Code event bridge as a pydantic-ai model backend

**Status:** Accepted (2026-06-19). Implements decisions **D6** (the bridge model),
**D7** (explicit usage), and **D8** (pluggable transport) of the
`evals-and-eval-driven-self-optimization` plan. Scope: a new app-agnostic
`core.bridge` package plus the engram `evals` worker CLI that drives it; the model
side is wired through `core.ai.create_chat_model`'s `claude-bridge` branch. The
transport *protocol* is model-agnostic, but only the Claude-Code worker is
implemented now; a Postgres-backed transport is designed-for but deferred.

**Status:** Superseded (2026-08-30). The bridge was removed, and no successor decision
replaces it.

**Context.** The eval harness ([ADR-0011](0011-single-source-retrieval-toolset.md))
needs a model backend with three properties at once: **free** (no API credits
burned per eval iteration — a factorial sweep is thousands of model turns),
**parallelisable** (N agents-under-test running concurrently), and **interactive**
(an actual Claude Code session doing the "thinking"), all *without* abandoning
pydantic-ai as the agent runtime — the agent-under-test must stay a real
`pydantic_ai.Agent` so we measure the real retrieval surface, not a bespoke loop.
Two obvious routes fail:

- A metered Azure/OpenAI deployment is none of free/interactive, and the credit
  cost of an eval sweep is the whole problem.
- Exposing engram's retrieval to an interactive Claude session **through MCP**
  hits a per-turn ceiling (~20 tool calls/turn) and — more fundamentally — would
  have the *brain* execute retrieval, so the eval would measure an MCP client's
  behaviour, not engram's in-process retrieval tools. MCP *sampling* (the
  protocol's "ask the client's model" hook) is deprecated, so it is not a path
  either.

The insight that unlocks it: invert the roles. Let **python** drive a real
pydantic-ai agent that executes the *real* retrieval tools, and let an interactive
Claude session act as a **pure brain** that only *decides* the next assistant
action. The only seam that must exist is a pydantic-ai `Model` whose "inference"
is a round-trip to that session. The brain never sees a tool ceiling because it
emits at most one batch of tool *requests* per turn; python executes them.

**Options considered.**

- **Metered API backend only.** Rejected: not free; an eval sweep's token bill is
  the cost this whole feature exists to avoid.
- **Expose retrieval over MCP to an interactive Claude session.** Rejected: the
  ~20-call/turn cap throttles a tool-heavy agent, MCP sampling is deprecated, and
  — decisively — the brain would *execute* retrieval, so the eval would measure the
  wrong surface.
- **A headless Claude (`claude -p` / the Agent SDK) as the backend.** Rejected:
  only an *interactive* session is free; headless invocations draw from a separate
  credit pool, which re-introduces the cost problem.
- **A custom `Model` over a pluggable event bridge (chosen).** A pydantic-ai
  `Model` whose `request()` enqueues a *model-request* event and awaits a
  *model-response* event; the brain is an interactive Claude Code session that
  returns only the next action as validated JSON, and python executes the real
  tools. Free, parallel, interactive, and pydantic-ai stays the runtime.

**Decision.**

- **`BridgedModel(pydantic_ai.models.Model)`** (`core/src/core/bridge/model.py`).
  Its `request()` serialises the message history (`ModelMessagesTypeAdapter`) and
  the turn's function-tool schemas into a `BridgeRequestPayload`, submits it over a
  `BridgeTransport`, and awaits the matching `BridgeResponsePayload`. The brain's
  action is mapped back to a pydantic-ai `ModelResponse` (`text` → `TextPart`;
  `tool_calls` → `ToolCallPart`), so the rest of the agent loop — tool execution,
  output validation — is unchanged and runs engram's **real** retrieval tools
  in-process. The brain is a **pure brain**: it decides, it never retrieves.
- **The wire payloads** (`core/src/core/bridge/protocol.py`) are frozen pydantic
  `BaseModel`s, not `TypedDict`, because they cross a boundary the brain controls —
  a malformed brain reply becomes a clean `ValidationError` at deserialisation
  rather than malformed data leaking into the model loop. `BridgeAssistantAction`
  enforces an **XOR**: a turn is *either* a final text answer *or* a batch of tool
  calls, never both, never neither.
- **Usage is populated explicitly (D7).** A custom model has no API usage object,
  and pydantic-ai instrumentation cannot auto-capture token usage for it, so the
  bridge builds the `RequestUsage` itself (`core/src/core/bridge/usage.py`): the
  worker's explicit `input_tokens`/`output_tokens` are used verbatim when given,
  otherwise a dependency-free `HeuristicTokenCounter` (~4 chars/token) estimates
  them — so per-turn cost can be computed once the response carries a priced
  identity (next point). The counter is a tight `TokenCounter` port; a consumer
  wanting exact counts injects a tiktoken-backed one rather than `core` pulling a
  tokenizer dependency.
- **The emulated identity is a real, priced model.** A `RequestUsage` alone is not
  enough for cost: `genai-prices` keys on a known `(provider, model)` pair. So
  `BridgedModel` reports a real `model_name` (default `claude-opus-4-8`;
  `engram evals run --bridge-model <id>` overrides it to the Claude tier actually
  driving the brain) and tags each turn **`anthropic`** in the *two* places cost is
  computed — the response's `provider_name` (pydantic-ai's `operation.cost`, read by
  the CLI report) and `system` → `gen_ai.system` (Logfire's "Tokens & cost" panel).
  An unknown pair (the original `claude-bridge` / `claude-code-bridge`) makes
  `calc_price` raise `LookupError`, silently dropping cost. The token *counts* stay
  estimates (above); only the *rates* are real — the brain is free, but the run shows
  its API-equivalent cost.
- **The transport is pluggable (D8).** `BridgeTransport` is a Protocol with a model
  side (`submit_request` / `await_response`) and a worker side (`claim_request` /
  `submit_response`). The default `FileQueueTransport` needs **zero
  infrastructure** — a directory of `requests/`, `claimed/`, `responses/` — and
  gives **one-claim-per-request** via an atomic rename into `claimed/`, so N
  parallel workers never double-claim. It lives in `core/` and imports no app. A
  Postgres-backed transport (`SELECT … FOR UPDATE SKIP LOCKED`) is the swap-in
  alternative that fits engram's existing database; the protocol is designed for it,
  but it is not implemented yet.
- **The brain is driven via the CLI.** `core.ai.create_chat_model("claude-bridge",
  transport=…)` returns a `BridgedModel` (the third backend alongside `test-model`
  and Azure deployments, decision D9). `engram evals run --model claude-bridge`
  blocks on the model side; an orchestrator spawns N bridge-worker subagents that
  each loop `engram evals bridge-claim` / `bridge-submit` (`src/cli.py`), claiming a
  request, deciding the action, and submitting it back. The operator-in-the-loop
  procedure is the runbook (`docs/reference/eval-bridge-runbook.md`).

**Consequences.**

- (+) **Free, parallel evals that measure the real retrieval surface.** The brain
  is an interactive Claude session (no credits); N workers run concurrently against
  one queue; and because python executes engram's real tools, the eval scores the
  exact retrieval path real clients exercise ([ADR-0011](0011-single-source-retrieval-toolset.md)).
- (+) **pydantic-ai stays the runtime.** The seam is one `Model` subclass; the
  agent, its toolset, instrumentation, and output validation are untouched, so the
  bridge is a drop-in third backend selected by a model spec string.
- (+) **App-agnostic core.** `core.bridge` imports no app; engram's evals slice
  drives it. A second app could reuse the identical bridge.
- (−) **Added latency and a manual operator loop.** Each model turn is a
  filesystem round-trip gated on a human-driven Claude session, so a run is
  operator-paced, not wall-clock-fast — acceptable for offline evals, unsuitable for
  a production request path. The runbook is the required operator procedure.
- (−) **Token counts are heuristic unless the worker overrides them.** The default
  ~4-chars/token estimate tracks *relative* cost well but is not exact; a
  tiktoken-backed `TokenCounter` is the opt-in for precise accounting.
- (−) **Priced bridge runs look like real Anthropic calls in Logfire.** Tagging
  `gen_ai.system=anthropic` (so the cost panel prices the turn) makes a bridge span
  indistinguishable from a metered Opus call except by the eval run's `model_spec`
  (`claude-bridge`); naively summing Logfire spend would count free bridge runs as
  if paid.
- (−) **Model-agnostic protocol, one worker.** The transport and payloads carry no
  Claude-specific assumptions, but only the interactive-Claude worker exists today;
  any other "brain" (or the Postgres transport) is future work.
- **Reversible?** Yes — additively so. `core.bridge` is a self-contained package,
  the `claude-bridge` branch is one arm of `create_chat_model`, and the worker
  commands are an additive `evals` sub-app; deleting them reverts the change with no
  impact on the Azure/`test-model` backends or the agent loop. The genuinely
  hard-to-reverse part — and why this is ADR-worthy — is the *architectural seam*
  itself: "a custom pydantic-ai model whose inference is an event round-trip to an
  interactive brain that only decides, while python executes the real tools" is a
  novel inversion that everything downstream (the worker protocol, the runbook, the
  eval orchestration) is now shaped around.

**Related.**
[ADR-0011](0011-single-source-retrieval-toolset.md) (the single-source retrieval
toolset the agent-under-test exercises — what the bridge makes free to evaluate).
Reference: the operator runbook
[eval-bridge-runbook](../reference/eval-bridge-runbook.md). Code:
`core/src/core/bridge/{protocol,transport,usage,model}.py`,
`core/src/core/ai.py` (the `claude-bridge` branch of `create_chat_model`), and the
`engram evals run` / `bridge-claim` / `bridge-submit` CLI (`src/cli.py`). Patterns:
[tight ports / DI seams](../../../docs/explanation/design-patterns.md#di-seams-and-fakes-over-mocks)
(the `BridgeTransport` and `TokenCounter` Protocols).
