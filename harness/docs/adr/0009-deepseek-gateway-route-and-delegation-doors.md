---
id: harness-adr-0009-deepseek-gateway-route-and-delegation-doors
aliases: ["harness-adr-0009-deepseek-gateway-route-and-delegation-doors"]
type: adr
app: harness
status: accepted
tags: []
title: "0009 — DeepSeek through a gateway route, and delegation mounted from the vendor"
---

# 0009 — DeepSeek through a gateway route, and delegation mounted from the vendor

**Status:** Accepted (2026-08-25). Applies [0002](0002-upstream-first.md) to two needs at once: a cheaper default model
reached through the one gateway key, and chat-side delegation with a model menu.

**Superseded in part:** on 2026-08-28. The single model knob (`GET/PUT/DELETE /api/harness/model`, `selected-model.json`)
and the deployment-wide `APP_THINKING` setting the delegation menu below chose a level from are replaced by three
owner-set tiers, `orchestrator`, `worker`, and `helper`, each carrying its own model and thinking level. See
[model-tiers](../reference/model-tiers.md).

## Context

The agent ran on `gpt-5.4` through the gateway's OpenAI route, on the responses API, answering the SPA with
`NativeOutput(Reply)`. The owner wants `deepseek-v4-pro` as the default workhorse and `deepseek-v4-flash` beside it, with the
tier chosen at delegation time. The gateway's official provider list has no DeepSeek and pydantic-ai has no
`gateway/deepseek:` model string, but the owner's gateway account carries a configured route named `DeepSeek` that speaks the
OpenAI chat wire, honours `reasoning_effort`, and prices responses. The same route refuses the responses endpoint, and the
DeepSeek API accepts `response_format: json_object` only, refusing `json_schema`.

The vendored `pydantic_ai_harness` ships `SubAgents` (a `delegate_task` tool with a keyed model menu, per-option
`ModelSettings`, per-delegate budgets) and `DynamicWorkflow` (a script tool over a catalog of sub-agents). Neither was
mounted.

## Decision

- `adapters/models.py` is a catalog: model id to gateway route, API flavour, and profile. DeepSeek ids build
  `OpenAIChatModel` on `gateway_provider("openai", route=<setting>)` with DeepSeek's own profile merged on top and
  `supports_json_schema_output` pinned off. `gpt-5.4` keeps the responses model. The route name is
  `ModelSettings.deepseek_gateway_route` (`APP_MODELS__DEEPSEEK_GATEWAY_ROUTE`, default `DeepSeek`). `MODEL_ID` is
  `deepseek-v4-pro`. A DeepSeek response answers under the OpenAI route's provider name, where the price registry has no
  DeepSeek entry, so the spend brake prices `deepseek-*` names through DeepSeek's own table (`adapters.models.price_response`).
  The named upstream gap: pydantic-ai has no DeepSeek branch in `gateway_provider`; the catalog entry
  pairs the gateway's generic route argument with DeepSeek's profile, and collapses onto `infer_model("gateway/deepseek:...")`
  the day upstream ships it.
- The SPA door answers through `PromptedOutput(Reply)`, the one structured mode every catalog model accepts. Tool-shaped
  output stays rejected (forced tool calls burn the turn budget, and v4 has no `tool_choice=required`).
- Two doors, both vendor capabilities behind `Switched`: `delegate` (`SubAgents`, one generic `worker`, menu `quick` =
  flash/high, `standard` = pro/high (default), `deep` = pro/max) and `agent-scripts` (`DynamicWorkflow`, tool
  `run_agent_script`, catalog `quick`/`standard`/`deep` as the same worker bound to a tier, `defer_loading`). Both set
  `forward_usage=False` so a worker's requests never count against the parent's turn budget, and bound workers on their
  own (40 requests, 600 s, 8 delegations or 12 script calls per turn). The parent's `SpendLimits` instance is mounted in
  every worker run, so the day ceiling covers the tree. A worker holds no delegation door: the tree is one level deep. A
  worker's shell, files, and reads sit behind the parent's own `shell`, `files`, and `research` doors, so closing one closes
  it for every worker as well, and `delegate` and `agent-scripts` are the switches over delegation itself.
- One skill, `delegating-work`, carries the judgment (when, which tier, how to brief, script or one at a time, escalate
  never repeat). The capabilities carry the mechanics in their own instructions.

## Consequences

- Every artifact packed under `gpt-5.4` reports model drift: the world stamp doing its job.
- The delegation prompt labels each tier `openai:<deepseek id>` because the model sits on the OpenAI route class. The key
  and its description are what the orchestrator routes on.
- A delegating turn's worker requests are not in the parent's `usage` and the workers write no step log, so the studio's
  per-turn and per-session token numbers cover the parent's own requests only. The spend brake still prices every worker
  response.
- Each worker run opens its own `run` spend window, so one turn may draw up to twenty fresh per-run ceilings (8 delegations,
  12 script calls); the day ceiling is the brake that bounds a single turn.
- Parked: delegates drawn from the self-authoring store's activated agents, a named reviewer, an owner-facing effort knob
  for the main agent, DeepSeek on the gateway's responses route (flip the catalog's `api` column when the gateway prices
  it).
