---
id: upstream-extensions
aliases: [upstream-extensions]
type: explanation
app: root
status: current
tags: [upstream, pydantic-ai, pydantic-ai-harness, capabilities]
title: "Upstream extensions: the features and capabilities we add over pydantic-ai and pydantic-ai-harness"
---

# What we add over pydantic-ai and pydantic-ai-harness

The list of everything we build that the two libraries do not offer, all of it in our own code in `core/`, `harness/` and `engram/`, over
the published `pydantic-ai-harness` and pydantic-ai's capability hooks. Section A lists what we once carried in a fork of
`pydantic-ai-harness`, and where each piece lives now. Each entry says
what it does, where it lives, and whether it is a candidate to propose upstream (as an issue, per the repo rule). Bugs we found
in the libraries live in [upstream-bugs.md](upstream-bugs.md).

Entry shape: what it does, where it lives, since when, upstream candidate (yes, no, or later) and why.

## A. Additions we once carried in a fork

### UE-1. `FormatMaps`: map-then-slice on known-format tool returns

- **What:** a capability plus `FormatMapsToolset` that recognises the format of a tool's return (diff, JSON, markdown outline by
  heading, directory listing) and hands the model a map of it first, with a `part=` argument to slice a run-scoped cached snapshot
  instead of re-reading the whole payload. `MappedTool` declares which verbs map and `command_starts(...)` gates by command prefix
  (a `when=` gate). `refetch_on_miss` is opt-in. Outline rendering uses `markdown-it-py`, a `core` dependency.
- **Where:** `core/src/core/ai/format_maps/` (`contracts.py`, `capability.py`, `toolset.py`, `domain.py`, `internal_contracts.py`, `infrastructure/file_cache.py`), tests in
  `core/tests/unit/format_maps/`. Consumer: the harness authoring agent maps `read_pyai_docs` pages and `run_command` git diffs.
  Moved out of the fork into `core` on 2026-09-29.
- **Since:** 2026-08-17 to 2026-08-19 (`9b89e919` .. `1a95b03e`). It replaced the earlier `Disclose` band action and `Outline`.
- **Upstream candidate:** yes. It is general (no engram or studio concept inside), sits beside `tool_output_limits` in spirit
  (size discipline on tool returns), and carries its own tests and README.

### UE-2. `CodeMode(compact_catalog=True)`: one line per folded function in the `run_code` catalog

- **What:** a flag on `CodeMode` (default off) that renders each folded tool as `async def name(*, args) -> return  # first
  sentence` instead of a full stub with the whole docstring and every TypedDict block. The sandbox's type stubs stay fully
  detailed, so a wrong call is still refused with an exact message. On our chat agent it took `run_code` from 23.6 KB to 8.3 KB.
- **Status:** removed with the fork on 2026-09-29. Without it the authoring agent's `run_code` description measures 15,567
  bytes, and the owner accepted that size (`TestRunCodeCatalogSize` now allows 20,000). A compact catalog option goes upstream
  as a feature request.
- **Where (in the fork):** `pydantic_ai_harness/code_mode/_toolset.py` (`_render_compact_line`, `_render_catalog`),
  `_capability.py` (the flag), tests in `tests/code_mode/`.
- **Since:** 2026-08-25 (`07a7fa54`. The keyword-only `*` fix is `a147fabf`).
- **Upstream candidate:** yes, as an option beside `dynamic_catalog`. The trade-off (per-argument descriptions and structured
  type fields leave the catalog, the stubs keep them) is worth stating in the issue.

### UE-3. `ToolOutputLimits` carries `tools` through a spill

- **What:** a reduced return keeps `ToolReturn.tools` ([UB-1](upstream-bugs.md)). On pydantic-ai 2.52 that does not make a
  shortened load count as loaded, so the chat agent also keeps `load_capability` out of its output limits.
- **Where:** `core/src/core/ai/tool_output_limits.py`, a subclass that puts `ToolReturn.tools` back after the upstream reduction,
  tests in `core/tests/unit/test_tool_output_limits.py`. Moved out of the fork on 2026-09-29.
- **Since:** 2026-08-25 (`07a7fa54`).
- **Upstream candidate:** yes, as the bug report.

### UE-10. An ACP **client**: driving another agent over the protocol

- **What:** the reverse direction of `experimental.acp`. `ExternalAgent.run(brief, *, cwd, permission, on_update,
  timeout_seconds)` spawns an ACP agent (Claude Code, Codex, Pi), advertises no client capabilities, opens one session at a
  working directory, sends one prompt turn, forwards every `session/update` to a sink, answers `session/request_permission`
  through a `PermissionPolicy` (`AllowOnce` ships as the permissive one), and cancels with `session/cancel` at the timeout.
  `ExternalResult` is the text, the stop reason, and the paths the agent reported touching. The unadvertised client methods
  (`fs/read_text_file`, `fs/write_text_file`, `terminal/*`) answer with a clear error rather than a silent failure.
- **Where:** `harness/src/harness/adapters/acp_client/` (`_external.py`, `_permission.py`, `__init__.py`), moved out of the
  fork on 2026-09-29. Consumer: the harness's `external-agent` plugin (`features/authoring/workers.py`), tests in
  `harness/tests/unit/test_external_agent_client.py` against a hand-written ACP agent subprocess.
- **Since:** 2026-08-26.
- **Upstream candidate:** yes. It is general (no engram or harness concept inside), it completes a protocol the fork already
  implements one half of, and it is what any embedder needs to use an ACP agent as a subagent.

### UE-12. `active_run_id()`: a public read of the run the step store registered

- **What:** `StepPersistence` derives its own run id whenever `agent_name` or an explicit `run_id` is set, registers the run
  under it, and holds it in a private contextvar (`step_persistence._context.current_run_id`) for the run's duration. Upstream
  reads that contextvar only from inside the package, so a capability that writes its own rows against the same run has no way
  to ask for the id: `RunContext.run_id` is a different string. `active_run_id() -> str | None` is a thin read of it.
- **Status:** retired on 2026-09-29. `RequestParametersRecorder` now finds the transcript's run id through
  `RunContext.capabilities`, the lookup upstream documents for compaction (`StepPersistence.compaction_transcript_handle`).
- **Where (in the fork):** `pydantic_ai_harness/step_persistence/_helpers.py`, beside `annotate_tool_effect`,
  which already resolves the active run the same way, and exported from `pydantic_ai_harness.step_persistence`. Consumer:
  `core.ai.history.request_parameters_recorder.RequestParametersRecorder`, which keys every request's parameters row by it (falling back to
  `RunContext.run_id` when no step store is mounted), so `history.step_parameters` joins the transcript on
  `(run_id, step_index)` and satisfies its foreign key on `history.runs`.
- **Since:** 2026-09-07.
- **Upstream candidate:** yes. Any capability recording beside the step store needs the same id, the read is three lines, and
  `annotate_tool_effect` already treats the contextvar as the package's answer to "which run is this".
- **Fork-side test:** none in the standing diff. The accessor is covered by core's
  `test_the_run_id_is_the_one_the_transcript_registered`, and the fork's own test rides the upstream issue this entry names.

### UE-15. `UnnumberedFileSystem`: `read_file` without line numbers

- **What:** pydantic-ai-harness's `FileSystem` with its toolset wrapped, so a `read_file` return keeps its header line and has
  the number taken off each line after it. The strip lives in the wrapper's `call_tool`, below every capability hook, so the
  order capabilities are mounted in does not matter. It also sets `max_read_chars=None`, which removes the new 50,000-character cap on one
  call. A call returns up to 2,000 lines as before and `ToolOutputLimits` spills a long result. The
  authoring agent's two file mounts use it, so spilled file content greps clean.
- **Where:** `core/src/core/ai/filesystem.py`, tests in `core/tests/unit/test_unnumbered_filesystem.py`.
- **Since:** 2026-09-29. Before that it was a `line_numbers` flag patched into the fork's `FileSystem`.
- **Upstream candidate:** yes, as a `line_numbers` option on `FileSystem`.

## B. Our own capabilities over pydantic-ai's hooks

Written in `core/src/core/ai/` (app-agnostic), `harness/src/harness/` (the agent) and `engram/src/engram/evals/` (the evaluation
harness). Listed here because each is a behaviour the libraries do not ship. "Upstream candidate" marks the ones with no engram or studio concept inside.

### UE-4. `EmptyAnswerRetry` (core)

- **What:** when the model's text output is empty although its reasoning is not (seen with `deepseek-v4-pro` under
  `PromptedOutput`), a `ModelRetry` asks it to put the answer in the reply text as the JSON object, instead of the run dying on
  "Exceeded maximum output retries".
- **Where:** `core/src/core/ai/empty_answer.py`, mounted in the harness's `features/authoring/agent.py` with
  `retries={"output": 2}`.
- **Since:** 2026-08-25 (`8c55c5f7`).
- **Upstream candidate:** yes. An output-mode-level retry for "blank text, non-blank reasoning" would help every reasoning model.

### UE-5. `NamedToolDeferral` (harness)

- **What:** retags named tools with a capability id and `defer_loading=True` after they leave their owning capability, so a set
  of tools spread across several capabilities can sit behind one `load_capability` name without rewriting those capabilities.
- **Where:** `harness/src/harness/features/authoring/deferred_tools.py`. Used for the `self-authoring` group.
- **Since:** 2026-08-25 (`07a7fa54`).
- **Upstream candidate:** later. Small, general, but the framework may prefer a first-class "capability group" instead.

### UE-6. `DedupToolResults` (core)

- **What:** replaces a repeated tool return in the history with a pointer to the first occurrence, so the same file or query
  fetched twice costs its tokens once.
- **Where:** `core/src/core/ai/dedup.py`.
- **Since:** 2026-08-19 (`7634c367`, audit fixes `4ec0dd01`).
- **Upstream candidate:** yes.

### UE-7. `ProgressiveDisclosure` and `ResultDisclosure` (core)

- **What:** a per-run token budget over disclosure envelopes (a large result becomes a budget stub past the cap, never a blocked
  call) and the "results teach the next move" rendering of tool results.
- **Where:** `core/src/core/ai/disclosure/capability.py`, `core/src/core/ai/disclosure/result_disclosure.py`.
- **Upstream candidate:** later. Tied to engram's envelope shape today.

### UE-8. `WorkflowContract` (core)

- **What:** the workflow contract as a port over somebody else's library tools: `search_workflows`, `fetch_workflow`,
  `save_workflow` (validated against this run's steps before any write), `adapt_workflow`. The host's own tools are hidden.
- **Where:** `core/src/core/ai/workflows/adapters/contract_port.py`.
- **Upstream candidate:** no. It is the product's own domain.

### UE-9. `PendingCapabilityCreation` mounts the vendor's capability tools under this app's names

- **What:** upstream's `CapabilityCreation` names its three tools `author_capability`, `list_authored_capabilities` and
  `disable_authored_capability`. The subclass mounts them as `write_custom_capability`, `list_custom_capabilities` and
  `disable_custom_capability` by wrapping the vendor's toolset in pydantic-ai's `RenamedToolset`, so the agent sees this
  repository's words and upstream keeps its own. The rename rides `tool_def`, so the code-mode marker travels with the
  write tool and it stays native beside `run_code`.
- **Where:** `harness/src/harness/features/selfauthoring/tools.py` (`_TOOL_NAMES`, `PendingCapabilityCreation.get_toolset`).
- **Upstream candidate:** no. It exists so upstream needs no rename, which is the point.

### UE-11. `RecordHistorySource` keeps both sides of a compaction (harness)

- **What:** upstream's `HistorySource` port documents `run_history` as returning the originals and *excluding* the
  compaction artifacts, and `SnapshotHistorySource` implements that by skipping a summary on its prefix. Ours returns
  both: every message of every step of a run, each counted once, in the order it first appeared, the summary among them.
  "Once" is decided by the record's own `message_key`, so a message pydantic-ai re-stamped between steps (its timestamp,
  run id, conversation id and instructions) is still the one message, while a summary is a message of its own.
- **Where:** `harness/src/harness/adapters/record.py`, over `core.ai.history.message_key`. It is what the chat agent's
  `search_conversation_history` reads, narrowed to the asking session's own conversation.
- **Since:** 2026-09-07.
- **Upstream candidate:** later, and as an issue against the port's contract sentence rather than a patch. A caller asks
  recall both what compaction dropped from the live context and what now stands in its place. A search that hid the
  summary would answer as though the replacement never happened. Whether the port should say "the record, artifacts
  included" is upstream's call. The source itself carries our conversation scoping and stays ours.

### UE-13. The harness's mounts

`NoteOverlay` (owner notes), `Preferences`, `FrictionFlagging` (a friction flag logged from the model's own
note), `Switched` (owner plugins that remove a capability's tools outright), `SandboxDiscipline`, `Corpus`, `WorkflowProcess`,
`RuntimeVoiced`, and the self-authoring set (`AgentCreation`, `LeafCreation`, `ProfileCreation`, `ProfileVoice`). All in
`harness/src/harness/` and `core/src/core/ai/voice.py`. Upstream candidates: no, they carry the product's own concepts. `Switched`
(an owner-controlled on/off plugin over any capability) is the one worth a second look later.

### UE-14. `BudgetedModel` (harness): admission before a request, exact settlement after it

- **What:** a `WrapperModel` that asks a persisted, cross-process budget record before every model request and records
  what the response actually cost afterwards. Upstream's `SpendLimits` capability is the nearest thing and answers a
  different question: it accumulates spend per window in a store, per agent run, and refuses the next request once a
  window is exhausted. Ours is scoped to an evaluation cycle and trial rather than a run, covers four model roles in
  four processes through one record on disk, admits one request at a time across the whole cycle, and settles each
  admission exactly once on both request paths, the plain `request` and the installed
  `request_stream(messages, model_settings, model_request_parameters, run_context=None)`. A stream that ran to the end
  is priced from its own complete response. An early close, a cancellation, an interrupted state, a failed iteration
  and a failure before the provider answered all settle unresolved, once, through a cancellation-shielded settlement.
  An unresolved charge blocks every later admission and survives a restart, because a charge nothing could price is
  not a charge of zero. It also closes the two paths `WrapperModel` forwards unmetered: `count_tokens` (a provider
  round trip whenever a run sets `count_tokens_before_request`) and `compact_messages` (a billed generative request on
  the providers that implement it) both refuse under a budgeted scope, because a coverage manifest cannot see either
  one. Both live inside a model the manifest already counts as budgeted.
- **Where:** `harness/src/harness/features/evals/infrastructure/budgeted_model.py`, over the record and its file adapter in
  `core/src/core/ai/evaluation/domain/budget.py` and `core/src/core/ai/evaluation/infrastructure/file_budget.py`.
- **Since:** 2026-09-16.
- **Upstream candidate:** later, and as an issue rather than a patch. The reusable half is the observation upstream's
  own docstring already makes: a per-window accumulator is a brake on a runaway loop, not an accounting record, and it
  reserves nothing. What a caller measuring spend actually needs is an admission that is persisted before the request
  and a settlement that lands exactly once however a stream ends. Whether that belongs beside `SpendLimits` is
  upstream's call. The evaluation scope, the roles and the coverage manifest are ours and stay here.

### UE-16. `LoopDetection` (harness): a copy of an upstream pull request that never merged

- **What:** a capability that notices an agent stuck repeating itself (the same call again and again, the same failing
  result, two calls alternating, or narration with no call) and asks the model to change approach. The authoring agent
  mounts it with `on_loop="nudge"`.
- **Where:** `harness/src/harness/vendor/loop_detection/`, a verbatim copy of pull request #336 on the old
  pydantic/pydantic-ai-harness repository, taken before that pull request closed.
- **Since:** 2026-08-01, the repository's first commit.
- **Upstream candidate:** already proposed upstream. Pull request #336 closed unmerged on 2026-09-28, when
  pydantic-ai-harness moved into the pydantic-ai repository. The idea continues as pydantic/pydantic-ai#9008 ("Add a
  `DetectLoops` capability to interrupt repeated-action loops"), with #336 linked as its reference implementation.
  Replace this copy with `DetectLoops` once a release ships it.

### UE-17. `PathGuardedShell` (core): a shell that refuses a command naming a denied path

- **What:** pydantic-ai-harness's `Shell` guards a command by its executable, its operators and its environment, and has no path
  guard, while `FileSystem` refuses paths by `denied_patterns`. This capability wraps a `Shell`'s toolset and refuses a
  `run_command`, `start_command` or `shell` call whose command names a path matching a denied pattern, before the command runs.
  A named path is every word, the part of a word after an `=`, and every file a glob word matches, read from the run's
  workspace working directory at call time (`Shell.cwd` is ignored by pydantic-ai-harness 0.52), with links followed and matched the way `FileSystem` matches. The authoring agent's and each delegate's shell mount pass it
  the list of the files plugin beside them.
- **Where:** `core/src/core/ai/shell.py`, tests in `core/tests/unit/test_path_guarded_shell.py`.
- **Since:** 2026-09-30.
- **Upstream candidate:** yes, as a `denied_path_patterns` option on `Shell` beside `denied_commands`, best effort like the
  command checks it sits beside.

### UE-18. `FileSpendStore` (core): spend counters every process on one machine shares, which a restart keeps

- **What:** a `BatchSpendStore` for pydantic-ai-harness's `SpendLimits` that keeps every window's counter and every applied
  `SpendEntry.token` in one JSON file. Each write takes the file's cooperative lock, reloads the document and swaps the whole
  new one in, so every process on one machine counts against the same figures and a restarted process reads what the last one
  counted. A counter past its horizon reads as zero, and a token applied within an hour is not applied again. Upstream ships
  `InMemorySpendStore`, which loses both with its process, and `RedisSpendStore`, which needs a Redis this deployment does not
  run. The harness mounts it on the one `SpendLimits` a session shares between its workflow steps and its chat agent, at
  `spend-counters.json` under the state home.
- **Where:** `core/src/core/ai/spend_store.py`, tests in `core/tests/unit/test_file_spend_store.py`, mounted by
  `_default_spend_limits` in `harness/src/harness/composition.py`.
- **Since:** 2026-10-01.
- **Upstream candidate:** later. A file binds the processes of one machine only, and upstream already names a shared store
  as the answer across machines. A database store beside `RedisSpendStore` is the shape an issue would propose.

### UE-19. `SkillHint` (core): a dynamic reminder naming the skill a decision model picks

- **What:** a `SystemReminders` dynamic reminder that asks a decision model which mounted skill a run's prompt needs, on the
  run's first model request only, and names it in one line when the averaged probability clears a threshold. It is never
  saved to the history. A missing prompt, no unloaded skill, a "(none)" pick, a weak pick or a failed model all answer nothing.
- **Where:** `core/src/core/ai/skill_hint.py`, tests in `core/tests/unit/test_skill_hint.py`, mounted on the harness chat
  agent by `harness/src/harness/features/authoring/skill_hint.py`.
- **Since:** 2026-10-06.
- **Upstream candidate:** later, once the evals show the hint helps.

### UE-20. Permutation self-consistency for decision model pick-one questions (core)

- **What:** `DecisionModelQuestions.pick` asks one `choice` question in several rotated option orders, one request each, and
  averages each option's probability across them. It counters the option-order sensitivity measured on Jev in
  pydantic/pydantic-ai#8472. A tie goes to the option listed first.
- **Where:** `core/src/core/ai/decision_models.py`, tests in `core/tests/unit/test_decision_models.py`.
- **Since:** 2026-10-06.
- **Upstream candidate:** yes, as a decision model setting. Drafted as I5 in the owner's issue drafts.

### UE-21. `FakeDecisionModel` (core): a scripted decision model for tests

- **What:** a `DecisionModel` that answers each yes/no and pick-one question from a scripted function, records every request
  it receives, and can raise an error or hold each request back for a delay. `TestModel` and `FunctionModel` have no
  decision model counterpart. Like any `DecisionModel`, it obeys `ALLOW_MODEL_REQUESTS`.
- **Where:** `core/src/core/testing/fakes.py`, used by `core/tests/unit/test_decision_models.py`,
  `core/tests/unit/test_skill_hint.py` and `engram/tests/unit/engram/retrieval/test_rerank.py`.
- **Since:** 2026-10-06.
- **Upstream candidate:** yes, as a decision model test double. Drafted as I6 in the owner's issue drafts.

### UE-22. `ModelCallsCodeMode` (engram): a `run_code` program's tool calls stay off the run's tool-call limit

- **What:** pydantic-ai-harness's `CodeMode` runs a program's tool calls through a nested `ToolManager` built on the
  `run_code` call's `RunContext`, so every one that succeeds is recorded on the run's `RunUsage.tool_calls`. pydantic-ai
  checks `UsageLimits.tool_calls_limit` against that count before the model's next tool calls, so a run whose limit holds
  without code mode fails with code mode as soon as one program loops over a few tools. Since 0.59.0 the harness also
  checks each of a program's calls against the run's `tool_calls_limit` before it runs, and documents that a program's
  calls count toward it. The harness already bounds a program's calls with its own `max_tool_calls` (100 per `run_code`
  call). Our subclass wraps code mode's toolset and hands each `run_code` call a fresh `RunUsage` and the run's
  `UsageLimits` without `tool_calls_limit`, so the run's count and its limit hold each `run_code` call and each direct
  call once, the way they count calls without code mode. Calls a program starts while the model is still writing it
  (`eager`, `speculate`) still count. Found when a code-mode evaluation variant failed 83 of 400 cases on a limit of 20
  that its twin without code mode failed 20 times.
- **Where:** `engram/src/engram/evals/harness/code_mode.py`, mounted by `build_agent` in
  `engram/src/engram/evals/harness/agent.py` on every code-mode evaluation run, tests in
  `engram/tests/unit/engram/evals/test_runner.py` (`TestCodeModeToolCallBudget`). It lives in engram, which installs the
  `pydantic-ai-harness[code-mode]` extra, and `core` does not.
- **Since:** 2026-10-16.
- **Upstream candidate:** yes, as an issue rather than a patch: give `CodeMode` an option that keeps a program's calls
  on a usage of their own and off the run's `tool_calls_limit` (0.59.0 documents that they count, and offers no way out).
  The setting a caller compares runs under should mean the same thing with code mode on and off.

### UE-23. `NoteMemory` (harness): memory notes in category folders, searched across every folder

- **What:** pydantic-ai-harness's `Memory` keeps flat files directly in a scope. `NoteMemory` keeps each note at
  `<category>/<slug>.md` inside its scope (categories world, project and reference, from `engram.agent_memory`), names a
  note by `category` and `slug` or by that relative path, and links every note in the `MEMORY.md` it injects. Since 0.60.0
  the bundled stores search only the files directly under the prefix they are given and skip a deeper folder as another
  scope. `search_memory` therefore makes one search on engram's agent-memory store, which ranks every category folder
  in one ranked search and searches its inner store folder by folder when it has no ranking, and searches folder by
  folder on any other store (`engram.agent_memory.search_categories`: each category folder at once, the matches ranked
  together and cut to one search's result count and characters).
- **Where:** `harness/src/harness/features/projects/memory.py` (`NoteToolset`, `NoteMemory`, `memory_mounts`),
  `engram/src/engram/agent_memory/service.py` (`search_categories`, `EngramMemoryStore.search`), tests in
  `harness/tests/unit/features/projects/test_memory.py` and `engram/tests/unit/engram/agent_memory/test_store.py`.
- **Since:** the category grammar since the memory mounts landed, the search across category folders since 2026-10-23.
- **Upstream candidate:** later. A store protocol that tells a subfolder of one scope from a nested scope would let a
  note grammar with folders search in one call on every store.

### UE-24. TypeSafe's Jev through the gateway (core)

- **What:** pydantic-ai's `gateway_provider` builds OpenAI, Groq, Anthropic, Bedrock and Google upstreams and refuses
  `typesafe` ("Unknown upstream provider: typesafe"), so `gateway/typesafe:jev-latest` does not build either.
  `decision_model` builds pydantic-ai's own `TypeSafeModel` on a `TypeSafeProvider` whose base URL is the gateway base URL
  joined with the gateway's TypeSafe route (`infrastructure.gateway.typesafe_route`, `type-safe` by default) and whose key
  is the gateway key. The TypeSafe SDK appends `/v1/systemone` itself, so the base URL carries no `/v1`.
- **Where:** `core/src/core/ai/decision_models.py`, tests in `core/tests/unit/test_decision_models.py`.
- **Since:** 2026-10-29.
- **Upstream candidate:** yes, as an issue: a `typesafe` upstream in `gateway_provider`, so `gateway/typesafe:<model>`
  reaches Jev on the gateway's TypeSafe route like any other gateway model.
