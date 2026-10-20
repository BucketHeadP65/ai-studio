"""The one workflow plugin: the library's tools, the draft validator, the runner, and their instructions.

Everything about workflows reaches the agent through `WorkflowProcess`: the library's contract
tools, `check_draft`, the run and schedule tools, and one instruction block. The block holds the
engine's process instructions, this app's own lines, the live step inventory, then the library's
usage note. The spec grammar lives in the `saving-a-workflow` skill, never in this block. The
inventory lists step names only, and `check_draft` answers how a draft stands against them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import yaml

from core.ai.workflows.contracts import (
    Workflow,
    WorkflowArguments,
    WorkflowIntent,
    intent_issues,
    prepare,
    process_instructions,
    referenced_steps,
    validate,
)
from pydantic import ValidationError

# Runtime imports, not TYPE_CHECKING: `FunctionToolset.add_function` evaluates the tool functions'
# annotations at registration, so under TYPE_CHECKING the names are gone at boot.
from pydantic_ai import FunctionToolset, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset, FilteredToolset

from harness.domain.contracts import DraftReadiness, GateMode, WorkflowPresentationPatch
from harness.features.authoring.service import draft_readiness, settled_draft
from harness.features.running.contracts import (
    DEFAULT_SINCE_HOURS,
    RunCollaborators,
    Scope,
    SpendReporter,
    check_runs,
    message_worker,
    recent_runs,
    run_values,
    run_workflow,
    spend_report,
    stop_run,
)
from harness.features.scheduling.contracts import delete_schedule, run_schedule_now, schedule_workflow, schedules, update_schedule
from harness.rendering import parallel_groups
from harness.session import Runtime, draft_path, remember_draft

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from core.ai.workflows.contracts import StepRegistry, WorkflowContract
    from core.cqrs.contracts import TopicSink
    from pydantic_ai.tools import ToolDefinition

    from harness.domain.contracts import KnowledgeBaseNames
    from harness.features.running.contracts import RunRecords
    from harness.features.scheduling.contracts import Scheduler
    from harness.session import PooledSessions
    from harness.settings import RunSettings

CHECK_TOOL = "check_draft"
RUN_TOOL = "run_workflow"

STARTS_WORK: frozenset[str] = frozenset({RUN_TOOL, "schedule_workflow", "update_schedule", "delete_schedule", "run_schedule_now"})
"""The verbs that start a run or change standing work, as opposed to reading or drafting.

A turn that asked for a design is not offered any of them. The reads stay (`check_runs`,
`schedules`, `recent_runs`, `run_values`, `spend_report`), and so does `stop_run`, which only ends
something already going."""


def _not_while_designing(ctx: RunContext[Runtime], tool: ToolDefinition) -> bool:
    """Whether `tool` is offered this turn: everything in `STARTS_WORK` is withheld from a session's design turn."""
    designing = isinstance(ctx.deps, Runtime) and ctx.deps.design_only
    return not (designing and tool.name in STARTS_WORK)


# What only this app can say about the process: its skill, its house style for specs and its
# panels. `process_instructions` says what is true of the process wherever it runs.
_LOCAL_VOICE = (
    "- A step that must act asks for the shell. There is no separate publish verb. A publish "
    "step writes over its file.\n"
    "- The grammar itself is a skill, not something to remember. `saving-a-workflow` has the "
    "YAML, how to define a step, every validator finding you will hit and what it means. Load it "
    "before you draft: writing a workflow from memory is how a wrong `like` reference costs three "
    "round trips.\n"
    "- Write workflows that read like the ones already in the library: same naming, same shape, same "
    "level of applicability prose.\n"
    "- The person can see a live graph of whatever workflow is on screen, with the validator's "
    "findings beside it. Do not narrate what they are already looking at.\n"
    "- A proposed workflow reaches the person's screen when you call `check_draft` with the YAML and the "
    "card facts (a title, a one-line summary, and applicability that says when NOT to use it). Do not "
    "restate the YAML in your answer, the card already carries it.\n"
    "- A workflow is saved only once somebody has described it: a title, a summary, and four "
    "sentences: what it is for, what a finished run leaves the person holding, what they do "
    "during a run, and what to reach for something else for. `check_draft` takes all six and says "
    "which are still missing. Ask the person for the ones you cannot answer. A placeholder is "
    "refused at the save, so writing TBD costs a round trip and buys nothing.\n"
    "- Somebody asking for an automation has stated an intent, and `check_draft` takes it: their "
    "request in their own words (never your rewrite of it), what they expect a finished run to "
    "leave them holding, one run that would count as a success, what a run does with nothing to "
    "work on, what it does when it cannot finish, and whether a person or a schedule starts each "
    "run. Pass what they said the first time you draft anything, and again whenever it changes. "
    "You may infer a default rather than interrogating them, on one condition: every inference "
    "goes in `assumptions`, one line each, so they can read it and say no. Ask only for what would "
    "change the work, and leave the rest blank rather than filling it in quietly. The panel "
    "shows an unanswered field as unanswered.\n"
    "- A request to design is answered with a draft, and with nothing else. Do not start a run and "
    "do not create a schedule off it, however ready the graph looks: the person presses Save, the "
    "person starts the run."
)


def step_inventory(ctx: RunContext[Runtime]) -> str:
    """The instruction line naming every step that resolves in this run, built in or activated by the owner.

    It is read from the run's own registry on every request, so a newly activated step shows on the
    next turn. It lists names only, never a step's brief. Nothing about the draft may go in it,
    because a line that moves with the draft breaks the prompt cache of the whole session.

    Args:
        ctx: The run context, whose session holds the registry.

    Returns:
        One instruction line.
    """
    names = ", ".join(ctx.deps.registry.names) or "none"
    return f"- Steps registered here, ready to name: {names}. Anything else, your workflow declares."


def draft_fit(workflow: Workflow, registry: StepRegistry) -> str:
    """How one draft stands against what this run can build, in the words `check_draft` answers with.

    Each step the draft uses is named once, under where it comes from. The draft is called runnable
    only when every name it uses resolves and its wiring validates.

    Args:
        workflow: The draft's parsed spec.
        registry: The run's registry of steps.

    Returns:
        One line per finding, joined by newlines.
    """
    prepared = prepare(workflow, registry)
    live = [name for name in referenced_steps(workflow) if name in prepared.registry]
    missing = [name for name in referenced_steps(workflow) if name not in prepared.registry]
    declared = frozenset(step.name for step in workflow.inline)
    lines: list[str] = []
    if inline_names := [name for name in live if name in declared]:
        lines.append(f"- It brings its own: {', '.join(inline_names)}.")
    if named := [name for name in live if name not in declared]:
        lines.append(f"- It also names, and this run can build: {', '.join(named)}.")
    if missing:
        lines.append(f"- Nothing here can build: {', '.join(missing)}. Declare each one or wire around it.")
    elif prepared.report.ok:
        lines.append("- Every step it names resolves here, and its wiring checked clean.")
    return "\n".join(lines)


async def check_draft(  # noqa: PLR0913, PLR0917 (the spec, the card and the four sentences the save gate reads are one flat contract)
    ctx: RunContext[Runtime],
    spec_yaml: str,
    title: str = "",
    summary: str = "",
    applicability: str = "",
    purpose: str = "",
    produces: str = "",
    owner_participation: str = "",
    not_for: str = "",
    intent: WorkflowIntent | None = None,
) -> str:
    """Validate a workflow spec and put it on the owner's screen as the draft.

    Pass the card fields with it, title, summary and applicability, when you have them.

    The real validator, reporting every problem at once. An `inline` step is built and type-checked
    here too, while it is still cheap to change.

    The answer ends with what the draft still owes before it can be saved. A workflow cannot be
    kept until the title, the summary and all four sentences below are written, so ask the person
    for whatever you cannot answer rather than filling a field with a placeholder.

    Args:
        ctx: The run context. A passing check settles its session's conversation and draft, and keeps that draft on disk.
        spec_yaml: The full spec as YAML.
        title: Human-facing name for the proposal. Blank keeps the draft's current title.
        summary: One line for the library card. Blank keeps the draft's current summary.
        applicability: When to use it and when NOT to. Blank keeps the draft's current applicability.
        purpose: What this workflow is for. Blank keeps what the draft already says.
        produces: What a finished run leaves the person holding. Blank keeps what the draft already says.
        owner_participation: What the person does during a run. Blank keeps what the draft already says.
        not_for: What to reach for something else for. Blank keeps what the draft already says.
        intent: What the person asked for and what they expect a run to do, in their words. Pass it
            once you have it, and again whenever it changes. Omitted, the draft keeps the one it
            already carries. An intent that still owes answers is fine: record what was said and
            ask for the rest, rather than filling it in yourself.
    """
    try:
        workflow = Workflow.model_validate(yaml.safe_load(spec_yaml))
    except (ValidationError, yaml.YAMLError) as exc:
        ctx.deps.conversation.last_issues = (str(exc)[:900],)
        return f"the draft is not a well-formed workflow yet:\n{str(exc)[:900]}"
    report = validate(workflow, ctx.deps.registry)
    ctx.deps.conversation.last_workflow = workflow
    ctx.deps.conversation.last_issues = tuple(f"[{i.kind}] {i.where}: {i.detail}" for i in report.issues)
    fit = draft_fit(workflow, ctx.deps.registry)
    if report.ok:
        draft = settled_draft(
            workflow,
            title=title,
            summary=summary,
            applicability=applicability,
            existing=ctx.deps.conversation.draft,
            presentation=WorkflowPresentationPatch(
                purpose=purpose.strip() or None,
                produces=produces.strip() or None,
                owner_participation=owner_participation.strip() or None,
                not_for=not_for.strip() or None,
            ),
            intent=intent,
        )
        ctx.deps.conversation.draft = draft
        # Kept on disk where the settle happens, so the draft survives a restart however the turn ends.
        remember_draft(draft, path=draft_path(ctx.deps))
        inline_names = ", ".join(step.name for step in workflow.inline)
        built = f" Steps it brings with it, built and typed: {inline_names}." if inline_names else ""
        answer = (
            f"the draft on the owner's screen now has {len(workflow.nodes)} nodes, titled {draft.title}. "
            f"Runs concurrently: {parallel_groups(workflow)}.{built}"
        )
        owed = _still_owed(draft_readiness(workflow, draft, ctx.deps.registry))
        return "\n".join(line for line in (answer, fit, owed, _intent_owed(draft.intent)) if line)
    refused = "NOT VALID:\n" + "\n".join(ctx.deps.conversation.last_issues)
    return f"{refused}\n{fit}" if fit else refused


def _intent_owed(intent: WorkflowIntent | None) -> str:
    """What the person has not said yet about what they asked for, judging completeness only and filling in nothing."""
    if intent is None:
        return "- Nothing records what the person asked for yet. Pass `intent` with their own words the next time you check."
    missing = intent_issues(intent)
    if not missing:
        return "- What they asked for is on record, in full."
    return f"- Still unanswered about what they asked for: {', '.join(missing)}. Ask, or infer and say so under assumptions."


def _still_owed(readiness: DraftReadiness) -> str:
    """What this draft owes before it can be saved, named field by field, or that it owes nothing."""
    errors = readiness.errors()
    if not errors:
        return "- Described: it can be saved as it stands."
    return "- Cannot be saved yet. " + " ".join(f"{finding.field}: {finding.message}" for finding in errors)


def _run_workflow_tool(run: RunCollaborators) -> Callable[..., Awaitable[str]]:
    """The `run_workflow` tool, closed over what a run this capability starts reaches."""

    async def run_workflow_tool(
        ctx: RunContext[Runtime], workflow_id: str, arguments: WorkflowArguments, *, gate_mode: GateMode = "wait"
    ) -> str:
        """Start a saved workflow, or the current draft, running in the background. The graph runs it, you do not.

        Answers the moment the run is going. Independent steps run at the same time, a step consuming
        two others waits for both, and at a gate the run waits and the person is asked in the
        interface. A rejection reruns from the gate's rejection target rather than from the
        beginning. When the run settles (finished, failed, stopped, or paused), you get one
        message in this session saying how. Do not wait for it, poll in a loop, or start it again:
        tell the person the run is going and end your turn. `check_runs` shows progress when asked.
        `message_worker` reaches a worker while the run is waiting at a gate, and `stop_run` ends it.

        Args:
            ctx: The run context. The session it carries supplies the library and the registry.
            workflow_id: A library id, or the word `draft` for the spec you last checked here.
            arguments: The workflow's parameters, one per declared parameter, each as the JSON
                value that parameter's own type asks for: a list where it declares a list, a
                number where it declares a number, true or false where it declares a boolean.
            gate_mode: What a person gate does. `wait` holds it for the person, `orchestrate` hands it to
                this run's own orchestrator, and `auto` passes it with nobody asked, on the person's
                explicit instruction only. An automatic pass is never counted as a person's approval.
        """
        return await run_workflow(ctx, workflow_id, arguments, run=run, gate_mode=gate_mode)

    return run_workflow_tool


def _stop_run_tool(records: RunRecords, sessions: PooledSessions, run: RunSettings) -> Callable[..., Awaitable[str]]:
    """The `stop_run` tool, closed over where the stopped run's own records land and which engine holds it."""

    async def stop_run_tool(ctx: RunContext[Runtime], session: str = "") -> str:
        """End a run for good, in this session or in another held one. A stop is not a pause: nothing resumes.

        Args:
            ctx: The run context. The session it carries is the default target.
            session: A session id or its prefix from check_runs, "" for this session.
        """
        return await stop_run(ctx, session, records=records, sessions=sessions, run=run)

    return stop_run_tool


@dataclass
class WorkflowProcess(AbstractCapability[Runtime]):
    """Everything workflow, behind one capability: the library's tools, the validator, the runner, and the run history."""

    port: WorkflowContract[Runtime]
    """The library's plugin, its contract tools and usage note, composed here and never mounted on its own."""

    records: RunRecords
    """Every record a run writes: the archive `recent_runs` and `run_values` read, and what a launch appends to."""

    reporter: SpendReporter
    """The telemetry reader `spend_report` asks."""

    scheduler: Scheduler
    """The scheduler the five schedule tools go through."""

    sessions: PooledSessions
    """The pool a run tool reaches another held session through, resolved when the tool runs."""

    topics: TopicSink
    """Where a run this capability starts announces that it moved."""

    run: RunSettings
    """Which engine this deployment runs workflows on, which decides how a run is stopped."""

    knowledge_bases: KnowledgeBaseNames | None = None
    """Which knowledge bases are registered here, for a run or a schedule whose own parameters
    select one. Read when a tool admits its arguments, never when this capability is composed.
    `None` for a composition that cannot reach the registry, whose tools then refuse a workflow
    naming a knowledge base rather than admitting a name nothing checked."""

    _toolset: AbstractToolset[Runtime] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Build the toolset once, so every plugin onto this instance dispatches the same callables."""
        own = FunctionToolset[Runtime]()
        own.add_function(check_draft, takes_ctx=True, name=CHECK_TOOL)
        own.add_function(message_worker, takes_ctx=True, name="message_worker")

        records = self.records
        sessions = self.sessions
        scheduler = self.scheduler

        async def check_runs_tool(ctx: RunContext[Runtime]) -> str:
            """What is running right now, in this session and every other one this app holds, one line each.

            Runs that have ended are listed by recent_runs.
            """
            return await check_runs(ctx, sessions=sessions)

        own.add_function(check_runs_tool, takes_ctx=True, name="check_runs")

        collaborators = RunCollaborators(
            records=records, topics=self.topics, run_settings=self.run, sessions=sessions, knowledge_bases=self.knowledge_bases
        )
        own.add_function(_run_workflow_tool(collaborators), takes_ctx=True, name=RUN_TOOL)
        own.add_function(_stop_run_tool(records, sessions, self.run), takes_ctx=True, name="stop_run")

        async def schedules_tool(ctx: RunContext[Runtime]) -> str:
            """Every standing schedule, one line each: workflow, cadence, enabled, next fire, last outcome."""
            del ctx
            return await schedules(scheduler)

        async def schedule_workflow_tool(  # noqa: PLR0913 (a schedule's own knobs are one flat contract, the same one the HTTP entry takes)
            ctx: RunContext[Runtime],
            workflow_id: str,
            cadence: str,
            *,
            arguments: WorkflowArguments | None = None,
            gate_policy: str = "wait",
            gate_mode: GateMode = "wait",
            capacity_key: str | None = None,
            capacity_limit: int | None = None,
            blackout: str | None = None,
            catch_up: bool = False,
        ) -> str:
            """Create a standing schedule that fires `workflow_id` on its own, on `cadence`, from now on.

            Says the schedule's id and its next three fires. Load `orchestrating-a-run` for what each
            knob (`gate_mode`, `capacity_key`, `blackout`, `catch_up`) does.
            """
            return await schedule_workflow(
                scheduler,
                ctx,
                workflow_id,
                cadence,
                run_engine=self.run.run_engine,
                arguments=arguments,
                names=self.knowledge_bases,
                gate_policy=gate_policy,
                gate_mode=gate_mode,
                capacity_key=capacity_key,
                capacity_limit=capacity_limit,
                blackout=blackout,
                catch_up=catch_up,
            )

        async def update_schedule_tool(  # noqa: PLR0913 (mirrors the HTTP patch route's own flat contract)
            ctx: RunContext[Runtime],
            schedule_id: str,
            *,
            workflow_id: str | None = None,
            cadence: str | None = None,
            arguments: WorkflowArguments | None = None,
            enabled: bool | None = None,
            gate_policy: str | None = None,
            gate_mode: GateMode | None = None,
            capacity_key: str | None = None,
            capacity_limit: int | None = None,
            blackout: str | None = None,
            catch_up: bool | None = None,
        ) -> str:
            """Change one or more of a standing schedule's own settings, and say it back in full."""
            return await update_schedule(
                scheduler,
                ctx,
                schedule_id,
                run_engine=self.run.run_engine,
                workflow_id=workflow_id,
                cadence=cadence,
                arguments=arguments,
                names=self.knowledge_bases,
                enabled=enabled,
                gate_policy=gate_policy,
                gate_mode=gate_mode,
                capacity_key=capacity_key,
                capacity_limit=capacity_limit,
                blackout=blackout,
                catch_up=catch_up,
            )

        async def delete_schedule_tool(ctx: RunContext[Runtime], schedule_id: str) -> str:
            """Remove a standing schedule for good. A run it already started keeps going."""
            del ctx
            return await delete_schedule(scheduler, schedule_id)

        async def run_schedule_now_tool(ctx: RunContext[Runtime], schedule_id: str) -> str:
            """Fire a standing schedule immediately, through the same path its own cadence uses."""
            del ctx
            return await run_schedule_now(scheduler, sessions, collaborators, schedule_id)

        own.add_function(schedules_tool, takes_ctx=True, name="schedules")
        own.add_function(schedule_workflow_tool, takes_ctx=True, name="schedule_workflow")
        own.add_function(update_schedule_tool, takes_ctx=True, name="update_schedule")
        own.add_function(delete_schedule_tool, takes_ctx=True, name="delete_schedule")
        own.add_function(run_schedule_now_tool, takes_ctx=True, name="run_schedule_now")

        archive = self.records.archive

        async def recent_runs_tool(ctx: RunContext[Runtime], limit: int = 10) -> str:
            """The runs in flight right now, then the last archived runs, newest first, with outcome and params.

            `limit` counts the archived runs, at most 50. Read an archived one with run_values.
            """
            return f"{await check_runs(ctx, sessions=sessions)}\n\n{await recent_runs(archive, limit)}"

        async def run_values_tool(run_id: str, node: str = "") -> str:
            """One archived run's per-node values, or one node's. A digest run's reference decisions are its references node."""
            return await run_values(archive, run_id, node)

        own.add_function(recent_runs_tool, takes_ctx=True, name="recent_runs")
        own.add_function(run_values_tool, name="run_values")

        reporter = self.reporter

        async def spend_report_tool(scope: Scope, subject: str = "", since_hours: int = DEFAULT_SINCE_HOURS) -> str:
            """What a run, workflow, session, step, model or day cost, from the telemetry.

            run, workflow, session and step take their id as subject.
            """
            return await spend_report(reporter, scope, subject, since_hours)

        own.add_function(spend_report_tool, name="spend_report")
        # What a turn is offered is decided when the tools are listed, from the session the turn runs in.
        verbs = FilteredToolset(own, _not_while_designing)
        library = self.port.get_toolset()
        self._toolset = verbs if library is None else CombinedToolset([library, verbs])

    def get_toolset(self) -> AbstractToolset[Runtime]:
        """The library's tools and the process's verbs, as one surface, minus what this turn may not have.

        Returns:
            The combined toolset, filtered on every turn.
        """
        return self._toolset

    def get_instructions(self) -> Callable[[RunContext[Runtime]], str]:
        """One workflow section: the engine's instructions, this app's lines, the live inventory, then the library's note.

        Returns:
            A function that renders the section for each request.
        """
        note = self.port.get_instructions()

        def compose(ctx: RunContext[Runtime]) -> str:
            text = f"# The workflow process\n{process_instructions(check_tool=CHECK_TOOL)}\n{_LOCAL_VOICE}\n{step_inventory(ctx)}"
            words = note(ctx) if note is not None else None
            return text if not words else f"{text}\n\n{words}"

        return compose
