---
id: harness-reference-sessions
aliases: ["harness-reference-sessions"]
type: reference
app: harness
status: current
tags: [sessions, pool, projection, api, projects]
title: "sessions, the parallel-session pool, its projection, and its HTTP contract"
---

# sessions, the parallel-session pool, its projection, and its HTTP contract

Sessions are parallel: many can be answering at once, each with its own conversation, draft, and active run, and viewing one
never changes which others are still working. The :8770 process holds a **registry of runtimes keyed by session id**
(`harness.session.SessionPool`) over the one agent it built. See [ADR 0004](../adr/0004-parallel-sessions-workspace-pool.md).
The session log and the record are canonical: the Postgres projection is a read model rebuilt from them. See
[ADR 0003](../adr/0003-rebuildable-session-projection.md) and [ADR 0018](../adr/0018-one-record-for-every-session.md).

## The pool (`harness.session.SessionPool`)

`create(project_root)` mints a session id, opens (or reuses) that project's workspace, and records `SessionStarted` through
the new runtime's own session log before handing it back. The session exists on its timeline before the first turn that
belongs to it does. `get(session_id)` returns the runtime if the pool already holds it, else **hydrates** it: rebuilds the
runtime from the project's stored session log and transcript (`session_spans` filtered to that id, `continue_session`
replaying its own run ids), raising `KeyError` when no span on that timeline names the id (a caller's 404). Two requests
racing on the same
unhydrated session settle on one hydration lock keyed by session id, so they end up sharing one runtime rather than each
building its own.

**One turn per session.** `begin_turn(session_id)` (and `begin_default_turn()` for the process's own session) takes that
session's `asyncio.Lock` or raises `SessionBusyError`. It never queues. `TurnHold` is the object a caller holds for the
turn's whole lifetime rather than just the request handler: `driven_by(task)` names the task actually doing the work (the
one iterating the streamed response body), `cancel()` cancels it, and `release()` gives the session back, idempotently,
however the turn ends. `interrupt(session_id)` cancels the held turn's task and releases the slot immediately, without
waiting for the cancelled turn to unwind. The very next message into that session is admitted at once. The cancelled
turn's own teardown records it `turn_abandoned` on the session log. `is_answering(session_id)` reports whether that session's
lock is currently held. `False` for a session the pool is not holding in memory, since nobody is mid-turn on a runtime
that has not been hydrated.

**Eviction.** `evict_idle(older_than_minutes=30)` drops every pooled runtime nobody has touched for that long, except one
holding its turn lock and except the process's own default session, which is never evicted. A dropped runtime costs its
next caller one hydration and nothing else. The conversation rebuilds from the transcript its own session log names.

## The stamped session-log events

Turn events carry their session id directly rather than relying on where they sit in the file, because sessions answer in
parallel and interleave: `TurnStarted.session_id`, `TurnSettled.started_seq`, and `TurnFailed.started_seq` (the seq of the
`turn_started` record each terminal event closes) are optional fields, `None` on a line written before a turn carried its
own stamp. `ConversationForgotten.session_id` is stamped the same way. `session_spans` and `live_session` prefer a stamp
when a line carries one and otherwise fall back to a positional walk, whichever session a boundary event (`session_started`
/ `session_switched` / an unstamped `conversation_forgotten`) most recently named, only for the legacy prefix of a session
log written before this shape existed. `open_turns` carries no session identity at all: a stamped settle or failure closes
exactly the `turn_started` record it names, and an unstamped one closes whichever turn is newest off a plain LIFO stack,
since turns were sequential in the app that wrote it. Either way an old session log still decodes and folds exactly as it
always did. `session_spans` assigns a deterministic legacy id (`uuid5` over the span's first `seq`) to any span that
predates the identity events entirely, so a rebuild gives it the same id every time. See
[the session log reference](session-journal.md) for the full event grammar and the codec.

## Per-project state homes

Every project a session opens against owns `<root>/.aifactory/state/` (`harness.workspace.open_workspace`): its own
`session-log.jsonl`, created on first open and gitignored via `<root>/.aifactory/.gitignore`. Every session's
conversation lives in the one record, told apart by the conversation id its runs carry. Beyond the messages, the
record also keeps what every request was sent with: `RequestParametersRecorder` writes each request's tool
definitions and model settings into `request_parameters` (one row per distinct set) and `step_parameters` (one row
per step, pointing at the set that step used), keyed by the run the transcript itself registered. The harness
checkout keeps its own separate, standing home at `.state/`. It is not a
project of its own. Opening a second state home under it would split one timeline in two. A `Runtime.session_log` is a
`SessionEvents` (one method, `record`): a pooled session's points at its workspace's session log, the checkout's own
session's points at the `.state/` file, and every emitter (including the friction-flag tool) writes through
`ctx.deps.session_log`, never a module-level path, so a capability's tool closure narrates into whichever session is actually
running it.

## The projection: tables and rebuild semantics

Five tables in the `aifactory_sessions` database (`projects`, `sessions`, `turns`, `messages`, `flags`) created
idempotently by `ensure_schema` (every statement is its own `CREATE ... IF NOT EXISTS`. No migration chain owns them.)
They are a pure projection: every durable session fact lands as a log entry first, and the projection is rebuildable from
the session log and transcript alone, which the process opens as one store. `rebuild(pool, events, store, project_root=...)` truncates all five tables and
re-derives every row for one project from a clean slate. `refresh_live(events, store, project_root=...,
only_session_id=...)` upserts one session's own rows and nothing else, cheap enough to run, fire-and-forget, after every
settle. A projection write failure warns and never breaks the turn it was narrating.

`harness sessions rebuild` sweeps every project the project registry names (plus the checkout's own `.state/` home),
truncates the five tables once, then reprojects every session each project's session log still remembers. It prints the
resulting `sessions=… turns=… flags=…` counts. The boot sweep runs the same pass without the truncate, so it only adds
and updates rows for sessions still on a session log somewhere.

`answering` is never a stored column. Every read computes it fresh from the pool's own lock state, because it describes
this instant, not the session log's history. `title` is a rename if the session log recorded one (`SessionRenamed`,
latest wins), else the first user-prompt excerpt from the session's own runs, else a short-id fallback. Search (`q` on
`GET /api/sessions`) ranks sessions whose messages match a `plainto_tsquery` over `messages.text_content`, joined through
`turns.session_id`.

## The session-scoped API

All on `:8770`, in `features/sessions/controller.py`, `features/authoring/controller.py`, and `features/running/controller.py`:

```
POST   /api/sessions                {"project_id"?: str}         -> {"session": SessionSummary}
GET    /api/sessions?project_id=&archived=false&q=               -> {"sessions": [SessionSummary]}
GET    /api/sessions/{id}                                        -> {"session": SessionSummary, "turns": [TurnView]}
GET    /api/sessions/{id}/transcript                             -> {"messages": [TranscriptMessage]}
GET    /api/sessions/{id}/conversation                    -> {"turns": [...], "draft": {...}, "issues": [...], "valid": bool,
                                                                "graph": {...}|null}   # draft's own fields below
POST   /api/sessions/{id}/messages   {"trigger": "submit-message", "id": str, "messages": [UIMessage],
                                      "context"?: PageContext, "tool_results"?: [ToolResultIn]}
                                                                 -> Vercel AI data stream | 404 | 409 (mid-turn, or a
                                                                    part-answered continuation)
DELETE /api/sessions/{id}/conversation                            -> {"ok": true} | 404, forgets the conversation in place
GET    /api/master                                                -> {"session_id": str}, the standing master session, minted lazily
POST   /api/sessions/{id}/interrupt                               -> {"ok": true} | 409 (nothing in flight) | 404
PATCH  /api/sessions/{id}   {"title"?: str, "archived"?: bool}   -> {"session": SessionSummary} | 404
DELETE /api/sessions/{id}                                         -> 204 | 409 (this session is answering) | 404
POST   /api/sessions/{id}/run       {"workflow_id", "arguments"} -> as /api/run, scoped | 409 (a run is already active here) | 404
POST   /api/sessions/{id}/run/pause                               -> as /api/run/pause, scoped | 404
POST   /api/sessions/{id}/run/resume    {"spec_yaml"?: str}      -> as /api/run/resume, scoped | 404
GET    /api/sessions/{id}/run/state?since=                       -> as /api/run/state, scoped | 404
POST   /api/sessions/{id}/decide        {"approve": bool}        -> as /api/decide, scoped | 404
GET    /api/runs                                                  -> {"runs": [RunView]}, one card per session with a live run
GET    /api/projects                                              -> {"projects": [ProjectView]}
POST   /api/projects        {"root_path": str}                  -> {"project": ProjectView} | 400 (not a directory)
PATCH  /api/projects/{id}   {"name": str}                        -> {"project": ProjectView} | 404
DELETE /api/projects/{id}                                         -> 204 | 409 (a session of this project is answering) | 404
GET    /api/flags?target_kind=&target=&session_id=               -> {"flags": [FlagView]}
```

After `DELETE /api/sessions/{id}/conversation`, the session row, its `turns`, and its transcript describe only the
conversation since that Forget: `turn_count`, `usage`, and `cost_usd` reset to what has happened since, and a derived
title follows the new conversation's first message (an explicit rename stays), while the session log and the transcript
keep every run before it, untouched and still on disk. Kind, archived state, and `started_at` are never touched by a
Forget.

`GET /api/sessions/{id}/conversation`'s `"draft"` is the authoring `Draft` model dumped as JSON: `spec_yaml`, `title`,
`summary`, `applicability`, and `lineage` (the typed diff it descends from, or `null`), re-derived so a page refresh shows
the same proposal, verdict, and graph the inspector already had.

The agent's own reply on either chat plugin is prose text, not a structured envelope. A draft reaches that `"draft"`
field when the agent calls `check_draft(spec_yaml, title, summary, applicability)` (the three card fields are each
optional, and an omitted one leaves what the draft already holds), and when `POST /api/adapt` puts an adaptation of a
stored workflow there. A passing check settles those fields onto the draft, and the reply names the result in prose. A
failing check reports the issues in prose too, and leaves the draft as it was.

`context` on either chat plugin (`PageContext` in `domain/page_context.py`: `url`, `page`, `ids`, `tab`, `trail`,
`extras`, `targets`) is rendered server-side as a `<page-context>` fenced preamble on the turn's user prompt (never the
system block, which would break the provider's prompt-cache prefix), capped at 4,000 characters, extras shed first. A
target is a named thing on the page the companion can act on (an input to prefill, a tab group to switch), declared by
the page itself. Sessions of kind `master` open the block with a standing role line. The fence is stripped wherever the
person's own words are displayed or derived: the conversation panel's turns, the transcript's user bubbles, and the
projection's title excerpt.

A session of kind `master` mounts the follow-me plugin, and only while the plugin itself is open. Every other kind never
mounts it, whatever the plugin's own setting. The plugin carries two kinds of tool: four **verbs** (`navigate`, `point`,
`prefill`, `set_tab`) the browser performs, never the server, and twelve **console acts** (`set_model`, `select_profile`,
`toggle_plugin`, `run_control`, `decide_gate`, `schedule_workflow`, `update_schedule`, `delete_schedule`, `run_schedule_now`,
`archive_workflow`, `restore_workflow`, `redescribe_workflow`) that run on the server but only once the person approves
each one. The owner's own gestures are never mounted while the plugin is open: `activate`, `promote`, `demote`, `approve`,
`import_path`, and `save_workflow`. A turn that calls either kind ends deferred rather than answering: it emits no
`data-settle` chunk, and an act awaiting the person's approval emits a `tool-approval-request` chunk instead. The browser
answers on the next request, in `tool_results`, and that continuation is its own turn, its own `turn_started`, carrying
a fresh `context` for whichever page the verbs moved it to. A continuation answers every call the turn stopped for or
none of them: one carrying only some of the answers is refused with 409 before it becomes a turn at all, because a run
resumed half-answered fails inside itself and words the failure for whoever wrote the caller. Typing a new message
instead has the browser deny everything still waiting first, so the person is never trapped behind a card.
Every model response the turn makes also puts one `data-step` chunk on the wire ahead of whatever the turn produces next
(`core.ai.history.step_usage`, a usage tap wrapped around the session's model for the turn): the tool calls that
response made, its four token counts, and its timestamp, so the browser reads a turn's spend per step as it happens
rather than after the session list refreshes at settle.

Response models (pydantic, `features/sessions/api_models.py`):

```python
class UsageTotals(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


class ContextVitals(BaseModel):
    used_tokens: int
    window_tokens: int
    fraction: float
    resolved: bool  # False when the window is the fallback guess, not the model's own


class SessionSummary(BaseModel):
    id: str
    project_id: str
    title: str
    started_at: datetime
    last_activity_at: datetime
    archived: bool
    answering: bool  # a turn is in flight in this session right now -- computed from the pool, never stored
    turn_count: int
    usage: UsageTotals
    kind: Literal["chat", "runner", "master"]  # authoring, unattended workflow driver, or the dashboard's follow-me companion
    cost_usd: Decimal  # priced total across the session's turns; a decimal string on the wire
    context: ContextVitals | None  # the session's own history anchored on the model's reported usage, or the live gauge while
    # a turn is running; None when this process does not hold the session


class TurnView(BaseModel):
    seq: int  # session-log seq of the turn_started event
    run_id: str | None  # transcript the turn wrote, when it settled
    entry: str  # spa or acp
    started_at: datetime
    settled_at: datetime | None
    outcome: str  # settled, failed, abandoned, or open
    usage: UsageTotals


class TranscriptMessage(BaseModel):
    run_id: str
    idx: int
    kind: str  # request or response
    payload: JsonValue  # the raw ModelMessage, JSON-shaped
    text: str  # extracted text for display and search


class FlagView(BaseModel):
    id: str
    session_id: str  # the subject the flag is about
    target_kind: str  # tool, skill, capability, workflow, agent, instructions, or self
    target: str
    note: str
    tool_call_id: str | None
    source: str  # live
    created_at: datetime


class ProjectView(BaseModel):
    id: str
    name: str
    root_path: str
    missing: bool  # whether the root path no longer exists on disk, checked fresh at read time
    running_sessions: int  # sessions of this project with a turn in flight right now
```

`RunView` (`features/running/controller.py`, `dict` shape): `session_id`, `workflow_id`, `run_title` (what the launch
called this run, `""` when it named none, never the session's own title), `state` (`"running" | "paused" | "waiting"`),
`active`/`done`/`failed` (step-id lists from the run's own progress), `waiting_at` and `gate` (the gate payload) when
waiting. A run belongs to the session that started it, and its gate is answered in that same session: the
unscoped `/api/decide` and the scoped `/api/sessions/{id}/decide` share one handler body, so a run behaves identically
whichever plugin reached it.

## The project lifecycle and the project registry

An append-only file at `<checkout>/.state/projects-log.jsonl`, separate from any session's own session log, with two
additive kinds: `project_registered {project_id, root_path}` and `project_unregistered {project_id}`
(`features/sessions/project_registry.py`). `register(root, project_id=...)` opens the workspace, drops a default `{}`
`.aifactory/config.json` when one is not already there, and records `ProjectRegistered`, idempotent on root.
`unregister(project_id)` only records `ProjectUnregistered`. A project's files and every session it ever projected keep
their rows, so a listing offers only what the project registry currently names while history is never lost. The harness
checkout auto-registers itself at boot (`project_registry.boot`), keeping its own `.state/` home rather than opening a project
workspace. `project_id` is deterministic: `uuid5` over the resolved root path
(`features/sessions/service.project_id_of`), so the same folder always resolves to the same id across restarts and across
`register`/`sync`. The projection's `projects` table reconciles the project registry's rows with the checkout's own
auto-derived row on every `GET /api/projects`. A project the project registry no longer names keeps its row for history,
never removed by a reconcile.

## `.aifactory/config.json`

Read fresh on every use (`features/sessions/project_config.project_config`), never cached. Absent or unreadable falls back
to all defaults rather than refusing the project. Three groups:

```python
class CompactionConfig(BaseModel):
    strategy: Literal["tiered", "provider-native"] = "tiered"     # which compaction capability the agent build mounts
    target_tokens: int = 120_000                                   # compact down to this size when a pass triggers
    summarizer_model: str | None = None                            # None rides the agent's own model
    recall_max_matches: int = 3                                    # result cap for search_conversation_history


class LoadoutConfig(BaseModel):
    profile: str | None = None   # selected at boot only when no profile is already selected
    model: str | None = None     # sets the orchestrator tier at boot, only when it carries no stored entry


class ProjectConfig(BaseModel):
    name: str | None = None            # display name; None falls back to the folder name
    skills: tuple[str, ...] = ()       # skill-library directories, relative to the project root, mounted alongside SKILLS
    compaction: CompactionConfig
    setup: SetupConfig
```

`strategy="provider-native"` mounts `AnthropicCompaction(token_threshold=target_tokens)`. `"tiered"` mounts `TieredCompaction`
built from the same target. `ReportContextUsage` rides right after the compaction capability either way, and what it reads
back lands on `Runtime.context_usage`. `recall_max_matches` bounds `ConversationSearch`'s `search_conversation_history` tool.
The setup applies at process boot, respecting whatever an owner already set by hand: a profile already selected or an
orchestrator tier already stored is never overridden by the project's config, and an unusable name warns rather than
failing the boot. See [model-tiers](model-tiers.md) for the tier store `model` sets and the stand-aside rule it follows.

## Friction flags

`flag_friction` (`features/sessions/flags.FrictionFlagging`) is the agent's own tool for reporting something that ground
against it: a `target_kind` (`tool`, `skill`, `capability`, `workflow`, `agent`, `instructions`, or `self`), the named
`target`, and a `note`. Every flag is a `FrictionFlagged` event on a session log: a **live** flag (`source="live"`,
mounted on the chat agent) is stamped with the filing session's own id and lands in its own session log.

## The default-session shim (planned removal)

Before session-scoped routes existed, every plugin acted on the process's own session (`session.current()`). The
following unscoped endpoints still bind to that one session so the pre-parallel AI Factory Studio page keeps working.
Most reuse the very handler body their session-scoped sibling above already has:

```
GET    /api/conversation  -> GET  /api/sessions/{id}/conversation   (the scoped route also carries the draft, see above)
POST   /api/run           -> POST /api/sessions/{id}/run
POST   /api/run/pause     -> POST /api/sessions/{id}/run/pause
POST   /api/run/resume    -> POST /api/sessions/{id}/run/resume
GET    /api/run/state     -> GET  /api/sessions/{id}/run/state
POST   /api/decide        -> POST /api/sessions/{id}/decide
DELETE /api/conversation  -> DELETE /api/sessions/{id}/conversation
```

Chat has no unscoped entry any more: `POST /api/sessions/{id}/messages` is the one path, serving both the studio's own
chat rail and the control-room drawer. Four have no scoped counterpart at all, and stay default-session-only until
S2's cutover decides their fate: `POST /api/adapt`, `POST /api/conversation` (starts the process's own conversation, while the
read never mints it), `POST /api/save`, and `GET /api/pending`. `GET /api/library`, `GET
/api/workflow/{id}`, and `GET /api/steps` (`features/browsing/controller.py`) are library-level rather than
session-bound, and stay regardless of which shape the chat plugin takes.

The unscoped run and decide endpoints and their session-scoped siblings share one turn lock: `begin_default_turn()` on
the pool takes the very same lock `begin_turn(session_id)` takes for the default session's own id, so a scoped turn and an
unscoped one can never both answer into that one conversation. Once the client cuts over fully to the session-scoped
contract, every shim endpoint with a scoped sibling above is deleted outright. The three without one either gain a
sibling first or are dropped instead, a call the cutover has not made yet.
