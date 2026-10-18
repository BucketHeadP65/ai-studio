---
id: harness-adr-0011-external-agent-door-over-acp
aliases: ["harness-adr-0011-external-agent-door-over-acp"]
type: adr
app: harness
status: accepted
tags: []
title: "0011 — The external-agent door: driving other harnesses over ACP"
---

# 0011 — The external-agent door: driving other harnesses over ACP

**Status:** Accepted (2026-08-26). Extends [0009](0009-deepseek-gateway-route-and-delegation-doors.md), which mounted the
two in-process delegation doors, with a third that delegates outside this process entirely.

## Context

The `delegate` and `agent-scripts` doors run a worker that is one of our own agents, on our own model menu, inside this
process. That worker cannot use another vendor's coding harness, and it inherits our tool surface rather than theirs.

Claude Code, Codex and Pi all publish ACP adapters, and this app already speaks ACP in the other direction: `harness.acp`
serves THE studio agent to an editor through `pydantic_ai_harness.experimental.acp`. The vendored fork had no client half,
so nothing here could spawn an ACP agent and drive it. The `agent-client-protocol` SDK (0.11.1) the harness already installs
ships `ClientSideConnection`, `spawn_agent_process`, and the `Client` protocol, so the missing piece was a small client over
them rather than a protocol implementation.

## Decision

- **The client half is an additive vendored package**, `pydantic_ai_harness/experimental/acp/client/`, beside the existing
  agent side. It now lives in `harness/src/harness/adapters/acp_client/`, since the fork was removed. `ExternalAgent.run(brief, *, cwd, permission, on_update, timeout_seconds)` spawns the configured command,
  sends `initialize` advertising **no** client capabilities, opens `session/new` at `cwd`, sends one `session/prompt`,
  forwards every `session/update` to `on_update`, answers `session/request_permission` through `permission`, and cancels
  with `session/cancel` at the timeout. `ExternalResult` is the streamed text, the stop reason, and the paths the agent
  reported touching. Because nothing is advertised, `fs/read_text_file`, `fs/write_text_file` and the `terminal/*` methods
  answer with a "not advertised" error: the external agent works its own disk. It is a contribution candidate upstream, and
  carries `UE-10` in the extensions ledger.
- **The registry is a JSON file in the state dir**, `external-agents.json`, name to `command`, `args`, `env`,
  `permission` (`auto` or `gate`), and `timeout_seconds` (default 600). Read fresh on every call, like `disabled-doors.json`,
  so an edit reaches the next turn with no restart. `permission` defaults to `gate`: spawning another coding agent on this
  machine is opted into, never inherited. `harness/external-agents.example.json` is the starting point.
- **One tool behind one door.** `external-agent` joins the toggleable catalog, and mounts
  `run_external_agent(agent, prompt, cwd)`. It is an act, so `CodeMode` never folds it into a program. `auto` answers
  allow-once to every request. `gate` routes each request to the session's own gate asker, the very object a human gate is
  answered through, so run mode decides an external agent's permissions exactly as it decides ours. The door reads the asker
  and the journal off the session per call rather than binding them at composition, because the ACP asker is born with the
  editor's session and a workspace session journals into its own file.
- **The run is journaled.** `ExternalAgentStarted`, `ExternalAgentUpdate` (coalesced to at most one a second, carrying an
  excerpt), and `ExternalAgentFinished` join the session-event grammar, so "model-visible means logged" holds for a
  subagent that is not ours.
- **It is also a workflow step.** `run_external_agent` is registered in every `WorkRegistry` `build_world` returns, with
  `replay_safe=False`, so on Temporal it runs as an activity with a timeout and a retry rather than being lost with the
  process. A retry starts a fresh external session, so the idempotency is the external agent's own.
- **One shot, one turn, no parent context.** The external agent sees the brief and nothing else, which is how deepseek-harness
  uses Codex and Claude Code as subagent providers.

## Consequences

- A `gate` entry can only be answered where a gate asker exists, which today is the ACP door. From the SPA and from a
  workflow run, a `gate` entry refuses and says to run it from an editor or to set `auto`. A browser-side permission surface
  for an external agent does not exist.
- An external agent's spend is invisible to the spend brake. It bills its own provider under its own key, so the day
  ceiling covers this app's tree and not the tree it starts.
- The registry file holds provider credentials in plain text under `.state/`. It is git-ignored like the rest of that
  directory. The example file carries `set-me` placeholders and never a real key.
- A spawned process starts from the SDK's trimmed default environment, so an adapter that cannot find its key exits before
  `initialize`. The entry's `env` is the only place that key can come from.
- The external agent's own transcript is not in our step log. The journal carries the shape of the run (started, alive,
  finished, what it touched) and the tool's answer carries its final text. Anything more detailed lives in that agent's own
  logs.
- The workflow step's journal is bound once, in `build_world`, to the checkout's own journal, so a workflow launched from a
  workspace session narrates its external-agent run there instead of into the workspace's own journal.
