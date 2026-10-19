"""The `harness` console script: the review-and-activate verbs, the server, evaluations and the session log.

The review verbs (`list`, `activate`, `disable`, `delete`, `plugins`, `profile`, `promote`, `demote`)
live in `harness.features.selfauthoring.routes_cli`. This module is the entry point that owns the
wiring and adds `serve`, `import` (load a folder of workflow and step files into the library),
`inspect` (issue `InspectRun` by hand, for one run or for every run that ended in a window), the
`evals` group, and the `sessions` group (`list`, `rebuild`, `import`, `tail`, `search`, `friction`)
over the session log.
A verb must import its heavy dependencies inside its body, so every verb keeps a fast, server-free import path.
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass
from typing import TYPE_CHECKING

import typer

from harness.features.selfauthoring.contracts import fail, harness_app

if TYPE_CHECKING:
    from contextlib import AsyncExitStack
    from pathlib import Path

    from core.ai.evaluation.contracts import EvaluationLimits, StateRootIdentity
    from core.ai.workflows.contracts import LibraryCollection, StepRegistry
    from pydantic_evals.reporting import EvaluationReport
    from wireup import AsyncContainer

    from harness.adapters.contracts import SessionLog
    from harness.features.evals.contracts import (
        BenchmarkCases,
        BenchmarkInputs,
        BenchmarkRecord,
        ComposedBenchmark,
        OutcomeStateReport,
        PilotParticipant,
    )
    from harness.settings import HarnessSettings


app = harness_app
app.info.name = "harness"

sessions_app = typer.Typer(name="sessions", help="The session log: what this app did, in order.", no_args_is_help=True)
app.add_typer(sessions_app)

evals_app = typer.Typer(
    name="evals", help="A step's own dataset, performed through the same executor a workflow run uses.", no_args_is_help=True
)
app.add_typer(evals_app)

outcomes_app = typer.Typer(
    name="outcomes",
    help="Governed outcome evaluations: the author-visible development replay, and the sealed comparison against a baseline.",
    no_args_is_help=True,
)
evals_app.add_typer(outcomes_app)


def _describe(kind: str, payload: dict[str, object]) -> str:
    """One compact clause per event, from the already-decoded line's own keys."""
    detail = ", ".join(f"{key}={value}" for key, value in payload.items() if key not in {"seq", "at", "kind"} and value not in (None, ""))
    return f"{kind}({detail})" if detail else kind


async def _library_session(stack: AsyncExitStack, settings: HarnessSettings) -> tuple[LibraryCollection, StepRegistry]:
    """The library and the registry for one console verb, from the container the serving entries build, closed with `stack`."""
    from harness.adapters.contracts import DatabaseBinding, ModelTiers  # noqa: PLC0415 (heavy async wiring stays off the review verbs' import path)
    from harness.composition import library_composition  # noqa: PLC0415 (same)
    from harness.container import create_container  # noqa: PLC0415 (same)
    from harness.features.running.contracts import RunRecords  # noqa: PLC0415 (same)

    container = create_container(settings)
    stack.push_async_callback(container.close)
    records = await container.get(RunRecords)
    tiers = await container.get(ModelTiers)
    binding = await container.get(DatabaseBinding)
    return await stack.enter_async_context(library_composition(settings, records, tiers, binding))


async def provision_record(container: AsyncContainer) -> None:
    """Provision this deployment's record: resolving it is what creates its schema and its tables.

    A console verb that touches the sessions database must provision the record through this,
    never through a pool of its own.

    Args:
        container: The verb's container.
    """
    from harness.adapters.contracts import RecordDatabase  # noqa: PLC0415 (heavy async wiring stays off the review verbs' import path)

    await container.get(RecordDatabase)


def _checkout_session_log() -> SessionLog:
    """This deployment's own session log, opened fresh: a console verb is a process with no container."""
    from harness.adapters.contracts import SessionLog as OpenLog  # noqa: PLC0415 (off the fast path)
    from harness.settings import HarnessSettings  # noqa: PLC0415 (same)

    state = HarnessSettings().state
    return OpenLog(state.session_log, topics=None)


@sessions_app.command(name="list")
def list_sessions() -> None:
    """Every session: its id, its title, when it ran, and how many turns it held."""
    from harness.domain.contracts import session_spans  # noqa: PLC0415 (keep the session log read off the other verbs' import path)
    from harness.features.sessions.contracts import title_for  # noqa: PLC0415 (same)

    events = _checkout_session_log().events()
    spans = session_spans(events)
    if not spans:
        typer.echo("no sessions recorded yet.")
        return
    for span in spans:
        ended = span.ended_at.isoformat() if span.ended_at is not None else "live"
        turn_word = "turn" if span.turns == 1 else "turns"
        title = title_for(events, span)
        typer.echo(f"{span.session_id[:8]}  {title}  {span.started_at.isoformat()} -> {ended}  ({span.turns} {turn_word})")


@sessions_app.command(name="rebuild")
def rebuild_sessions() -> None:
    """Recompute the session projection from every registered project's own state, from scratch.

    Empties the five projection tables, then runs the same sweep the server runs at boot, over
    every project the registry names plus this checkout's own state folder. A session removed from
    disk leaves no row behind. Prints the resulting row counts.

    Creates the projection's tables and the record's schema first when they are missing.
    """
    import asyncio  # noqa: PLC0415 (heavy async wiring stays off the review verbs' import path)

    from harness.adapters.contracts import SessionsDatabase  # noqa: PLC0415 (same)
    from harness.container import create_container  # noqa: PLC0415 (same)
    from harness.domain.contracts import SessionTranscript  # noqa: PLC0415 (same)
    from harness.features.sessions.contracts import project_registry, sweep_projects  # noqa: PLC0415 (same)
    from harness.features.sessions.infrastructure.postgres import PostgresProjectionPool  # noqa: PLC0415 (same)
    from harness.features.sessions.infrastructure.schema import ensure_schema  # noqa: PLC0415 (same)
    from harness.settings import CHECKOUT, HarnessSettings  # noqa: PLC0415 (same)

    settings = HarnessSettings()

    async def _run() -> None:
        container = create_container(settings)
        try:
            sessions = await container.get(SessionsDatabase)
            await provision_record(container)
            transcript = await container.get(SessionTranscript)
            projected = PostgresProjectionPool(sessions.pool)
            await ensure_schema(projected)
            project_registry.boot(CHECKOUT)
            await projected.execute("TRUNCATE projects, sessions, turns, messages, flags")
            await sweep_projects(projected, CHECKOUT, session_log=_checkout_session_log(), store=transcript)
            counts = await sessions.pool.fetchrow(
                "SELECT (SELECT count(*) FROM sessions) AS sessions, (SELECT count(*) FROM turns) AS turns, "
                "(SELECT count(*) FROM flags) AS flags"
            )
            typer.echo(f"sessions={counts['sessions']} turns={counts['turns']} flags={counts['flags']}")
        finally:
            await container.close()

    asyncio.run(_run())


async def import_sessions_report(directories: list[Path], *, container: AsyncContainer) -> list[str]:
    """Copy every directory's file-store transcript into the container's own record, one report line each.

    The record's schema is created first when it is missing. A directory that is not there is
    reported and skipped, and never fails the whole pass.

    Args:
        directories: The folders a file-store transcript was written to.
        container: The container whose record receives the runs.

    Returns:
        One line per directory, with its counts or `nothing there`.
    """
    from pydantic_ai_harness.step_persistence import FileStepStore  # noqa: PLC0415 (heavy async wiring stays off the review verbs' import path)

    from harness.domain.contracts import SessionTranscript  # noqa: PLC0415 (same)
    from harness.features.sessions.contracts import import_file_store  # noqa: PLC0415 (same)

    await provision_record(container)
    record = await container.get(SessionTranscript)
    lines: list[str] = []
    for directory in directories:
        if not directory.is_dir():
            lines.append(f"{directory}: nothing there")
            continue
        report = await import_file_store(FileStepStore(directory), into=record)
        lines.append(
            f"{directory}: runs={report.runs} events={report.events} snapshots={report.snapshots} "
            f"effects={report.effects} skipped={report.skipped} resumed={report.resumed} unnamed={report.unnamed} "
            f"reconciled={report.reconciled} malformed={report.malformed}"
        )
    return lines


_IMPORT_PATHS_ARGUMENT = typer.Argument(
    None, help="Directories a file-store transcript was written to. Defaults to this checkout's and every registered project's."
)
"""The `paths` argument of `sessions import`."""


@sessions_app.command(name="import")
def import_sessions(paths: list[str] | None = _IMPORT_PATHS_ARGUMENT) -> None:
    """Copy the transcripts written before the record into it, once. Safe to run again.

    Reads each directory as a file store and writes every run, event, snapshot and tool effect it
    holds into the record. A run the record already holds a snapshot for is skipped by id. One it
    holds with no snapshot yet is resumed, so running the verb again finishes an interrupted pass.
    """
    import asyncio  # noqa: PLC0415 (heavy async wiring stays off the review verbs' import path)

    from pathlib import Path  # noqa: PLC0415 (same)

    from harness.container import create_container  # noqa: PLC0415 (same)
    from harness.features.sessions.contracts import project_registry  # noqa: PLC0415 (same)
    from harness.settings import CHECKOUT, HarnessSettings, aifactory_state  # noqa: PLC0415 (same)

    settings = HarnessSettings()
    if paths:
        directories = [Path(path).resolve() for path in paths]
    else:
        project_registry.boot(CHECKOUT)
        directories = [
            settings.state.steps,
            *(aifactory_state(Path(project.root_path)) / "steps" for project in project_registry.registered_projects()),
        ]

    async def _run() -> None:
        container = create_container(settings)
        try:
            for line in await import_sessions_report(directories, container=container):
                typer.echo(line)
        finally:
            await container.close()

    asyncio.run(_run())


@sessions_app.command(name="tail")
def tail_sessions(n: int = typer.Option(20, "--n", help="How many of the newest events to print.")) -> None:
    """The newest log entries, oldest of them first."""
    records = _checkout_session_log().events()[-n:]
    if not records:
        typer.echo("no sessions recorded yet.")
        return
    for record in records:
        payload = record.event.model_dump(mode="json")
        typer.echo(f"{record.seq:>5}  {record.at.isoformat()}  {_describe(record.event.kind, payload)}")


@sessions_app.command(name="friction")
def friction_sessions(
    target_kind: str | None = typer.Option(
        None, "--target-kind", help="Only flags about this kind of thing (tool, skill, capability, workflow, agent, instructions, self)."
    ),
    n: int = typer.Option(50, "--n", help="How many of the newest flags to print."),
) -> None:
    """What agents flagged as grinding against them, newest first, straight off the session log."""
    from pydantic import TypeAdapter, ValidationError  # noqa: PLC0415 (same as `list`)

    from harness.domain.contracts import FrictionTargetType  # noqa: PLC0415 (same)
    from harness.features.sessions.contracts import friction_lines  # noqa: PLC0415 (same)

    kind: FrictionTargetType | None = None
    if target_kind is not None:
        try:
            kind = TypeAdapter(FrictionTargetType).validate_python(target_kind)
        except ValidationError:
            typer.echo("--target-kind is one of tool, skill, capability, workflow, agent, instructions, self.", err=True)
            raise typer.Exit(code=2) from None
    lines = friction_lines(_checkout_session_log().events(), target_kind=kind)[:n]
    if not lines:
        typer.echo("no friction flagged yet.")
        return
    for line in lines:
        typer.echo(line)


@sessions_app.command(name="search")
def search_sessions(term: str, limit: int = typer.Option(20, "--limit", help="Stop after this many matching lines.")) -> None:
    """Case-insensitive scan over the session log and this checkout's state home."""
    from harness.settings import HarnessSettings  # noqa: PLC0415 (same as `list`)

    home = HarnessSettings().state
    needle = term.lower()
    shown = 0
    candidates = [home.session_log, *sorted(path for path in home.home.rglob("*") if path.is_file() and path != home.session_log)]
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if needle in line.lower():
                where = path.name if path == home.session_log else str(path.relative_to(home.home))
                snippet = line.strip()[:160]
                typer.echo(f"{where}:{lineno}: {snippet}")
                shown += 1
                if shown >= limit:
                    typer.echo(f"(stopped at {limit} matches)")
                    return
    if shown == 0:
        typer.echo(f"no matches for {term!r}.")


async def import_path_in_session(path: Path, *, by: str) -> tuple[str, ...]:
    """Open the library and the registry once and import the folder at `path` into the library.

    Each workflow is stamped with this host's environment and the registry it was checked against.
    One refused file refuses the whole folder, and nothing is written.

    Args:
        path: A folder holding `steps/` and `workflows/` files.
        by: Who the import is recorded as, such as `owner:cli`.

    Returns:
        The ids written, the reusable steps first and then the workflows.

    Raises:
        SaveRefusedError: A workflow would not run on this registry.
        PresentationRefusedError: A workflow's `presentation` names an input or an artifact the
            workflow does not declare.
        DocumentError: A step or a workflow document does not parse. The message names its file.
        pydantic.ValidationError: A step, a workflow's `uses` list, or its `presentation` does not parse.
    """
    from contextlib import AsyncExitStack  # noqa: PLC0415 (heavy async wiring stays off the review verbs' import path)

    from harness.adapters.contracts import environment_stamps  # noqa: PLC0415 (same)
    from harness.features.browsing.contracts import import_path  # noqa: PLC0415 (same)
    from harness.settings import HarnessSettings, custom_root  # noqa: PLC0415 (same)

    settings = HarnessSettings()
    async with AsyncExitStack() as stack:
        library, registry = await _library_session(stack, settings)
        custom = custom_root(settings.self_authoring)
        return await import_path(path, library=library, registry=registry, by=by, environment=environment_stamps(custom))


@app.command(name="import")
def import_files(path: str = typer.Argument(..., help="A directory holding steps/ and workflows/ files.")) -> None:
    """Import a folder of workflow and step files into the library, steps first, then the workflows that use them."""
    import asyncio  # noqa: PLC0415 (heavy async wiring stays off the review verbs' import path)

    from pathlib import Path  # noqa: PLC0415 (same)

    ids = asyncio.run(import_path_in_session(Path(path).resolve(), by="owner:cli"))
    for written in ids:
        typer.echo(written)


def _dataset_path(step_name: str, *, artifacts: str) -> Path:
    """`<artifacts>/evals/<step_name, hyphenated>.yaml`, a relative `artifacts` resolved against the workspace root."""
    from harness.settings import CHECKOUT  # noqa: PLC0415 (keeps this file's fast, server-free import path)

    return CHECKOUT.parent / artifacts / "evals" / f"{step_name.replace('_', '-')}.yaml"


async def evals_run_in_session(step_id: str, *, knowledge_base: str, artifacts: str) -> EvaluationReport:
    """Open the library and the registry once, resolve `step_id` in the library, and perform its dataset through the registry.

    Args:
        step_id: The library id of the step to evaluate.
        knowledge_base: The knowledge base a case binds when its own inputs name none.
        artifacts: The workflows directory whose `evals/` folder holds the step's dataset, resolved
            against the workspace root unless it is absolute.

    Returns:
        The evaluation report.

    Raises:
        RuntimeError: The registry carries no `perform`, so it cannot run the dataset.
    """
    from contextlib import AsyncExitStack  # noqa: PLC0415 (heavy async wiring stays off the review verbs' import path)

    from harness.features.evals.contracts import bind_collection, resolve_step, run_step_evals  # noqa: PLC0415 (same)
    from harness.features.evals.infrastructure.files import load_dataset  # noqa: PLC0415 (same)
    from harness.settings import HarnessSettings  # noqa: PLC0415 (same)

    async with AsyncExitStack() as stack:
        library, registry = await _library_session(stack, HarnessSettings())
        step = await resolve_step(library, step_id)
        perform = registry.perform
        if perform is None:
            msg = "this session's registry does not perform briefings"
            raise RuntimeError(msg)
        dataset = load_dataset(_dataset_path(step.name, artifacts=artifacts))
        bind_collection(dataset, knowledge_base)
        return await run_step_evals(dataset, perform, step)


@evals_app.command(name="run")
def run_evals(
    step_id: str = typer.Argument(..., help="The library id of the step to evaluate, for example st.check_adrs.v1."),
    knowledge_base: str = typer.Option(..., "--knowledge-base", help="The collection a case binds when its own inputs name none."),
    artifacts: str = typer.Option(
        "harness/workflows",
        "--artifacts",
        help=(
            "Where the step's own workflows/evals/ dataset lives, resolved against the workspace root: "
            "harness/workflows for a step this app ships, or a template's own directory, for example templates/repo-docs/workflows."
        ),
    ),
) -> None:
    """Perform the step's own dataset and print the report. Exits 1 if any case fails."""
    import asyncio  # noqa: PLC0415 (heavy async wiring stays off the review verbs' import path)

    from harness.features.evals.contracts import report_failed  # noqa: PLC0415 (same)

    report = asyncio.run(evals_run_in_session(step_id, knowledge_base=knowledge_base, artifacts=artifacts))
    report.print()
    if report_failed(report):
        raise typer.Exit(code=1)


_OBJECTIVE_OPTION = typer.Option(
    "task-success", "--objective", help="What this benchmark is asked to establish: task-success or measured-generative-model-cost."
)
_INPUTS_OPTION = typer.Option(
    ..., "--inputs", help="JSON naming the content every fingerprint is taken over: workflow, prompt, model, tools, sources, grader."
)
_SHARED_ROOT_OPTION = typer.Option(
    ..., "--shared-budget-root", help="The one POSIX directory every participating process opens the budget record in."
)
_REPORT_ROOT_OPTION = typer.Option(..., "--report-root", help="Where the immutable state report and its per-slot trial files are filed.")
_CYCLE_STOP_OPTION = typer.Option(None, "--cycle-cost-stop-usd", help="Where the whole benchmark stops accruing measured model charges.")
_TRIAL_STOP_OPTION = typer.Option(None, "--trial-cost-stop-usd", help="Where one trial stops accruing measured model charges.")
_DEADLINE_OPTION = typer.Option(None, "--trial-deadline-seconds", help="How long one trial may stay open before admission is refused.")
_REQUEST_LIMIT_OPTION = typer.Option(None, "--trial-request-limit", help="How many model requests one trial may be admitted.")
_ATTEMPT_LIMIT_OPTION = typer.Option(None, "--trial-attempt-limit", help="How many attempts one trial may record.")
"""The options the benchmark verbs share."""


def stated_limits(
    cycle_cost_stop_usd: str | None,
    trial_cost_stop_usd: str | None,
    trial_deadline_seconds: int | None,
    trial_request_limit: int | None,
    trial_attempt_limit: int | None,
) -> EvaluationLimits | None:
    """The stop thresholds the flags stated, or `None` when the caller stated none of them.

    The caller must state all five or none. No threshold is ever filled in from a default.

    Args:
        cycle_cost_stop_usd: Where the whole benchmark stops accruing measured model charges.
        trial_cost_stop_usd: Where one trial stops accruing measured model charges.
        trial_deadline_seconds: How long one trial may stay open before admission is refused.
        trial_request_limit: How many model requests one trial may be admitted.
        trial_attempt_limit: How many attempts one trial may record.

    Returns:
        The limits, or `None` when all five are absent.

    Raises:
        typer.Exit: Some of the five were given and some were not, or one of them is not positive.
    """
    from decimal import Decimal, InvalidOperation  # noqa: PLC0415 (evaluation dependencies load only when a benchmark verb runs)

    from core.ai.evaluation.contracts import EvaluationLimits as Limits  # noqa: PLC0415 (same)
    from pydantic import ValidationError  # noqa: PLC0415 (same)

    given = (cycle_cost_stop_usd, trial_cost_stop_usd, trial_deadline_seconds, trial_request_limit, trial_attempt_limit)
    if all(value is None for value in given):
        return None
    if any(value is None for value in given):
        fail(
            "state all five stop thresholds or none of them: --cycle-cost-stop-usd, --trial-cost-stop-usd, "
            "--trial-deadline-seconds, --trial-request-limit, --trial-attempt-limit"
        )
    try:
        return Limits(
            cycle_cost_stop_usd=Decimal(str(cycle_cost_stop_usd)),
            trial_cost_stop_usd=Decimal(str(trial_cost_stop_usd)),
            trial_deadline_seconds=int(str(trial_deadline_seconds)),
            trial_request_limit=int(str(trial_request_limit)),
            trial_attempt_limit=int(str(trial_attempt_limit)),
        )
    except (InvalidOperation, ValidationError, ValueError) as error:
        fail(f"every stop threshold must be a positive number: {error}")


def announce(report: OutcomeStateReport) -> None:
    """Print what the report established, refusals first, so a reader meets the limits before any figure.

    Args:
        report: The benchmark's state report.
    """
    typer.echo(f"{report.empirical_status}: {report.dataset} {report.dataset_version}, models {report.model_kind}, report {report.report_id}")
    for note in report.refusals:
        typer.echo(f"  refused ({note.refusal}): {note.detail}")
    if report.custody is not None:
        verified = ", ".join(report.custody.verified_case_ids) or "none"
        typer.echo(f"  independent holdout verified: {report.custody.independent_holdout_verified}, custody-clean cases: {verified}")
    if report.trials:
        typer.echo(f"  trials filed: {len(report.trials)}, graded: {len(report.graded_case_ids)}, ungraded: {len(report.ungraded_case_ids)}")
        spent = "unknown" if report.cycle_measured_model_spent_usd is None else str(report.cycle_measured_model_spent_usd)
        typer.echo(f"  measured generative-model charges: {spent}, overshoot {report.cycle_measured_model_overshoot_usd}")
    if report.verdict is not None:
        reasons = ", ".join(report.verdict_reasons) or "none"
        regressions = ", ".join(report.regressions) or "none"
        typer.echo(f"  verdict: {report.verdict} (reasons: {reasons}), regressions: {regressions}")
        typer.echo(f"  success rate, baseline {report.baseline_success_rate}, candidate {report.candidate_success_rate}")
    typer.echo("  This recommends nothing, changes no policy and accepts nothing on the owner's behalf.")


@outcomes_app.command(name="replay")
def outcomes_replay(  # noqa: PLR0913, PLR0917 (a typer command's parameters are its flags, and each one is a distinct term of the benchmark)
    dataset: str = typer.Argument(..., help="The workflow-automation development dataset JSON."),
    stamp: str = typer.Argument(..., help="The governance stamp filed beside that dataset version."),
    inputs: str = _INPUTS_OPTION,
    report_root: str = _REPORT_ROOT_OPTION,
    shared_budget_root: str = _SHARED_ROOT_OPTION,
    objective: str = _OBJECTIVE_OPTION,
) -> None:
    """Read the author-visible development set back and file what it does not establish.

    It opens no cycle, builds no model and makes no request. The set declares itself author-visible,
    so no result over it is independent generalization evidence, and the report says exactly that.
    """
    from pathlib import Path  # noqa: PLC0415 (evaluation dependencies load only when this verb runs)

    from harness.features.evals.contracts import DevelopmentReplay  # noqa: PLC0415 (same)

    cases = DevelopmentReplay(dataset_path=Path(dataset).resolve(), stamp_path=Path(stamp).resolve())
    asked = _BenchmarkFlags(
        cases=cases, inputs=Path(inputs).resolve(), report_root=Path(report_root).resolve(), shared_root=Path(shared_budget_root).resolve(),
        objective=objective,
    )
    announce(asyncio.run(run_benchmark_in_session(asked)))


@outcomes_app.command(name="compare")
def outcomes_compare(  # noqa: PLR0913, PLR0917 (a typer command's parameters are its flags, and the five thresholds are the owner's money)
    dataset: str = typer.Argument(..., help="The custodian's sealed task dataset JSON."),
    custody: str = typer.Argument(..., help="The custody record filed beside it: seals, exposures and implementation authors."),
    journey: str = typer.Option(..., "--journey", help="The index of which run each case's workflow-automation trial produced."),
    inputs: str = _INPUTS_OPTION,
    report_root: str = _REPORT_ROOT_OPTION,
    shared_budget_root: str = _SHARED_ROOT_OPTION,
    objective: str = _OBJECTIVE_OPTION,
    cycle_cost_stop_usd: str | None = _CYCLE_STOP_OPTION,
    trial_cost_stop_usd: str | None = _TRIAL_STOP_OPTION,
    trial_deadline_seconds: int | None = _DEADLINE_OPTION,
    trial_request_limit: int | None = _REQUEST_LIMIT_OPTION,
    trial_attempt_limit: int | None = _ATTEMPT_LIMIT_OPTION,
) -> None:
    """Compare the simple single-run baseline with workflow automation over a custodian's sealed cases.

    Nothing is admitted until the seals, the fingerprints, the authorship disjointness and the
    freshness verdicts all hold, every generative model path this benchmark takes is on the record,
    and both arms carry the identity the seal froze. Absent cases write a blocked report and skip
    every paid step.
    """
    from pathlib import Path  # noqa: PLC0415 (evaluation dependencies load only when this verb runs)

    from harness.features.evals.contracts import SealedComparison  # noqa: PLC0415 (same)

    cases = SealedComparison(dataset_path=Path(dataset).resolve(), custody_path=Path(custody).resolve())
    asked = _BenchmarkFlags(
        cases=cases,
        inputs=Path(inputs).resolve(),
        report_root=Path(report_root).resolve(),
        shared_root=Path(shared_budget_root).resolve(),
        objective=objective,
        journey=Path(journey).resolve(),
        limits=stated_limits(cycle_cost_stop_usd, trial_cost_stop_usd, trial_deadline_seconds, trial_request_limit, trial_attempt_limit),
    )
    announce(asyncio.run(run_benchmark_in_session(asked)))


@dataclass(frozen=True)
class _BenchmarkFlags:
    """One benchmark verb's flags, with every path resolved."""

    cases: BenchmarkCases
    inputs: Path
    report_root: Path
    shared_root: Path
    objective: str
    journey: Path | None = None
    limits: EvaluationLimits | None = None


def this_process_participant(identity: StateRootIdentity, root: Path) -> PilotParticipant:
    """This process as a participant, with the device it can actually see and the lock it can actually take.

    Neither answer is taken from a caller. The device is read from `root` through `stat`, and a lock
    is taken and released on a probe file in `root`. A process that cannot do either says so rather
    than claiming it can.

    Args:
        identity: The budget record's state root identity.
        root: The shared budget root the device and the lock are read from.

    Returns:
        The participant.
    """
    import fcntl  # noqa: PLC0415 (evaluation dependencies load only when a benchmark verb runs)
    import os  # noqa: PLC0415 (same)

    from harness.features.evals.contracts import PilotParticipant as Participant  # noqa: PLC0415 (same)

    try:
        device: int | None = root.stat().st_dev
    except OSError:
        device = None
    locked = False
    try:
        with (root / ".participant-probe").open("a", encoding="utf-8") as probe:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            fcntl.flock(probe, fcntl.LOCK_UN)
    except OSError:
        locked = False
    return Participant(
        name=f"harness-cli-{os.getpid()}",
        canonical_state_root=identity.canonical_state_root,
        device_id=device,
        cooperative_locking=locked,
    )


async def run_benchmark_in_session(asked: _BenchmarkFlags) -> OutcomeStateReport:
    """Open this deployment's budget record on the caller's shared root and run one benchmark on it.

    The record opens on the root the caller named, never on this checkout's state folder, so every
    participating process opens the same one. A benchmark refused on its cases opens no database.

    Args:
        asked: The verb's resolved flags.

    Returns:
        The benchmark's state report.

    Raises:
        typer.Exit: The objective is not a known one, the inputs file cannot be read or parsed, or no
            `--journey` index was named once the cases are admitted.
    """
    from contextlib import AsyncExitStack  # noqa: PLC0415 (evaluation dependencies load only when a benchmark verb runs)

    from core.ai.evaluation.contracts import FileEvaluationBudgetRecord  # noqa: PLC0415 (same)
    from pydantic import TypeAdapter, ValidationError  # noqa: PLC0415 (same)

    from harness.features.evals.contracts import BenchmarkInputs, BenchmarkRequest, OutcomeObjective, run_outcome_benchmark  # noqa: PLC0415 (same)
    from harness.settings import HarnessSettings  # noqa: PLC0415 (same)

    try:
        objective = TypeAdapter(OutcomeObjective).validate_python(asked.objective)
    except ValidationError:
        fail("--objective is one of task-success, measured-generative-model-cost")
    try:
        inputs = BenchmarkInputs.model_validate_json(asked.inputs.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        fail(str(error))
    settings = HarnessSettings()
    asked.shared_root.mkdir(parents=True, exist_ok=True)
    record = FileEvaluationBudgetRecord(asked.shared_root)
    request = BenchmarkRequest(
        objective=objective,
        cases=asked.cases,
        inputs=inputs,
        report_root=asked.report_root,
        limits=asked.limits,
        participants=(this_process_participant(record.state_identity(), asked.shared_root),),
        run_engine=settings.run.run_engine,
        temporal_address=settings.run.temporal_address,
    )
    async with AsyncExitStack() as stack:
        return await run_outcome_benchmark(request, record, _DeploymentComposer(settings, record, asked, inputs, stack))
    raise AssertionError


class _DeploymentComposer:
    """Builds the budgeted baseline and grader and the workflow-automation arm, asked only once the cases are admitted."""

    def __init__(self, settings: HarnessSettings, record: BenchmarkRecord, asked: _BenchmarkFlags, inputs: BenchmarkInputs, stack: AsyncExitStack) -> None:
        self.settings = settings
        self.record = record
        self.journey = asked.journey
        self.inputs = inputs
        self.stack = stack

    async def __call__(self, cycle_id: str) -> ComposedBenchmark:
        """Everything this benchmark runs through, mounted on the record for `cycle_id`, failing when no journey index was named."""
        from core.ai.evaluation.contracts import RunIdentity, content_fingerprint  # noqa: PLC0415 (evaluation dependencies load only here)

        from harness.adapters.contracts import ModelTiers, tier_in_effect  # noqa: PLC0415 (same)
        from harness.container import create_container  # noqa: PLC0415 (same)
        from harness.features.evals.contracts import (  # noqa: PLC0415 (same)
            BenchmarkArms,
            BenchmarkMount,
            ComposedBenchmark,
            WorkflowAutomationTaskRunner,
            budgeted,
        )
        from harness.features.evals.infrastructure.agents import BASELINE_IDENTITY, GRADER_IDENTITY, ModelTaskGrader, SingleRunTaskRunner  # noqa: PLC0415 (same)
        from harness.features.evals.infrastructure.files import FilePilotRuns  # noqa: PLC0415 (same)
        from harness.features.running.contracts import RunArchiveQueries, SpendLogQueries  # noqa: PLC0415 (same)

        if self.journey is None:
            fail("--journey names the index of which run each case's workflow-automation trial produced")
        container = create_container(self.settings)
        self.stack.push_async_callback(container.close)
        tiers = await container.get(ModelTiers)
        selection = tier_in_effect(tiers, "worker")
        built = tiers.build(selection.setting.model, thinking=selection.setting.thinking)
        mount = BenchmarkMount(record=self.record, cycle_id=cycle_id)
        baseline_model = budgeted(built, mount, path=f"baseline:{built.model_name}", role="worker")
        grader_model = budgeted(built, mount, path=f"grader:{built.model_name}", role="grader")
        conditions = content_fingerprint(f"{self.settings.run.run_engine}\n{built.model_name}\n{selection.setting.thinking}")
        grader_fingerprint = content_fingerprint(GRADER_IDENTITY)
        return ComposedBenchmark(
            models=(baseline_model, grader_model),
            arms=BenchmarkArms(
                baseline=SingleRunTaskRunner(baseline_model),
                candidate=WorkflowAutomationTaskRunner(
                    FilePilotRuns(self.journey), await container.get(RunArchiveQueries), await container.get(SpendLogQueries)
                ),
                grader=ModelTaskGrader(grader_model),
            ),
            baseline=RunIdentity(
                model=built.model_name,
                model_version=selection.setting.model,
                prompt_fingerprint=content_fingerprint(BASELINE_IDENTITY),
                implementation_fingerprint=content_fingerprint("harness.features.evals.outcome_runner.SingleRunTaskRunner"),
                conditions_fingerprint=conditions,
                grader_fingerprint=grader_fingerprint,
            ),
            candidate=RunIdentity(
                model=built.model_name,
                model_version=selection.setting.model,
                prompt_fingerprint=content_fingerprint(self.inputs.prompt),
                implementation_fingerprint=content_fingerprint(self.inputs.workflow),
                conditions_fingerprint=conditions,
                grader_fingerprint=grader_fingerprint,
            ),
        )


@app.command(name="serve")
def serve(host: str = "127.0.0.1", port: int = 8770) -> None:
    """Serve the app: the coding agent and workflow runtime AI Factory Studio talks to."""
    import uvicorn  # noqa: PLC0415 (the server dependency stays off the review verbs' import path)

    uvicorn.run("harness.main:create_app", factory=True, host=host, port=port)


@evals_app.command(name="inspection")
def inspection_evals(
    dataset: str | None = typer.Option(None, "--dataset", help="Versioned diagnostic JSON. Defaults to the shipped evidence-access cases."),
    output: str | None = typer.Option(None, "--output", help="Directory for immutable run reports, traces and accumulating learning records."),
    tier: str = typer.Option("helper", "--tier", help="The model tier to freeze for both variants: helper, worker or orchestrator."),
    repetitions: int = typer.Option(3, "--repetitions", min=2, max=10, help="Repeated trials per evaluation case and variant."),
) -> None:
    """Compare complete inspector evidence against hidden instructions and tool definitions."""
    asyncio.run(_inspection_evals(dataset, output, tier, repetitions))


async def _inspection_evals(dataset: str | None, output: str | None, tier: str, repetitions: int) -> None:
    """Resolve one fixed model and run a paired comparison without changing the configured tier."""
    from pathlib import Path  # noqa: PLC0415 (evaluation dependencies load only when this verb runs)

    from core.ai.inspection.contracts import DiagnosticDataset  # noqa: PLC0415 (same)
    from core.ai.workflows.contracts import Tier  # noqa: PLC0415 (same)
    from pydantic import TypeAdapter  # noqa: PLC0415 (same)

    from harness.adapters.contracts import ModelTiers, tier_in_effect  # noqa: PLC0415 (same)
    from harness.container import create_container  # noqa: PLC0415 (same)
    from harness.features.inspection.contracts import run_comparison  # noqa: PLC0415 (same)
    from harness.settings import CHECKOUT, HarnessSettings  # noqa: PLC0415 (same)

    settings = HarnessSettings()
    try:
        selected_tier = TypeAdapter(Tier).validate_python(tier)
        path = Path(dataset) if dataset else CHECKOUT / "evals" / "inspection" / "evidence-access.json"
        given = DiagnosticDataset.model_validate_json(await asyncio.to_thread(path.read_text, encoding="utf-8"))
    except (OSError, ValueError) as error:
        fail(str(error))
    destination = Path(output) if output else settings.state.home / "inspection-evaluations"
    container = create_container(settings)
    try:
        tiers = await container.get(ModelTiers)
        selection = tier_in_effect(tiers, selected_tier)
        model = tiers.build(selection.setting.model, thinking=selection.setting.thinking)
        evaluation_cases = sum(case.split == "test" for case in given.cases)
        typer.echo(f"Evaluating {evaluation_cases} cases, {repetitions} trials per variant, model {model.model_name}.")
        typer.echo("Evidence-removal experiment, not a previous-release comparison. Split labels alone do not establish an independent test set.")
        report = await run_comparison(given, model, output=destination, repetitions=repetitions)
    finally:
        await container.close()
    typer.echo(
        f"{report.comparison.verdict}: task success {report.comparison.baseline_success_rate:.1%} -> "
        f"{report.comparison.candidate_success_rate:.1%}, owner-calibrated={report.candidate.owner_calibrated}"
    )
    typer.echo(f"Reports and traces: {report.output_directory}")
    typer.echo(f"Learning history: {destination / 'learning'}")


@app.command(name="inspect")
def inspect_cmd(
    run: str | None = typer.Option(None, "--run", help="A run id: InspectRun under every watch that selects it."),
    since: int | None = typer.Option(
        None, "--since", help="Hours: InspectRun for every run that ended in the window and was never inspected, under every watch."
    ),
) -> None:
    """Issue inspection commands by hand. The running harness serves them. This verb only queues them and reports."""
    if (run is None) == (since is None):
        fail("give exactly one of --run and --since")
    asyncio.run(_inspect(run, since))


async def _inspect(run: str | None, since: int | None) -> None:
    from contextlib import AsyncExitStack  # noqa: PLC0415 (heavy async wiring stays off the review verbs' import path)

    from core.ai.history.contracts import PoolDatabase  # noqa: PLC0415 (same)
    from core.ai.inspection.contracts import JobLanes, Sweep  # noqa: PLC0415 (same)

    from harness.adapters.contracts import RecordDatabase  # noqa: PLC0415 (same)
    from harness.container import create_container  # noqa: PLC0415 (same)
    from harness.features.inspection.contracts import issue_for_run, issue_since  # noqa: PLC0415 (same)
    from harness.features.inspection.infrastructure.postgres import InspectionDatabase, RecordRuns  # noqa: PLC0415 (same)
    from harness.settings import HarnessSettings, InspectionSettings  # noqa: PLC0415 (same)

    settings = HarnessSettings()
    container = create_container(settings)
    async with AsyncExitStack() as stack:
        stack.push_async_callback(container.close)
        await provision_record(container)
        await container.get(InspectionDatabase)
        watches = (await container.get(InspectionSettings)).watches
        lanes = await container.get(JobLanes)
        if run is not None:
            record = PoolDatabase((await container.get(RecordDatabase)).pool)
            issued = await issue_for_run(run, runs=RecordRuns(record), watches=watches, lanes=lanes)
            if issued is None:
                fail(f"no run {run!r} in the record.")
            if not issued:
                typer.echo(f"no watch selects run {run}.")
            for one in issued:
                typer.echo(f"{'queued' if one.created else 'already live'}  inspect_run {one.job_id}  watch {one.watch}")
            return
        if since is None:
            fail("give exactly one of --run and --since")
        for name, passed in await issue_since(since, sweep=await container.get(Sweep)):
            typer.echo(f"watch {name}: {passed.considered} run(s) in the last {since}h, {passed.enqueued} queued")


if __name__ == "__main__":
    app()
