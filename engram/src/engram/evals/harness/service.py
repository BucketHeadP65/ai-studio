"""The evaluation harness's use cases: score a dataset, run an experiment, run blind trials, average the metrics.

Every use case takes the `Resolver` port and reads its collaborators through it.
The use cases raise plain exceptions, such as `ValueError` for an unknown retrieval strategy, and each
caller turns them into its own error for the person running it.
"""

from __future__ import annotations

import hashlib
import re

from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from time import perf_counter
from typing import TYPE_CHECKING

from core.ai import create_azure_model
from core.ai.disclosure.contracts import ProgressiveDisclosure
from core.ai.evaluation.contracts import (
    EvaluationError,
    EvaluationInput,
    FileDeliveredEvidenceStore,
    Nonempty,
    TaskExecution,
    TraceRecorder,
    TracingModel,
    measured_usage,
    model_fingerprint,
)
from core.ai.models import Thinking, chat_http_client
from core.settings import AzureOpenAiSettings
from pydantic_ai import capture_run_messages
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits
from pydantic_evals.lifecycle import CaseLifecycle

from engram.evals.harness.agent import build_agent, make_task
from engram.evals.harness.domain.evaluators import AnswerAccuracy
from engram.evals.harness.domain.experiments import ExperimentRun, expand_variants
from engram.evals.harness.infrastructure.instrumentation import configure_logfire, enable_local_span_recording, resolve_logfire_settings
from engram.evals.harness.infrastructure.results import ResultsHeader, dataset_sha256, read_results, results_lines, write_results
from engram.evals.harness.seen_units import bare_unit_id, seen_units
from engram.evals.harness.transient_failures import is_transient_failure
from engram.evals.outcomes.contracts import GuardedTracingToolset, WithheldRefs, page_of
from engram.evals.shared.contracts import (
    EnvelopeHandler,
    QaCaseMeta,
    QaInput,
    RetrievalAnswer,
    RetrievalHandlers,
    RetrievalToolset,
    build_retrieval_toolset,
    fetch_opens_map,
)
from engram.evals.tuning.contracts import DEFAULT_STRATEGY, resolve_strategy
from engram.kb.contracts import KnowledgeBaseSchema

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Iterator, Mapping, MutableMapping, Sequence
    from pathlib import Path

    from core.ai.evaluation.contracts import DeliveredEvidenceStore, ExecutionTrace, ResourceUsage, RunIdentity
    from core.ai.retrieval.contracts import Row, SchemaDescription, SqlGateway, SqlResult
    from core.resolver import Resolver
    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai.models import Model
    from pydantic_ai.toolsets import AbstractToolset
    from pydantic_evals.reporting import EvaluationReport

    from engram.evals.generation.contracts import EvaluationDataset
    from engram.evals.harness.agent import AgentUnderTest
    from engram.evals.harness.domain.experiments import ExperimentAxes, ExperimentAxis
    from engram.evals.harness.infrastructure.results import ResultsLine
    from engram.evals.harness.internal_contracts import VariantEvaluator
    from engram.evals.scoring.contracts import HitSources
    from engram.evals.shared.contracts import SqlKnowledgeBase

type EvaluationRunReport = EvaluationReport[QaInput, RetrievalAnswer, QaCaseMeta]
"""The report an evaluation run of a dataset returns, one scored case per dataset case."""


@dataclass(frozen=True)
class EvaluationRunConfig:
    """The settings of one evaluation run: everything `run_eval` needs besides the container and the dataset.

    `instructions` already carries any reasoning-effort directive from `apply_reasoning_effort`. That directive is a
    sentence the model reads, and `thinking` is a request setting the model is built with, so the two are set apart.
    It refuses `code_mode`, `sql` or a `repeat` other than `1` together with `delivered_evidence_root`, and `resume`
    without `results_path` or together with `delivered_evidence_root`.

    Attributes:
        model_spec: `"test-model"` or an Azure deployment name.
        instructions: The agent's system instructions, with any effort directive already applied.
        retrieval_strategy: The retrieval strategy to expose, or `None` for the full set of tools.
        code_mode: Whether the agent drives the tools through the sandboxed `run_code` tool.
        tool_descriptions: Tool descriptions to use in place of the shipped ones, keyed by tool name.
            `None` keeps the shipped descriptions.
        disclosure: Whether to run under the progressive-disclosure protocol. `fetch` becomes the
            leveled read and the agent gets a `ProgressiveDisclosure` capability, as on the MCP server.
        max_concurrency: The most cases run at once, or `None` to run every case at once. Set it
            against a rate-limited deployment, which fails cases with HTTP 429 rather than slowing them.
        delivered_evidence_root: The folder each trial's delivered evidence is stored under. When set,
            every case runs behind the guarded tracing toolset, its tool results are stored before
            the model reads them, withheld references are kept out, and each case gets an
            `ExecutionTrace`. `None` runs the plain retrieval toolset and records nothing.
        withheld_refs: The references no trial of this run may read, canonical or bare. Read only
            when `delivered_evidence_root` is set.
        repeat: How many times each case runs.
        sql: The SQL tools to give the agent beside the retrieval tools, or `None` for none. The run notes,
            per case, whether a row they returned named another knowledge base. A statement that reads only the
            views describing every knowledge base, such as `api.about`, is left out.
        max_tool_calls: The most tool calls one case may make. A case that asks for more fails.
            `None` sets no limit.
        pinned_search: Whether `search` reads only the knowledge base's own collection. It keeps its date range.
        hit_sources: The page-to-source lookup that maps each case's citations, and the units its tool results named,
            to source ids for the results file, or `None` to score the cited page ids and no seen recall. Read only when
            `results_path` is set.
        results_path: Where to write the results file, one line per case after a header line, or `None` for no file.
        thinking: The thinking level the model under test is built at, sent with every request, or `None` for the
            model's own default. The test model ignores it.
        resume: Whether to continue the results file at `results_path` when it exists. The run keeps every case line
            that did not fail transiently, runs only the cases the file lacks or that failed transiently, and rewrites
            the file under the earlier header. A file whose header differs from this run's settings in a field the paired
            comparison holds, or in the index build, is refused before any case runs. With no file there yet, the run
            runs every case.
    """

    model_spec: str
    instructions: str
    retrieval_strategy: str | None = None
    code_mode: bool = False
    tool_descriptions: Mapping[str, str] | None = None
    disclosure: bool = False
    max_concurrency: int | None = None
    delivered_evidence_root: Path | None = None
    withheld_refs: frozenset[str] = frozenset()
    repeat: int = 1
    sql: SqlKnowledgeBase[object] | None = None
    max_tool_calls: int | None = None
    pinned_search: bool = False
    hit_sources: HitSources | None = None
    results_path: Path | None = None
    thinking: Thinking | None = None
    resume: bool = False

    def __post_init__(self) -> None:
        """Refuse settings that cannot run together.

        Raises:
            ValueError: `code_mode` and `delivered_evidence_root` are both set, because the model
                then reads the sandbox's output rather than the stored tool results.
            ValueError: `sql` and `delivered_evidence_root` are both set, because the SQL tools' rows
                reach the model without passing the guard.
            ValueError: `repeat` is not `1` and `delivered_evidence_root` is set, because every
                repetition of a case carries the same case name, which names its trial.
            ValueError: `repeat` is not `1` and `results_path` is set, because the results file keys
                each line by its case name.
            ValueError: `resume` is set without `results_path`, because a resumed run continues a results file.
            ValueError: `resume` and `delivered_evidence_root` are both set, because a case run again would
                file its evidence under the trial its first run already filed.
        """
        if self.code_mode and self.delivered_evidence_root is not None:
            msg = "code mode cannot run under delivered evidence: the model reads the sandbox's output, not the stored tool results"
            raise ValueError(msg)
        if self.sql is not None and self.delivered_evidence_root is not None:
            msg = "the SQL tools cannot run under delivered evidence: their rows reach the model without passing the guard"
            raise ValueError(msg)
        if self.repeat != 1 and self.delivered_evidence_root is not None:
            msg = "a guarded run cannot repeat a case: pydantic-evals gives every repetition the same case name, which a trial id cannot tell apart"
            raise ValueError(msg)
        if self.repeat != 1 and self.results_path is not None:
            msg = "a results file cannot hold a repeated case: each line is keyed by its case name, which every repetition shares"
            raise ValueError(msg)
        if self.resume and self.results_path is None:
            msg = "a resumed run continues a results file, so it needs a results path"
            raise ValueError(msg)
        if self.resume and self.delivered_evidence_root is not None:
            msg = "a resumed run cannot run under delivered evidence: a case run again would file its evidence under the trial its first run already filed"
            raise ValueError(msg)


# The model spec that selects pydantic-ai's network-free `TestModel`. Any other spec names an Azure deployment.
_TEST_MODEL = "test-model"

REASONING_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "max")
"""The reasoning-effort levels AI Factory Studio offers for a run, each delivered as an instruction directive."""

# Each directive asks for more or less care in reasoning, never for more retrieval. Each opens with
# `Reasoning effort: <LEVEL>.` so the level shows in the trace.
_EFFORT_DIRECTIVES: dict[str, str] = {
    "low": "Reasoning effort: LOW. Answer directly once you have enough to support it. Keep deliberation brief.",
    "medium": "Reasoning effort: MEDIUM. Weigh whether the retrieved sources actually answer the question before composing your answer.",
    "high": "Reasoning effort: HIGH. Reason carefully: cross-check the retrieved sources against each other and confirm each claim is grounded before answering.",
    "max": "Reasoning effort: MAXIMUM. Reason rigorously: scrutinize whether each source truly supports the claim it backs, resolve conflicts between sources, and verify every citation before answering.",
}


def apply_reasoning_effort(instructions: str, effort: str | None) -> str:
    """Append the reasoning-effort directive for `effort` to `instructions`.

    `None` or an unknown effort leaves `instructions` unchanged and never raises.

    Args:
        instructions: The agent-under-test's base system instructions.
        effort: The chosen effort level (one of `REASONING_EFFORTS`), or `None`.

    Returns:
        `instructions` with the effort directive appended, or unchanged when there is no directive.
    """
    directive = _EFFORT_DIRECTIVES.get(effort or "")
    return f"{instructions}\n\n{directive}" if directive else instructions


async def build_eval_model(container: Resolver, model_spec: str, *, thinking: Thinking | None = None) -> Model:
    """Resolve the model to evaluate: a network-free `TestModel`, or an Azure deployment.

    `"test-model"` gives a `TestModel` that calls no tools and answers at once. Any other spec is an
    Azure deployment over the endpoint of the container's `AzureOpenAiSettings`, sending its requests through the
    chat retry client. A thinking level is refused for a deployment pydantic-ai sends no thinking setting to.

    Args:
        container: The resolver the Azure endpoint comes from. The test model never touches it.
        model_spec: `"test-model"` or an Azure deployment name.
        thinking: The thinking level every request carries, or `None` for the model's own default.

    Returns:
        The configured pydantic-ai `Model` to evaluate.

    Raises:
        ValueError: `thinking` is set and the deployment takes no thinking level, so its requests would run at the
            model's default.
    """
    settings = ModelSettings(thinking=thinking) if thinking is not None else None
    if model_spec == _TEST_MODEL:
        from pydantic_ai.models.test import TestModel  # noqa: PLC0415 (lazy: the test backend loads only on the test-model path)

        return TestModel(call_tools=[], settings=settings)
    azure_openai = await container.get(AzureOpenAiSettings)
    model = create_azure_model(model_spec, endpoint=azure_openai.endpoint, settings=settings, http_client=chat_http_client())
    if thinking is not None and not (model.profile.get("supports_thinking") or model.profile.get("thinking_always_enabled")):
        msg = (
            f"the model {model_spec!r} takes no thinking level, so a run at {thinking!r} would run at the model's default. "
            "Run it without a thinking level, or name a deployment pydantic-ai knows as a reasoning model."
        )
        raise ValueError(msg)
    return model


async def run_eval(container: Resolver, *, config: EvaluationRunConfig, dataset_path: Path) -> EvaluationRunReport:
    """Load the dataset at `dataset_path` and evaluate the agent under test on it.

    It is `run_eval_on_dataset` over the loaded dataset.

    Args:
        container: The resolver the model, the retrieval handlers and the Logfire settings come from.
        config: The settings of the evaluation run.
        dataset_path: The dataset YAML to score against, already resolved by the caller.

    Returns:
        The evaluation report from `evaluate`.

    Raises:
        ValueError: When `config.retrieval_strategy` is not a known strategy name, or when `config.thinking` is set
            for a deployment that takes no thinking level.
        DeliveredEvidenceError: With `config.delivered_evidence_root` set, when a guarded trial could not store what a tool
            delivered, could not write it at all, or a payload or the trial crossed a delivered-evidence size bound.
        WithheldEvidenceLeakError: With `config.delivered_evidence_root` set, when a guarded trial's finished payload still
            named a withheld reference.
        UnnamedCaseError: With `config.delivered_evidence_root` set, when the run reaches a case that carries no name.
        ResultsFileNotResumableError: With `config.resume` set, when the results file cannot be read or was written by
            a run whose settings differ in a held field.
    """
    from engram.evals.generation.contracts import load_dataset  # noqa: PLC0415 (engram.evals.generation imports this slice: a cycle)

    return await run_eval_on_dataset(container, config=config, dataset=load_dataset(dataset_path))


async def run_eval_on_dataset(
    container: Resolver, *, config: EvaluationRunConfig, dataset: EvaluationDataset, model: Model | None = None
) -> EvaluationRunReport:
    """Evaluate the agent under test on an in-memory dataset and return the report alone.

    It is `run_traced_eval_on_dataset` without the per-trial traces.

    Args:
        container: The resolver the model, the retrieval handlers and the Logfire settings come from.
        config: The settings of the evaluation run.
        dataset: The dataset to score against, held in memory and never loaded again.
        model: The model to evaluate in place of the one `config.model_spec` names, or `None` to build that one.

    Returns:
        The evaluation report from `evaluate`.

    Raises:
        ValueError: When `config.retrieval_strategy` is not a known strategy name, or when `config.thinking` is set
            for a deployment that takes no thinking level and `model` is `None`.
        DeliveredEvidenceError: With `config.delivered_evidence_root` set, when a guarded trial could not store what a tool
            delivered, could not write it at all, or a payload or the trial crossed a delivered-evidence size bound.
        WithheldEvidenceLeakError: With `config.delivered_evidence_root` set, when a guarded trial's finished payload still
            named a withheld reference.
        UnnamedCaseError: With `config.delivered_evidence_root` set, when the run reaches a case that carries no name.
        ResultsFileNotResumableError: With `config.resume` set, when the results file cannot be read or was written by
            a run whose settings differ in a held field.
    """
    return (await run_traced_eval_on_dataset(container, config=config, dataset=dataset, model=model)).report


@dataclass(frozen=True)
class TracedEvaluationRun:
    """One run's report beside the execution trace of each trial that produced it, and what it read for its results file.

    `traces` is keyed by trial id and is empty whenever the run was not guarded, which says the
    trials were not traced rather than that they did nothing.
    """

    report: EvaluationRunReport
    traces: Mapping[str, ExecutionTrace] = field(default_factory=dict)
    knowledge_base_fingerprint: str = ""
    """The knowledge base's index fingerprint read before the first case, or empty when the run wrote no results file."""
    source_lookup_error: str | None = None
    """What this run's lookup of the cited pages' sources raised, or None when it succeeded or the run mapped no sources."""


async def run_traced_eval_on_dataset(
    container: Resolver, *, config: EvaluationRunConfig, dataset: EvaluationDataset, model: Model | None = None
) -> TracedEvaluationRun:
    """Evaluate the agent under test on an in-memory dataset, with each trial's trace when the run is guarded.

    The toolset is built for this run from the retrieval handlers and `config.retrieval_strategy`.
    Logfire is configured when a write token is present, and spans are recorded locally otherwise.
    With `config.delivered_evidence_root` set, each case runs as its own trial behind the guarded
    tracing toolset and a traced model, and its trace comes back in `traces`. Without it, `traces` is empty.
    When a case expects an answer, the run scores it with `AnswerAccuracy` too, once, and leaves `dataset` unchanged.
    With `config.results_path` set, the knowledge base fingerprint and index build are read before the first case, and
    the results file is written after the last one, failed cases included, each failure marked transient or not. With
    `config.hit_sources` set too, each line carries its seen recall, from the units the tool results in the case's own
    messages named. A failed source lookup is noted in the file's header and leaves the sources and retrieval scores
    null. With `config.resume` set and the file already there, its header is checked before any case runs, the index
    build included, and the report covers only the cases run again. The file keeps the earlier header and its kept lines
    exactly, whatever the fingerprint now reads. A failed source lookup then leaves only the lines run now without
    sources and scores, and the run returns the error rather than writing it.

    Args:
        container: The resolver the model, the retrieval handlers and the Logfire settings come from.
        config: The settings of the evaluation run.
        dataset: The dataset to score against, held in memory and never loaded again.
        model: The model to evaluate in place of the one `config.model_spec` names, or `None` to build that one.

    Returns:
        The evaluation report from `evaluate`, and each trial's execution trace.

    Raises:
        ValueError: When `config.retrieval_strategy` is not a known strategy name, or when `config.thinking` is set
            for a deployment that takes no thinking level and `model` is `None`.
        DeliveredEvidenceError: When a guarded trial could not store what a tool delivered, could
            not write it at all, or a payload or the trial crossed a delivered-evidence size bound.
        WithheldEvidenceLeakError: When a guarded trial's finished payload still named a withheld
            reference.
        UnnamedCaseError: When a guarded run reaches a case that carries no name.
        ResultsFileNotResumableError: With `config.resume` set, when the results file cannot be read or was written by
            a run whose settings differ in a held field or whose index build differs.
    """
    started = datetime.now(tz=UTC)
    model = model if model is not None else await build_eval_model(container, config.model_spec, thinking=config.thinking)
    resolved_handlers = await container.get(RetrievalHandlers)
    profile = resolve_strategy(config.retrieval_strategy)
    # The schema's `retrieval.collection` scopes `search` to the knowledge base's own collection, as on the MCP server.
    schema = await container.get(KnowledgeBaseSchema)
    collection = schema.retrieval.collection
    # Read before the first case, so the stamp is the index the run starts on and a failed read costs no case.
    identity = await _index_identity(container, collection) if config.results_path is not None else _IndexIdentity(fingerprint="", build="")
    stamp = _RunStamp(collection=collection, started=started, knowledge_base_fingerprint=identity.fingerprint, index_build=identity.build)
    kept = _kept_results(config, dataset, stamp) if config.resume else None
    envelope_handler = await container.get(EnvelopeHandler) if config.disclosure else None
    toolset: RetrievalToolset = build_retrieval_toolset(
        resolved_handlers,
        only=profile.tools,
        search_hybrid=profile.search_hybrid,
        descriptions=config.tool_descriptions,
        collection=collection,
        envelope=envelope_handler,
        can_sql=config.sql is not None,
        pinned=config.pinned_search,
        facet_keys=tuple(facet.key for facet in schema.facets),
    )
    collections_read: dict[str, set[str]] = {}
    capabilities: list[AbstractCapability] = [ProgressiveDisclosure()] if config.disclosure else []
    if config.sql is not None:
        capabilities.append(replace(config.sql, gateway=_CollectionRecordingGateway(config.sql.gateway, collections_read)))
    extra_capabilities = capabilities or None
    usage_limits = _usage_limits(config.max_tool_calls)
    scored = _cases_to_run(_with_answer_accuracy(dataset), kept)
    transient: set[str] = set()
    seen: dict[str, tuple[str, ...]] = {}

    # Local span recording follows the token rather than the return of `configure_logfire`, because
    # that return is `True` whenever an earlier call in the process configured Logfire.
    logfire_settings = await resolve_logfire_settings(container)
    configure_logfire(logfire_settings)
    if not logfire_settings.write_token:
        enable_local_span_recording()

    traces: dict[str, ExecutionTrace] = {}
    if config.delivered_evidence_root is None:
        agent = build_agent(
            model, instructions=config.instructions, toolset=toolset, code_mode=config.code_mode, extra_capabilities=extra_capabilities
        )
        report = await scored.evaluate(
            _noting_transient_failures(_noting_seen_units(make_task(agent, usage_limits=usage_limits), seen, collection), transient),
            progress=False,
            max_concurrency=config.max_concurrency,
            repeat=config.repeat,
            lifecycle=_PublishCaseName,
        )
    else:
        trial = _TrialWiring(
            model=model,
            instructions=config.instructions,
            code_mode=config.code_mode,
            capabilities=extra_capabilities,
            toolset=toolset,
            store=FileDeliveredEvidenceStore(config.delivered_evidence_root),
            withheld=WithheldRefs(collection, config.withheld_refs),
            search_hybrid=profile.search_hybrid,
            browsable=fetch_opens_map(envelope_handler, profile.tools),
            usage_limits=usage_limits,
        )
        report = await scored.evaluate(
            _noting_transient_failures(_noting_seen_units(_traced_task(trial, traces), seen, collection), transient),
            progress=False,
            max_concurrency=config.max_concurrency,
            repeat=config.repeat,
            lifecycle=_PublishCaseName,
        )
    lookup_error: str | None = None
    if config.results_path is not None:
        notes = _CaseNotes(collections_read=collections_read, transient=frozenset(transient), seen_units=seen)
        lookup_error = await _write_run_results(config, config.results_path, report, dataset, stamp=stamp, notes=notes, kept=kept)
    return TracedEvaluationRun(report=report, traces=traces, knowledge_base_fingerprint=identity.fingerprint, source_lookup_error=lookup_error)


def _usage_limits(max_tool_calls: int | None) -> UsageLimits | None:
    """The usage limits that hold each case to `max_tool_calls`, or `None` for pydantic-ai's defaults."""
    return UsageLimits(tool_calls_limit=max_tool_calls) if max_tool_calls is not None else None


def _noting_transient_failures(
    task: Callable[[QaInput], Awaitable[RetrievalAnswer]], transient: set[str]
) -> Callable[[QaInput], Awaitable[RetrievalAnswer]]:
    """The task, adding the running case's name to `transient` when it fails transiently, then raising the failure on."""

    async def run_case(inp: QaInput) -> RetrievalAnswer:
        try:
            return await task(inp)
        except Exception as error:
            case = _CURRENT_CASE_NAME.get()
            if case is not None and is_transient_failure(error):
                transient.add(case)
            raise

    return run_case


def _noting_seen_units(
    task: Callable[[QaInput], Awaitable[RetrievalAnswer]], seen: dict[str, tuple[str, ...]], collection: str
) -> Callable[[QaInput], Awaitable[RetrievalAnswer]]:
    """The task, filing the units its run's tool results named under the running case's name, whether it answers or fails."""

    async def run_case(inp: QaInput) -> RetrievalAnswer:
        with capture_run_messages() as messages:
            try:
                return await task(inp)
            finally:
                case = _CURRENT_CASE_NAME.get()
                if case is not None:
                    seen[case] = seen_units(messages, collection=collection)

    return run_case


def _cases_to_run(dataset: EvaluationDataset, kept: _KeptResults | None) -> EvaluationDataset:
    """A copy of `dataset` without the cases whose lines a resumed run keeps, or `dataset` itself when it keeps none."""
    if kept is None:
        return dataset
    kept_cases = {line.case for line in kept.lines}
    return dataset.model_copy(update={"cases": [case for case in dataset.cases if case.name not in kept_cases]})


def _with_answer_accuracy(dataset: EvaluationDataset) -> EvaluationDataset:
    """A copy of `dataset` that also scores answers when a case expects one and it does not already, else `dataset` itself."""
    expects_answer = any(case.expected_output is not None and case.expected_output.answer.strip() for case in dataset.cases)
    if not expects_answer or any(isinstance(evaluator, AnswerAccuracy) for evaluator in dataset.evaluators):
        return dataset
    return dataset.model_copy(update={"evaluators": [*dataset.evaluators, AnswerAccuracy()]})


# The columns of an `api` row that name the knowledge base the row belongs to.
_COLLECTION_COLUMNS = ("collection", "src_collection", "dst_collection", "subject_collection")

# The `api` views that describe every knowledge base the reader can see, one row each, rather than hold one knowledge
# base's content. A statement that reads only these never flags its case.
_DESCRIBING_VIEWS = frozenset({"about", "authoring_rules", "page_types", "schema_guide"})

_API_OBJECT = re.compile(r'"?\bapi"?\s*\.\s*"?(\w+)', re.IGNORECASE)


@dataclass(frozen=True)
class _CollectionRecordingGateway:
    """Runs each statement through `wrapped` and files the collections its rows name under the running case."""

    wrapped: SqlGateway
    collections: dict[str, set[str]]

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        """Run the statement, then note the collections its rows name for the case whose task called it, unless it read only describing views."""
        result = await self.wrapped.rows(sql, params, cap=cap)
        case = _CURRENT_CASE_NAME.get()
        if case is not None and _reads_content(sql):
            self.collections.setdefault(case, set()).update(_named_collections(result.rows))
        return result

    async def api_objects(self) -> SchemaDescription:
        """Describe the `api` surface through `wrapped`."""
        return await self.wrapped.api_objects()


def _reads_content(sql: str) -> bool:
    """Whether the statement names an `api` object other than a describing view, or names none the pattern can see."""
    named = {name.lower() for name in _API_OBJECT.findall(sql)}
    return not named or not named <= _DESCRIBING_VIEWS


def _named_collections(rows: Sequence[Row]) -> Iterator[str]:
    """Every collection the rows name in a collection column."""
    for row in rows:
        for column in _COLLECTION_COLUMNS:
            value = row.get(column)
            if isinstance(value, str):
                yield value


@dataclass(frozen=True)
class _IndexIdentity:
    """The knowledge base's index fingerprint and index build, read together before the first case."""

    fingerprint: str
    build: str


@dataclass(frozen=True)
class _RunStamp:
    """What the results header records about the run's start: the knowledge base, the time, the index fingerprint and the index build."""

    collection: str
    started: datetime
    knowledge_base_fingerprint: str
    index_build: str


@dataclass(frozen=True)
class _CaseNotes:
    """What the run noted per case while it ran: its SQL rows' collections, the transient failures and the units its tool results named."""

    collections_read: Mapping[str, Collection[str]]
    transient: frozenset[str]
    seen_units: Mapping[str, Sequence[str]]


@dataclass(frozen=True)
class _KeptResults:
    """What a resumed run keeps from its results file: the header and the lines of the cases it does not run again."""

    header: ResultsHeader
    lines: tuple[ResultsLine, ...]


def _results_header(config: EvaluationRunConfig, dataset: EvaluationDataset, stamp: _RunStamp, *, source_lookup_error: str | None) -> ResultsHeader:
    """The header that records how this run was set up."""
    return ResultsHeader(
        strategy=config.retrieval_strategy or DEFAULT_STRATEGY.value,
        sql=config.sql is not None,
        code_mode=config.code_mode,
        model=config.model_spec,
        thinking=config.thinking,
        instructions_sha256=hashlib.sha256(config.instructions.encode("utf-8")).hexdigest(),
        max_tool_calls=config.max_tool_calls,
        pinned_search=config.pinned_search,
        search_date_range=True,
        dataset=dataset.name,
        dataset_sha256=dataset_sha256(dataset),
        collection=stamp.collection,
        knowledge_base_fingerprint=stamp.knowledge_base_fingerprint,
        index_build=stamp.index_build,
        started=stamp.started.isoformat(),
        source_lookup_error=source_lookup_error,
    )


def _held_differences(written: ResultsHeader, run: ResultsHeader) -> list[str]:
    """Each field the paired comparison holds, and the index build, on which the file's header and this run differ, with both values."""
    held: tuple[tuple[str, str | int | bool | None, str | int | bool | None], ...] = (
        ("strategy", written.strategy, run.strategy),
        ("sql", written.sql, run.sql),
        ("code_mode", written.code_mode, run.code_mode),
        ("model", written.model, run.model),
        ("thinking", written.thinking, run.thinking),
        ("instructions_sha256", written.instructions_sha256, run.instructions_sha256),
        ("max_tool_calls", written.max_tool_calls, run.max_tool_calls),
        ("pinned_search", written.pinned_search, run.pinned_search),
        ("search_date_range", written.search_date_range, run.search_date_range),
        ("dataset_sha256", written.dataset_sha256, run.dataset_sha256),
        ("collection", written.collection, run.collection),
        ("index_build", _build_or_unknown(written.index_build), _build_or_unknown(run.index_build)),
    )
    return [f"{name} ({in_file} in the file, {now} now)" for name, in_file, now in held if in_file != now]


def _build_or_unknown(index_build: str | None) -> str:
    """The index build as a refusal names it, `unknown` for a header that did not record one."""
    return index_build if index_build is not None else "unknown"


def _kept_results(config: EvaluationRunConfig, dataset: EvaluationDataset, stamp: _RunStamp) -> _KeptResults | None:
    """The results file's header and the dataset cases' lines that did not fail transiently, or `None` when there is no file yet."""
    path = config.results_path
    if path is None or not path.exists():
        return None
    try:
        written, lines = read_results(path)
    except (OSError, ValueError) as error:
        msg = f"the results file {path} cannot be resumed: {error}"
        raise ResultsFileNotResumableError(msg) from error
    differences = _held_differences(written, _results_header(config, dataset, stamp, source_lookup_error=None))
    if differences:
        msg = (
            f"the results file {path} was written by a run whose settings differ in {', '.join(differences)}, so its cases "
            "cannot be kept. Name another results file, or run without resuming to replace this one."
        )
        raise ResultsFileNotResumableError(msg)
    dataset_cases = {case.name for case in dataset.cases}
    return _KeptResults(
        header=written, lines=tuple(line for line in lines if line.case in dataset_cases and not (line.failed and line.transient))
    )


async def _write_run_results(  # noqa: PLR0913 (the run's settings, its file, its report, its dataset, its start, its notes and its kept lines are distinct inputs)
    config: EvaluationRunConfig,
    path: Path,
    report: EvaluationRunReport,
    dataset: EvaluationDataset,
    *,
    stamp: _RunStamp,
    notes: _CaseNotes,
    kept: _KeptResults | None,
) -> str | None:
    """Write the run's results file and return what the source lookup raised, noting it in a header the run wrote itself."""
    collection = stamp.collection
    mapped: _CaseSources | None = None
    lookup_error: str | None = None
    if config.hit_sources is not None:
        try:
            mapped = await _case_sources(report, notes.seen_units, config.hit_sources, collection)
        except Exception as exc:  # noqa: BLE001 (any lookup failure is noted in the header so the run's results are still written)
            lookup_error = f"{type(exc).__name__}: {exc}"
    header = _results_header(config, dataset, stamp, source_lookup_error=lookup_error) if kept is None else kept.header
    other_collection = frozenset(case for case, names in notes.collections_read.items() if any(name != collection for name in names))
    case_order = [case.name for case in dataset.cases if case.name is not None]
    lines = results_lines(
        report,
        sources=mapped.cited if mapped is not None else None,
        seen=mapped.seen if mapped is not None else None,
        other_collection=other_collection,
        case_order=case_order,
        source_lookup_failed=lookup_error is not None,
        transient=notes.transient,
        kept=kept.lines if kept is not None else (),
    )
    write_results(path, header, lines)
    return lookup_error


async def _index_identity(container: Resolver, collection: str) -> _IndexIdentity:
    """The knowledge base's index fingerprint and name, read the way a dataset version's stamp reads it, and its index build."""
    from engram.evals.commands import InventoryReaderFactory  # noqa: PLC0415 (import cycle: engram.evals.harness and engram.evals.commands)

    make_reader = await container.get(InventoryReaderFactory)
    async with make_reader() as reader:
        return _IndexIdentity(
            fingerprint=await reader.knowledge_base_fingerprint(collection=collection), build=await reader.index_build(collection=collection)
        )


@dataclass(frozen=True)
class _CaseSources:
    """The ranked sources each answered case cited, and the sources each case's tool results named, keyed by case name."""

    cited: Mapping[str, Sequence[str]]
    seen: Mapping[str, frozenset[str]]


async def _case_sources(
    report: EvaluationRunReport, seen_units: Mapping[str, Sequence[str]], hit_sources: HitSources, collection: str
) -> _CaseSources:
    """The sources each case cited, each ref read as its bare unit id, and each case's tool results named, in one lookup of each kind."""
    from engram.evals.scoring.contracts import HitRef, ranked_sources  # noqa: PLC0415 (import cycle: engram.evals.harness and engram.evals.scoring)

    def as_hit(ref: str, pages: Collection[str]) -> HitRef:
        """A page, or an anchor on one, as a page hit, and any other id as a raw chunk hit."""
        page = page_of(ref)
        return HitRef(id=page, hit_level="page", page_id=page) if page in pages else HitRef(id=ref, hit_level="raw", page_id=None)

    cited = {case.name: [bare_unit_id(ref, collection=collection) for ref in case.output.cited_source_ids] for case in report.cases}
    candidates = list(dict.fromkeys(page_of(ref) for refs in (*cited.values(), *seen_units.values()) for ref in refs))
    pages = await hit_sources.page_ids(collection, candidates) if candidates else frozenset[str]()
    page_sources = await hit_sources.source_ids(collection, sorted(pages)) if pages else {}
    return _CaseSources(
        cited={name: ranked_sources([as_hit(ref, pages) for ref in refs], page_sources)[0] for name, refs in cited.items()},
        seen={name: frozenset(ranked_sources([as_hit(unit, pages) for unit in units], page_sources)[0]) for name, units in seen_units.items()},
    )


class ResultsFileNotResumableError(ValueError):
    """A resumed run cannot keep the cases of its results file.

    The file cannot be read, or it was written by a run whose settings differ in a field the paired comparison holds.
    The message names the line, or each differing field with both values.
    """


class UnnamedCaseError(EvaluationError):
    """A guarded trial cannot be identified because the case it runs has no name."""


# The case the running task belongs to. `CaseLifecycle.setup` runs in the same coroutine as the task
# that follows it, so what it publishes here is what that task reads.
_CURRENT_CASE_NAME: ContextVar[str | None] = ContextVar("current_case_name", default=None)


class _PublishCaseName(CaseLifecycle[QaInput, RetrievalAnswer, QaCaseMeta]):
    """Puts the case's own name where the task running it can read it."""

    async def setup(self) -> None:
        """Publish this case's name for the task about to run."""
        _CURRENT_CASE_NAME.set(self.case.name)


def current_trial_id() -> str:
    """Return the name of the case the running task belongs to, which names its trial.

    Returns:
        The running case's name.

    Raises:
        UnnamedCaseError: The case carries no name, so its evidence has no identity to be filed
            under and nothing may be derived from its content instead.
    """
    name = _CURRENT_CASE_NAME.get()
    if not name:
        msg = "a guarded evaluation trial needs its case's name, and this case has none"
        raise UnnamedCaseError(msg)
    return name


@dataclass(frozen=True)
class _TrialWiring:
    """Everything one traced trial builds its own agent from, fixed for the whole run."""

    model: Model
    instructions: str
    code_mode: bool
    capabilities: list[AbstractCapability] | None
    toolset: RetrievalToolset
    store: DeliveredEvidenceStore
    withheld: WithheldRefs
    search_hybrid: bool | None
    browsable: bool
    usage_limits: UsageLimits | None


def _traced_task(trial: _TrialWiring, traces: dict[str, ExecutionTrace]) -> Callable[[QaInput], Awaitable[RetrievalAnswer]]:
    """The task that runs each case as its own trial, named by the case, and files its trace even when it fails."""

    async def run_case(inp: QaInput) -> RetrievalAnswer:
        trial_id = current_trial_id()
        recorder = TraceRecorder()
        guarded = GuardedTracingToolset[object](
            wrapped=trial.toolset,
            store=trial.store,
            sink=recorder,
            trial_id=trial_id,
            withheld=trial.withheld,
            search_hybrid=trial.search_hybrid,
            browsable=trial.browsable,
        )
        agent = build_agent(
            TracingModel(trial.model, recorder),
            instructions=trial.instructions,
            toolset=guarded,
            code_mode=trial.code_mode,
            extra_capabilities=trial.capabilities,
        )
        started = perf_counter()
        try:
            result = await agent.run(inp.question, usage_limits=trial.usage_limits)
        finally:
            traces[trial_id] = recorder.trace(run_duration_ms=_elapsed_ms(started))
        return result.output

    return run_case


class TracedRetrievalExecution(TaskExecution):
    """What one blind trial produced: its answer, the sources it cited, and where its record sits.

    `output` is the answer prose a grader is given. `cited_source_ids` is what the agent said it
    relied on, which is a different fact from what its tools actually delivered, and both are scored
    separately. `trial_id` names the delivered-evidence record `trace` points into.
    """

    trial_id: Nonempty
    cited_source_ids: tuple[str, ...] = ()


def trial_id_for(case_id: str, identity: RunIdentity, repetition: int) -> str:
    """The identity of one trial: this case, under this run identity, at this repetition.

    Two arms of a comparison differ in their run identity and therefore in their trial ids, so
    neither can read or overwrite the other's delivered evidence. Nothing about which arm it is
    can be recovered from the id.

    Args:
        case_id: The case being run.
        identity: The run identity this trial executes under.
        repetition: The repetition slot within the comparison.

    Returns:
        The trial id its delivered evidence is filed under.
    """
    return f"{case_id}:{repetition}:{model_fingerprint(identity)}"


@dataclass(frozen=True)
class BlindTrialRunner:
    """A `TaskRunner` that runs one case knowing only its id and its input.

    It never receives the case's `AnswerCriterion`, its expected sources or its variant, so nothing
    it produces can be shaped by the labels it will be graded against. Each trial gets its own
    `GuardedTracingToolset` over this run's withheld set for that case, its own traced model and its
    own evidence record, so two trials never share ordinals or an evidence total.

    A trial files whatever trace it recorded however it ends, including one that died inside a tool
    call or a store write, so the row naming what went wrong always reaches the caller. A recoverable
    agent failure then raises `EvaluationError` carrying the usage it had already spent.

    Attributes:
        model: The model under test, wrapped per trial so its turns are recorded.
        instructions: The agent-under-test's system instructions.
        toolset: The run's retrieval toolset, wrapped per trial by the guard.
        store: Where each trial's delivered evidence is written.
        collection: The knowledge base the trial reads, which is how a bare reference resolves.
        traces: Where each trial files its `ExecutionTrace`, keyed by trial id.
        withheld: The references each case withholds, keyed by case id. A case with no entry
            withholds nothing.
        search_hybrid: The leg this run fixed `search` to, or `None` when the model picks per call.
        browsable: Whether the toolset's `fetch` opens a map, so a rebuilt `search` answer may offer it.
        capabilities: Capabilities appended to every trial's agent.
        max_tool_calls: The most tool calls one trial may make. A trial that asks for more fails
            recoverably. `None` sets no limit.
    """

    model: Model
    instructions: str
    toolset: AbstractToolset[object]
    store: DeliveredEvidenceStore
    collection: str
    traces: MutableMapping[str, ExecutionTrace]
    withheld: Mapping[str, frozenset[str]] = field(default_factory=dict)
    search_hybrid: bool | None = None
    browsable: bool = False
    capabilities: list[AbstractCapability] | None = None
    max_tool_calls: int | None = None

    async def __call__(self, case: EvaluationInput, identity: RunIdentity, repetition: int) -> TracedRetrievalExecution:
        """Run `case` once behind the guard and return what it produced.

        Args:
            case: The case's id and its input, which is everything execution is told.
            identity: The run identity this trial executes under.
            repetition: The repetition slot within the comparison.

        Returns:
            The trial's answer, citations, usage and trace.

        Raises:
            EvaluationError: The agent run failed recoverably. The trace recorded so far is filed
                before the error is raised.
            DeliveredEvidenceError: A tool's result could not be stored, so the trial has no record.
                The trace recorded up to that point is still filed.
            WithheldEvidenceLeakError: A finished payload still named a withheld reference. The
                trace is still filed, carrying the row that names the offending call.
        """
        trial_id = trial_id_for(case.case_id, identity, repetition)
        recorder = TraceRecorder()
        agent = build_agent(
            TracingModel(self.model, recorder),
            instructions=self.instructions,
            toolset=GuardedTracingToolset[object](
                wrapped=self.toolset,
                store=self.store,
                sink=recorder,
                trial_id=trial_id,
                withheld=WithheldRefs(self.collection, self.withheld.get(case.case_id, frozenset())),
                search_hybrid=self.search_hybrid,
                browsable=self.browsable,
            ),
            extra_capabilities=self.capabilities,
        )
        started = perf_counter()
        try:
            outcome = await _run_blind(agent, case.input, _usage_limits(self.max_tool_calls))
        finally:
            self.traces[trial_id] = recorder.trace(run_duration_ms=_elapsed_ms(started))
        if outcome.answer is None:
            raise EvaluationError(outcome.failure, usage=outcome.usage)
        return TracedRetrievalExecution(
            output=outcome.answer.answer,
            trial_id=trial_id,
            cited_source_ids=tuple(outcome.answer.cited_source_ids),
            usage=outcome.usage,
            trace=self.traces[trial_id],
        )


@dataclass(frozen=True)
class _BlindOutcome:
    """What one blind run produced and what it spent, with `answer` unset when it produced none."""

    usage: ResourceUsage
    answer: RetrievalAnswer | None = None
    failure: str = ""


async def _run_blind(agent: AgentUnderTest, prompt: str, usage_limits: UsageLimits | None) -> _BlindOutcome:
    """Drive one agent run to its end within `usage_limits` and return its usage, with its answer or its failure."""
    failure: AgentRunError | TimeoutError | None = None
    async with agent.iter(prompt, usage_limits=usage_limits) as run:
        try:
            async for _node in run:
                pass
        except (AgentRunError, TimeoutError) as error:
            failure = error
        usage = measured_usage(run.usage)
        result = run.result
    if failure is not None:
        return _BlindOutcome(usage=usage, failure=str(failure))
    if result is None:
        return _BlindOutcome(usage=usage, failure="the agent under test finished without an answer")
    return _BlindOutcome(usage=usage, answer=result.output)


def _elapsed_ms(started: float) -> int:
    """Whole milliseconds since `started`, never negative."""
    return max(round((perf_counter() - started) * 1000), 0)


async def run_experiment(axes: ExperimentAxes, *, hold: ExperimentAxis, evaluate: VariantEvaluator) -> list[ExperimentRun]:
    """Evaluate every variant of an experiment that holds `hold` constant, one after another.

    The caller tags each evaluation, for example with a Logfire span, so the runs stay comparable.

    Args:
        axes: The values to try on each axis.
        hold: The axis held constant.
        evaluate: The evaluator that builds and scores the agent for one variant.

    Returns:
        One `ExperimentRun` per variant, in `expand_variants` order.
    """
    runs: list[ExperimentRun] = []
    for variant in expand_variants(axes, hold=hold):
        metrics = await evaluate(variant)
        runs.append(ExperimentRun(variant=variant, metrics=dict(metrics)))
    return runs


def average_metrics(report: EvaluationRunReport) -> dict[str, float]:
    """Average each metric across the report's cases.

    Each metric is averaged over the cases that scored it, and the keys come back sorted.

    Args:
        report: The evaluation report whose cases to average over.

    Returns:
        `{metric_name: mean_score}` over the report's cases, ordered by metric name.
    """
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for case in report.cases:
        for name, result in case.scores.items():
            value = float(result.value)
            totals[name] = totals.get(name, 0.0) + value
            counts[name] = counts.get(name, 0) + 1
    return {name: totals[name] / counts[name] for name in sorted(totals)}

