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

## UB-9. `_utils.gather` leaves the coroutines it never started unawaited when it is cancelled

- **Library:** pydantic-ai 2.60.0 with anyio 4.15.1 (`pydantic_ai/_utils.py`, `gather`, lines 276-309).
- **Symptom:** `RuntimeWarning: coroutine '...' was never awaited` when a run is cancelled while it resolves its capabilities, for
  `AbstractCapability.for_run` and for `gather.<locals>._run`. In the harness it shows when a chat run's outcome turn (the
  headless turn `settled_turn` starts) is still resolving its capabilities as the event loop closes: in the tests that ended
  before that turn did, and in production when the harness shuts down during an outcome turn.
- **Cause:** `gather` is an `async def` that takes the job coroutines as arguments and hands each to
  `tg.start_soon(_run, i, coro)` only once its body runs. Cancelled before that, or with its task group cancelled before a
  `_run` starts, it never awaits or closes those coroutines. `asyncio.gather` is a plain function that wraps every job in a task
  at call time, so it leaves nothing behind. The helper breaks the rule that whoever takes a coroutine awaits or closes it, and its
  docstring says it matches `asyncio.gather`'s shape.
- **Grade:** cosmetic. A job that never started has no side effect and opens nothing, so the warning is the only effect.
- **Found:** 2026-10-29, Task 47, clearing the harness unit tier's warnings: a forced `gc.collect()` after each test named the
  four tests that left an outcome turn in flight.
- **What we did:** a chat run's delivery task is on the session (`session.active.delivery`), and those tests await it, so no
  outcome turn is cancelled mid-flight (`harness/tests/unit/test_chat_run_visibility.py`, `test_run_launch_events.py`,
  `test_restore_every_session.py`). Production shutdown is unchanged.
- **Upstream state:** present in 2.60.0, and `gather` is unchanged in 2.63.0. The ask is that `gather` close every coroutine it
  never started.
- **Issue:** not filed.
- **Repro:** create two job coroutines, wrap `pydantic_ai._utils.gather(*jobs)` in a task, cancel it before it first runs, and
  collect garbage: two "coroutine 'job' was never awaited" warnings. The same with `asyncio.gather` gives none.

## UB-10. `sanitize_messages` warns about the message id `load_messages` wrote itself

- **Library:** pydantic-ai 2.61.0 to 2.63.0 (`pydantic_ai/messages.py`, `sanitize_messages`. `pydantic_ai/ui/_utils.py`,
  `set_ui_message_id`).
- **Symptom:** `UserWarning: Framework-reserved __pydantic_ai__ metadata was dropped from 1 client-submitted message(s)` on
  every chat turn, for a request whose messages carry no metadata at all.
- **Cause:** `VercelAIAdapter.load_messages`, a public classmethod, keeps each UI message's id in the reserved
  `__pydantic_ai__` namespace of the message it builds. Since 2.61 the public `sanitize_messages` drops that namespace from
  client messages and warns each time. The adapter's own `UIAdapter.sanitize_messages` sets the id aside first, through the
  private helpers `get_ui_message_id`, `without_ui_message_id` and `set_ui_message_id`, so it never warns. A caller that loads
  client messages with the public classmethod and sanitizes them with the public function, as the docstrings of both suggest
  for client-held history, gets the warning and loses the id.
- **Grade:** cosmetic for us. The turn reads only the typed text, so the lost id changes nothing.
- **Found:** 2026-10-30, Task 49, the harness unit tier on 2.63: 53 warnings, all from `harness.features.authoring.wire`.
- **What we did:** `messages_of` drops every client message's metadata before `sanitize_messages`, so nothing reserved is left
  to drop (`34e13af`).
- **Upstream state:** present in 2.63.0. The ask is that `sanitize_messages` keep or silently drop the UI message id that
  `load_messages` wrote, or that the three helpers be public.
- **Issue:** not filed.
- **Repro:** `sanitize_messages(VercelAIAdapter.load_messages([UIMessage(id="m-1", role="user", parts=[TextUIPart(text="hi")])]))`
  under `warnings.simplefilter("error")` raises the `UserWarning` above.

## UB-11. `CapabilityStore.write` lands every entry active, and its directory lock is private

- **Library:** pydantic-ai-harness 0.61.0 to 0.63.0 (`pydantic_ai_harness/capability_creation/_store.py`, `write` and
  `_directory_lock`).
- **Symptom:** a store that must land authored code inactive cannot do it in one step. `FileCustomCapabilities.write` calls
  the store's `write`, which saves the entry `active`, then the store's `disable`. Between the two, a `load_active` in the
  same process can load the new, unreviewed code. Its own manifest writes (`activate`, `deactivate`, `delete`) cannot
  take the per-directory lock that `write` and `disable` hold since 0.61, so an owner's activate that races an agent's
  write in the same process can save the old entry over the new one while the new file is on disk.
- **Cause:** `AuthoredCapability.status` defaults to `'active'` and `write` takes no status. The lock is the module-private
  `_directory_lock`.
- **Grade:** a narrow race, present before 0.61 too, when nothing took a lock.
- **Found:** 2026-10-30, Task 49 review, adapting `FileCustomCapabilities` to 0.61.
- **What we did:** nothing in code. `write` lands the entry disabled right after the store's `write` (`34e13af`).
- **Upstream state:** present in 0.63.0. The ask is a landing status on `CapabilityStore.write` (for example
  `write(name, code, status='disabled')`), or a public directory lock a subclass can hold around its own manifest writes.
- **Issue:** not filed.

## UB-12. A response a content filter cut is validated as output, and its retries all fail

- **Library:** pydantic-ai 2.63.0 (`pydantic_ai/_agent_graph.py`, `CallToolsNode`, the `content_filter` check under
  "No actionable output was returned by the model". `pydantic_ai/models/openai.py`, `OpenAIResponsesModel._process_response`
  and `OpenAIResponsesStreamedResponse._handle_terminal_response`).
- **Symptom:** an Azure deployment whose content filter cuts a streamed answer returns a response that finishes on
  `content_filter` with zero usage and one text part, the stock sentence "I'm sorry, but I cannot assist with that
  request." The agent validates that sentence as its output. Under a structured output type it fails as `json_invalid`,
  the agent sends a retry prompt, the filter cuts the answer again, and the item fails with "Exceeded maximum output
  retries (4)" after five paid requests. The same run raised `ContentFilterError` at once for two filtered responses that
  carried no text. None of the 8 filtered items recovered on a retry. Neither error names the filter category, and the
  run's record kept none, so nobody could tell which filter fired.
- **Cause:** two gaps. First, the graph raises `ContentFilterError` only when a `content_filter` response is empty, blank
  or thinking only. A response with text goes to output validation like any other. The opt-in capability
  `RaiseContentFilterError` raises for every `content_filter` response, but it has to be added to each agent, and its
  message, like the graph's, is only "Content filter triggered. Finish reason: 'content_filter'". Second, Azure sends its
  filter results on a successful Responses API response as a top-level `content_filters` list the OpenAI SDK has no field
  for. pydantic-ai parses Azure's result types (`_AzureContentFilterResult`, `_AzureContentFilter`) only from the body of
  an HTTP 400 that refused the prompt (`_check_azure_content_filter`). On a 200 response, streamed or not, the list
  reaches the provider details only through a subclass of `OpenAIResponsesModel` that overrides
  `_process_provider_details`, the hook added for this in pydantic-ai#9094 and run on streams since pydantic-ai#10515.
- **Grade:** costly. Each filtered item spends every output retry on a request the filter refuses again.
- **Found:** 2026-10-31, reading a workflow run in which 8 items were filtered and every one ended this way or at once.
- **What we did:** `core.ai.content_filter` (UE-26): `ContentFilterGuard` around every generation model the repo builds raises
  `ContentFilterError` for any `content_filter` response, and `FilterReportingResponsesModel` keeps the `content_filters`
  list, so the error's first line names each flagged category and its severity (`343ec2df`).
- **Upstream state:** present in 2.63.0. The open pull request pydantic-ai#12039 makes `ModelFallback` fall back on a
  refusal with text, but leaves the default and the message as they are. The ask is that a `content_filter` response never
  be retried as output (raise `ContentFilterError`, or at least send no output retry for it), that `OpenAIResponsesModel`
  keep Azure's `content_filters` in the provider details by default, and that `ContentFilterError` name the flagged
  categories when the provider details hold them.
- **Issue:** not filed.
- **Repro:** an agent over a `FunctionModel` that returns `ModelResponse(parts=[TextPart("I'm sorry, but I cannot
  assist with that request.")], finish_reason='content_filter')`, with `output_type=PromptedOutput(Verdict)` and
  `retries={'output': 4}`, raises `UnexpectedModelBehavior: Exceeded maximum output retries (4)` after 5 model calls.
  With `capabilities=[RaiseContentFilterError()]` it raises `ContentFilterError` after 1.

## UB-13. Monty's `json.dumps` refuses `default=`

- **Library:** pydantic-monty 1.4.0, the sandbox behind pydantic-ai-harness 0.63.0's `CodeMode` (`run_code`).
- **Symptom:** `json.dumps(value, default=str)` in a program fails with `TypeError: JSONEncoder.__init__() got an
  unexpected keyword argument 'default'`. `indent=` works. `run_code`'s description lists `json` among the importable
  modules with no caveat, so a model writes the CPython call it knows and spends a retry on it.
- **Cause:** Monty's `json.dumps` takes a subset of CPython's keywords, and `default` is not among them. Neither Monty nor
  the `run_code` description says which keywords are missing.
- **Grade:** cheap. It costs one retry. A tool's return reaches a program already converted to JSON values, so a program
  never needs `default=` to dump one.
- **Found:** the harness inspector's finding 5, confirmed 2026-11-04 by a review of its findings: a chat model's program
  called `json.dumps(..., default=str)` and was refused.
- **What we did:** the sandbox dialect (`harness/src/harness/sandbox.py`, `_DIALECT`) says `json.dumps` takes no
  `default=` and that a tool's return is already a JSON value. Nothing in the runtime.
- **Upstream state:** present in pydantic-monty 1.4.0. The ask is that `json.dumps` accept `default=` as CPython does, or
  that `run_code`'s description name the `json` keywords Monty leaves out.
- **Issue:** not filed.
- **Repro:** `with pydantic_monty.Monty() as monty, monty.checkout() as session:` then
  `session.feed_run('import json\njson.dumps({"a": 1}, default=str)')` raises `MontyRuntimeError: TypeError:
  JSONEncoder.__init__() got an unexpected keyword argument 'default'`. The same call with `indent=1` in place of
  `default=str` returns `'{\n "a": 1\n}'`.

## UB-14. Without `rg`, `search_files` refuses an inline regex flag and advises an argument it does not take

- **Library:** pydantic-ai-harness 0.63.0 (`pydantic_ai_harness/filesystem/_command_search.py`, `validate_posix_pattern`,
  lines 22-29, called from `run_posix_search` at line 53. `filesystem/_toolset.py`, `_command_search_files`, which falls
  back to it at line 1385 when `rg` is missing).
- **Symptom:** in a workspace with no `rg` on its `PATH`, `search_files(pattern="(?i)proposition")` fails with a refusal that says
  POSIX grep does not support that syntax and ends `Use literal=True or install rg.` `search_files` takes only
  `pattern`, `path` and `include_glob` (`_search_files_tool`, `_toolset.py:1184`). `literal` belongs to the opt-in `grep`
  tool (`_grep_tool`, `_toolset.py:1624`), which `FileSystem` does not register by default. A model that follows the
  advice calls `search_files` with an argument it refuses.
- **Cause:** the fallback runs `grep -E`, which has no inline flags, `\d`, `\w` or lazy quantifiers, so the check refuses
  them. The refusal text is shared by both tools and written for `grep`'s arguments.
- **Grade:** costly in a workspace without `rg`. The model spends retries on a flag and an argument it cannot use, and
  `search_files` offers no case-insensitive search at all.
- **Found:** the harness inspector's findings 15, 20, 21 and 23 (one conversation, filed four times), confirmed
  2026-11-04: the harness's environment had no `rg`, so every search the harness ran took the fallback.
- **What we did:** the harness depends on pydantic-ai-harness's `coder` extra, which installs `rg` (ripgrep 14.1.0) into
  its environment, so `rg` is on the `PATH` every workspace inherits under `uv run` and the fallback never runs there.
  `harness/tests/unit/test_file_search.py` holds the control and the fix.
- **Upstream state:** present in 0.63.0. The ask is that the refusal name only what the calling tool takes (for
  `search_files`, "rewrite it without the inline flag, or install rg"), or that the fallback translate `(?i)` into
  `grep -i`.
- **Issue:** not filed.
- **Repro:** a `FileSystem(root_dir=tree)` toolset's `search_files("(?i)x", workspace=LocalWorkspaceBackend(tree,
  env={"PATH": "/usr/bin:/bin"}))` raises `ModelRetry` with that refusal.

## UB-15. Without `rg`, `search_files` fails the whole search on a tracked symlink to a folder

- **Library:** pydantic-ai-harness 0.63.0 (`pydantic_ai_harness/filesystem/_command_search.py`, `run_posix_search`: the
  enumeration at lines 59-71, whose `git ls-files -co` is line 63, and the per-file `grep` at lines 93-97).
- **Symptom:** in a git work tree that tracks a symlink to a folder, with no `rg` on the workspace's `PATH`, every
  `search_files` call whose path covers the symlink fails with `POSIX search failed: grep: skills/agent-threads: Is a
  directory`. No match from the rest of the tree comes back. In this repository the symlink is
  `engram/plugin/skills/agent-threads`.
- **Cause:** `git ls-files -co` lists a tracked symlink as a file, whatever it points at. The fallback greps each listed
  path, and `grep` exits 2 on a folder. A path that exits above 1 ends the search unless it is unreadable, and the check
  (`[ -r "$file" ] || [ ! -e "$file" ]`) holds for a readable folder, so the loop exits 2 and the search fails. `rg` does
  not follow symlinks while it walks, so it skips the path.
- **Grade:** costly. One symlink anywhere under the search path makes every content search there fail.
- **Found:** the harness inspector's finding 30, confirmed 2026-11-04 on this repository's tree.
- **What we did:** the same `coder` extra as UB-14, so the harness searches with `rg`. The same test file proves it.
- **Upstream state:** present in 0.63.0. The ask is that the fallback skip a listed path that is not a regular file, as
  `rg` does, for example by testing each listed path with `[ -f "$file" ]` before it greps it.
- **Issue:** not filed.
- **Repro:** in an empty folder, `mkdir real && echo x > real/a && ln -s real link && git init -q && git add -A`, then a
  `FileSystem(root_dir=folder)` toolset's `search_files("x", workspace=LocalWorkspaceBackend(folder, env={"PATH":
  "/usr/bin:/bin"}))` raises `ModelRetry: POSIX search failed: grep: link: Is a directory`.

## UB-16. A dynamic capability inside another dynamic capability contributes no tools

- **Library:** pydantic-ai 2.63.0, unchanged in 2.66.0 (`pydantic_ai/toolsets/_dynamic.py`, `DynamicToolset.for_run`.
  `pydantic_ai/capabilities/_dynamic.py`, `DynamicCapability._resolve_toolset`).
- **Symptom:** a `DynamicCapability` whose factory returns a capability tree holding another `DynamicCapability` offers
  none of the inner one's tools. The inner one's instructions and hooks still reach the run. In the harness the memory
  guidance told the agent to call `global_search_memory` and the other `global_*_memory` functions inside `run_code`, and
  `run_code`'s catalog did not hold them, so every such call failed.
- **Cause:** every `DynamicCapability` contributes a `DynamicToolset` with `per_run_step=False`, whose factory runs only in
  `DynamicToolset.for_run`. The outer `DynamicToolset.for_run` runs its own factory and keeps the toolset it returns, but
  never calls that toolset's `for_run`. The inner `DynamicToolset` is part of that toolset, so its factory never runs, it
  holds no toolset, and its `get_tools` answers `{}`. At the top of an agent, the agent itself calls `for_run` on every
  toolset, through a `CombinedToolset` too, which is why the same capability offers its tools there.
- **Grade:** the model is told to call functions it does not have.
- **Found:** 2026-11-04, reviewing the harness inspector's findings 16, 19, 22 and 24 (one event filed four times). The
  global memory mount was a `DynamicCapability` inside the `CombinedCapability` that the memory plugin's `Switched` wraps,
  and `Switched` is a `DynamicCapability`.
- **What we did:** `memory_plugin_mounts` (`harness/src/harness/features/projects/memory.py`) places the global mount at
  the top of the agent's capabilities, beside the plugin's switch, and the mount reads the plugin's state itself.
  `harness/tests/unit/test_agent_mounts.py` (`TestMemoryReachesTheSandbox`) holds every memory function to `run_code`'s
  catalog.
- **Upstream state:** present in 2.63.0 and 2.66.0. The ask is that `DynamicToolset.for_run` call `for_run` on the
  toolset its factory returns, as the agent does for a toolset at the top.
- **Issue:** not filed.
- **Repro:** `Agent(FunctionModel(answer), capabilities=[DynamicCapability(lambda _ctx: DynamicCapability(lambda _ctx:
  Toolset(FunctionToolset([ping])), id="inner"), id="outer")])` offers the model no tools, while the inner
  `DynamicCapability` alone offers `ping`. Patching `DynamicToolset.for_run` to call `for_run` on the toolset its factory
  returned brings `ping` back.

## UB-17. A join finalized in the same pass as another join fires before that join's new task can feed it

- **Library:** pydantic-graph 2.63.0 (`pydantic_graph/graph_builder.py`, `_GraphIterator`, the per-result loop at lines
  726-740 and `_get_completed_fork_runs` at lines 994-1011).
- **Symptom:** a node waiting on two producers starts with only one of them. In a stance refresh where `write_stances`
  waited on `list_changes` and on the rewrite judge, and the proposals waited on `write_stances`, `write_stances` started
  right after `merge_work`, before any rewrite ran, and failed with `KeyError('check_stances')`. In the plain graph below,
  `tail` runs twice, once with only `src`'s item and once with only `mid`'s, and the end node gets three items.
- **Cause:** after each task result, the loop takes one snapshot of the active tasks and asks
  `_get_completed_fork_runs` for every open join whose fork run no active task still feeds. It then finalizes all of
  them and dispatches their new tasks. When two joins share a fork run and the first one's new task feeds the second,
  the snapshot lacks that task, so the second join counts as complete in the same pass and fires early. The sweep that
  runs once no task is pending already re-reads the live tasks before each join ("An earlier join in this pass may have
  dispatched tasks that feed this join"). The per-result loop does not.
- **Related:** UB-5. Its fix in pydantic-graph 2.49.0 (pydantic/pydantic-ai#8385) made the idle sweep re-read the live
  tasks. This bug is in the per-result loop, which that fix did not cover.
- **Grade:** wrong results. A node runs on a partial input, and runs again later on the rest, with no error from the graph.
- **Found:** 2026-11-04, review of the stance refresh fix round, traced with a wrapper around `_get_completed_fork_runs`
  and `_is_fork_run_completed` on the real graph, then cut down to the graph below.
- **What we did:** the stance refresh avoids the shape. `write_stances` reads `merge_work` instead of `list_changes`.
  `merge_work` already sits upstream of the rewrite judge `write_stances` waits on, so no join of `write_stances` holds
  an item while `merge_work`'s join is finalized (`cafa27f9`).
- **Upstream state:** present in 2.63.0. Checking each finalized join again against the live tasks plus the tasks this
  pass already dispatched, as the sweep does, fixes the graph below and the stance refresh shape. The ask is that fix.
- **Issue:** not filed. Draft I8 in `void/upstream-issues-to-file.md`.
- **Repro:** `src` broadcasts to `b`, `mid_join` and `tail_join`. `b` goes to `mid_join` and `last_join`. `mid_join`
  goes to `mid`, `mid` to `tail_join`, `tail_join` to `tail`, `tail` to `last_join`, `last_join` to `last`, and `last` to
  the end. Each join collects its items into a list. `tail` is handed `['src']`, then `['mid']`, where it should be
  handed `['mid', 'src']` once.

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
- **`CapabilityStore.write` keeps a validated version when new code fails, and `_upsert` is gone** (pydantic-ai-harness
  0.61.0). Code that fails validation over a version that validated replaces nothing: the store keeps `<name>.py` and its
  manifest entry, saves the rejected code as `<name>.rejected.py`, returns a record that is not in the manifest, and tells
  the model the previous version is unchanged. Writes and `disable` hold a lock per directory. `FileCustomCapabilities.write`
  lands a written entry disabled through the store's own `disable`, and leaves a rejected rewrite as the store leaves it:
  the kept version keeps its status and its promotion, and nothing is recorded (`34e13af`). `delete` also removes the
  rejected code (`1dd6f9d`). UB-11 asks for a landing status.
- **`SystemReminders` keeps a reminder in the history as a request-scoped system prompt** (pydantic-ai-harness 0.62.0).
  A dynamic reminder is a `SystemPromptPart(scope='request')` in a request of its own, sent with the request it was made
  for and never again. The skill hint (UE-19) is therefore in each run's messages and in the conversation record, which
  keeps the scope through its JSON form, so a later turn does not send it again. Its test expects it there once
  (`c78d246`).
- **`CodeMode` defaults to `dynamic_catalog=True`** (pydantic-ai-harness 0.63.0). The catalog of sandboxed functions moves
  from `run_code`'s description into the instruction block `toolset:code_mode:catalog`, and a tool revealed mid-run is
  appended to that block instead of rewriting the description. Each choice has its cost. With the default, each
  conversation misses the prompt cache once at the switch, and results stop comparing with runs measured on the
  description. With `dynamic_catalog=False`, a capability load that reveals a sandboxed tool rewrites `run_code`'s
  description, and the prompt cache misses from that request on. The chat agent takes the default (`1dd6f9d`), because
  loading self-authoring reveals six sandboxed functions (`list_custom_capabilities`, `disable_custom_capability`,
  `list_custom_steps`, `list_custom_agents`, `list_custom_profiles` and `list_skills`). A workflow step's worker and a turn
  worker pass `False` (`34e13af`), since they reveal nothing mid-run and the pin costs them nothing. The evaluation agent
  passes `False` (`160cf10`), so its results stay comparable with the filter-first runs. Each choice has a test that
  fails without it.
- **`GraphBuilder` starts broadcast branches in `.to()` order** (pydantic-graph 2.62.0). They used to start reversed. The
  in-process engine's run log is keyed by node id, so a finished node stays finished across a restart, and only the order
  in which parallel branches run changes. The restore test that holds one branch mid-flight holds the right branch, which
  now runs last (`34e13af`). On the Temporal engine, a run inside parallel branches at the restart replays its branch
  activities in the new order. Where the branches run different steps, the activity types differ, Temporal raises a
  non-determinism error, and the run stalls on a workflow task that keeps retrying. Where both run the same activity
  type, which every inline step does (`perform_inline_step`), the default activity ids still match, and replay could hand
  one branch the other branch's recorded result (inferred from Temporal's default ids, not reproduced). The restart guard
  is required, not a recommendation: `GET /api/runs` shows no live or paused Temporal run when the harness restarts onto 2.62 or
  later.
- **Messages dump three new fields** (pydantic-ai 2.61.0 to 2.63.0): `ModelResponse.canonical_system`,
  `TextPart.citations` and `CompactionPart.metadata`, `null` by default, and `ModelRequestParameters` gains
  `instruction_visibility`. Message keys do not change, since they are built from timestamps and part names. The record
  stores a message re-sent under 2.63 as a new form, once per message, and a request's parameters as a new set, which costs
  space, never correctness. A checkpoint committed before the upgrade digests differently after it. On the Temporal
  engine, the next turn of a step conversation that continues from such a checkpoint is claimed, `read_checkpoint`
  recomputes the digest and finds no match, and the `ContinuationUnsafeReplayError` becomes a non-retryable
  `ContinuationRefused` failure: the run fails rather than pausing, and a retry of the same attempt is refused too,
  because the attempt began and never settled. Nothing was moved forward, unlike the 2.60 `retry` field: the in-process
  engine never reads a checkpoint, and the restart guard in the entry above is required.
