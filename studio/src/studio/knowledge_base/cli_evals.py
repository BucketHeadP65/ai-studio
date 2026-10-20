"""The `studio evals` command group: score the agent under test against a dataset and tune its retrieval.

`run` scores a dataset and `gen-dataset` draws a new dataset or tops one up. `tune`, `apply-policy` and
`rollback-policy` tune the retrieval policy and apply it, and `recommend`, `apply-spec` and `verify-recommendation`
propose schema edits, apply them and measure them. `coverage`, `freshness` and `freeze` check a dataset and freeze a
held-out part of it. `experiment` runs a factorial sweep and `rca` analyses the telemetry of evaluation runs. The
entry module adds `evals_app` to the root `app`, so each command runs as `studio evals <name>`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol

import typer

from core.cqrs.contracts import MessageBus
from core.thinking import Thinking  # noqa: TC002 (typer reads the --thinking option's annotation at runtime)
from engram.evals.commands import (
    CheckFreshness,
    CheckGenerator,
    ComputeCoverage,
    CoverageComputedReport,
    DatasetFrozenReport,
    DatasetScored,
    FreezeDataset,
    FreshnessCheckedReport,
    GenerateEvalDataset,
    GenerateEvalDatasetReport,
    GeneratorChecked,
    PolicyRolledBackReport,
    RecommendationReport,
    RecommendStructure,
    RollbackPolicy,
    ScoreDataset,
    TuneRetrieval,
    TuningReport,
    VerifyRecommendation,
    VerifyReport,
)
from engram.evals.generation.contracts import (
    ARCHETYPE_NAMES,
    build_logfire_dataset_client,
    freshness_artifacts_dir,
    load_dataset,
    push_dataset_to_logfire,
    stratum_label,
)
from engram.evals.generator_check.contracts import RANK_DECIMALS
from engram.evals.harness.contracts import (
    EvaluationRunConfig,
    ExperimentAxes,
    apply_policy,
    average_metrics,
    configure_logfire,
    enable_local_span_recording,
    experiment_span,
    resolve_logfire_settings,
    run_eval,
    run_experiment,
    run_traced_eval_on_dataset,
)
from engram.evals.judgments.contracts import JudgmentsImported, import_judgments
from engram.evals.paired_comparison.contracts import (
    ResultsFileUnreadableError,
    paired_comparisons,
    read_results,
    source_level_retrieval,
    source_level_retrieval_per_query_type,
    write_paired_comparison,
)
from engram.evals.rca.contracts import (
    build_logfire_query_client,
    build_rca_prompt,
    build_rca_report,
    load_named_queries,
    render_rca_report_markdown,
)
from engram.evals.recommendation.contracts import ApplySchemaEditsReport, apply_schema_edits
from engram.evals.scoring.contracts import DEFAULT_SEARCH_SETUP, SEARCH_SETUPS, HitSources, search_setup
from engram.evals.shared.contracts import Archetype
from engram.evals.tuning.contracts import (
    DEFAULT_INSTRUCTIONS,
    DEFAULT_STRATEGY,
    EVAL_STRATEGY_NAMES,
    POLICY_MODULE_PATH,
    load_active_policy,
    load_policy,
)
from engram.kb.contracts import ACTIVE_POLICY_PATH
from pydantic import ValidationError

from studio.knowledge_base.cli_support import (
    ActiveKnowledgeBase,
    KnowledgeBaseMissingError,
    _build_container,
    _fail,
    _run_command,
    announce_identity,
    announce_knowledge_base,
    load_knowledge_base_schema_or_fail,
    resolve_knowledge_base_root,
    resolve_partition,
    settings_knowledge_base_path,
)
from studio.knowledge_base.settings import EvalsSettings, KnowledgeBaseSettings, fetch_application_settings
from studio.knowledge_base.wiring.contracts import EvaluationSqlTools

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import AbstractContextManager

    from core.resolver import Resolver
    from engram.evals.harness.contracts import (
        ApplyReport,
        EvaluationRunReport,
        ExperimentAxis,
        ExperimentRun,
        ExperimentVariant,
        TracedEvaluationRun,
        VariantEvaluator,
    )
    from engram.evals.paired_comparison.contracts import PairedComparison, PairedScores, ResultsFile, SourceLevelRetrieval


# The default model: pydantic-ai's `TestModel`, which needs no network, deployment or credentials.
_TEST_MODEL = "test-model"

# The dataset file under `evals.datasets_dir` that `evals run` scores when `--dataset` is not given.
_DEFAULT_DATASET_FILE = "seed.yaml"

# The shipped `searching-the-knowledge-base` skill file whose managed instructions block `evals apply-policy`
# rewrites, relative to the engram package root.
_SKILL_MD_RELPATH = ("plugin", "skills", "searching-the-knowledge-base", "SKILL.md")


evals_app = typer.Typer(
    name="evals", help="Run retrieval evals: score the agent-under-test against a dataset.", no_args_is_help=True
)


def _resolve_active_settings(knowledge_base: str | None, *, cwd: Path) -> ActiveKnowledgeBase:
    """The settings and the knowledge base for an evals command: the one `--knowledge-base` names, or the one `cwd` sits in."""
    try:
        root = resolve_knowledge_base_root(knowledge_base, cwd=cwd)
    except KnowledgeBaseMissingError as exc:
        _fail(str(exc))
    return ActiveKnowledgeBase(application=fetch_application_settings(), knowledge_base=KnowledgeBaseSettings(path=root))


async def _resolve_eval_dataset_path(ctx: typer.Context, dataset_override: str | None) -> Path:
    """The dataset path: `--dataset` when given, else `seed.yaml` under the configured `evals.datasets_dir`."""
    if dataset_override is not None:
        return Path(dataset_override)
    container = _build_container(ctx)
    evals_settings = await container.get(EvalsSettings)
    return evals_settings.datasets_dir / _DEFAULT_DATASET_FILE


async def _default_run_instructions(container: Resolver) -> str:
    """The instructions of the knowledge base's retrieval policy file, else `DEFAULT_INSTRUCTIONS`, exiting 1 on a malformed file."""
    knowledge_base_settings = await container.get(KnowledgeBaseSettings)
    try:
        resolution = load_active_policy(knowledge_base_settings.path)
    except (ValidationError, ValueError) as exc:
        _fail(f"invalid active-policy file in {knowledge_base_settings.path}: {exc}")
    if resolution is None:
        return DEFAULT_INSTRUCTIONS
    typer.echo("instructions: active-policy (knowledge-base-tuned)", err=True)
    return resolution.instructions


async def _run_evals(container: Resolver, *, config: EvaluationRunConfig, dataset_path: Path) -> EvaluationRunReport:
    """Score the dataset at `dataset_path` with the agent `config` describes."""
    return await run_eval(container, config=config, dataset_path=dataset_path)


async def _run_evals_after_identity(
    ctx: typer.Context,
    container: Resolver,
    settings: ActiveKnowledgeBase,
    *,
    collection: str,
    dataset_override: str | None,
    instructions_override: str | None,
    model: str,
    retrieval_strategy: str | None,
    code_mode: bool,
    thinking: Thinking | None,
    disclosure: bool,
    max_concurrency: int | None,
    sql: bool,
    max_tool_calls: int | None,
    pinned_search: bool,
    source_level: bool,
    results: str | None,
    resume: bool,
) -> TracedEvaluationRun:
    """Announce the identity, then resolve the dataset, instructions and run options and score the run, one event loop for all."""
    await announce_identity(container, settings, collection=collection)
    dataset_path = await _resolve_eval_dataset_path(ctx, dataset_override)
    resolved_instructions = (
        instructions_override if instructions_override is not None else await _default_run_instructions(container)
    )
    return await run_traced_eval_on_dataset(
        container,
        config=EvaluationRunConfig(
            model_spec=model,
            instructions=resolved_instructions,
            retrieval_strategy=retrieval_strategy,
            code_mode=code_mode,
            disclosure=disclosure,
            max_concurrency=max_concurrency,
            sql=(await container.get(EvaluationSqlTools)).capability if sql else None,
            max_tool_calls=max_tool_calls,
            pinned_search=pinned_search,
            hit_sources=await container.get(HitSources) if source_level else None,
            results_path=Path(results) if results is not None else None,
            thinking=thinking,
            resume=resume,
        ),
        dataset=load_dataset(dataset_path),
    )


_THINKING_OPTION = typer.Option(
    None,
    "--thinking",
    help="Build the model at this thinking level, a request setting sent as its reasoning effort on every call, not a "
    "sentence added to the instructions. Default: the model's own. A deployment that takes no thinking level is refused.",
)


@evals_app.command(name="run")
def evals_run_cmd(
    ctx: typer.Context,
    *,
    model: str = typer.Option(_TEST_MODEL, "--model", help="Model under test: 'test-model', or an Azure chat-deployment name."),
    dataset: str | None = typer.Option(None, "--dataset", help="Dataset YAML. Defaults to <evals.datasets_dir>/seed.yaml."),
    instructions: str | None = typer.Option(None, "--instructions", help="System instructions for the agent-under-test."),
    retrieval_strategy: str | None = typer.Option(
        None, "--retrieval-strategy", help=f"Retrieval surface to expose (default 'full'): {', '.join(EVAL_STRATEGY_NAMES)}."
    ),
    code_mode: bool = typer.Option(
        default=False, help="Orchestrate the tools via the sandboxed run_code capability instead of sequential tool calls."
    ),
    thinking: Thinking | None = _THINKING_OPTION,
    disclosure: bool = typer.Option(
        default=False,
        help="Level the fetch tool (content plus child summaries) and add the ProgressiveDisclosure capability.",
    ),
    max_concurrency: int | None = typer.Option(
        None, "--max-concurrency", help="Cap the cases run in parallel (default: all at once). Use it against a rate-limited deployment."
    ),
    sql: bool = typer.Option(
        default=False,
        help="Give the agent the SQL tools (run_sql and describe_schema) beside the retrieval tools. A refused statement goes back to it as a retry.",
    ),
    max_tool_calls: int | None = typer.Option(
        None,
        "--max-tool-calls",
        min=0,
        help="End a case as failed once the agent asks for more tool calls than this (default: no limit). With --code-mode, "
        "each run_code call counts once and the tool calls its program makes do not count.",
    ),
    pinned_search: bool = typer.Option(
        default=False, help="Pin search to the knowledge base's own collection and leave out its collection argument. It keeps since and until."
    ),
    source_level: bool = typer.Option(
        default=False,
        help="Map each cited id to its source document and score recall@4, recall@10 and mrr against the dataset's gold "
        "source ids, one rank per citation, so a citation with no source or a repeat of a source cited above it matches "
        "nothing. Also score seen recall: the share of the gold sources that any tool result the model received named. "
        "The scores land in the results file, so it needs --results, and their means print after the table, overall and "
        "per query type.",
    ),
    results: str | None = typer.Option(
        None, "--results", help="Write the results file here: a header line, then one JSON line per case, failed cases included."
    ),
    resume: bool = typer.Option(
        default=False,
        help="Continue the --results file: keep every case that did not fail transiently (a model connection error, a timeout, "
        "an HTTP 429 or 5xx, a refused or closed database connection, or a statement timeout), run only the missing cases and "
        "those, and rewrite the file under the earlier header. A file written with other settings, on another index build or "
        "under another source scoring version is refused before any case runs. With no file there yet, every case runs.",
    ),
    knowledge_base: str | None = typer.Option(default=None, help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."),
) -> None:
    """Score the agent under test against a dataset and print the retrieval metrics.

    Builds the agent from `--model`, `--instructions` and the retrieval tools, runs every case of the dataset, and
    prints recall@k, precision@k, mrr and n_tool_calls, plus answer_strict and answer_official when the dataset holds
    expected answers. `--model test-model` needs no network. Any other value names an Azure deployment, whose endpoint
    comes from the settings. `--retrieval-strategy` picks the tools and search the agent gets, and `--code-mode` runs
    the tools inside one sandboxed `run_code` call, the SQL tools included. `--thinking` builds the model at a thinking
    level, sent with every request. `--disclosure` makes `fetch` return content plus child summaries
    and adds the progressive disclosure capability. `--max-concurrency` caps how many cases run at once, for a
    deployment that rejects a whole dataset sent at once. `--sql` adds the SQL tools, `--max-tool-calls` fails a case
    that asks for more tool calls, counting each `run_code` call once and never the calls its program makes under
    `--code-mode`, and `--pinned-search` keeps search in the knowledge base, its date range kept.
    `--results` writes one line per case, and `--source-level` scores that file's retrieval against source ids, so it
    is refused without `--results`. It then prints the mean source-level recall@4, recall@10 and mrr over the cases with
    gold ids, beside the mean seen recall, the share of the gold sources that any tool result the model received named,
    cited or not, overall and per query type. The table's recall, precision and mrr compare cited page ids and do not
    apply to source-id gold. `--resume` continues the `--results` file, so it is refused without `--results`. It keeps
    every case that did not fail transiently, runs only the missing cases and the transient failures, and rewrites the
    file under the earlier header. It then prints how many cases it kept and ran and a summary over every case in the
    file, and the source-level lines cover every case too. It warns when the knowledge base was indexed again since the
    file was started, and says when the cases run now miss their source scores because the source lookup failed. A file
    written with other settings, on another index build or under another source scoring version, is refused before any
    case runs. A failed run, such as a missing dataset, an unreachable model or database, or an unknown strategy, exits
    1 with a message.
    """
    if source_level and results is None:
        _fail("--source-level writes its scores to the results file, so pass --results PATH with it.")
    if resume and results is None:
        _fail("--resume continues a results file, so pass --results PATH with it.")
    settings = _resolve_active_settings(knowledge_base, cwd=Path.cwd())
    container = _build_container(ctx, settings)
    schema = load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings))
    run = _run_command(
        _run_evals_after_identity(
            ctx,
            container,
            settings,
            collection=schema.retrieval.collection,
            dataset_override=dataset,
            instructions_override=instructions,
            model=model,
            retrieval_strategy=retrieval_strategy,
            code_mode=code_mode,
            thinking=thinking,
            disclosure=disclosure,
            max_concurrency=max_concurrency,
            sql=sql,
            max_tool_calls=max_tool_calls,
            pinned_search=pinned_search,
            source_level=source_level,
            results=results,
            resume=resume,
        ),
        on_error="evals run failed", database=settings.database,
    )
    run.report.print(include_input=False, include_output=False, include_durations=False)
    if results is not None and (resume or source_level):
        written = _read_written_results(Path(results))
        ran = len(run.report.cases) + len(run.report.failures)
        if resume:
            _echo_resumed(written, ran=ran, knowledge_base_fingerprint=run.knowledge_base_fingerprint)
        if source_level:
            if run.source_lookup_error is not None and written.header.source_lookup_error is None:
                typer.echo(
                    f"source scores missing for the {ran} case(s) run now: the source lookup failed ({run.source_lookup_error}). "
                    "The kept cases keep theirs."
                )
            _echo_source_level_retrieval(written)
    if results is not None:
        typer.echo(f"results: {results}", err=True)


def _read_written_results(results: Path) -> ResultsFile:
    """The results file the run just wrote, exiting 1 with the reason when it cannot be read."""
    try:
        return read_results(results)
    except ResultsFileUnreadableError as exc:
        _fail(f"evals run failed: {exc}")


def _echo_resumed(written: ResultsFile, *, ran: int, knowledge_base_fingerprint: str) -> None:
    """Print how many cases a resumed run kept and ran, a warning when the index changed, then a summary over every case in the file."""
    lines = written.lines
    typer.echo(f"kept {len(lines) - ran} case(s) from the results file and ran {ran}. The table above covers the {ran} case(s) run now.")
    if knowledge_base_fingerprint != written.header.knowledge_base_fingerprint:
        typer.echo(
            f"warning: the knowledge base was indexed again since the results file was started "
            f"({written.header.knowledge_base_fingerprint} in the file, {knowledge_base_fingerprint} now), "
            f"so the {ran} case(s) run now read another index than the kept ones."
        )
    if not lines:
        return
    failed = sum(line.failed for line in lines)
    strict = sum(line.strict for line in lines) / len(lines)
    official = sum(line.official for line in lines) / len(lines)
    typer.echo(f"results file over {len(lines)} case(s): {failed} failed, strict accuracy {strict:.1%}, official accuracy {official:.1%}")


def _echo_source_level_retrieval(written: ResultsFile) -> None:
    """Print the run's mean source-level retrieval and seen recall, overall and per query type, then why the table above does not apply."""
    retrieval = source_level_retrieval(written.lines)
    if written.header.source_lookup_error is not None:
        typer.echo(f"source-level retrieval: none, the source lookup failed ({written.header.source_lookup_error})")
    elif retrieval is None:
        typer.echo("source-level retrieval: none, no case has gold ids")
    else:
        typer.echo(f"source-level retrieval over {retrieval.cases} case(s) with gold ids: {_retrieval_text(retrieval)}")
        for row in source_level_retrieval_per_query_type(written.lines):
            label = row.query_type if row.query_type is not None else "no query type"
            typer.echo(f"  {label}: {row.retrieval.cases} case(s), {_retrieval_text(row.retrieval)}")
        typer.echo("Seen recall is the share of each case's gold sources that any tool result the model received named, cited or not.")
    typer.echo(
        "The recall, precision and mrr in the table above compare cited page ids with the gold ids, "
        "so they do not apply when the gold ids are source ids."
    )


def _retrieval_text(retrieval: SourceLevelRetrieval) -> str:
    """Mean recall@4, recall@10, mrr and seen recall as the run summary prints them, the seen recall missing when no line holds one."""
    seen = f"{retrieval.seen_recall:.3f}" if retrieval.seen_recall is not None else "missing"
    return f"recall@4 {retrieval.recall_at_4:.3f}, recall@10 {retrieval.recall_at_10:.3f}, mrr {retrieval.mrr:.3f}, seen recall {seen}"


def _dataset_slug(page_id: str) -> str:
    """The dataset file stem for a page id: the id without its prefix up to the first dot, with dots turned into dashes."""
    _prefix, separator, rest = page_id.partition(".")
    return (rest if separator else page_id).replace(".", "-")


async def _default_dataset_out(container: Resolver, *, page_id: str | None, collection: str) -> Path:
    """The default dataset path under `evals.datasets_dir`: `<slug>.yaml` for one page, else `<collection>-structural.yaml`."""
    evals_settings = await container.get(EvalsSettings)
    stem = _dataset_slug(page_id) if page_id is not None else f"{collection}-structural"
    return evals_settings.datasets_dir / f"{stem}.yaml"


def _parse_archetypes(names: list[str]) -> tuple[Archetype, ...]:
    """Narrow the `--archetype` values to archetypes, where an empty list means all five and an unknown name exits 1."""
    archetypes: list[Archetype] = []
    for name in names:
        try:
            archetypes.append(Archetype(name))
        except ValueError:
            _fail(f"evals gen-dataset: --archetype must be one of {', '.join(ARCHETYPE_NAMES)}, got {name!r}")
    return tuple(archetypes)


async def _gen_dataset(container: Resolver, command: GenerateEvalDataset, *, push: bool) -> GenerateEvalDatasetReport:
    """Dispatch `command` and, when `push` is set and the run wrote a dataset, publish it to Logfire."""
    bus = await container.get(MessageBus)
    report = await bus.handle(command)
    if not isinstance(report, GenerateEvalDatasetReport):  # pragma: no cover (the generate handler returns its report)
        _fail("evals gen-dataset produced no report.")
    if push and report.plan is None:
        dataset = load_dataset(Path(report.out))
        push_dataset_to_logfire(dataset, client=build_logfire_dataset_client(await resolve_logfire_settings(container)))
    return report


async def _build_gen_command(
    container: Resolver,
    *,
    page: str | None,
    collection: str,
    archetypes: tuple[Archetype, ...],
    model: str,
    count: int | None,
    out: str | None,
    top_up_from: str | None = None,
    dry_run: bool = False,
    salt: str | None = None,
) -> GenerateEvalDataset:
    """Assemble the `GenerateEvalDataset` from the CLI flags, resolving the default `--out` path."""
    out_path = Path(out) if out is not None else await _default_dataset_out(container, page_id=page, collection=collection)
    return GenerateEvalDataset(
        collection=collection,
        page_ids=(page,) if page else (),
        archetypes=archetypes,
        model=model,
        count=count,
        out=str(out_path),
        top_up_from=top_up_from,
        dry_run=dry_run,
        salt=salt,
    )


async def _run_gen_dataset(
    container: Resolver,
    settings: ActiveKnowledgeBase,
    *,
    page: str | None,
    collection: str,
    archetypes: tuple[Archetype, ...],
    model: str,
    count: int | None,
    out: str | None,
    push: bool,
    top_up_from: str | None = None,
    dry_run: bool = False,
    salt: str | None = None,
) -> GenerateEvalDatasetReport:
    """Announce the identity, then build the command from the flags and dispatch it, one event loop for both."""
    await announce_identity(container, settings, collection=collection)
    command = await _build_gen_command(
        container,
        page=page,
        collection=collection,
        archetypes=archetypes,
        model=model,
        count=count,
        out=out,
        top_up_from=top_up_from,
        dry_run=dry_run,
        salt=salt,
    )
    return await _gen_dataset(container, command, push=push)


# The repeatable `--archetype` option.
_ARCHETYPE_OPTION = typer.Option(
    [], "--archetype", help=f"Structural archetype(s) to cover. Repeatable, and empty means all five: {', '.join(ARCHETYPE_NAMES)}."
)


def _echo_gen_dataset_report(report: GenerateEvalDatasetReport) -> None:
    """Print a gen-dataset report: the top-up plan of a dry run, or the summary of the dataset written."""
    archetypes = ", ".join(archetype.value for archetype in report.requested_archetypes)
    if report.plan is not None:
        generate = ", ".join(f"{stratum_label(cell.stratum)} x{cell.quota}" for cell in report.plan.generate) or "none"
        retire = ", ".join(f"{record.case_name} ({record.reason})" for record in report.plan.retire) or "none"
        typer.echo(f"Top-up plan (parent {report.plan.parent_version}) [{archetypes}]: dry run, nothing written")
        typer.echo(f"  generate: {generate}")
        typer.echo(f"  retire: {retire}")
        return
    retired = f", {report.retired} retired" if report.retired else ""
    typer.echo(f"Generated {report.n_cases} golden case(s) [{archetypes}]{retired} -> {report.out}")


@evals_app.command(name="gen-dataset")
def evals_gen_dataset_cmd(
    ctx: typer.Context,
    *,
    page: str | None = typer.Option(
        None, "--page", help="Restrict seeds to one page id (e.g. note.release-plan). Omit for the whole collection."
    ),
    collection: str | None = typer.Option(
        default=None, help="Knowledge base partition to draw structural seeds from, defaults to the knowledge base schema's own retrieval.collection."
    ),
    archetype: list[str] = _ARCHETYPE_OPTION,
    count: int | None = typer.Option(None, "--count", help="Override the total number of cases the run generates."),
    model: str = typer.Option(_TEST_MODEL, "--model", help="Drafter model: 'test-model' or an Azure chat-deployment name."),
    out: str | None = typer.Option(
        None, "--out", help="Output YAML path. Defaults to <evals.datasets_dir>/<slug-or-collection>-structural.yaml."
    ),
    top_up: str | None = typer.Option(
        None, "--top-up", help="Top up a parent version ('current' or a version path) instead of a fresh draw."
    ),
    dry_run: bool = typer.Option(default=False, help="For --top-up: print the per-stratum generate/retire plan and write nothing."),
    salt: str | None = typer.Option(None, "--salt", help="Sampling salt. Defaults to the parent stamp's on a top-up, else generated."),
    push: bool = typer.Option(default=False, help="Also publish the dataset to the hosted Logfire datasets API."),
    knowledge_base: str | None = typer.Option(default=None, help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."),
) -> None:
    """Draw a new dataset from the knowledge base's structure, or top one up, and write it as YAML.

    Draws structural seeds whose expected pages are known from the structure itself, drafts one question per seed,
    curates the pool, and writes the next dataset version. A written version never changes. `--archetype`, which
    repeats, picks the structural shapes to cover, all five when it is not given. `--collection` and `--page` scope
    the seeds, `--count` overrides the total, and `--salt` fixes the sampling draw. `--top-up current`, or a version
    path, drafts only the strata that lack cases and retires the cases they displace into the new version's
    retirement record, never changing the parent. The same salt always retires the same cases. `--dry-run` shows that plan and writes nothing.
    `--model test-model` needs no network. A failure, such as a bad archetype, a missing parent version or stamp,
    or an unreachable model or database, exits 1 with a message.
    """
    settings = _resolve_active_settings(knowledge_base, cwd=Path.cwd())
    container = _build_container(ctx, settings)
    partition = resolve_partition(collection, load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings)))
    archetypes = _parse_archetypes(archetype)
    report = _run_command(
        _run_gen_dataset(
            container,
            settings,
            page=page,
            collection=partition,
            archetypes=archetypes,
            model=model,
            count=count,
            out=out,
            push=push,
            top_up_from=top_up,
            dry_run=dry_run,
            salt=salt,
        ),
        on_error="evals gen-dataset failed", database=settings.database,
    )
    _echo_gen_dataset_report(report)


async def _default_policy_out(container: Resolver, *, collection: str) -> Path:
    """The default path of the winning policy: `<collection>.yaml` under the configured `evals.tuning.policies_dir`."""
    evals_settings = await container.get(EvalsSettings)
    return evals_settings.tuning.policies_dir / f"{collection}.yaml"


async def _build_tune_command(
    container: Resolver,
    *,
    dataset: str,
    model: str,
    collection: str,
    out: str | None,
    max_rounds: int | None,
    token_budget: int | None,
    knowledge_base: str = ".",
    apply_runtime: bool = False,
) -> TuneRetrieval:
    """Assemble the `TuneRetrieval` from the CLI flags, resolving the default `--out` path."""
    out_path = Path(out) if out is not None else await _default_policy_out(container, collection=collection)
    return TuneRetrieval(
        dataset=dataset,
        model=model,
        collection=collection,
        out=str(out_path),
        max_rounds=max_rounds,
        token_budget=token_budget,
        knowledge_base=knowledge_base,
        apply_runtime=apply_runtime,
    )


async def _tune(container: Resolver, command: TuneRetrieval) -> TuningReport:
    """Dispatch `command` and return the tuning report."""
    bus = await container.get(MessageBus)
    report = await bus.handle(command)
    if not isinstance(report, TuningReport):  # pragma: no cover (the tune handler returns its report)
        _fail("evals tune produced no report.")
    return report


async def _run_tune(
    container: Resolver,
    settings: ActiveKnowledgeBase,
    *,
    dataset: str,
    model: str,
    collection: str,
    out: str | None,
    max_rounds: int | None,
    token_budget: int | None,
    knowledge_base: str = ".",
    apply_runtime: bool = False,
) -> TuningReport:
    """Announce the identity, then build the command from the flags and dispatch it, one event loop for both."""
    await announce_identity(container, settings, collection=collection)
    command = await _build_tune_command(
        container,
        dataset=dataset,
        model=model,
        collection=collection,
        out=out,
        max_rounds=max_rounds,
        token_budget=token_budget,
        knowledge_base=knowledge_base,
        apply_runtime=apply_runtime,
    )
    return await _tune(container, command)


@evals_app.command(name="tune")
def evals_tune_cmd(
    ctx: typer.Context,
    *,
    dataset: str = typer.Option(..., "--dataset", help="Dataset YAML the tuning loop hill-climbs against."),
    model: str = typer.Option(
        _TEST_MODEL, "--model", help="Model under test: 'test-model', or an Azure chat-deployment name."
    ),
    collection: str | None = typer.Option(
        default=None, help="Knowledge base partition the run is tagged with, defaults to the knowledge base schema's own retrieval.collection."
    ),
    out: str | None = typer.Option(
        None, "--out", help="Winning-policy YAML path. Defaults to <evals.tuning.policies_dir>/<collection>.yaml."
    ),
    max_rounds: int | None = typer.Option(None, "--max-rounds", help="Override the configured objective's hard cap on tuning rounds."),
    token_budget: int | None = typer.Option(None, "--token-budget", help="Override the configured objective's cumulative token cap."),
    knowledge_base: str = typer.Option(default=".", help="Knowledge base root: the incumbent baseline source and the --apply-runtime target."),
    apply_runtime: bool = typer.Option(
        default=False,
        help="Apply the winner to the knowledge base's _meta/active-policy.yaml iff all four guardrails pass (refusal is reported).",
    ),
) -> None:
    """Tune the retrieval policy against a dataset and write the winning policy.

    Splits the dataset and measures the incumbent, then proposes one targeted policy change per round and scores it
    on both splits. It saves each round's summary and writes the accepted candidate that scored best on the
    validation split. `--max-rounds` and `--token-budget` override the configured limits, and `--model test-model`
    needs no network. The incumbent is the knowledge base's retrieval policy when `--knowledge-base` has one.
    `--apply-runtime` applies the winner to the knowledge base's `_meta/active-policy.yaml` only when the frozen
    split exists, the freshness check passes, and the winner is not the incumbent and beat it on the frozen split.
    A refusal to apply is reported and the run still exits 0. A failure, such as a missing dataset or an unreachable
    model or database, exits 1 with a message.
    """
    settings = _resolve_active_settings(knowledge_base, cwd=Path.cwd())
    container = _build_container(ctx, settings)
    partition = resolve_partition(collection, load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings)))
    report = _run_command(
        _run_tune(
            container,
            settings,
            dataset=dataset,
            model=model,
            collection=partition,
            out=out,
            max_rounds=max_rounds,
            token_budget=token_budget,
            knowledge_base=knowledge_base,
            apply_runtime=apply_runtime,
        ),
        on_error="evals tune failed", database=settings.database,
    )
    val = "n/a" if report.winner_val_recall is None else f"{report.winner_val_recall:.3f}"
    typer.echo(
        f"Tuned in {report.n_rounds} round(s) ({report.accepted_rounds} accepted, stop: {report.stop_reason or 'none'}): "
        f"recall {report.baseline_recall:.3f} -> {report.winner_recall:.3f} (val {val}) -> {report.out}"
    )
    if report.frozen_baseline_recall is not None and report.frozen_winner_recall is not None:
        beat = "beat" if report.frozen_beat else "did not beat"
        typer.echo(f"  frozen: {report.frozen_baseline_recall:.3f} -> {report.frozen_winner_recall:.3f} (winner {beat} the incumbent)")
    if report.n_findings:
        typer.echo(
            f"  reflection: {report.n_findings} gated finding(s), "
            f"predictions graded {report.n_prediction_hits}/{report.n_predictions_graded} hit"
        )
    if report.applied_runtime:
        typer.echo("  applied runtime policy -> _meta/active-policy.yaml (previous snapshotted to _meta/policy-history/)")
    elif report.apply_refusal is not None:
        typer.echo(f"  apply-runtime REFUSED: {report.apply_refusal}")
    if report.dataset_staleness:
        typer.echo(f"WARNING: dataset is {report.dataset_staleness:.1%} stale. Consider `evals gen-dataset --top-up`")


async def _rollback(container: Resolver, command: RollbackPolicy) -> PolicyRolledBackReport:
    """Dispatch `command` and return the rollback report."""
    bus = await container.get(MessageBus)
    report = await bus.handle(command)
    if not isinstance(report, PolicyRolledBackReport):  # pragma: no cover (the rollback handler returns its report)
        _fail("evals rollback-policy produced no report.")
    return report


async def _rollback_with_identity(
    container: Resolver, settings: ActiveKnowledgeBase, command: RollbackPolicy, *, collection: str
) -> PolicyRolledBackReport:
    """Announce the identity, then dispatch the rollback, one event loop for both."""
    await announce_identity(container, settings, collection=collection)
    return await _rollback(container, command)


@evals_app.command(name="rollback-policy")
def evals_rollback_policy_cmd(
    ctx: typer.Context, *, knowledge_base: str = typer.Option(default=".", help="Knowledge base root whose _meta/active-policy.yaml is rolled back.")
) -> None:
    """Restore the knowledge base's latest policy snapshot as its retrieval policy.

    Copies the highest-numbered `_meta/policy-history/active-policy.v{N}.yaml` into `_meta/active-policy.yaml`,
    after snapshotting the outgoing policy, so the history keeps every policy ever applied. A knowledge base with no
    policy history exits 1 with a message.
    """
    settings = _resolve_active_settings(knowledge_base, cwd=Path.cwd())
    container = _build_container(ctx, settings)
    collection = load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings)).retrieval.collection
    report = _run_command(
        _rollback_with_identity(container, settings, RollbackPolicy(knowledge_base=knowledge_base), collection=collection),
        on_error="evals rollback-policy failed", database=settings.database,
    )
    typer.echo(f"restored {report.restored_from} -> _meta/active-policy.yaml")
    if report.history_snapshot is not None:
        typer.echo(f"  outgoing policy snapshotted to {report.history_snapshot}")


def _skill_md_path() -> Path:
    """The shipped `searching-the-knowledge-base` skill file, found from the engram package root."""
    package_root = POLICY_MODULE_PATH.resolve().parents[4]
    return package_root.joinpath(*_SKILL_MD_RELPATH)


async def _import_judgments(source: Path, out: Path, *, per_type: int | None, salt: str) -> JudgmentsImported:
    """Import the judgments file at `source` into the evals dataset at `out`, sampled per query type when `per_type` is set."""
    return import_judgments(source, out, per_type=per_type, salt=salt)


@evals_app.command(name="import-judgments")
def evals_import_judgments_cmd(
    ctx: typer.Context,  # noqa: ARG001 (the shared command signature, import-judgments does pure file IO with no container)
    *,
    file: str = typer.Option(
        ...,
        "--file",
        help="Relevance judgments file, one JSON line per query: query_id, question and judgments, and optionally answer and query_type.",
    ),
    out: str = typer.Option(..., "--out", help="The evals dataset YAML to write, with its schema file beside it."),
    per_type: int | None = typer.Option(
        None, "--per-type", min=1, help="Keep this many cases of each query type, chosen by a salted hash of the case name. Default: every case."
    ),
    salt: str = typer.Option("", "--salt", help="The salt the per-type sample hashes with. The same salt keeps the same cases."),
) -> None:
    """Turn a relevance judgments file into an evals dataset. Reads no knowledge base and changes nothing else.

    Each query becomes one case named after it. Its gold ids are the documents graded above 0, the highest grade
    first, and its answer, its query type and every grade are kept on the case. A query with an answer and no
    relevant document becomes a case with no gold ids. A query with neither is left out and counted. With
    --per-type, only that many cases of each query type are kept, and the cases with no query type count as
    one type. A line that does not parse exits 1 naming the line, and nothing is written.
    """
    report = _run_command(_import_judgments(Path(file), Path(out), per_type=per_type, salt=salt), on_error="evals import-judgments failed", database=None)
    typer.echo(f"Imported {report.cases} case(s), {report.skipped} skipped for having no relevant document and no answer -> {report.out}")
    for query_type, count in report.cases_per_type.items():
        typer.echo(f"  {query_type}: {count} case(s)")
    untyped = report.cases - sum(report.cases_per_type.values())
    if untyped:
        typer.echo(f"  no query type: {untyped} case(s)")


class _ComparedPair(NamedTuple):
    """One `--pair`: the baseline's results file, then the candidate's."""

    baseline: Path
    candidate: Path


def _compared_pair(value: str) -> _ComparedPair:
    """One `--pair` value split at its comma, refused as a usage error unless it names exactly two paths."""
    paths = [path.strip() for path in value.split(",")]
    if len(paths) != 2 or not all(paths):  # noqa: PLR2004 (a pair is two paths)
        msg = f"--pair needs exactly two paths separated by a comma, BASE.jsonl,CANDIDATE.jsonl, got {value!r}"
        raise typer.BadParameter(msg)
    baseline, candidate = paths
    return _ComparedPair(baseline=Path(baseline), candidate=Path(candidate))


def _report_path(out: str) -> Path:
    """The `--out` report path, refused as a usage error unless it ends in `.md`."""
    path = Path(out)
    if path.suffix != ".md":
        msg = f"--out must end in .md, so the YAML twin can sit beside it, got {out!r}"
        raise typer.BadParameter(msg)
    return path


async def _compare(pairs: Sequence[_ComparedPair], out: Path) -> tuple[tuple[PairedComparison, ...], Path, Path]:
    """Read every pair's results files, compare each pair case by case and write the report and its YAML twin."""
    comparisons = paired_comparisons([(read_results(pair.baseline), read_results(pair.candidate)) for pair in pairs])
    markdown, twin = write_paired_comparison(comparisons, out=out)
    return comparisons, markdown, twin


def _difference_text(scores: PairedScores) -> str:
    """The difference in points, with its 95 percent interval when there is one."""
    difference = f"{scores.difference * 100:+.1f} points"
    if scores.interval is None:
        return difference
    low, high = scores.interval
    return f"{difference} (95% interval {low * 100:+.1f} to {high * 100:+.1f})"


_COMPARE_PAIR_OPTION = typer.Option(
    ...,
    "--pair",
    metavar="BASE,CANDIDATE",
    help=(
        "Two results files of one dataset, as `evals run --results` writes them, separated by a comma: the baseline's, then the "
        "candidate's. A value that is not exactly two paths is refused. Repeat it to compare more pairs in one call."
    ),
)


@evals_app.command(name="compare")
def evals_compare_cmd(
    ctx: typer.Context,  # noqa: ARG001 (the shared command signature, compare does pure file IO with no container)
    *,
    pair: list[str] = _COMPARE_PAIR_OPTION,
    out: str = typer.Option(..., "--out", help="The markdown report to write, ending in .md. Its YAML twin is written beside it."),
) -> None:
    """Compare evaluation runs of one dataset case by case. Reads no knowledge base and changes nothing else.

    For each pair it reports the cases, both strict accuracies, the difference with its 95 percent interval, the
    cases only one run answered correctly, McNemar's exact p value and the p value Holm's method adjusts across
    every pair of the call. It reports the same per query type, with both official accuracies, both failed-case
    counts, the cases that read another knowledge base, each run's mean source-level recall@4, recall@10 and MRR
    over the cases with gold ids, and each run's mean seen recall with the mean paired difference. A seen recall a
    results file does not hold, as for a run that mapped no sources, shows as missing, never as zero. A failed
    case counts as incorrect. Two runs whose dataset, model, thinking level, instructions, tool-call budget, search
    pinning, search date range or knowledge base differ are refused. Two runs may differ in the knowledge base
    fingerprint and in the index build, and the report shows both runs' values. Two runs under one strategy may
    differ in the SQL switch or in code mode, not in both, and two runs under different strategies must share both and
    the candidate must be the baseline plus the graph leg. A results file missing a field the run writes predates the
    current format and is refused. Two runs that both carry source-level scores under different source scoring versions
    are refused, naming the run to run again. A refusal exits 1 naming the case or the field, and nothing is written. A `--pair` that is not two paths separated by a comma, or an `--out` that does not end in .md, exits 2
    before any file is read.
    """
    pairs = [_compared_pair(value) for value in pair]
    report_path = _report_path(out)
    comparisons, markdown, twin = _run_command(_compare(pairs, report_path), on_error="evals compare failed", database=None)
    for comparison in comparisons:
        scores = comparison.overall
        typer.echo(
            f"{comparison.candidate} against {comparison.baseline}: strict {scores.baseline_strict:.1%} -> {scores.candidate_strict:.1%} "
            f"over {scores.cases} case(s), difference {_difference_text(scores)}, "
            f"McNemar p {scores.p_value:.4f}, Holm p {scores.holm_p_value:.4f}"
        )
    typer.echo(f"Report -> {markdown}, YAML twin -> {twin}")


async def _apply_policy(*, policy_path: Path, knowledge_base_path: Path) -> ApplyReport:
    """Load the winning policy at `policy_path` and write it into its three targets in the working tree, never running git."""
    policy = load_policy(policy_path)
    return apply_policy(
        policy,
        policy_module_path=POLICY_MODULE_PATH,
        skill_md_path=_skill_md_path(),
        active_policy_path=knowledge_base_path.joinpath(*ACTIVE_POLICY_PATH),
    )


@evals_app.command(name="apply-policy")
def evals_apply_policy_cmd(
    ctx: typer.Context,  # noqa: ARG001 (the shared command signature, apply-policy does pure file IO with no container)
    *,
    policy: str = typer.Option(..., "--policy", help="Winning-policy YAML (an 'evals tune' artifact) to apply."),
    knowledge_base: str = typer.Option(default=".", help="Knowledge base root the active-policy file is written under (defaults to the CWD)."),
) -> None:
    """Apply a winning policy to the working tree. Never commits.

    Rewrites two source files in this repository: the engram package's `DEFAULT_INSTRUCTIONS` and the managed
    instructions block of the shipped `searching-the-knowledge-base` skill. It also saves the whole policy, with its
    strategy and tool description overrides, to the knowledge base's `_meta/active-policy.yaml`, where the server
    reads it. Every write stays in the working tree for you to review and commit, and the command runs no git.
    Applying the same policy again gives the same files. A failure, such as a missing policy file, an unreadable
    skill or bad YAML, exits 1 with a message.
    """
    announce_knowledge_base(Path(knowledge_base).resolve())
    report = _run_command(_apply_policy(policy_path=Path(policy), knowledge_base_path=Path(knowledge_base)), on_error="evals apply-policy failed", database=None)
    typer.echo(
        f"Applied policy (working tree only, no commit) -> instructions: {report.instructions_target}, "
        f"skill: {report.skill_target}, active-policy: {report.active_policy_target}"
    )


# -- recommendation loop ------------------------------------------------------


async def _default_recommendation_out(container: Resolver, *, collection: str) -> Path:
    """The default recommendation path: `<collection>.yaml` under the configured `evals.recommendation.recommendations_dir`."""
    evals_settings = await container.get(EvalsSettings)
    return evals_settings.recommendation.recommendations_dir / f"{collection}.yaml"


async def _build_recommend_command(
    container: Resolver, *, dataset: str, model: str, collection: str, policy: str | None, out: str | None, simulate: bool
) -> RecommendStructure:
    """Assemble the `RecommendStructure` from the CLI flags, resolving the default `--out` path."""
    out_path = Path(out) if out is not None else await _default_recommendation_out(container, collection=collection)
    return RecommendStructure(dataset=dataset, model=model, collection=collection, policy=policy, out=str(out_path), simulate=simulate)


async def _recommend(container: Resolver, command: RecommendStructure) -> RecommendationReport:
    """Dispatch `command` and return the recommendation report."""
    bus = await container.get(MessageBus)
    report = await bus.handle(command)
    if not isinstance(report, RecommendationReport):  # pragma: no cover (the recommend handler returns its report)
        _fail("evals recommend produced no report.")
    return report


async def _run_recommend(
    container: Resolver,
    settings: ActiveKnowledgeBase,
    *,
    dataset: str,
    model: str,
    collection: str,
    policy: str | None,
    out: str | None,
    simulate: bool,
) -> RecommendationReport:
    """Announce the identity, then build the command from the flags and dispatch it, one event loop for both."""
    await announce_identity(container, settings, collection=collection)
    command = await _build_recommend_command(
        container, dataset=dataset, model=model, collection=collection, policy=policy, out=out, simulate=simulate
    )
    return await _recommend(container, command)


@evals_app.command(name="recommend")
def evals_recommend_cmd(
    ctx: typer.Context,
    *,
    dataset: str = typer.Option(..., "--dataset", help="Dataset YAML the recommendation loop diagnoses against."),
    model: str = typer.Option(
        _TEST_MODEL, "--model", help="Model under test: 'test-model', or an Azure chat-deployment name."
    ),
    collection: str | None = typer.Option(
        default=None,
        help="Knowledge base partition the detectors read and the recommendation is stamped for, defaults to the knowledge base schema's own "
        "retrieval.collection.",
    ),
    policy: str | None = typer.Option(
        None,
        "--policy",
        help="Explicit policy YAML pinning the baseline run. Omit for the knowledge base's active policy, then the shipped baseline.",
    ),
    out: str | None = typer.Option(
        None, "--out", help="Recommendation YAML path. Defaults to <evals.recommendation.recommendations_dir>/<collection>.yaml."
    ),
    simulate: bool = typer.Option(
        default=False,
        help="Also run the shadow mechanical-split simulation: rank the over-budget gold sections' paragraph splits "
        "against the live collection and attach the measured movement to each restructure proposal's evidence.",
    ),
    knowledge_base: str | None = typer.Option(default=None, help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."),
) -> None:
    """Recommend edits to the knowledge base's schema from a baseline evaluation run, and write the recommendation.

    Scores one baseline evaluation run, diagnoses it against the indexed knowledge base, and drafts schema proposals
    plus free-form findings. It keeps the proposals that pass schema validation and the evidence thresholds, and writes
    the recommendation YAML with a draft ADR beside it. Nothing is applied: you migrate the notes, reindex and commit.
    `--policy` pins the baseline run, which otherwise uses the knowledge base's retrieval policy, then the shipped one.
    `--collection` scopes what is read, and `--model test-model` needs no network. `--simulate` adds the measured rank
    movement of a simulated split to each restructure proposal, still with no index and no judge. A failure, such as
    a missing dataset or an unreachable model or database, exits 1 with a message.
    """
    settings = _resolve_active_settings(knowledge_base, cwd=Path.cwd())
    container = _build_container(ctx, settings)
    partition = resolve_partition(collection, load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings)))
    report = _run_command(
        _run_recommend(container, settings, dataset=dataset, model=model, collection=partition, policy=policy, out=out, simulate=simulate),
        on_error="evals recommend failed", database=settings.database,
    )
    simulated = f", {report.n_simulated} simulated" if simulate else ""
    typer.echo(
        f"Recommended {report.n_proposals} proposal(s) [{report.evidence_grade}], {report.n_findings} finding(s){simulated}: "
        f"{', '.join(report.top_proposals) or 'none'} -> {report.recommendation_path} (ADR: {report.adr_path})"
    )


_APPLY_SPEC_FOLLOW_UP = """Follow-up protocol (the spec diff is inert until the knowledge base catches up):
  1. Migrate knowledge base content per each applied proposal's migration brief (authorial work).
  2. Reindex the collection: studio index
  3. Check the recommendation's predictions against the new knowledge base: studio evals verify-recommendation --recommendation {recommendation}
  4. Regenerate datasets for the new structure: studio evals gen-dataset"""


async def _apply_spec(*, recommendation_path: Path, proposal_ids: tuple[str, ...], knowledge_base_path: Path) -> ApplySchemaEditsReport:
    """Apply the chosen proposals of the recommendation to the knowledge base's schema file."""
    return apply_schema_edits(recommendation_path=recommendation_path, proposal_ids=proposal_ids, knowledge_base_path=knowledge_base_path)


_APPLY_SPEC_ID_OPTION = typer.Option(..., "--id", help="Proposal id to apply. Repeat the flag to apply several.")


@evals_app.command(name="apply-spec")
def evals_apply_spec_cmd(
    ctx: typer.Context,  # noqa: ARG001 (the shared command signature, apply-spec does pure file IO with no container)
    *,
    recommendation: str = typer.Option(..., "--recommendation", help="Recommendation YAML (an 'evals recommend' artifact) to select proposals from."),
    proposal_id: list[str] = _APPLY_SPEC_ID_OPTION,
    knowledge_base: str = typer.Option(default=".", help="Knowledge base root whose knowledge-base-schema.json is rewritten (defaults to the CWD)."),
) -> None:
    """Apply chosen proposals of a recommendation to the knowledge base's schema, in the working tree only.

    Rewrites the knowledge base's `knowledge-base-schema.json` with the chosen proposals' edits, then validates the
    result as a schema. It never indexes, migrates notes, regenerates datasets or runs git. You review the diff,
    migrate the notes as the briefs say, and commit. Applying the same selection again leaves the file byte for byte
    the same. A failure, such as a missing recommendation, an unknown `--id` or an edit the current schema rejects,
    exits 1 with a message.
    """
    announce_knowledge_base(Path(knowledge_base).resolve())
    report = _run_command(
        _apply_spec(recommendation_path=Path(recommendation), proposal_ids=tuple(proposal_id), knowledge_base_path=Path(knowledge_base)),
        on_error="evals apply-spec failed", database=None,
    )
    state = "changed" if report.changed else "unchanged"
    applied = ", ".join(report.applied_ids) or "none"
    skipped = f" (already applied: {', '.join(report.already_applied_ids)})" if report.already_applied_ids else ""
    typer.echo(f"Applied: {applied}{skipped} (working tree only, no commit) -> {report.knowledge_base_schema_path} [{state}]")
    typer.echo(_APPLY_SPEC_FOLLOW_UP.format(recommendation=recommendation))


async def _verify_recommendation(
    container: Resolver, settings: ActiveKnowledgeBase, command: VerifyRecommendation, *, collection: str
) -> VerifyReport:
    """Announce the identity, then dispatch `VerifyRecommendation` through the bus, one event loop for both."""
    await announce_identity(container, settings, collection=collection)
    bus = await container.get(MessageBus)
    report = await bus.handle(command)
    if not isinstance(report, VerifyReport):  # pragma: no cover (the verify handler returns its report)
        _fail("evals verify-recommendation produced no report.")
    return report


@evals_app.command(name="verify-recommendation")
def evals_verify_recommendation_cmd(
    ctx: typer.Context,
    *,
    recommendation: str = typer.Option(..., "--recommendation", help="Recommendation YAML (an 'evals recommend' artifact) to verify."),
    model: str = typer.Option(
        _TEST_MODEL, "--model", help="Model for the re-run: 'test-model', or an Azure chat-deployment name."
    ),
    dataset: str | None = typer.Option(None, "--dataset", help="Dataset override. Omit to re-run the recommendation's baseline dataset."),
    knowledge_base: str | None = typer.Option(default=None, help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."),
) -> None:
    """Re-run a saved recommendation's dataset after its edits are applied, and measure what changed.

    Re-runs the dataset under the policy the baseline recorded. A baseline case is kept only while its expected pages
    still exist, and the rest are dropped. Every prediction of the recommendation is graded by a fixed rule, and each
    change in the knowledge base is traced through the index manifest. Content changed outside the migration briefs
    raises a warning. Writes the `.verify.md` report beside the recommendation and saves the run. A failure, such as
    a missing recommendation or an unreachable model or database, exits 1 with a message.
    """
    settings = _resolve_active_settings(knowledge_base, cwd=Path.cwd())
    container = _build_container(ctx, settings)
    schema = load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings))
    command = VerifyRecommendation(recommendation=recommendation, model=model, dataset=dataset)
    report = _run_command(
        _verify_recommendation(container, settings, command, collection=schema.retrieval.collection), on_error="evals verify-recommendation failed", database=settings.database
    )
    verdicts = f"{report.predictions_held} held / {report.predictions_failed} failed / {report.predictions_unmeasurable} unmeasurable"
    deltas = f"targeted {report.targeted_mean_delta:+.3f}, untargeted {report.untargeted_mean_delta:+.3f}"
    typer.echo(
        f"Verified {report.recommendation}: retained {report.n_retained}, dropped {report.n_dropped}. {verdicts}. {deltas} -> {report.out}"
    )
    if report.drift_warning:
        typer.echo(f"WARNING: {report.drift_warning}")
    if report.dataset_staleness:
        typer.echo(f"WARNING: dataset is {report.dataset_staleness:.1%} stale. Consider `evals gen-dataset --top-up`")


# -- coverage ------------------------------------


async def _default_coverage_out(container: Resolver) -> Path:
    """The default coverage report folder: `coverage/` beside the configured `evals.datasets_dir`."""
    evals_settings = await container.get(EvalsSettings)
    return evals_settings.datasets_dir.parent / "coverage"


async def _build_coverage_command(container: Resolver, *, collection: str, dataset: str | None, out: str | None) -> ComputeCoverage:
    """Assemble the `ComputeCoverage` from the CLI flags, resolving the default `--out` directory."""
    out_dir = Path(out) if out is not None else await _default_coverage_out(container)
    return ComputeCoverage(collection=collection, dataset=dataset, out=str(out_dir))


async def _coverage(container: Resolver, command: ComputeCoverage) -> CoverageComputedReport:
    """Dispatch `command` and return the coverage report."""
    bus = await container.get(MessageBus)
    report = await bus.handle(command)
    if not isinstance(report, CoverageComputedReport):  # pragma: no cover (the coverage handler returns its report)
        _fail("evals coverage produced no report.")
    return report


async def _run_coverage(
    container: Resolver, settings: ActiveKnowledgeBase, *, collection: str, dataset: str | None, out: str | None
) -> CoverageComputedReport:
    """Announce the identity, then build the command from the flags and dispatch it, one event loop for both."""
    await announce_identity(container, settings, collection=collection)
    command = await _build_coverage_command(container, collection=collection, dataset=dataset, out=out)
    return await _coverage(container, command)


@evals_app.command(name="coverage")
def evals_coverage_cmd(
    ctx: typer.Context,
    *,
    collection: str | None = typer.Option(
        default=None, help="Knowledge base partition to census per stratum, defaults to the knowledge base schema's own retrieval.collection."
    ),
    dataset: str | None = typer.Option(
        None, "--dataset", help="Dataset YAML (or 'current') to audit. Omit to report the inventory-only census."
    ),
    out: str | None = typer.Option(None, "--out", help="Output directory. Defaults to <evals.datasets_dir>/../coverage."),
    knowledge_base: str | None = typer.Option(default=None, help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."),
) -> None:
    """Compute the knowledge base's coverage report and write it to disk. Changes nothing else.

    Takes a census of the knowledge base per stratum and computes the target allocation from the configured
    generation settings. Without `--dataset` it reports the census alone. With `--dataset PATH`, or
    `--dataset current` for the current version, it audits that dataset's coverage against the target. It
    writes the report as YAML with a markdown summary, and prints the gaps, the coverage debt, the strata that cannot
    be filled and the dataset's version chain. It never changes the database or any dataset file. A failure, such as
    a missing dataset or an unreachable database, exits 1 with a message.
    """
    settings = _resolve_active_settings(knowledge_base, cwd=Path.cwd())
    container = _build_container(ctx, settings)
    partition = resolve_partition(collection, load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings)))
    report = _run_command(
        _run_coverage(container, settings, collection=partition, dataset=dataset, out=out), on_error="evals coverage failed", database=settings.database
    )
    mode = f"version {report.dataset_version}" if report.dataset_version else "inventory-only"
    typer.echo(f"Coverage [{report.collection}] {mode}: debt {report.debt:.3f}")
    typer.echo(f"  gaps: {', '.join(report.gaps) or 'none'}")
    typer.echo(f"  over-represented: {', '.join(report.over_represented) or 'none'}")
    typer.echo(f"  infeasible: {', '.join(report.infeasible) or 'none'}")
    typer.echo(f"  thin pools: {', '.join(report.thin) or 'none'}")
    if report.version_chain:
        typer.echo(f"  version chain: {' -> '.join(report.version_chain)}")
    typer.echo(f"-> {report.out} (summary: {report.markdown})")


# -- score ---------------------------------------------


async def _run_score(
    container: Resolver, settings: ActiveKnowledgeBase, *, collection: str, dataset: str, setup: str, out: str | None
) -> DatasetScored:
    """Announce the identity, then score `dataset` with search alone under `setup`, one event loop for both."""
    await announce_identity(container, settings, collection=collection)
    out_dir = Path(out) if out is not None else (await container.get(EvalsSettings)).datasets_dir.parent / "scores"
    bus = await container.get(MessageBus)
    report = await bus.handle(ScoreDataset(dataset=dataset, collection=collection, out=str(out_dir), setup=setup))
    if not isinstance(report, DatasetScored):  # pragma: no cover (the score handler returns its report)
        _fail("evals score produced no report.")
    return report


@evals_app.command(name="score")
def evals_score_cmd(
    ctx: typer.Context,
    *,
    dataset: str = typer.Option(..., "--dataset", help="Dataset YAML to score, such as one made by 'evals import-judgments'."),
    collection: str | None = typer.Option(
        default=None, help="Knowledge base partition to search, defaults to the knowledge base schema's own retrieval.collection."
    ),
    setup: str = typer.Option(
        DEFAULT_SEARCH_SETUP, "--setup", help=f"Search setup every search runs under, one of {', '.join(SEARCH_SETUPS)}."
    ),
    out: str | None = typer.Option(None, "--out", help="Output directory. Defaults to <evals.datasets_dir>/../scores."),
    knowledge_base: str | None = typer.Option(default=None, help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."),
) -> None:
    """Score a dataset with search alone under one search setup and write the report. Changes nothing else.

    Runs one search per case at k = 50 with no agent, under the search setup `--setup` names (hybrid or keyword-only
    ranking, over every hit level or one), maps every hit to the source document it came from (a raw chunk by its id,
    a page through the source its page record names), and scores each case per document: nDCG@10 from the relevance
    judgments, and recall@1, recall@5, recall@10 and MRR from the gold ids. Each hit holds one rank, so a hit with no
    source document, or a repeat of a document ranked above it, holds its rank and matches nothing. It writes the per-case scores as YAML with
    a markdown summary, both named after the dataset and the setup, and prints the means. The hybrid setup's report
    leaves the setup out of its file names. A case whose search fails transiently, on a statement timeout or a failed
    connection, runs again after a wait that doubles each time, a bounded number of times, and the report counts the
    retries. A failure, such as an unknown setup, a missing dataset, an unreachable database or a case that still fails
    after its last retry, exits 1 with a message.
    """
    try:
        search_setup(setup)
    except ValueError as refusal:
        _fail(f"evals score failed: {refusal}")
    settings = _resolve_active_settings(knowledge_base, cwd=Path.cwd())
    container = _build_container(ctx, settings)
    partition = resolve_partition(collection, load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings)))
    report = _run_command(
        _run_score(container, settings, collection=partition, dataset=dataset, setup=setup, out=out), on_error="evals score failed", database=settings.database
    )
    typer.echo(f"Score [{report.collection}] {report.dataset} under {report.setup}: {report.cases} case(s)")
    for name, value in report.means.items():
        typer.echo(f"  {name}: {value:.4f}")
    typer.echo(f"  hits with no source document: {report.unmapped_hits}")
    typer.echo(f"  retries after a transient failure: {report.retries}")
    if report.unmappable_gold or report.cases_without_gold:
        typer.echo(f"  gold ids that name a page with no source document: {report.unmappable_gold}")
        typer.echo(f"  cases left out because no gold id names a source document: {report.cases_without_gold}")
    typer.echo(f"-> {report.out} (summary: {report.markdown})")


# -- check-generator ---------------------------------------------


async def _run_check_generator(
    container: Resolver, settings: ActiveKnowledgeBase, *, collection: str, human: str, generated: str, out: str | None
) -> GeneratorChecked:
    """Announce the identity, then check `generated` against `human` under every search setup, one event loop for both."""
    await announce_identity(container, settings, collection=collection)
    out_dir = Path(out) if out is not None else (await container.get(EvalsSettings)).datasets_dir.parent / "scores"
    bus = await container.get(MessageBus)
    report = await bus.handle(CheckGenerator(human=human, generated=generated, collection=collection, out=str(out_dir)))
    if not isinstance(report, GeneratorChecked):  # pragma: no cover (the check handler returns its report)
        _fail("evals check-generator produced no report.")
    return report


_CHECK_GENERATOR_HELP = (
    "Run the generator check and write a score report per setup for each dataset and the check report. Changes nothing else."
    "\n\n"
    "Scores the human-judged and the generated dataset under every search setup, the way `evals score` does, and writes each "
    f"score report. It ranks the setups by nDCG@10 rounded to {RANK_DECIMALS} decimals within each dataset, setups that print "
    "the same nDCG@10 sharing a rank, and compares the two rankings by Kendall's tau-b and by how many setup pairs both "
    "datasets order the same way. The check passes only when both datasets rank the setups in the same order. It writes the "
    "check as YAML with a markdown summary beside the score reports, named after the generated dataset, and prints each "
    "setup's scores and ranks, tau-b and the verdict. A failed check still exits 0. A failure, such as a missing dataset, two "
    "datasets that would write a score report of the same name, a dataset that gives every setup the same nDCG@10 or an "
    "unreachable database, exits 1 with a message."
)


@evals_app.command(name="check-generator", help=_CHECK_GENERATOR_HELP)
def evals_check_generator_cmd(
    ctx: typer.Context,
    *,
    human: str = typer.Option(..., "--human", help="Human-judged dataset YAML, such as one made by 'evals import-judgments'."),
    generated: str = typer.Option(
        ..., "--generated", help="Generated dataset YAML of the same knowledge base, such as one made by 'evals gen-dataset'."
    ),
    collection: str | None = typer.Option(
        default=None, help="Knowledge base partition to search, defaults to the knowledge base schema's own retrieval.collection."
    ),
    out: str | None = typer.Option(None, "--out", help="Output directory. Defaults to <evals.datasets_dir>/../scores."),
    knowledge_base: str | None = typer.Option(default=None, help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."),
) -> None:
    """Run the generator check and write a score report per setup for each dataset and the check report. Changes nothing else."""
    settings = _resolve_active_settings(knowledge_base, cwd=Path.cwd())
    container = _build_container(ctx, settings)
    partition = resolve_partition(collection, load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings)))
    report = _run_command(
        _run_check_generator(container, settings, collection=partition, human=human, generated=generated, out=out),
        on_error="evals check-generator failed", database=settings.database,
    )
    verdict = "passed" if report.check.passed else "failed"
    typer.echo(f"Generator check [{report.collection}] {report.generated} against {report.human}: {verdict}")
    for row in report.check.setups:
        decimals = report.check.rank_decimals
        human_part = f"human {row.human_ndcg:.{decimals}f} (rank {row.human_rank})"
        typer.echo(f"  {row.setup}: {human_part}, generated {row.generated_ndcg:.{decimals}f} (rank {row.generated_rank})")
    typer.echo(f"  Kendall's tau-b: {report.check.tau_b:.4f}")
    typer.echo(f"  setup pairs both datasets order the same way: {report.check.agreeing_pairs} of {report.check.pairs}")
    typer.echo(f"-> {report.out} (summary: {report.markdown})")


# -- freshness ---------------------------------------------


async def _default_freshness_out(container: Resolver) -> Path:
    """The default freshness report folder: `freshness/` beside the configured `evals.datasets_dir`."""
    evals_settings = await container.get(EvalsSettings)
    return freshness_artifacts_dir(evals_settings.datasets_dir)


async def _build_freshness_command(container: Resolver, *, collection: str, dataset: str | None, out: str | None) -> CheckFreshness:
    """Assemble the `CheckFreshness` from the CLI flags, resolving the default `--out` directory."""
    out_dir = Path(out) if out is not None else await _default_freshness_out(container)
    return CheckFreshness(collection=collection, dataset=dataset, out=str(out_dir))


async def _freshness(container: Resolver, command: CheckFreshness) -> FreshnessCheckedReport:
    """Dispatch `command` and return the freshness report."""
    bus = await container.get(MessageBus)
    report = await bus.handle(command)
    if not isinstance(report, FreshnessCheckedReport):  # pragma: no cover (the freshness handler returns its report)
        _fail("evals freshness produced no report.")
    return report


async def _run_freshness(
    container: Resolver, settings: ActiveKnowledgeBase, *, collection: str, dataset: str | None, out: str | None
) -> FreshnessCheckedReport:
    """Announce the identity, then build the command from the flags and dispatch it, one event loop for both."""
    await announce_identity(container, settings, collection=collection)
    command = await _build_freshness_command(container, collection=collection, dataset=dataset, out=out)
    return await _freshness(container, command)


@evals_app.command(name="freshness")
def evals_freshness_cmd(
    ctx: typer.Context,
    *,
    collection: str | None = typer.Option(
        default=None,
        help="Knowledge base partition the dataset's anchors and gold ids are checked against, defaults to the knowledge base schema's own "
        "retrieval.collection.",
    ),
    dataset: str | None = typer.Option(
        None, "--dataset", help="Dataset YAML (or 'current') to sweep. Omit to sweep the collection's current version."
    ),
    out: str | None = typer.Option(None, "--out", help="Output directory. Defaults to <evals.datasets_dir>/../freshness."),
    knowledge_base: str | None = typer.Option(default=None, help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."),
) -> None:
    """Check a dataset version against the knowledge base as it is now, and write a report. Changes nothing else.

    Checks the version the `.current` pointer names, or the one `--dataset PATH` or `--dataset current` names. It
    compares each case's anchor page and expected pages with the knowledge base, and writes the report as YAML with a
    markdown summary. It prints the count per verdict, the stale fraction, the pages that made the most cases stale,
    and the version checked. It never changes the database or any dataset file, and exits 0 on a finished check even
    when every case is stale. A failure, such as a missing dataset or an unreachable database, exits 1 with a message.
    """
    settings = _resolve_active_settings(knowledge_base, cwd=Path.cwd())
    container = _build_container(ctx, settings)
    partition = resolve_partition(collection, load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings)))
    report = _run_command(
        _run_freshness(container, settings, collection=partition, dataset=dataset, out=out), on_error="evals freshness failed", database=settings.database
    )
    typer.echo(f"Freshness [{report.collection}] version {report.dataset_version}: {report.staleness_fraction:.1%} stale")
    typer.echo(f"  verdicts: {', '.join(f'{verdict}={count}' for verdict, count in report.counts.items())}")
    top = ", ".join(f"{page}({count})" for page, count in report.top_stale_pages)
    typer.echo(f"  top stale pages: {top or 'none'}")
    if report.n_premise_checked:
        typer.echo(f"  structural premises (report-only): checked {report.n_premise_checked}, broken {report.n_premise_broken}")
    typer.echo(f"-> {report.out} (summary: {report.markdown})")


# -- freeze -------------------------------------------------


async def _freeze(container: Resolver, command: FreezeDataset) -> DatasetFrozenReport:
    """Dispatch `command` and return the freeze report."""
    bus = await container.get(MessageBus)
    report = await bus.handle(command)
    if not isinstance(report, DatasetFrozenReport):  # pragma: no cover (the freeze handler returns its report)
        _fail("evals freeze produced no report.")
    return report


async def _run_freeze(
    container: Resolver, settings: ActiveKnowledgeBase, *, collection: str, dataset: str | None, fraction: float | None
) -> DatasetFrozenReport:
    """Announce the identity, then build the `FreezeDataset` from the flags and dispatch it, one event loop for both."""
    await announce_identity(container, settings, collection=collection)
    return await _freeze(container, FreezeDataset(collection=collection, dataset=dataset, fraction=fraction))


@evals_app.command(name="freeze")
def evals_freeze_cmd(
    ctx: typer.Context,
    *,
    dataset: str | None = typer.Option(
        None, "--dataset", help="Dataset to freeze: a NAME:vN version, 'current', or a path. Omit for the collection's current."
    ),
    fraction: float | None = typer.Option(None, "--fraction", help="Fraction of the version to freeze. Defaults to the configured 0.2."),
    collection: str | None = typer.Option(
        default=None,
        help="Knowledge base partition the frozen cases' freshness is checked against, defaults to the knowledge base schema's own "
        "retrieval.collection.",
    ),
    knowledge_base: str | None = typer.Option(default=None, help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."),
) -> None:
    """Freeze a held-out part of a dataset version, taken stratum by stratum.

    Freezes the version the `.current` pointer names, or the one `--dataset NAME:vN`, `--dataset current` or a path
    names. It checks that version's freshness against the knowledge base, takes a salted-hash sample of each stratum,
    sized by the configured `frozen_fraction` or by `--fraction`, and writes a new `<name>.frozen.v{M}.json` file and
    its `.frozen.current` pointer. It prints the version, the frozen version, the count per stratum and the stale
    fraction at freeze time. It never changes the database, the dataset version or an earlier frozen version, and only
    a person running freeze again makes a new frozen version. A failure exits 1 with a message.
    """
    settings = _resolve_active_settings(knowledge_base, cwd=Path.cwd())
    container = _build_container(ctx, settings)
    partition = resolve_partition(collection, load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings)))
    report = _run_command(
        _run_freeze(container, settings, collection=partition, dataset=dataset, fraction=fraction), on_error="evals freeze failed", database=settings.database
    )
    typer.echo(
        f"Froze [{partition}] version {report.dataset_version} -> frozen v{report.frozen_version}: "
        f"{report.n_frozen} case(s), {report.staleness_at_freeze:.1%} stale"
    )
    for stratum, count in sorted(report.per_stratum.items()):
        typer.echo(f"  {stratum}: {count}")
    typer.echo(f"-> {report.sidecar}")


def _parse_hold(value: str) -> ExperimentAxis:
    """Narrow a `--hold` value to an experiment axis, exiting 1 on an unknown one."""
    by_name: dict[str, ExperimentAxis] = {
        "model": "model",
        "instructions": "instructions",
        "dataset": "dataset",
        "retrieval_strategy": "retrieval_strategy",
    }
    if value not in by_name:
        _fail(f"evals experiment: --hold must be one of model|instructions|dataset|retrieval_strategy, got {value!r}")
    return by_name[value]


def _parse_strategies(values: list[str]) -> tuple[str, ...]:
    """Narrow the `--retrieval-strategy` values to known strategies, exiting 1 on none or an unknown one."""
    if not values:
        _fail("evals experiment: --retrieval-strategy needs at least one value")
    unknown = tuple(value for value in values if value not in EVAL_STRATEGY_NAMES)
    if unknown:
        _fail(f"evals experiment: --retrieval-strategy must be one of {'|'.join(EVAL_STRATEGY_NAMES)}, got {', '.join(unknown)}")
    return tuple(values)


def _experiment_axes(
    *, models: tuple[str, ...], instructions: tuple[str, ...], datasets: tuple[str, ...], retrieval_strategies: tuple[str, ...]
) -> ExperimentAxes:
    """The experiment's four axes, exiting 1 on an axis value no run can use."""
    try:
        return ExperimentAxes(model=models, instructions=instructions, dataset=datasets, retrieval_strategy=retrieval_strategies)
    except ValidationError as error:
        _fail(f"evals experiment: {error}")


class VariantRun(Protocol):
    """Scores one variant's run. The experiment calls it once per variant."""

    async def __call__(self, container: Resolver, *, config: EvaluationRunConfig, dataset_path: Path) -> EvaluationRunReport:
        """Run `config` against `dataset_path` and return its report.

        Args:
            container: Resolves what the run needs.
            config: The run's configuration.
            dataset_path: The dataset file to run against.

        Returns:
            The run's report.
        """
        ...


class VariantSpan(Protocol):
    """Opens the Logfire span one variant's run is recorded under."""

    def __call__(self, *, model: str, instructions: str, dataset: str, retrieval_strategy: str) -> AbstractContextManager[object]:
        """Open the span for this variant.

        Args:
            model: The variant's model.
            instructions: The variant's instructions.
            dataset: The variant's dataset.
            retrieval_strategy: The variant's retrieval strategy.

        Returns:
            The context manager that holds the span open.
        """
        ...


async def _experiment_evaluator(
    container: Resolver, variant: ExperimentVariant, *, run: VariantRun = _run_evals, span: VariantSpan = experiment_span
) -> dict[str, float]:
    """Score one variant inside its Logfire span, passing every axis of the variant to its run."""
    with span(
        model=variant.model, instructions=variant.instructions, dataset=variant.dataset, retrieval_strategy=variant.retrieval_strategy
    ):
        report = await run(
            container,
            config=EvaluationRunConfig(
                model_spec=variant.model, instructions=variant.instructions, retrieval_strategy=variant.retrieval_strategy
            ),
            dataset_path=Path(variant.dataset),
        )
    return average_metrics(report)


def _variant_evaluator(container: Resolver, *, run: VariantRun = _run_evals, span: VariantSpan = experiment_span) -> VariantEvaluator:
    """The per-variant evaluator the factorial run drives, bound to one container."""

    async def evaluate(variant: ExperimentVariant) -> dict[str, float]:
        return await _experiment_evaluator(container, variant, run=run, span=span)

    return evaluate


async def _run_experiment(
    ctx: typer.Context,
    container: Resolver,
    settings: ActiveKnowledgeBase,
    *,
    collection: str,
    hold: ExperimentAxis,
    models: tuple[str, ...],
    instructions: tuple[str, ...],
    datasets: tuple[str, ...],
    retrieval_strategies: tuple[str, ...],
) -> list[ExperimentRun]:
    """Resolve the axes (defaulting the dataset), announce the identity, configure Logfire once, and run every variant."""
    resolved_datasets = datasets or (str(await _resolve_eval_dataset_path(ctx, None)),)
    axes = _experiment_axes(
        models=models, instructions=instructions, datasets=resolved_datasets, retrieval_strategies=retrieval_strategies
    )
    await announce_identity(container, settings, collection=collection)
    logfire_settings = await resolve_logfire_settings(container)
    configure_logfire(logfire_settings)
    if not logfire_settings.write_token:
        enable_local_span_recording()
    return await run_experiment(axes, hold=hold, evaluate=_variant_evaluator(container))


def _print_experiment(runs: list[ExperimentRun]) -> None:
    """Print one line per variant with its aggregate metrics."""
    for run in runs:
        metrics = ", ".join(f"{name}={value:.3f}" for name, value in run.metrics.items())
        typer.echo(f"[{run.variant.model} | {run.variant.retrieval_strategy} | {run.variant.dataset}] {metrics}")


# The repeatable options of `evals experiment`.
_EXPERIMENT_MODEL_OPTION = typer.Option([_TEST_MODEL], "--model", help="Model variant(s). Repeatable.")
_EXPERIMENT_INSTRUCTIONS_OPTION = typer.Option([DEFAULT_INSTRUCTIONS], "--instructions", help="Instruction variant(s). Repeatable.")
_EXPERIMENT_DATASET_OPTION = typer.Option([], "--dataset", help="Dataset path variant(s). Repeatable (defaults to the seed dataset).")
_EXPERIMENT_STRATEGY_OPTION = typer.Option(
    [DEFAULT_STRATEGY.value],
    "--retrieval-strategy",
    help=f"Retrieval surface variant(s), repeatable (default '{DEFAULT_STRATEGY.value}'): {', '.join(EVAL_STRATEGY_NAMES)}.",
)


@evals_app.command(name="experiment")
def evals_experiment_cmd(
    ctx: typer.Context,
    *,
    hold: str = typer.Option(..., "--hold", help="Axis to hold constant: model | instructions | dataset | retrieval_strategy."),
    model: list[str] = _EXPERIMENT_MODEL_OPTION,
    instructions: list[str] = _EXPERIMENT_INSTRUCTIONS_OPTION,
    dataset: list[str] = _EXPERIMENT_DATASET_OPTION,
    retrieval_strategy: list[str] = _EXPERIMENT_STRATEGY_OPTION,
    knowledge_base: str | None = typer.Option(default=None, help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."),
) -> None:
    """Run a factorial retrieval experiment: hold one axis constant, vary the rest, and compare.

    Each variant builds the agent from its model, instructions and retrieval strategy, and scores its dataset. Every
    run is recorded in its own Logfire span. `--retrieval-strategy` repeats like the other axes and defaults to
    `full`. A failure, such as a bad `--hold`, an unknown strategy, an unreachable model or database, or a missing
    dataset, exits 1 with a message.
    """
    held = _parse_hold(hold)
    strategies = _parse_strategies(retrieval_strategy)
    settings = _resolve_active_settings(knowledge_base, cwd=Path.cwd())
    container = _build_container(ctx, settings)
    schema = load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings))
    runs = _run_command(
        _run_experiment(
            ctx,
            container,
            settings,
            collection=schema.retrieval.collection,
            hold=held,
            models=tuple(model),
            instructions=tuple(instructions),
            datasets=tuple(dataset),
            retrieval_strategies=strategies,
        ),
        on_error="evals experiment failed", database=settings.database,
    )
    _print_experiment(runs)


async def _resolve_evals_settings(container: Resolver) -> EvalsSettings:
    """The evals settings from the container, which RCA reads `rca_queries_path` off."""
    return await container.get(EvalsSettings)


def _load_rca_queries(path: Path) -> dict[str, str]:
    """Load the saved RCA queries from `path`, or print a notice and return no queries when the file is absent."""
    if not path.is_file():
        typer.echo(f"No saved RCA queries at {path}. Author them to run them programmatically.")
        return {}
    return load_named_queries(path)


def _print_rca_degraded(prompt: str, query_names: Sequence[str]) -> None:
    """Print the RCA prompt and the saved query names, for a model on the Logfire MCP when no read token is set."""
    typer.echo("No Logfire read token (infrastructure.logfire.read_token). Emitting the RCA prompt for the Logfire-MCP path.")
    if query_names:
        typer.echo(f"Saved queries available: {', '.join(query_names)}")
    typer.echo("")
    typer.echo(prompt)


async def _run_rca(
    container: Resolver,
    settings: ActiveKnowledgeBase,
    *,
    collection: str,
    query_names: tuple[str, ...],
    focus: str | None,
    prompt_only: bool,
) -> None:
    """Announce the identity, then print the RCA prompt, or run the saved queries when `prompt_only` is off and a read token is set."""
    await announce_identity(container, settings, collection=collection)
    names = query_names or None
    prompt = build_rca_prompt(focus=focus, query_names=names)
    if prompt_only:
        typer.echo(prompt)
        return

    evals_settings = await _resolve_evals_settings(container)
    queries = _load_rca_queries(evals_settings.rca_queries_path)
    logfire_settings = await resolve_logfire_settings(container)

    if not logfire_settings.read_token:
        surfaced = list(names) if names is not None else list(queries)
        _print_rca_degraded(prompt, surfaced)
        return

    client = build_logfire_query_client(logfire_settings)
    report = build_rca_report(client, queries, query_names=names)
    typer.echo(render_rca_report_markdown(report))


# The repeatable `--query` option.
_RCA_QUERY_OPTION = typer.Option([], "--query", help="Saved query name(s) to run. Repeatable (defaults to the report's default set).")


@evals_app.command(name="rca")
def evals_rca_cmd(
    ctx: typer.Context,
    *,
    query: list[str] = _RCA_QUERY_OPTION,
    focus: str | None = typer.Option(None, "--focus", help="Scope to anchor the analysis on (e.g. an experiment or run name)."),
    prompt: bool = typer.Option(default=False, help="Print the RCA prompt for the Logfire-MCP-driven model and exit."),
    knowledge_base: str | None = typer.Option(default=None, help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."),
) -> None:
    """Root-cause analysis over the telemetry of evaluation runs: print the analysis prompt, or run the saved queries.

    The analysis itself is done by a model that queries the traces over the Logfire MCP. With `--prompt`, or when no
    Logfire read token is set, this prints that model's prompt. With a read token it runs the saved SQL from the
    `evals.rca_queries_path` setting against the Logfire Query API and prints a Markdown report. `--query` picks
    named queries, the report's default set when not given, and `--focus` scopes the prompt. It never uses the
    network without a read token, and a missing token or query file never crashes it.
    """
    settings = _resolve_active_settings(knowledge_base, cwd=Path.cwd())
    container = _build_container(ctx, settings)
    schema = load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings))
    _run_command(
        _run_rca(container, settings, collection=schema.retrieval.collection, query_names=tuple(query), focus=focus, prompt_only=prompt),
        on_error="evals rca failed", database=settings.database,
    )
