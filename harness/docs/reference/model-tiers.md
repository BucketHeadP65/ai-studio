---
id: harness-reference-model-tiers
aliases: ["harness-reference-model-tiers"]
type: reference
app: harness
status: current
tags: [models, tiers, delegation, thinking, api]
title: "model-tiers: three owner-set tiers, the model list, and the effort scale"
---

# model-tiers: three owner-set tiers, the model list, and the effort scale

Three named tiers, `orchestrator`, `worker`, and `helper`, each a model id and a thinking level the owner sets from
AI Factory Studio. A tier is read fresh on every request (`harness.adapters.model_tiers`), so a change reaches the very next
turn and the very next workflow step with no restart.

## The three tiers

| Tier | What runs on it |
| --- | --- |
| `orchestrator` | The chat agent, deep delegations, and steps that name it. |
| `worker` | Workflow steps by default, and standard delegations. |
| `helper` | Quick delegations, and steps that name it. |

The three delegation tiers (`quick`, `standard` (the default), and `deep`) sit on top of this: `quick`
runs on `helper`, `standard` runs on `worker`, and `deep` runs on `orchestrator`. A workflow step that names no tier of
its own runs on `worker`.

A run or map node, and a step file it names, may each name a `tier`. The node's tier wins when both name one. A
node naming none defers to its step file's own tier, and naming neither still runs on `worker`.

Each tier is mounted once, at composition time, as a `TierModel(tier, tiers)` (a `WrapperModel` over `pydantic_ai.models.Model`)
and handed to the agent or the worker builder like any other model. `tiers` is the `ModelTiers` the container builds:
where the owner's entries are stored, the list an entry is checked against, and how an id at a thinking level is built.
`TierModel` never resolves the tier at mount time.
Every attribute read against it, including the very first one, resolves the tier's current setting and rebuilds the
underlying model only when that setting changed since the last read. A turn already in flight keeps whatever model it
already built. The next turn or the next step picks up a change.

## The store

`.state/model-tiers.json`, beside the transcript, holds only what the owner has set by hand: a JSON object keyed by tier
name, each entry a model id and a thinking level, written atomically so a concurrent reader never sees a half-written
file. A tier the owner has never touched, or has cleared, has no entry at all and runs on its default, `glm-5.3-flash`
at `xhigh`. `glm-5.3-flash` is reachable through OpenRouter only, so a tier on its default needs
`infrastructure.openrouter.api_key`.

**The stand-aside rule.** Reading a tier (`tier_in_effect`) checks the stored entry's model against the model list. When
the model is still listed, the owner's entry stands. When it is not, meaning the model has dropped off the deployment's
reachable list since it was set, the stored entry stands aside for the tier's default rather than blocking the turn that
asked to run on it. Nothing needs to notice or repair the stale entry. The next successful `PUT` on that tier overwrites
it.

## The routes

All on `:8770`, on `SelfAuthoringController` in `features/selfauthoring/controller.py`.

```
GET    /api/harness/tiers                    -> {"tiers": [TierView, ...]}
PUT    /api/harness/tiers/{tier}   {"model", "thinking"}   -> {"tiers": [TierView, ...]} | 400 (model not on the list)
DELETE /api/harness/tiers/{tier}                            -> {"tiers": [TierView, ...]}
GET    /api/harness/models?q=                               -> {"models": [ModelEntry, ...]}
```

`TierView`: `tier` (the tier name), `role` (the sentence in the table above), `model` (the full `ModelEntry`, not just
the id), `thinking`, and `default` (whether this is the tier's fallback rather than the owner's own choice).

`GET /api/harness/tiers`:

```json
{
  "tiers": [
    {
      "tier": "orchestrator",
      "role": "the chat agent, deep delegations, and steps that name it",
      "model": {
        "id": "glm-5.3-flash",
        "name": "glm-5.3-flash",
        "route": "openrouter",
        "context_length": 1048576,
        "price_in_per_m": "0.075",
        "price_out_per_m": "0.25",
        "reasoning": "effort",
        "pinned": true
      },
      "thinking": "xhigh",
      "default": true
    }
  ]
}
```

`PUT /api/harness/tiers/worker {"model": "deepseek-v4-flash-0731", "thinking": "medium"}` puts `worker` on that model
at that thinking level, records a `TierSelected` log entry, and answers with the same `TiersListing` shape as the
`GET` above, `worker`'s row now carrying `"default": false`. A model id the list does not carry is refused with 400,
naming the id and pointing at the search route. `DELETE /api/harness/tiers/worker` drops the stored entry and answers
the same shape with `worker` back on its default.

`GET /api/harness/models?q=flash` searches the list and answers only the id and name match:

```json
{
  "models": [
    {
      "id": "deepseek-v4-flash-0731",
      "name": "deepseek-v4-flash-0731",
      "route": "openrouter",
      "context_length": 1000000,
      "price_in_per_m": null,
      "price_out_per_m": null,
      "reasoning": "effort",
      "pinned": true
    },
    {
      "id": "glm-5.3-flash",
      "name": "glm-5.3-flash",
      "route": "openrouter",
      "context_length": 1048576,
      "price_in_per_m": "0.075",
      "price_out_per_m": "0.25",
      "reasoning": "effort",
      "pinned": true
    }
  ]
}
```

This is the state with an OpenRouter key on file, the route every pinned id with an OpenRouter wire name resolves to
(`_pinned` in `adapters/model_list.py` decides every pinned entry's route from the same one condition, whether the
deployment holds an OpenRouter key, so a single list never splits pinned entries between routes). With no key on file,
every pinned entry answers route `"gateway"` instead, and the search runs over the pinned catalog alone. That route is
the list's label only: the model factory refuses a Z.ai id such as `glm-5.3-flash` with no OpenRouter key, because Z.ai's
models are reachable through OpenRouter only.

## The model list

`harness.adapters.model_list.ModelList`, registered once by `adapters/wiring.model_list(infrastructure)` and resolved from the
container, is the one list the process keeps: the pinned catalog (`adapters.models.CATALOG`,
the models this deployment has a proven route for) first, then every model OpenRouter's own list offers under the key on
file, narrowed to models that accept tools, since every agent here calls tools. A remote entry that shares a pinned id's
OpenRouter wire name prices the pinned entry rather than appearing twice.

The remote half refreshes at most once an hour (`REFRESH_SECONDS`). A request that lands mid-hour reuses what is already
in hand, and a failed refresh keeps the last successful list rather than falling back to the pinned catalog alone. With
no OpenRouter key on file (the settings file's `infrastructure.openrouter.api_key` is empty), the list is the pinned catalog and nothing else.

`search(q, limit=50)` is one case-insensitive substring match over id and name, pinned entries first, capped at fifty
hits. `is_listed(model_id)` and `entry(model_id)` answer off whatever is already in hand, with no fetch of their own.
`entries()` and `search()` both call `warm()` themselves before answering, so every route that reads the list, the
tiers routes and the search route alike, answers off a list that is never more than an hour stale.

## The effort scale

`low`, `medium`, `high`, `xhigh`, in that order, is the one scale every tier is set on and the one the store keeps
verbatim (`core.thinking.Thinking`). Each route the model factory builds maps that scale onto its own:
OpenAI's chat and responses models (the gateway route, and any catalog model reached through it) carry all four levels
through unchanged, `xhigh` included. OpenRouter's own reasoning field tops out at `high`, so a tier stored at `xhigh`
and built through OpenRouter runs at OpenRouter's `high`, the closest level that route offers. The stored value never
changes to reflect this. Only the request each route sends does.

## Anthropic models

The model factory builds every tier on the OpenAI wire (an Azure OpenAI deployment, the gateway's OpenAI provider, or
OpenRouter), so no tier's request goes through the anthropic SDK, an Anthropic model on OpenRouter included. Only a
model built as pydantic-ai's `AnthropicModel` would. That SDK is at 2.x, which sends API version `2026-10-21` (the 1.x
SDK sent `2023-06-01`), and that version refuses a request that carries `temperature`, `top_p` or `top_k`. pydantic-ai
sends those only when a model's settings name them, and nothing here sets them. The provider-native compaction strategy
(`AnthropicCompaction`, see [sessions](sessions.md)) only adds an Anthropic request setting, which only an
`AnthropicModel` reads.

## The hosts OpenRouter may use

OpenRouter sends a request to any host that serves the model, except the hosts `_OPENROUTER_IGNORED_HOSTS` in
`adapters/models.py` names for the model's wire name. GLM 5.3 Flash is never sent to Together. Together bills some
GLM 5.3 Flash replies whose stream carries no text, no reasoning and no tool call. Once one such reply is in a turn's
history, the retries that follow mostly come back empty too, and the turn fails with "Exceeded maximum output retries".
The other hosts answered the same history with a reply every time.

## The setup's `model`

`.aifactory/config.json`'s `LoadoutConfig.model` sets the orchestrator tier's model at process boot, and only when the
orchestrator has no stored entry yet (`stored_tiers()` carries no `"orchestrator"` key). An owner who has already put the
orchestrator on a model by hand is never overridden by the project's own setup, and a setup naming a model the list
does not carry warns rather than failing the boot. It leaves the orchestrator's thinking at its default. The setup has
no field for thinking.

## The follow-me `set_model` act

The follow-me plugin's `set_model(model_id, tier="orchestrator")` console act, mounted behind the person's approval, is the
same act as `PUT /api/harness/tiers/{tier}`: it calls the very same route handler, so a companion's model change and a
station click carry the same guard, the same log entry, and the same words on refusal. Naming no tier puts the
orchestrator on the given model, and naming `worker` or `helper` puts that tier on it instead, keeping the tier's own
current thinking level. Its own description names no model id and no thinking level, so the agent chooses among what the
deployment actually offers rather than what a prompt happens to remember.
