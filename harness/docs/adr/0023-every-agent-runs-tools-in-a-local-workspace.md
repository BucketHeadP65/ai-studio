---
id: harness-adr-0023-every-agent-runs-tools-in-a-local-workspace
type: adr
title: Every agent that holds a shell, file tools or skills runs in a local workspace with an allow-listed environment
summary: pydantic-ai 2.52 and pydantic-ai-harness 0.52 run tools through a run workspace, so every agent that holds a shell, file tools or skills runs in a LocalWorkspace, commands see a short list of environment variables, and the path guard and the unnumbered reader move onto the new API.
status: accepted
aliases:
- harness-adr-0023-every-agent-runs-tools-in-a-local-workspace
app: harness
created: '2026-09-30'
---

## Context

pydantic-ai-harness now ships with pydantic-ai and versions with it. Version 0.52 pins pydantic-ai 2.52. Its capabilities work through a run workspace, and six things broke for us.

F1. `Shell`, `FileSystem`, `Skills`, `CapabilityCreation` and `Memory` refuse to start a run that has no workspace.

F2. A local workspace passes commands only `PATH`, `HOME`, `LANG`, `LC_ALL` and `LC_CTYPE`, plus what the caller adds. `Shell.cwd` is ignored, so the path guard that read it broke.

F3. `FileSystem.read_file` formats its line numbers in a function a subclass cannot override, so `UnnumberedFileSystem` broke. `max_read_chars` also defaults to 50,000.

F4. The private skill loader we imported is gone. `parse_skill` replaces it, still private.

F5. A child run of `SubAgents` always gets the parent run's workspace.

F6. Every model response records its workspace (`workspace_ref`), and a local workspace refuses a ref for another directory, which fails the run.

## Decision

A1. Every agent that holds a shell, file tools or skills runs in a `LocalWorkspace`, mounted as a capability. The chat agent, over HTTP and ACP, mounts `LocalWorkspace(WORKSPACE, env=<allow-list>)`, writable. A delegate has no workspace of its own and runs in its parent's. The agent-script tier workers mount their own `LocalWorkspace` on the same root with the allow-list, because the `agent-scripts` plugin starts them through `DynamicWorkflow`, which gives them none. A step agent gets `LocalWorkspace(CHECKOUT)` only when it holds skills or a shell: read-only with only skills, writable with a shell. Other step agents mount none, and neither do the inspection and evals agents. The document rule covers a stored agent document, which `validate_agent_document` checks when the agent library writes it, and it refuses a capability that supplies a workspace. An inline agent document is not checked by it.

A2. Every agent's commands see only `TMPDIR`, `SSH_AUTH_SOCK`, `USER` and `LOGNAME` on top of the workspace's own variables. That includes step shells, which used to pass the whole process environment. A step shell also carries `HARNESS_CONSOLE_CLOSED`. `Shell.denied_env_patterns` stays as a second guard.

A3. The path guard stays a wrapper around the shell's tools. It reads relative paths from the run's workspace working directory at call time and forwards the wrapped shell's `before_run` and `wrap_run`. Every rule from the earlier guard still holds.

A4. `UnnumberedFileSystem` strips line numbers in its files toolset wrapper's `call_tool`, below every capability hook, so mount order does not matter. It sets `max_read_chars=None`, which removes the new 50,000-character cap on one `read_file` call. A call returns up to 2,000 lines as before (`max_read_lines`), and `ToolOutputLimits` spills a long result.

A5. A delegate's file tools keep `root_dir=None`, and `allowed_patterns` confine them to the checkout. The patterns are the checkout's path relative to the workspace, glob-escaped, and that path plus `/**`. The bare path is the first one, so a listing shows the checkout. When the checkout is the workspace itself, the one pattern is `**`. The deny list still wins. When the checkout is not inside the workspace, the delegate gets no file tools. Its commands start in `WORKSPACE`, and its instructions tell it to `cd` into the checkout.

A6. Skills load through `parse_skill`. The import is private, so it is recorded as technical debt beside the private `MontyExecutor` import.

A7. `core.ai.history.conversation_record.messages_of` clears `workspace_ref` on every response it reads from the record.

A8. Deprecations stay errors. Only the harness suite fails on `HarnessDeprecationWarning`. The core, engram, studio and connectors suites show other deprecations once per module and do not fail on them. Pyright sets `reportDeprecated = "error"` in every project.

## Consequences

Commands see only the allow-listed environment. A tool that needs another variable fails until the variable joins the list by name. A delegate's commands start in `WORKSPACE`, by default the checkout's parent folder (`APP_WORKSPACE` changes it). A file read returns up to 2,000 lines as before, and `ToolOutputLimits` spills a long result. A conversation continues after a workspace moved. `start_command` writes each background job's files under `<working directory>/.pydantic-ai-harness/shell/<id>/`. pydantic-ai-harness writes a `.gitignore` holding `*` into `.pydantic-ai-harness/`, so git ignores it. A job's folder is removed only when the job is stopped, so a job that finishes on its own leaves its folder behind. A step agent gets a workspace only when it holds skills or a shell, so a promoted custom capability built on `Shell`, `FileSystem` or `Memory` would fail such a step at run start. pydantic-ai 2.52 renamed the model profile flag `openai_supports_tool_choice_required` to `ModelProfile(supports_forced_tool_choice=...)`, and `harness/src/harness/adapters/models.py` sets the new one. The next pydantic-ai-harness upgrade may move the two private imports again, and the gates catch it.

## Options considered

A workspace wrapper as the path guard's home. Lost, because its `run` receives harness-built scripts and the harness's own `rg`, `wc` and `readlink` calls, so it cannot tell the model's command from the tools' own.

Passing a workspace on every run call. Lost, because there are many call sites and one missed call fails a run.

Staying on 0.36. Lost, because the harness now ships with pydantic-ai, and 0.36 gets no fixes.

A line-number strip in a capability hook. Lost, because it depends on mount order and let `ToolOutputLimits` spill numbered text.
