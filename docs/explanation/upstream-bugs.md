---
id: upstream-bugs
aliases: [upstream-bugs]
type: explanation
app: root
status: current
tags: [upstream, pydantic-ai, pydantic-ai-harness]
title: "Upstream bugs: what we found in pydantic-ai and pydantic-ai-harness, and what we did about it"
---

# Upstream bugs in pydantic-ai and pydantic-ai-harness

The list of defects found in the two libraries we build on: `pydantic-ai` and `pydantic-ai-harness`, both installed from PyPI
and both released from the pydantic-ai repository. Every entry is a candidate upstream issue, filed on `pydantic/pydantic-ai`.
The repo rule is that upstream contributions are filed as issues, never PRs. Add an entry the moment a workaround lands in our
code, so the issue text is written while the evidence is fresh. Extensions we add over the libraries live in
[upstream-extensions.md](upstream-extensions.md).

Entry shape: library and version, symptom, cause (file and function), how it was found, what we did (commit), upstream state, issue.

## UB-1. `ToolOutputLimits` breaks `load_capability` when it shortens the load's return

- **Library:** pydantic-ai-harness with pydantic-ai, present in 0.52.0 and 2.52.0 and on pydantic-ai's `main` (2026-10-01)
  (`pydantic_ai_harness/tool_output_limits/_capability.py`, `_assemble`). The dropped `tools` field is there since v0.25.0.
- **Symptom:** a deferred capability whose `load_capability` return crosses a band does not count as loaded. Its tools stay
  locked, and a call to one is refused with "Tool 'X' is not available yet: it belongs to capability 'Y'. Call
  `load_capability` for it first". Its instructions arrive as a preview with a spill handle, and reading the whole spill back
  with `read_tool_result` does not make the load count. A second load is accepted instead of refused as already active. In the harness it hit `saving-a-workflow`: its skill is 21.8 KB, the chat agent limited loads at 16,000
  characters, so `save_workflow` stayed locked after the load.
- **Cause:** in pydantic-ai 2.52 a load counts only when its return content validates as `{'instructions': ...}`
  (`_narrow_load_capability_return` in `pydantic_ai/_deferred_capabilities.py`, read by `_parse_loaded_capabilities`), and the
  shortened content is a string. The rebuilt `ToolReturn` also leaves out `tools`, the field the deferred-capability loader
  (`DeferredCapabilityLoaderToolset._load_capability`) reveals tools with. On 2.52 that alone does not lock a tool: with the
  content whole and only `tools` dropped, the tool was offered and the call worked. Keeping `tools` does not fix the lock.
- **Found:** 2026-08-25, the dropped `tools`, while putting `save_workflow` behind the `saving-a-workflow` capability (its skill
  text was then about 14.5 KB). 2026-10-01, the content shape, by running a load end to end on 2.52 and calling the revealed
  tool: with `tools` kept the call was still refused.
- **What we did:** first threaded `tools` through the rebuild in a fork (`07a7fa54`), then
  `core.ai.tool_output_limits.ToolOutputLimits`, a subclass that puts `ToolReturn.tools` back after the upstream reduction
  (`core/tests/unit/test_tool_output_limits.py`). Since 2026-10-01 the chat agent also keeps `load_capability` out of its
  output limits (`CONTEXT_BOUND` in `harness/src/harness/features/authoring/agent.py`), so a load always arrives whole, and
  `test_loading_the_capability_reveals_its_tool` checks that the tool is offered after the load, not only named.
- **Upstream state:** unfixed in pydantic-ai-harness 0.52.0 and on `main` (2026-10-01). Not checked on 2.51. Affects any user
  combining `ToolOutputLimits` with a deferred capability whose instructions exceed a band, unless `tool_filter` leaves
  `load_capability` out.
- **Issue:** drafted, not filed.
- **Repro:** mount `ToolOutputLimits` with a band below the instructions' length and a capability with `defer_loading=True`
  that brings a tool. Call `load_capability` on it, then the tool. The call is refused. With `tool_filter` excluding
  `load_capability` it works.

## UB-2. The `PromptedOutput` preamble is injected as static instruction text, so it always sorts first

- **Library:** pydantic-ai 2.34.0 (`pydantic_ai/models/__init__.py`, `Model.prepare_request`).
- **Symptom:** with `PromptedOutput`, the system prompt always opens with "Always respond with a JSON object that's compatible with
  this schema ..." (about 1.3 KB), ahead of the agent's identity and every other instruction, no matter how the capabilities are
  ordered.
- **Cause:** `InstructionPart.sorted` places literal (static) instruction text before function-shaped (dynamic) text, a cache
  placement rule, and `prepare_request` injects the output-format block as literal text. A `ManagedPrompt` identity is necessarily
  dynamic (resolved per run), so it can only be first among the dynamic parts.
- **Found:** 2026-08-25, while putting the studio identity first in the chat agent's prompt.
- **What we did:** made every capability of ours contribute dynamically so the identity leads the rest, and pinned the framework
  limit in a test (`harness/tests/unit/test_agent_mounts.py`,
  `test_the_prompted_output_preamble_is_a_static_pydantic_ai_injection_not_ours_to_reorder`, retired with `8191d8ac` when the
  chat agent moved to plain text).
- **Upstream state:** by design, still present in 2.51.0: an agent with a dynamic identity instruction and `PromptedOutput`
  still receives the output-format block first (reproduced 2026-09-27). The ask would be a way to position the output-format block (or mark it dynamic) so an
  agent's identity can open the prompt.
- **Issue:** not filed.

## UB-3. A tool refusal raised inside `run_code` can come back as a sandbox protocol error

- **Library:** pydantic-monty runtime 0.0.21 under pydantic-ai's `CodeMode` (`run_code`).
- **Symptom:** a plugin called from inside a `run_code` script raises `ModelRetry` (engram's `write_note` refusing a page on its
  grammar). Most of the time the script fails with the refusal text as a plain exception and the model reads it. Some of the time
  the worker answers `monty worker protocol error: invalid exception payload: invalid value for ...` and pydantic-ai reports
  `Code execution failed and the session was reset`, so the model never sees which field was refused and spends a retry blind.
- **Cause:** not isolated. Both observed cases carried a refusal whose text embeds pydantic's `input_value={...}` dump of the whole
  payload. The plain-exception cases carried shorter refusals. The exception payload the runtime serializes appears to choke on
  that content.
- **Found:** 2026-08-26, watching `wf.research-digest.v2` digest steps (logfire, 08:58:02Z and 08:58:20Z, span `execute_tool
  run_code`).
- **What we did:** nothing in code yet. The digest brief tells the agent to fix exactly the named field on a refusal, and the
  refusals now name the registered values, which shortens the loop when the text does come through.
- **Upstream state:** unknown. The ask would be a serializable exception payload for any exception a hosted tool raises, or a
  documented limit on the exception text. The pydantic-ai 2.51.0 move left the Monty runtime at 0.0.21, so nothing here changed.
- **Issue:** not filed.

## UB-4. An output-validation retry reaches the model as if the person had sent it

- **Library:** pydantic-ai 2.34.0 (`pydantic_ai/_agent_graph.py`, `ModelRequestNode._build_retry_node`. Rendered by
  `pydantic_ai/models/openai.py`, `OpenAIChatModel._map_user_message`).
- **Symptom:** a structured-output run (`PromptedOutput` and friends) whose text fails validation gets a retry turn where the
  validator's own error reads, to the model, as something the person just said. The model answers the wrong party rather than
  fixing its output.
- **Cause:** `_build_retry_node` builds `RetryPromptPart(content=error.message)` on an output-validation failure and passes no
  `tool_name`, so the field defaults to `None`. `_map_user_message` renders any `RetryPromptPart` whose `tool_name` is `None`
  as `role="user"` rather than `role="tool"`, the branch every tool-shaped retry takes in the same mapping. Nothing marks this
  part as coming from the validator rather than the person.
- **Found:** 2026-08-26, on `deepseek/deepseek-v4-pro`. The model read "Invalid JSON: expected value at line 1 column 1" as
  the owner speaking and answered as though shown a validation error, then failed the turn after two output retries with
  "Exceeded maximum output retries (2)".
- **What we did:** the chat plugin's own agent answers in plain text, never through `PromptedOutput`, so its runs never build an
  output-validation retry and this failure has no path to trigger here.
- **Upstream state:** by design, still present in 2.51.0: a `PromptedOutput` validation retry carries `tool_name=None` and
  `OpenAIChatModel._map_user_message` renders it as `role="user"` (reproduced 2026-09-27). An output-validation retry and a
  tool-shaped retry share one part type, and a provider mapping tells them apart only by `tool_name`. The ask would be a way to render a validator's own retry as coming from the
  validator, not the person, when `tool_name` is `None`. pydantic-ai 2.60.0 answers it (upstream issue 6404): a retry that
  answers no tool call is a `RetryFeedbackPart`, which each model renders as feedback from the application, fenced in
  the user voice behind a line saying the application is speaking. Fixed for us since we took 2.60 (see the
  `RetryPromptPart` entry below).
- **Issue:** not filed.

## UB-5. A join behind another join fires in the same idle pass as the inner join, when the inner join was registered first

- **Library:** pydantic-graph 2.36.0 (`pydantic_graph/graph_builder.py`, the `GraphRun` main loop's idle path, the block
  that starts "there are no pending tasks", together with `_compute_intermediate_join_nodes`).
- **Symptom:** in a workflow where a run node waits on two producers, one of them an orchestrator gate that reviews a map's
  output, the run node executes the moment the map's last item finishes, before the gate has decided. It resolves the gate's
  wire and dies with `KeyError: '<gate id>'`. Seen on `wf.docs-promote-initiative.v1` (commit 2fed7c91): `pending` and
  `numbering` are roots, `drafts` maps over `pending`, `check_adrs` reviews `drafts`, `written` reads `check_adrs` and
  `numbering`. `compile_spec` gives `written` a join (`written__await_all`) with two inputs. `numbering` finishes at 0.6 s,
  the map at 380.6 s, `written` starts at 380.6 s and fails at 380.9 s while `check_adrs` is still running.
- **Cause (reproduced on the bare library):** when no task is active, the run loop finalizes every active reducer in one
  pass over a snapshot of `active_reducers`. It skips a join whose intermediate join is still an active reducer on the same
  fork run, but that check reads `active_reducers` live, and each join finalized earlier in the same pass has already been
  popped from it. So when the inner join (the map's collect join) sits before the outer join in insertion order, the inner
  join is finalized and popped first, the outer join then finds no active intermediate reducer, and it is finalized in the
  same pass, before the inner join's successor tasks (the gather step, then the gate) have run. Insertion order is the
  roots' completion order: the map's reducer is created when the map forks (after `pending` finishes), the outer join's
  reducer when `numbering`'s value arrives. `pending` first triggers it, `numbering` first does not. The per-task path
  (`_get_completed_fork_runs`) is not the culprit: the finishing task is still in `active_tasks` during that check and
  blocks the finalization.
- **Found:** 2026-08-29, the first shipped workflow with a run node joined on a gate and a second producer. The traceback came
  from logfire (`_compiler._run` resolving the wire, `KeyError: 'check_adrs'`), the timeline from the run archive.
- **Repro (bare `GraphBuilder`, 2.36.0):** roots `pending` (returns `[1, 2, 3]`) and `numbering`. A map over `pending`
  (`add_mapping_edge` with `fork_id` and `downstream_join_id`) into a list-append collect join, then a gather step. A gate
  step after the gather that stores its value in state and returns an `Approved` marker. A decision behind the gate
  (`Approved` to the outer join, `Rejected` back to the map's head). An `await_all` join fed by that decision and by
  `numbering`. Then `written`, which reads the gate's stored value. With `numbering` sleeping so `pending` finishes first,
  `written` runs before the gate starts and raises `KeyError`. With `numbering` finishing first, the run is correct.
- **What we did:** the promote workflow was made one chain (dd64d83d), and the design kept every workflow here to that rule
  while the bug stood. Two validator guards refused the shape at save time: `gate_with_sibling` (a run or map node whose
  direct producers mix a gate and a non-gate) and `wait_across_a_map` (a wait where one producer carries a map's collected
  output and another is not upstream of that map). Once the fix below landed, both guards and the one-chain rule were
  removed (2026-09-27), and `core` requires `pydantic-graph>=2.49.0`. A core unit test runs the formerly refused shape (a
  node waiting on a judge over a map and on a sibling root, ordered so both waits are open when the map collects) and
  checks the node fires once, after the judge. That test raises `KeyError` on 2.36.0 and passes on 2.51.0.
- **Upstream state:** fixed in pydantic-graph 2.49.0 (pydantic/pydantic-ai#8385): the idle pass now skips a
  join while any active task still feeds its fork run, so a task an earlier join in the same pass dispatched holds the outer
  join back. The bare repro above runs `KeyError` on 2.36.0 and correctly on 2.51.0, with the control ordering correct on
  both (2026-09-27).
- **Issue:** not filed by us. The fix closed pydantic/pydantic-ai#7785, a related report of a join dropping a fanned
  producer's contribution.

## UB-6. A syntax error in a long `run_code` program crashes the sandbox worker instead of reporting the error

- **Library:** pydantic-monty runtime 0.0.21 under pydantic-ai's `CodeMode` (`run_code`).
- **Symptom:** a program of a few thousand characters that contains a syntax error (a pasted string literal that breaks the
  quoting, an unterminated line continuation) does not come back as `Syntax error in code` with a traceback. The worker answers
  `monty worker protocol error: invalid exception payload: invalid value for StackFrame.end.column: 2 is before start column 7`,
  pydantic-ai reports `Code execution failed and the session was reset`, and the model loses every name it had bound in the
  session. A 300-character program with only tool calls also crashed the worker once (`The code crashed the sandbox worker`)
  with no payload at all.
- **Cause:** not isolated. The two reproducible cases were 6374- and 6895-character programs whose syntax error sat inside a
  multi-line string. The runtime's exception serializer builds a `StackFrame` whose end column precedes its start column, which
  the protocol layer refuses instead of the syntax error reaching the model. Sibling of UB-3 (same serializer, a different bad
  payload).
- **Found:** 2026-08-30, the studio orchestrator adapting `wf.research-compound.v5` inside `run_code` (logfire, 23:38:52Z and
  23:39:26Z, span `execute_tool run_code`), then hitting `run_code`'s three-retry ceiling. The 300-character crash was a digest
  worker at 22:39:57Z. Recurred 2026-08-31 (20:56:59Z and 20:57:43Z, same session): two programs carrying twelve edit ops as
  literals crashed the worker with `StackFrame.end.column: 2 is before start column 16`, each reset destroying the ops the model
  had built, and the turn again died at the retry ceiling.
- **What we did:** the sandbox doctrine (`harness/sandbox.py`) now tells the agent to build data as Python literals, never to
  paste prose or markdown into a program as code, and to keep any one program under a few thousand characters, naming the crash
  as the reason. The digest brief says the same for pages. Nothing in the runtime. After the recurrence, `SandboxDiscipline`
  also converts `run_code`'s retry ceiling into a result the model reads instead of an `UnexpectedModelBehavior` that ends the
  turn, so a crash loop costs retries, not the conversation.
- **Upstream state:** unknown. The ask is the same as UB-3's: an exception payload that serializes for any exception the runtime
  raises, syntax errors included, so a bad program costs one retry with the error in hand rather than a session reset. The
  pydantic-ai 2.51.0 move left the Monty runtime at 0.0.21.
- **Issue:** not filed.

## UB-7. A `run_code` result no serializer knows ends the whole run instead of reaching the model

- **Library:** pydantic-ai 2.36 (`capabilities/instrumentation.py`, `_run_tool_span`) with pydantic-ai-harness's `CodeMode`
  (`code_mode/_toolset.py`, `call_tool`), Monty runtime 0.0.21.
- **Symptom:** a program whose last expression is `type(x)` (or any value Monty returns that pydantic cannot serialize) does not
  come back to the model as a result or a retry. The turn dies with `Unable to serialize unknown type: <class 'type'>`, raised by
  the instrumentation capability's `serialize_result` (`tool_return_ta.dump_json`) while it records the tool span, before the
  tool-return part is built. `PydanticSerializationError` is not a `ModelRetry`, so nothing catches it and the agent run fails.
- **Cause:** two layers agree not to care. `CodeMode` validates the sandbox result as `ToolReturnContent`, whose last branch is
  `Any`, so a class object passes through untouched. The instrumentation span then serializes every non-string tool result with
  no fallback. The same result would have crashed `ToolReturnPart.model_response_str` a moment later anyway.
- **Found:** 2026-08-31, the studio orchestrator inspecting a fetched spec with `type(spec['spec_yaml']), str(...)[:300]`
  (logfire, 12:04:05Z, span `invoke_agent harness-author`, session `d0ba8ead`). The turn failed instead of the model reading the
  class name.
- **What we did:** first patched a fork's `CodeMode.call_tool` to pass the result through `to_jsonable_python(..., fallback=repr)`.
  That patch went with the fork: pydantic-ai-harness 0.36.0 renders a result with no JSON form as its `repr` itself.
- **Upstream state:** the `CodeMode` half is fixed in pydantic-ai-harness 0.36.0 (#1077, a result with no JSON form comes back
  as its `repr`). The instrumentation half is still present in 2.51.0: a plain tool returning `str` (the class) under the
  `Instrumentation` capability ends the run with `PydanticSerializationError` (reproduced 2026-09-27). The remaining ask: the
  instrumentation span should record a `repr` rather than raise when a tool result cannot be serialized, since observability
  must never end a run.
- **Issue:** not filed.

## UB-8. `Agent.iter` replaces a caller's own exception with the run failure it already recorded

- **Library:** pydantic-ai 2.36.0 (`pydantic_ai/agent/__init__.py`, `iter` and its `_extract_error` helper at lines 1883-1886).
- **Symptom:** code that catches a run failure inside `async with agent.iter(...)` and raises a different exception in its place
  never sees that replacement escape. The original failure comes back out of the block instead. An adapter converting provider
  errors into its own typed error therefore raises `UnexpectedModelBehavior` to its caller while its own `except` clause looks
  like it handled the case.
- **Cause:** `_extract_error` returns `agent_run._node_error or error` unconditionally. It exists to undo the `CancelledError` and
  `ExceptionGroup` transform the anyio task group applies, but a node error recorded earlier in the run wins over whatever the
  caller deliberately raised afterwards, so a replacement is discarded rather than merely unwrapped.
- **Found:** 2026-09-16, building the blind trial runner in `engram/src/engram/evals/harness/runner.py`, whose `TaskRunner`
  contract requires recoverable failures to surface as `core.ai.evaluation.contracts.EvaluationError`. The conversion inside the block
  silently did nothing.
- **What we did:** the failure is caught inside the block and carried out as a value, and the caller raises its own error after
  the block has exited (`_run_blind` in `engram/src/engram/evals/harness/service.py`).
- **Upstream state:** still present in 2.51.0, reproduced with the repro below on 2.36.0 and 2.51.0 (2026-09-27). The ask is that `_extract_error` only unwrap the transform it was written for
  and leave an exception the caller raised itself alone.
- **Issue:** not filed.
- **Repro:** run an agent over a `FunctionModel` whose function raises `UnexpectedModelBehavior`. Inside
  `async with agent.iter(prompt) as run`, catch `AgentRunError` around `async for _ in run` and raise `RuntimeError("converted")`.
  `UnexpectedModelBehavior` escapes, not the `RuntimeError`. The control case proves it is not ordinary context-manager
  behaviour: a `RuntimeError` raised in the same position after a run that succeeded escapes normally.

## Upstream changes that required adaptation (not bugs)

- **`ConversationSearch` defaults to `scope='conversation'`** (pydantic-ai-harness v0.25.0, upstream #621. It was `'all'`). Our
  recall mount already isolates by the asking session's own transcript, and runs land there without a `conversation_id`, so the new
  default returned nothing. We pass `scope="all"` explicitly (`0a22657d`).
- **`pydantic-ai-slim>=2.33.0` floor** (upstream #664) pulls `anthropic` to 1.0 and `openai` to 3.x so the Anthropic provider runs
  on the HTTPX2 generation. Taken with the v0.25.0 sync (`e8258030`, `0a22657d`).
- **A run that dies during tool execution leaves an empty interrupted request** (pydantic-ai 2.38.0, "Keep a run cancelled
  during its first tool call resumable"). `ModelRequest(state='interrupted')` is now appended even when no tool finished, so
  the resume path knows the last response's calls will never be answered. The harness's `rescue_history` closes the open calls
  inside that request and marks it complete, rather than appending a second request behind an empty one (`37ea109b`).
- **An unnamed capability's id is a fresh run-local handle** (pydantic-ai 2.38.0 and 2.40.0). A capability with no `id` gets
  `<snake_name:6 hex>`, new every run, carried on `ToolDefinition.capability_id` for the tools it contributes and never sent
  to a model. Tests that compare a turn's tool surface across runs compare what the provider receives, with that handle
  dropped (`c1240682`).
- **`ctx.enqueue` called from `before_model_request` lands in the request that hook ran ahead of** (pydantic-ai 2.58.0). It
  used to wait for the next request. A steer waiting before a model request now reaches the model in that request.
  `SteerDelivery` and `StepSteerDelivery` needed no change. Their tests and the step chat reference now say so (`7fe680c`).
- **A `run_code` program's tool calls are checked against the run's `tool_calls_limit`** (pydantic-ai-harness 0.59.0).
  Each call a program makes counts toward the limit together with its `run_code` call, and is checked before it runs. One
  that would cross the limit ends the run with `UsageLimitExceeded` in the middle of the program, and catching it inside
  the program does not help. Before 0.59 a program's calls were only counted, and checked before the model's next tool
  calls. `ModelCallsCodeMode` now also hands each `run_code` call the run's limits without `tool_calls_limit`, so an
  evaluation's budget still holds only the calls the model asks for (UE-22, `4a884bf`). Harness workflow steps take the
  new counting as it is (the owner's ruling, 2026-10-23): a step's `tool_calls` budget counts a program's calls, and the
  three template steps that search inside code mode carry doubled budgets, `check_adrs` 24, `draft_decision_brief` 28 and
  `review_decision_brief` 20.
- **`RetryPromptPart` is replaced by a retried `ToolReturnPart` and a `RetryFeedbackPart`** (pydantic-ai 2.60.0, upstream
  issue 6404). The agent loop no longer builds a `RetryPromptPart`. A retry that answers a call is that call's
  `ToolReturnPart` with `outcome='retried'` and a `RetryInfo` whose `cause` says why: `validation_error` (the schema
  rejected the arguments), `model_retry` (the tool, its argument validator or a hook raised `ModelRetry`),
  `unknown_tool` or `unavailable_tool`. A retry that answers no call is a `RetryFeedbackPart` (`validation_error`,
  `no_output` or `model_retry`). A stored `'retry-prompt'` part still loads as one of the two, with no cause when its
  content is text. Taken on 2026-10-23: every reader moved to the two parts (`core.ai.transcripts`, where a call's answer is
  now always its return, the inspection's rules, views and taxonomy, `core.ai.history.message_key`, the harness session
  rescue and pending calls, the inspection API's evidence kinds, the SPA's inspector trace and turn inspector), and
  engram's seen recall skips a retried return, which 2.60 would otherwise read as a tool result. By the owner's ruling
  `schema_rejected_arguments` counts a call no tool ran for (`validation_error`, and by the controller's ruling
  `unknown_tool` and `unavailable_tool`), and a call that passed the schema before the tool, a guard or its own check
  asked for it again (`model_retry`) is the new `tool_refused_call` symptom. Both show in the tool return. Neither
  counts an output tool's retry, which an output validator or the output's schema asks for, nor a retry with no
  cause. The inspection views read the instruction changes 2.60 sends (`InstructionDeltaPart`). Stored retries were
  moved forward by a one-off script (the record's forms, keys and checkpoint digests, the session projection, the run
  archive, and the stored leads and findings a tool refusal produced), not read by a compatibility branch.
- **A dumped `ToolReturnPart` carries a `retry` field** (pydantic-ai 2.60.0). Every tool return now dumps `"retry": null`,
  so a step's checkpoint digest, which digests the dumped messages, differs under 2.60 from the one 2.59 stored, and a
  durable run's next turn would refuse the checkpoint. The same one-off script readdresses every completed continuation
  attempt whose digest changed. The record also stores a tool return re-sent under 2.60 as a new form of its message,
  which costs space, never correctness.
- **The bundled memory stores search only the files directly under a prefix** (pydantic-ai-harness 0.60.0). A deeper
  folder is skipped as another scope, so a search over a scope found none of the harness's notes, which sit in category
  folders. The agent-memory store's ranked search, which the global and project mounts use, was never affected and
  stays one search per call. The store's fallback without ranking, the scratchpad's in-memory store and any other
  bundled store are searched one category folder at a time and the matches merged (UE-23).
- **A framework capability's dynamic instructions are framed as a `<context id="...">` element** (pydantic-ai 2.60.0).
  Built-in and harness capabilities now default a computed instruction block to `on_change='append'`, so a later change
  is appended as an `InstructionDeltaPart` instead of rewriting the cached prefix, and the prefix renders each such block
  inside `<context id="capability:<id>">` with one sentence after the last saying a later block with the same id
  replaces it. The studio identity (`ManagedPrompt`), the shell and the files sentences arrive that way. No code of ours
  reads that text. The prefix's text changes once at the upgrade, so each conversation's first request on 2.60 misses
  the prompt cache once, and a changed block no longer breaks the cached prefix afterwards. The instruction order tests
  read the text inside the element.
- **The files plugin's refusal of a note sidecar** reached the model as a retry in 2.59 and still does in 2.60, now as
  a retried return with the cause `model_retry`. The 2.60 trial saw a raised `PermissionError` only because the test's
  model never recognised the new retry part, kept calling, and spent the tool's one retry.
