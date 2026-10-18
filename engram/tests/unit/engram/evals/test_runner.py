r"""Unit specs for the shared evaluation-run service (`engram.evals.harness.service`).

The single source of truth the `engram evals` CLI and the `POST /api/evals/run` job both
delegate to (Phase-3 finding E — the run flow had been copied into both). These specs pin the
three public functions directly, network-free:

* :func:`build_eval_model` returns the right model per spec — ``test-model`` is a network-free
  ``TestModel``; any other spec is an Azure deployment built over the container's
  :class:`~core.settings.AzureOpenAiSettings` endpoint (constructed, never called, no socket),
* :func:`average_metrics` means each metric across a hand-built report's cases;
* :func:`run_eval` over ``TestModel(call_tools=[])`` (which synthesizes a
  :class:`~engram.evals.shared.domain.RetrievalAnswer` without invoking the tools, so the toolset must be
  *resolvable* but is never *called*) produces a report with one scored case per dataset case.

Fakes over mocks: a :class:`FakeContainer` resolves exactly the keys the service asks for from
scriptable public state, and the retrieval toolset it hands back is the **real**
:func:`~engram.retrieval.tools.build_retrieval_toolset` over the inert :class:`FakeSqlGateway` /
:class:`FakeEmbedding` — so resolving it touches no Postgres, Azure, or network. ``average_metrics``
is driven by a tiny structural report stand-in (the function only reads `report.cases` and each
case's `scores`), which keeps the spec off pydantic-evals' heavy `ReportCase` constructor.
"""

from __future__ import annotations

import hashlib
import json

from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING

import pytest

from core.ai.disclosure.contracts import ProgressiveDisclosure
from core.ai.evaluation.contracts import (
    EvaluationError,
    EvaluationInput,
    ExecutionTrace,
    FileDeliveredEvidenceStore,
    RunIdentity,
    TracingModel,
    content_fingerprint,
)
from core.ai.retrieval.contracts import SchemaDescription
from core.settings import AzureOpenAiSettings, LogfireSettings
from pydantic_ai import FunctionToolset
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelRequest, ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.profiles.openai import OPENAI_REASONING_EFFORT_MAP
from pydantic_ai.retries import AsyncHTTPX2TenacityTransport
from pydantic_evals import Case
from sqlalchemy.exc import OperationalError

from engram.config import EvalsSettings
from engram.evals.commands import InventoryReaderFactory
from engram.evals.generation.contracts import AuthoredCase, EvaluationDataset, build_dataset, save_dataset
from engram.evals.harness import service
from engram.evals.harness.domain.evaluators import AnswerAccuracy
from engram.evals.harness.infrastructure.instrumentation import reset_logfire_for_testing
from engram.evals.harness.infrastructure.results import dataset_sha256
from engram.evals.harness.service import (
    BlindTrialRunner,
    EvaluationRunConfig,
    ResultsFileNotResumableError,
    UnnamedCaseError,
    apply_reasoning_effort,
    average_metrics,
    build_eval_model,
    run_eval,
    run_eval_on_dataset,
    run_traced_eval_on_dataset,
    trial_id_for,
)
from engram.evals.outcomes.contracts import GuardedTracingToolset
from engram.evals.shared.domain import ExpectedQa, QaCaseMeta, QaInput
from engram.kb.contracts import KnowledgeBaseSchema
from engram.retrieval.capability import SqlKnowledgeBase
from engram.retrieval.domain import ObjectInfo, SqlResult
from engram.retrieval.envelope import EnvelopeHandler
from engram.retrieval.queries import DescribeSchemaHandler, FetchHandler, GraphExpandHandler, GraphPathsHandler, SearchHandler
from engram.retrieval.tools import RetrievalHandlers, RetrievalToolset, build_retrieval_toolset
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.gateway import FakeSqlGateway
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
    from pathlib import Path

    from core.thinking import Thinking
    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models import Model
    from pydantic_ai.toolsets import AbstractToolset


# -- fakes -------------------------------------------------------------------


def _handlers() -> RetrievalHandlers:
    """The real retrieval-handler bundle over inert fakes — `run_eval` builds the per-run toolset from it offline."""
    gateway = FakeSqlGateway()
    return RetrievalHandlers(
        search=SearchHandler(gateway, FakeEmbedding()),
        fetch=FetchHandler(gateway, "research"),
        graph_expand=GraphExpandHandler(gateway, "research"),
        find_paths=GraphPathsHandler(gateway, "research"),
    )


def _azure_openai() -> AzureOpenAiSettings:
    """A dummy Azure endpoint — the client is built (lazy auth, no socket), never called."""
    return AzureOpenAiSettings(endpoint="https://example.openai.azure.com")


def _envelope_handler() -> EnvelopeHandler:
    """The real `EnvelopeHandler` over the inert `FakeSqlGateway` — no DB, mirrors `_handlers`."""
    return EnvelopeHandler(FakeSqlGateway())


@dataclass
class _FakeCoverageReader:
    """Answers a knowledge base fingerprint of `{spec_hash}:{collection}`, the shape the real reader reads, and the scripted index build."""

    spec_hash: str = "spec-hash"
    build: str = "7+500-1000 test-embedding"

    async def knowledge_base_fingerprint(self, *, collection: str) -> str:
        return f"{self.spec_hash}:{collection}"

    async def index_build(self, *, collection: str) -> str:
        return self.build


@dataclass
class _FakeInventoryReaders:
    """Opens the one scripted coverage reader, counting how many times it was opened."""

    reader: _FakeCoverageReader = field(default_factory=_FakeCoverageReader)
    opened: int = 0

    @asynccontextmanager
    async def __call__(self) -> AsyncGenerator[_FakeCoverageReader]:
        self.opened += 1
        yield self.reader


@dataclass
class FakeContainer:
    """An async DI container resolving exactly the keys the run service asks for, from public state.

    The retrieval handlers are the real serve query handlers over the inert :class:`FakeSqlGateway` /
    :class:`FakeEmbedding` (``run_eval`` builds the per-run toolset from them), so resolving them is
    fully offline. The :class:`~core.settings.LogfireSettings` defaults to an empty token (the run
    service's local span-recording path). `requested` records every resolved key for assertions.
    """

    evals_settings: EvalsSettings = field(default_factory=EvalsSettings)
    openai: AzureOpenAiSettings = field(default_factory=_azure_openai)
    logfire: LogfireSettings = field(default_factory=LogfireSettings)
    handlers: RetrievalHandlers = field(default_factory=_handlers)
    envelope_handler: EnvelopeHandler = field(default_factory=_envelope_handler)
    # The knowledge base whose partition the run's `search` tool scopes to — the bundled research default
    # unless a spec scripts another collection.
    spec: KnowledgeBaseSchema = field(default_factory=research_spec)
    inventory: _FakeInventoryReaders = field(default_factory=_FakeInventoryReaders)
    requested: list[type] = field(default_factory=list)

    async def get[T](self, klass: type[T]) -> T:
        self.requested.append(klass)
        services: dict[type, object] = {
            EvalsSettings: self.evals_settings,
            AzureOpenAiSettings: self.openai,
            LogfireSettings: self.logfire,
            RetrievalHandlers: self.handlers,
            EnvelopeHandler: self.envelope_handler,
            KnowledgeBaseSchema: self.spec,
            InventoryReaderFactory: self.inventory,
        }
        service = services.get(klass)
        if not isinstance(service, klass):
            msg = f"FakeContainer has no provider for {klass!r}"
            raise KeyError(msg)
        return service

    async def close(self) -> None:  # pragma: no cover — never closed in these specs
        return


@dataclass(frozen=True)
class _Score:
    """A structural stand-in for one `EvaluationResult` — `average_metrics` reads only `.value`."""

    value: float


@dataclass(frozen=True)
class _Case:
    """A structural stand-in for one `ReportCase` — `average_metrics` reads only `.scores`."""

    scores: dict[str, _Score]


@dataclass(frozen=True)
class _Report:
    """A structural stand-in for an `EvaluationReport` — `average_metrics` reads only `.cases`."""

    cases: list[_Case]


def _report(*case_scores: dict[str, float]) -> _Report:
    """Build a structural report whose cases carry the given metric→value maps."""
    return _Report(cases=[_Case(scores={name: _Score(value) for name, value in scores.items()}) for scores in case_scores])


# -- apply_reasoning_effort --------------------------------------------------


class TestApplyReasoningEffort:
    def test_known_effort_appends_a_directive(self):
        result = apply_reasoning_effort("Base instructions.", "high")

        assert result.startswith("Base instructions.")
        assert "HIGH" in result  # the high-effort directive is appended

    def test_none_effort_leaves_instructions_unchanged(self):
        assert apply_reasoning_effort("Base instructions.", None) == "Base instructions."

    def test_unknown_effort_is_ignored_not_raised(self):
        # The request boundary is lenient — an unrecognised effort just yields no directive.
        assert apply_reasoning_effort("Base instructions.", "turbo") == "Base instructions."


# -- build_eval_model --------------------------------------------------------


class TestBuildEvalModel:
    async def test_test_model_spec_is_a_network_free_test_model(self):
        model = await build_eval_model(FakeContainer(), "test-model")

        assert isinstance(model, TestModel)
        # `call_tools=[]` so the agent synthesizes the structured answer instead of calling every
        # offered tool: a bare `TestModel` trips `fetch`'s `ModelRetry`-on-empty.
        assert model.call_tools == []

    async def test_other_spec_is_an_azure_deployment_built_over_the_endpoint(self):
        container = FakeContainer()

        model = await build_eval_model(container, "gpt-4o-eval")

        # The Azure path builds the client (lazy auth, no socket) — it is not the test model.
        assert not isinstance(model, TestModel)
        assert AzureOpenAiSettings in container.requested

    async def test_a_thinking_level_is_sent_as_the_responses_reasoning_effort(self):
        model = await build_eval_model(FakeContainer(), "gpt-5.4", thinking="xhigh")

        assert isinstance(model, OpenAIResponsesModel)
        assert model.settings == {"thinking": "xhigh"}
        _settings, parameters = model.prepare_request(None, ModelRequestParameters())
        assert parameters.thinking == "xhigh"
        assert OPENAI_REASONING_EFFORT_MAP["xhigh"] == "xhigh"

    async def test_no_thinking_level_leaves_the_model_at_its_default(self):
        model = await build_eval_model(FakeContainer(), "gpt-5.4")

        assert isinstance(model, OpenAIResponsesModel)
        assert model.settings is None

    @pytest.mark.parametrize("thinking", [None, "low"])
    async def test_an_azure_deployment_retries_on_the_chat_client_and_never_in_the_sdk(self, thinking: Thinking | None):
        model = await build_eval_model(FakeContainer(), "gpt-5.4", thinking=thinking)

        assert isinstance(model, OpenAIResponsesModel)
        assert isinstance(model.client._client._transport, AsyncHTTPX2TenacityTransport)
        assert model.client.max_retries == 0

    async def test_a_thinking_level_for_a_model_that_takes_none_is_refused_naming_the_model_and_the_way_out(self):
        with pytest.raises(ValueError, match="gpt-4o-eval") as refused:
            await build_eval_model(FakeContainer(), "gpt-4o-eval", thinking="high")

        assert "Run it without a thinking level, or name a deployment pydantic-ai knows as a reasoning model." in str(refused.value)


# -- average_metrics ---------------------------------------------------------


class TestAverageMetrics:
    def test_means_each_metric_across_cases(self):
        report = _report({"recall@1": 1.0, "mrr": 0.5}, {"recall@1": 0.0, "mrr": 1.0})

        averaged = average_metrics(report)  # type: ignore[arg-type]  # structural EvaluationRunReport slice

        assert averaged == {"mrr": 0.75, "recall@1": 0.5}

    def test_keys_are_sorted_for_a_stable_aggregate(self):
        report = _report({"recall@5": 1.0, "mrr": 1.0, "precision@1": 1.0})

        averaged = average_metrics(report)  # type: ignore[arg-type]  # structural EvaluationRunReport slice

        assert list(averaged) == ["mrr", "precision@1", "recall@5"]

    def test_an_empty_report_is_an_empty_aggregate(self):
        assert average_metrics(_report()) == {}  # type: ignore[arg-type]  # structural EvaluationRunReport slice


# -- run_eval ----------------------------------------------------------------


def _write_golden(tmp_path: Path) -> Path:
    """Write a tiny two-case golden dataset via the slice's own builders."""
    cases = [
        AuthoredCase(
            question=QaInput(question="What is LightRAG?"),
            expected=ExpectedQa(answer="A graph-based RAG system.", expected_source_ids=("paper.lightrag-2025",)),
            meta=QaCaseMeta(anchor_page_id="paper.lightrag-2025", difficulty="easy"),
        ),
        AuthoredCase(
            question=QaInput(question="What is a vector cache?"),
            expected=ExpectedQa(answer="A hot-passage embedding cache.", expected_source_ids=("paper.vector-cache",)),
            meta=QaCaseMeta(anchor_page_id="paper.vector-cache", difficulty="easy"),
        ),
    ]
    path = tmp_path / "seed.yaml"
    save_dataset(build_dataset(cases), path)
    return path


_EXPECTED_METRICS = {"recall@1", "recall@5", "recall@10", "precision@1", "precision@5", "precision@10", "mrr", "n_tool_calls"}


def _spy_on_build_agent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[AbstractToolset[object], Sequence[AbstractCapability] | None]]:
    """Patch `service.build_agent` to record each call's `(toolset, extra_capabilities)`.

    Delegates to the real `build_agent` after recording, so the run still completes over a working
    agent (mirrors `test_passes_tool_descriptions_into_the_toolset_build`'s spy over
    `build_retrieval_toolset`). Shared by the disclosure specs below, which differ only in the
    `EvaluationRunConfig` they build and the assertions on the captured call.
    """
    captured: list[tuple[AbstractToolset[object], Sequence[AbstractCapability] | None]] = []
    real_build_agent = service.build_agent

    def _spy(
        model: Model,
        *,
        instructions: str,
        toolset: AbstractToolset[object],
        code_mode: bool = False,
        extra_capabilities: Sequence[AbstractCapability] | None = None,
    ) -> object:
        captured.append((toolset, extra_capabilities))
        return real_build_agent(
            model, instructions=instructions, toolset=toolset, code_mode=code_mode, extra_capabilities=extra_capabilities
        )

    monkeypatch.setattr(service, "build_agent", _spy)
    return captured


def _spy_on_build_retrieval_toolset(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Mapping[str, str] | None, str]]:
    """Patch `service.build_retrieval_toolset` to record each call's `(descriptions, collection)`.

    Delegates to the real builder after recording, so the run still completes over a working toolset
    (mirrors `_spy_on_build_agent`). Shared by the two specs that pin what the run threads into the
    per-run toolset build: the tuned description overrides and the knowledge base's own partition.
    """
    captured: list[tuple[Mapping[str, str] | None, str]] = []
    real_build = service.build_retrieval_toolset

    def _spy(
        handlers: RetrievalHandlers,
        *,
        only: frozenset[str] | None = None,
        search_hybrid: bool | None = None,
        descriptions: Mapping[str, str] | None = None,
        collection: str,
        envelope: EnvelopeHandler | None = None,
        can_sql: bool = False,
        pinned: bool = False,
        facet_keys: tuple[str, ...] = (),
    ) -> RetrievalToolset:
        captured.append((descriptions, collection))
        return real_build(
            handlers,
            only=only,
            search_hybrid=search_hybrid,
            descriptions=descriptions,
            collection=collection,
            envelope=envelope,
            can_sql=can_sql,
            pinned=pinned,
            facet_keys=facet_keys,
        )

    monkeypatch.setattr(service, "build_retrieval_toolset", _spy)
    return captured


def _spec_for_collection(collection: str) -> KnowledgeBaseSchema:
    """The bundled research spec re-pointed at another knowledge base partition."""
    spec = research_spec()
    return spec.model_copy(update={"retrieval": spec.retrieval.model_copy(update={"collection": collection})})


class TestRunEval:
    @pytest.fixture(autouse=True)
    def _reset_logfire(self) -> Callable[[], None]:
        """Clear the process-wide configured-once guard so `run_eval`'s local-span path actually runs."""
        reset_logfire_for_testing()
        return reset_logfire_for_testing

    async def test_scores_every_case_over_the_test_model(self, tmp_path: Path):
        dataset_path = _write_golden(tmp_path)

        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite the page ids.")
        report = await run_eval(FakeContainer(), config=config, dataset_path=dataset_path)

        # `TestModel(call_tools=[])` synthesizes a RetrievalAnswer (scores are arbitrary); the point
        # is the flow produced one scored case per golden case, each carrying the RetrievalMetrics keys.
        assert len(report.cases) == 2
        for case in report.cases:
            assert set(case.scores) >= _EXPECTED_METRICS, f"missing metrics for {case.name}: {_EXPECTED_METRICS - set(case.scores)}"

    async def test_the_averaged_report_carries_the_metric_keys(self, tmp_path: Path):
        dataset_path = _write_golden(tmp_path)

        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite the page ids.")
        report = await run_eval(FakeContainer(), config=config, dataset_path=dataset_path)
        averaged = average_metrics(report)

        assert set(averaged) >= _EXPECTED_METRICS

    async def test_runs_under_a_restricted_retrieval_strategy(self, tmp_path: Path):
        # A graph-only strategy exposes only the graph tools to the agent; the run still scores every
        # case (the test model synthesizes the answer), proving the per-run toolset build is wired in.
        dataset_path = _write_golden(tmp_path)

        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite the page ids.", retrieval_strategy="graph-only", code_mode=False)
        report = await run_eval(FakeContainer(), config=config, dataset_path=dataset_path)

        assert len(report.cases) == 2

    async def test_an_unknown_retrieval_strategy_raises(self, tmp_path: Path):
        dataset_path = _write_golden(tmp_path)

        config = EvaluationRunConfig(model_spec="test-model", instructions="x", retrieval_strategy="bogus")
        with pytest.raises(ValueError, match="unknown retrieval strategy"):
            await run_eval(FakeContainer(), config=config, dataset_path=dataset_path)

    async def test_tool_descriptions_default_to_none(self):
        assert EvaluationRunConfig(model_spec="test-model", instructions="x").tool_descriptions is None

    async def test_passes_tool_descriptions_into_the_toolset_build(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # The override map is threaded into `build_retrieval_toolset` as the per-run `descriptions`.
        captured = _spy_on_build_retrieval_toolset(monkeypatch)
        dataset_path = _write_golden(tmp_path)
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite the page ids.", tool_descriptions={"search": "X"})

        await run_eval(FakeContainer(), config=config, dataset_path=dataset_path)

        assert [descriptions for descriptions, _collection in captured] == [{"search": "X"}]

    async def test_scopes_search_to_the_knowledge_base_own_collection(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # The knowledge base's spec — not engram's shipped `"research"` default — fixes the partition `search`
        # reads, so an eval over a non-research knowledge base finds its own gold pages (the MCP surface has
        # always done this; the eval path had not, which scored every such case 0 recall silently).
        captured = _spy_on_build_retrieval_toolset(monkeypatch)
        container = FakeContainer(spec=_spec_for_collection("workdocs"))

        await run_eval(container, config=EvaluationRunConfig(model_spec="test-model", instructions="Cite."), dataset_path=_write_golden(tmp_path))

        assert [collection for _descriptions, collection in captured] == ["workdocs"]

    async def test_disclosure_defaults_to_false(self):
        assert EvaluationRunConfig(model_spec="test-model", instructions="x").disclosure is False

    async def test_disclosure_levels_the_fetch_tool_and_adds_the_capability(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # `disclosure=True` swaps `fetch` for the leveled read (its description advertises the knowledge base
        # map) and adds a `ProgressiveDisclosure` capability to `extra_capabilities`.
        captured = _spy_on_build_agent(monkeypatch)
        dataset_path = _write_golden(tmp_path)
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite the page ids.", disclosure=True)

        await run_eval(FakeContainer(), config=config, dataset_path=dataset_path)

        toolset, extra_capabilities = captured[0]
        assert isinstance(toolset, FunctionToolset)
        assert "fetch" in toolset.tools
        assert "map" in (toolset.tools["fetch"].description or "")
        assert extra_capabilities is not None
        assert any(isinstance(capability, ProgressiveDisclosure) for capability in extra_capabilities)

    async def test_disclosure_false_keeps_the_flat_fetch_and_no_capability(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        captured = _spy_on_build_agent(monkeypatch)
        dataset_path = _write_golden(tmp_path)
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite the page ids.")

        await run_eval(FakeContainer(), config=config, dataset_path=dataset_path)

        toolset, extra_capabilities = captured[0]
        assert isinstance(toolset, FunctionToolset)
        assert "map" not in (toolset.tools["fetch"].description or "")
        assert extra_capabilities is None


# -- run_eval_on_dataset -----------------------------------------------------


def _golden_dataset() -> EvaluationDataset:
    """A tiny two-case in-memory golden dataset (no disk), the tuning loop's evaluate subject."""
    cases = [
        AuthoredCase(
            question=QaInput(question="What is LightRAG?"),
            expected=ExpectedQa(answer="A graph-based RAG system.", expected_source_ids=("paper.lightrag-2025",)),
            meta=QaCaseMeta(anchor_page_id="paper.lightrag-2025", difficulty="easy"),
        ),
        AuthoredCase(
            question=QaInput(question="What is a vector cache?"),
            expected=ExpectedQa(answer="A hot-passage embedding cache.", expected_source_ids=("paper.vector-cache",)),
            meta=QaCaseMeta(anchor_page_id="paper.vector-cache", difficulty="easy"),
        ),
    ]
    return build_dataset(cases)


class _RecordingDataset(EvaluationDataset):
    """An `EvaluationDataset` that records each `evaluate` call's `max_concurrency`, then really evaluates.

    A fake over the dataset seam rather than a patch of pydantic-evals' own `Dataset.evaluate`: the
    run service calls `dataset.evaluate(...)` on whatever it is handed, so a subclass that records
    and delegates pins the argument without monkeypatching a library we do not own. The list is a
    public scriptable attribute, and `model_config` already permits the extra field.
    """

    max_concurrency_calls: list[int | None] = []  # noqa: RUF012 (rebound per instance by `over`, never shared)

    @classmethod
    def over(cls, dataset: EvaluationDataset) -> _RecordingDataset:
        """Wrap `dataset`'s cases and evaluators in a fresh recorder with its own call log.

        `Dataset.__init__` is hand-written and rejects unknown kwargs, so the log is bound after
        construction rather than passed in.
        """
        recorder = cls(name=dataset.name, cases=dataset.cases, evaluators=dataset.evaluators)
        recorder.max_concurrency_calls = []
        return recorder

    async def evaluate(self, task, **kwargs):  # type: ignore[no-untyped-def, override]  # records, then delegates verbatim
        self.max_concurrency_calls.append(kwargs.get("max_concurrency"))
        return await super().evaluate(task, **kwargs)


class TestRunEvalOnDataset:
    @pytest.fixture(autouse=True)
    def _reset_logfire(self) -> Callable[[], None]:
        """Clear the process-wide configured-once guard so the local-span path actually runs."""
        reset_logfire_for_testing()
        return reset_logfire_for_testing

    async def test_scores_every_case_of_an_in_memory_dataset(self):
        # The tuning loop evaluates in-memory train/val subsets, so the post-load flow runs over a
        # `EvaluationDataset` it is handed directly (no disk path), scoring one case per dataset case.
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite the page ids.")

        report = await run_eval_on_dataset(FakeContainer(), config=config, dataset=_golden_dataset())

        assert len(report.cases) == 2
        for case in report.cases:
            assert set(case.scores) >= _EXPECTED_METRICS, f"missing metrics for {case.name}: {_EXPECTED_METRICS - set(case.scores)}"

    async def test_max_concurrency_defaults_to_unbounded(self):
        # No cap unless a caller asks for one, so the offline/`test-model` runs keep pydantic-evals'
        # run-everything-at-once default.
        recorder = _RecordingDataset.over(_golden_dataset())

        await run_eval_on_dataset(FakeContainer(), config=EvaluationRunConfig(model_spec="test-model", instructions="Cite."), dataset=recorder)

        assert recorder.max_concurrency_calls == [None]

    async def test_threads_max_concurrency_into_the_evaluation(self):
        # A live run against a provisioned-throughput deployment 429s a whole dataset launched in
        # parallel, so the cap has to reach `Dataset.evaluate` rather than stopping at the config.
        recorder = _RecordingDataset.over(_golden_dataset())
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", max_concurrency=1)

        await run_eval_on_dataset(FakeContainer(), config=config, dataset=recorder)

        assert recorder.max_concurrency_calls == [1]


# -- run_traced_eval_on_dataset ----------------------------------------------


# The two case names `build_dataset` gives `_golden_dataset`'s cases, which are what a guarded run
# files each trial's evidence under.
_CASE_NAMES = ("paper.lightrag-2025-0", "paper.vector-cache-1")


def _duplicate_question_dataset() -> EvaluationDataset:
    """Two differently named cases asking the very same question."""
    cases = [
        AuthoredCase(
            question=QaInput(question="What is LightRAG?"),
            expected=ExpectedQa(answer="A graph-based RAG system.", expected_source_ids=("paper.lightrag-2025",)),
            meta=QaCaseMeta(anchor_page_id="paper.lightrag-2025", difficulty="easy"),
        ),
        AuthoredCase(
            question=QaInput(question="What is LightRAG?"),
            expected=ExpectedQa(answer="A graph-based RAG system, asked twice.", expected_source_ids=("paper.lightrag-2025",)),
            meta=QaCaseMeta(anchor_page_id="paper.lightrag-repeat", difficulty="easy"),
        ),
    ]
    return build_dataset(cases)


def _spy_on_built_agents(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Model, AbstractToolset[object]]]:
    """Patch `service.build_agent` to record each call's `(model, toolset)`, then build for real."""
    captured: list[tuple[Model, AbstractToolset[object]]] = []
    real_build_agent = service.build_agent

    def _spy(
        model: Model,
        *,
        instructions: str,
        toolset: AbstractToolset[object],
        code_mode: bool = False,
        extra_capabilities: Sequence[AbstractCapability] | None = None,
    ) -> object:
        captured.append((model, toolset))
        return real_build_agent(
            model, instructions=instructions, toolset=toolset, code_mode=code_mode, extra_capabilities=extra_capabilities
        )

    monkeypatch.setattr(service, "build_agent", _spy)
    return captured


def _use_tool_calling_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the run's model call `search` once per case, so a real tool result passes the guard."""

    async def _model(container: object, model_spec: str, *, thinking: Thinking | None = None) -> Model:
        return TestModel(call_tools=["search"])

    monkeypatch.setattr(service, "build_eval_model", _model)


def _fail_after_the_first_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the run's model call `search` once and then die, so a trial fails with rows recorded."""

    def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("search", {"query": "anything"})])
        msg = "the model stopped answering"
        raise RuntimeError(msg)

    async def _model(container: object, model_spec: str, *, thinking: Thinking | None = None) -> Model:
        return FunctionModel(_respond)

    monkeypatch.setattr(service, "build_eval_model", _model)


class TestTracedEvaluationRun:
    """The guarded, traced run: each case is its own trial, storing what its tools delivered."""

    @pytest.fixture(autouse=True)
    def _reset_logfire(self) -> Callable[[], None]:
        """Clear the process-wide configured-once guard so the local-span path actually runs."""
        reset_logfire_for_testing()
        return reset_logfire_for_testing

    async def test_evidence_and_withheld_refs_default_to_off(self):
        config = EvaluationRunConfig(model_spec="test-model", instructions="x")

        assert config.delivered_evidence_root is None
        assert config.withheld_refs == frozenset()

    async def test_an_unguarded_run_records_no_traces(self):
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite the page ids.")

        run = await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=_golden_dataset())

        assert len(run.report.cases) == 2
        assert run.traces == {}

    async def test_an_unguarded_run_builds_the_plain_toolset_and_the_plain_model(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        captured = _spy_on_build_agent(monkeypatch)
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite the page ids.")

        await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=_golden_dataset())

        assert len(captured) == 1
        assert not isinstance(captured[0][0], GuardedTracingToolset)

    async def test_a_guarded_run_files_one_trial_per_case_under_the_case_name(self, tmp_path: Path):
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", delivered_evidence_root=tmp_path)

        run = await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=_golden_dataset())

        assert set(run.traces) == set(_CASE_NAMES)
        assert len(run.report.cases) == 2

    async def test_two_cases_asking_the_same_question_stay_two_trials(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # Identity is the case, not the text it asks. Keying on the question made the second case
        # collide with the first one's already-stored evidence and fail outright.
        _use_tool_calling_model(monkeypatch)
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", delivered_evidence_root=tmp_path)

        run = await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=_duplicate_question_dataset())

        assert len(run.traces) == 2
        assert len(list((tmp_path / "delivered-evidence").iterdir())) == 2
        assert [failure.name for failure in run.report.failures] == []

    async def test_a_case_with_no_name_is_refused_rather_than_named_from_its_question(self, tmp_path: Path):
        unnamed = EvaluationDataset(name="unnamed", cases=[Case(inputs=QaInput(question="What is LightRAG?"))])
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", delivered_evidence_root=tmp_path)

        run = await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=unnamed)

        assert [failure.error_message for failure in run.report.failures] == [
            f"{UnnamedCaseError.__name__}: a guarded evaluation trial needs its case's name, and this case has none"
        ]
        assert not (tmp_path / "delivered-evidence").exists()

    async def test_code_mode_cannot_run_under_delivered_evidence(self, tmp_path: Path):
        # Code mode collapses the tools into one sandboxed program, so the model reads that program's
        # output rather than the tool results the guard stores.
        with pytest.raises(ValueError, match="code mode cannot run under delivered evidence"):
            EvaluationRunConfig(model_spec="test-model", instructions="Cite.", code_mode=True, delivered_evidence_root=tmp_path)

    async def test_code_mode_without_a_state_root_is_still_allowed(self):
        assert EvaluationRunConfig(model_spec="test-model", instructions="Cite.", code_mode=True).code_mode is True

    async def test_repeat_cannot_run_under_delivered_evidence(self, tmp_path: Path):
        # pydantic-evals gives every repetition of a case the same one case name, and a trial is
        # identified by that name, so a guarded run could not tell two repetitions of one case apart.
        with pytest.raises(ValueError, match="a guarded run cannot repeat a case"):
            EvaluationRunConfig(model_spec="test-model", instructions="Cite.", repeat=2, delivered_evidence_root=tmp_path)

    async def test_repeat_without_a_state_root_is_still_allowed(self):
        assert EvaluationRunConfig(model_spec="test-model", instructions="Cite.", repeat=3).repeat == 3

    async def test_a_failed_trial_still_files_the_trace_it_recorded(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # The trace of a trial that died is the one a reader most wants, exposure flags included.
        _fail_after_the_first_tool_call(monkeypatch)
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", delivered_evidence_root=tmp_path)

        run = await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=_golden_dataset())

        assert run.report.failures
        assert set(run.traces) == set(_CASE_NAMES)
        for trace in run.traces.values():
            assert [call.tool_name for call in trace.tool_calls] == ["search"]

    async def test_each_trial_records_its_own_model_turns(self, tmp_path: Path):
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", delivered_evidence_root=tmp_path)

        run = await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=_golden_dataset())

        for trace in run.traces.values():
            assert trace.model_turns
            assert trace.model_turns[0].cost_usd is None  # the test model has no known price
            assert trace.run_duration_ms >= 0

    async def test_a_guarded_run_wraps_the_toolset_and_the_model_per_trial(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        captured = _spy_on_built_agents(monkeypatch)
        config = EvaluationRunConfig(
            model_spec="test-model", instructions="Cite.", delivered_evidence_root=tmp_path, withheld_refs=frozenset({"page:research/x"})
        )

        await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=_golden_dataset())

        assert len(captured) == 2  # one agent per trial, not one shared agent
        for model, toolset in captured:
            assert isinstance(model, TracingModel)
            assert isinstance(toolset, GuardedTracingToolset)
            assert toolset.withheld.refs == frozenset({"page:research/x"})
            assert toolset.withheld.collection == "research"

    async def test_each_trial_stores_its_evidence_under_its_own_directory(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # `TestModel` calling `search` drives one real tool call per trial through the guard, so each
        # trial's answer is stored before the model reads it.
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", delivered_evidence_root=tmp_path)
        _use_tool_calling_model(monkeypatch)

        run = await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=_golden_dataset())

        assert len(list((tmp_path / "delivered-evidence").iterdir())) == 2
        for trace in run.traces.values():
            assert [call.tool_name for call in trace.tool_calls] == ["search"]
            assert trace.tool_calls[0].status == "ok"
            assert not trace.withheld_exposures

    async def test_run_eval_on_dataset_still_answers_with_the_report_alone(self, tmp_path: Path):
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", delivered_evidence_root=tmp_path)

        report = await run_eval_on_dataset(FakeContainer(), config=config, dataset=_golden_dataset())

        assert len(report.cases) == 2


# -- BlindTrialRunner --------------------------------------------------------


def _identity(tag: str) -> RunIdentity:
    """A run identity distinguishable only by the fingerprints `tag` seeds."""
    digest = content_fingerprint(tag)
    return RunIdentity(
        model="test-model",
        model_version="1",
        prompt_fingerprint=digest,
        implementation_fingerprint=digest,
        conditions_fingerprint=content_fingerprint("conditions"),
        grader_fingerprint=content_fingerprint("grader"),
    )


_CRITERION_TEXT = "hybrid retrieval beats keyword search"
_MODEL_STOPPED = "the model under test stopped answering"


@dataclass
class _Answering:
    """A scripted agent-under-test model, recording every request it received.

    Attributes:
        answer: The answer to emit.
        cited: The page ids to cite.
        first_tool: A tool to call once before answering, so a real tool result passes the guard.
        fail: Raise `UnexpectedModelBehavior` instead of answering.
        prompts: The user-prompt text of every request, in order.
    """

    answer: str = "Hybrid retrieval beats keyword search."
    cited: tuple[str, ...] = ("paper.lightrag-2025",)
    first_tool: str | None = None
    fail: bool = False
    prompts: list[str] = field(default_factory=list)

    def model(self) -> FunctionModel:
        """The `FunctionModel` running this script."""

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            turn = len(self.prompts)
            self.prompts.append(_prompt_text(messages))
            if self.fail:
                raise UnexpectedModelBehavior(_MODEL_STOPPED)
            if self.first_tool is not None and turn == 0:
                return ModelResponse(parts=[ToolCallPart(self.first_tool, {"query": "hybrid retrieval"})])
            payload = {"answer": self.answer, "cited_source_ids": list(self.cited)}
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, payload)])

        return FunctionModel(respond)


def _prompt_text(messages: list[ModelMessage]) -> str:
    """Every user-prompt string in `messages`, joined."""
    return "\n".join(
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    )


def _exploding_toolset() -> FunctionToolset[object]:
    """A toolset whose one tool dies, so a trial ends inside a tool call rather than in the model."""

    def explode(query: str) -> str:
        msg = "the tool came apart"
        raise RuntimeError(msg)

    toolset: FunctionToolset[object] = FunctionToolset()
    toolset.add_function(explode)
    return toolset


def _blind_runner(
    answering: _Answering,
    tmp_path: Path,
    traces: dict[str, ExecutionTrace],
    *,
    withheld: Mapping[str, frozenset[str]] | None = None,
) -> BlindTrialRunner:
    """The blind runner over the real retrieval toolset and a real file-backed evidence store."""
    return BlindTrialRunner(
        model=answering.model(),
        instructions="Cite the page ids.",
        toolset=build_retrieval_toolset(_handlers(), collection="research"),
        store=FileDeliveredEvidenceStore(tmp_path),
        collection="research",
        traces=traces,
        withheld=withheld or {},
    )


class TestBlindTrialRunner:
    """One case per trial, told its id and its input and nothing else."""

    def test_the_runner_s_input_carries_only_the_case_id_and_the_input(self):
        # The hidden criterion has no way through: the shape execution receives holds nothing else.
        assert set(EvaluationInput.model_fields) == {"case_id", "input"}

    async def test_the_request_is_the_case_input_and_names_no_criterion(self, tmp_path: Path):
        answering = _Answering()
        traces: dict[str, ExecutionTrace] = {}
        case = EvaluationInput(case_id="case.hybrid", input="Does hybrid retrieval beat keyword search?")

        await _blind_runner(answering, tmp_path, traces)(case, _identity("baseline"), 0)

        assert answering.prompts == ["Does hybrid retrieval beat keyword search?"]
        assert _CRITERION_TEXT not in answering.prompts[0]

    async def test_it_answers_with_the_output_the_citations_and_the_trace(self, tmp_path: Path):
        traces: dict[str, ExecutionTrace] = {}
        answering = _Answering(first_tool="search")
        case = EvaluationInput(case_id="case.hybrid", input="Does hybrid retrieval beat keyword search?")

        execution = await _blind_runner(answering, tmp_path, traces)(case, _identity("baseline"), 0)

        assert execution.output == "Hybrid retrieval beats keyword search."
        assert execution.cited_source_ids == ("paper.lightrag-2025",)
        assert execution.trace is not None
        assert [call.tool_name for call in execution.trace.tool_calls] == ["search"]
        assert execution.usage.requests == 2

    async def test_each_trial_runs_behind_its_own_guarded_toolset_carrying_that_case_s_withheld_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        captured = _spy_on_built_agents(monkeypatch)
        traces: dict[str, ExecutionTrace] = {}
        runner_under_test = _blind_runner(
            _Answering(), tmp_path, traces, withheld={"case.hybrid": frozenset({"page:research/paper.hidden"})}
        )

        await runner_under_test(EvaluationInput(case_id="case.hybrid", input="Ask."), _identity("baseline"), 0)
        await runner_under_test(EvaluationInput(case_id="case.other", input="Ask again."), _identity("baseline"), 0)

        toolsets = [toolset for _, toolset in captured]
        assert all(isinstance(toolset, GuardedTracingToolset) for toolset in toolsets)
        assert [toolset.withheld.refs for toolset in toolsets if isinstance(toolset, GuardedTracingToolset)] == [
            frozenset({"page:research/paper.hidden"}),
            frozenset(),
        ]

    async def test_every_trial_model_is_traced(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        captured = _spy_on_built_agents(monkeypatch)
        traces: dict[str, ExecutionTrace] = {}

        await _blind_runner(_Answering(), tmp_path, traces)(EvaluationInput(case_id="case.hybrid", input="Ask."), _identity("a"), 0)

        assert [isinstance(model, TracingModel) for model, _ in captured] == [True]

    async def test_a_case_a_repetition_and_an_identity_each_change_the_trial_id(self, tmp_path: Path):
        traces: dict[str, ExecutionTrace] = {}
        runner_under_test = _blind_runner(_Answering(), tmp_path, traces)
        case = EvaluationInput(case_id="case.hybrid", input="Ask.")

        await runner_under_test(case, _identity("baseline"), 0)
        await runner_under_test(case, _identity("baseline"), 1)
        await runner_under_test(case, _identity("candidate"), 0)
        await runner_under_test(EvaluationInput(case_id="case.other", input="Ask."), _identity("baseline"), 0)

        assert len(traces) == 4

    async def test_the_trial_id_carries_no_variant_name(self, tmp_path: Path):
        assert trial_id_for("case.hybrid", _identity("baseline"), 0).startswith("case.hybrid:0:")
        assert "baseline" not in trial_id_for("case.hybrid", _identity("baseline"), 0)

    async def test_a_failed_trial_raises_an_evaluation_error_and_still_files_its_trace(self, tmp_path: Path):
        traces: dict[str, ExecutionTrace] = {}
        runner_under_test = _blind_runner(_Answering(fail=True), tmp_path, traces)
        case = EvaluationInput(case_id="case.hybrid", input="Ask.")

        with pytest.raises(EvaluationError, match="stopped answering"):
            await runner_under_test(case, _identity("baseline"), 0)

        assert set(traces) == {trial_id_for("case.hybrid", _identity("baseline"), 0)}

    async def test_a_guarded_trial_stores_what_its_tools_delivered(self, tmp_path: Path):
        traces: dict[str, ExecutionTrace] = {}
        case = EvaluationInput(case_id="case.hybrid", input="Ask.")

        execution = await _blind_runner(_Answering(first_tool="search"), tmp_path, traces)(case, _identity("baseline"), 0)

        assert execution.trace is not None
        delivered = execution.trace.tool_calls[0].delivered_evidence
        assert len(delivered) == 1
        stored = await FileDeliveredEvidenceStore(tmp_path).read(delivered[0])
        assert stored.ref == delivered[0]

    async def test_the_execution_names_the_trial_its_evidence_is_filed_under(self, tmp_path: Path):
        traces: dict[str, ExecutionTrace] = {}
        case = EvaluationInput(case_id="case.hybrid", input="Ask.")

        execution = await _blind_runner(_Answering(), tmp_path, traces)(case, _identity("baseline"), 0)

        assert execution.trial_id == trial_id_for("case.hybrid", _identity("baseline"), 0)
        assert execution.trial_id in traces

    async def test_the_raw_model_failure_never_escapes_in_place_of_an_evaluation_error(self, tmp_path: Path):
        # The `TaskRunner` contract is that a recoverable failure arrives as `EvaluationError`, and
        # `Agent.iter` returns its own recorded failure from its block whatever the body raised.
        traces: dict[str, ExecutionTrace] = {}
        runner_under_test = _blind_runner(_Answering(fail=True), tmp_path, traces)

        # Anything other than `EvaluationError` reaching here, `UnexpectedModelBehavior` in
        # particular, propagates out of `pytest.raises` and fails this test outright.
        with pytest.raises(EvaluationError) as caught:
            await runner_under_test(EvaluationInput(case_id="case.hybrid", input="Ask."), _identity("baseline"), 0)

        assert str(caught.value) == _MODEL_STOPPED

    async def test_a_tool_that_raises_still_files_the_trace_and_its_failed_row(self, tmp_path: Path):
        # The row naming the call that died is the one an operator needs, so it cannot leave with it.
        traces: dict[str, ExecutionTrace] = {}
        runner_under_test = BlindTrialRunner(
            model=_Answering(first_tool="explode").model(),
            instructions="Cite the page ids.",
            toolset=_exploding_toolset(),
            store=FileDeliveredEvidenceStore(tmp_path),
            collection="research",
            traces=traces,
        )
        trial_id = trial_id_for("case.hybrid", _identity("baseline"), 0)

        with pytest.raises(RuntimeError, match="the tool came apart"):
            await runner_under_test(EvaluationInput(case_id="case.hybrid", input="Ask."), _identity("baseline"), 0)

        assert trial_id in traces
        assert [(call.tool_name, call.status) for call in traces[trial_id].tool_calls] == [("explode", "failed")]


# -- the SQL switch, the tool-call budget, answers, sources and the results file --------


def _answer_part(info: AgentInfo, answer: str, cited: Sequence[str]) -> ModelResponse:
    """The final answer, sent through the run's output tool."""
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"answer": answer, "cited_source_ids": list(cited)})])


def _tool_calls_made(messages: list[ModelMessage]) -> int:
    """How many tool calls the model has asked for so far in this run."""
    return sum(isinstance(part, ToolCallPart) for message in messages if isinstance(message, ModelResponse) for part in message.parts)


def _named_dataset(*cases: tuple[str, str, tuple[str, ...]]) -> EvaluationDataset:
    """A dataset of `(name, expected answer, gold ids)` cases, each asking `question <name>`."""
    return build_dataset(
        AuthoredCase(
            question=QaInput(question=f"question {name}"),
            expected=ExpectedQa(answer=answer, expected_source_ids=gold),
            meta=QaCaseMeta(),
            case_name=name,
        )
        for name, answer, gold in cases
    )


def _read_results(path: Path) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    """The results file's header and its case lines keyed by case name."""
    header, *lines = (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    return header["header"], {line["case"]: line for line in lines}


@dataclass
class _RefusingGateway(FakeSqlGateway):
    """Rejects its first `refusals` statements the way the read-only role rejects one, then answers like the plain fake."""

    refusals: int = 1
    refused: int = 0

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        if self.refused < self.refusals:
            self.refused += 1
            raise OperationalError(sql, {}, Exception("permission denied for table pages"))
        return await super().rows(sql, params, cap=cap)


def _sql_knowledge_base(gateway: FakeSqlGateway, *, retry_refusals: bool = True) -> SqlKnowledgeBase[object]:
    """The SQL tools over `gateway`, the way an evaluation run with the SQL switch receives them."""
    return SqlKnowledgeBase[object](
        gateway=gateway, describe_handler=DescribeSchemaHandler(gateway), domain="A knowledge base of notes.", retry_refusals=retry_refusals
    )


@dataclass
class _FakeHitSources:
    """A knowledge base whose pages are the keys of `sources`, recording every lookup it answers."""

    sources: Mapping[str, str]
    page_lookups: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    source_lookups: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)

    async def page_ids(self, collection: str, ids: Sequence[str]) -> frozenset[str]:
        self.page_lookups.append((collection, tuple(ids)))
        return frozenset(identifier for identifier in ids if identifier in self.sources)

    async def source_ids(self, collection: str, page_ids: Sequence[str]) -> Mapping[str, str]:
        self.source_lookups.append((collection, tuple(page_ids)))
        return {page_id: self.sources[page_id] for page_id in page_ids if page_id in self.sources}


class _UnreachableHitSources:
    """A page-to-source lookup whose database cannot be reached."""

    async def page_ids(self, collection: str, ids: Sequence[str]) -> frozenset[str]:
        msg = "the admin database is unreachable"
        raise RuntimeError(msg)

    async def source_ids(self, collection: str, page_ids: Sequence[str]) -> Mapping[str, str]:
        msg = "the admin database is unreachable"
        raise RuntimeError(msg)


class _ResetsLogfire:
    @pytest.fixture(autouse=True)
    def _reset_logfire(self) -> Callable[[], None]:
        """Clear the process-wide configured-once guard so the local-span path actually runs."""
        reset_logfire_for_testing()
        return reset_logfire_for_testing


class TestDeclaredFacets(_ResetsLogfire):
    """The run's `search` takes `facets` over the facet keys the knowledge base's schema declares."""

    @pytest.mark.parametrize("guarded", [False, True])
    async def test_the_search_names_and_accepts_the_schemas_facet_keys(self, tmp_path: Path, guarded: bool):
        spec = research_spec()
        keys = [facet.key for facet in spec.facets]
        descriptions: list[str] = []
        refusals: list[RetryPromptPart] = []

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            descriptions.extend(tool.description or "" for tool in info.function_tools if tool.name == "search")
            if _tool_calls_made(messages) == 0:
                return ModelResponse(parts=[ToolCallPart("search", {"query": "graph retrieval", "facets": {keys[0]: ["any"]}})])
            refusals.extend(part for message in messages if isinstance(message, ModelRequest) for part in message.parts if isinstance(part, RetryPromptPart))
            return _answer_part(info, "yes", [])

        config = EvaluationRunConfig(
            model_spec="test-model", instructions="Cite.", delivered_evidence_root=tmp_path / "evidence" if guarded else None
        )

        await run_traced_eval_on_dataset(
            FakeContainer(spec=spec), config=config, dataset=_named_dataset(("case-a", "yes", ())), model=FunctionModel(respond)
        )

        assert keys
        assert descriptions
        assert all(f"`{key}`" in description for key in keys for description in descriptions)
        assert refusals == []


class TestToolCallBudget(_ResetsLogfire):
    @pytest.mark.parametrize("guarded", [False, True])
    async def test_a_case_past_the_budget_fails_and_is_written_as_incorrect(self, tmp_path: Path, guarded: bool):
        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if "LightRAG" in _prompt_text(messages) or _tool_calls_made(messages) == 0:
                return ModelResponse(parts=[ToolCallPart("search", {"query": "graph retrieval"})])
            return _answer_part(info, "A hot-passage embedding cache.", ["paper.vector-cache"])

        results = tmp_path / "results.jsonl"
        config = EvaluationRunConfig(
            model_spec="test-model",
            instructions="Cite.",
            max_tool_calls=2,
            results_path=results,
            delivered_evidence_root=tmp_path / "evidence" if guarded else None,
        )

        run = await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=_golden_dataset(), model=FunctionModel(respond))

        assert [failure.name for failure in run.report.failures] == ["paper.lightrag-2025-0"]
        assert "tool_calls_limit of 2" in run.report.failures[0].error_message
        _header, lines = _read_results(results)
        assert lines["paper.lightrag-2025-0"]["failed"] is True
        assert lines["paper.lightrag-2025-0"]["strict"] is False
        assert lines["paper.lightrag-2025-0"]["official"] is False
        assert "tool_calls_limit of 2" in str(lines["paper.lightrag-2025-0"]["error"])
        assert lines["paper.vector-cache-1"]["failed"] is False
        assert lines["paper.vector-cache-1"]["strict"] is True

    async def test_a_blind_trial_past_its_budget_raises_an_evaluation_error(self, tmp_path: Path):
        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[ToolCallPart("search", {"query": "hybrid retrieval"})])

        runner_under_test = BlindTrialRunner(
            model=FunctionModel(respond),
            instructions="Cite the page ids.",
            toolset=build_retrieval_toolset(_handlers(), collection="research"),
            store=FileDeliveredEvidenceStore(tmp_path),
            collection="research",
            traces={},
            max_tool_calls=2,
        )

        with pytest.raises(EvaluationError, match="tool_calls_limit of 2"):
            await runner_under_test(EvaluationInput(case_id="case.hybrid", input="Ask."), _identity("baseline"), 0)


class TestCodeModeToolCallBudget(_ResetsLogfire):
    async def test_the_calls_a_run_code_program_makes_do_not_count_against_the_budget(self):
        # pydantic-ai checks the budget before the model's next tool call, so the second run_code is where it would fail.
        gateway = FakeSqlGateway()
        programs = (
            "for view in ('api.cards', 'api.edges', 'api.claims'):\n    await run_sql(sql=f'SELECT id FROM {view}')",
            "len('done')",
        )

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            made = _tool_calls_made(messages)
            if made < len(programs):
                return ModelResponse(parts=[ToolCallPart("run_code", {"code": programs[made]})])
            return _answer_part(info, "yes", [])

        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", code_mode=True, sql=_sql_knowledge_base(gateway), max_tool_calls=2)

        run = await run_traced_eval_on_dataset(
            FakeContainer(), config=config, dataset=_named_dataset(("case-a", "yes", ())), model=FunctionModel(respond)
        )

        assert run.report.failures == []
        assert len(gateway.rows_calls) == 3

    async def test_a_case_whose_model_asks_for_more_run_code_calls_than_the_budget_still_fails(self):
        gateway = FakeSqlGateway()

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[ToolCallPart("run_code", {"code": "await run_sql(sql='SELECT id FROM api.cards')"})])

        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", code_mode=True, sql=_sql_knowledge_base(gateway), max_tool_calls=2)

        run = await run_traced_eval_on_dataset(
            FakeContainer(), config=config, dataset=_named_dataset(("case-a", "yes", ())), model=FunctionModel(respond)
        )

        assert [failure.name for failure in run.report.failures] == ["case-a"]
        assert "tool_calls_limit of 2 (tool_calls=3)" in run.report.failures[0].error_message
        assert len(gateway.rows_calls) == 2


class TestSqlSwitch(_ResetsLogfire):
    @pytest.mark.parametrize("refusals", [1, 2])
    async def test_refused_statements_go_back_to_the_model_as_retries_and_the_case_finishes(self, refusals: int):
        gateway = _RefusingGateway(refusals=refusals)
        retries: list[str] = []

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if _tool_calls_made(messages) <= refusals:
                return ModelResponse(parts=[ToolCallPart("run_sql", {"sql": "SELECT id FROM api.cards"})])
            retries.extend(
                str(part.content)
                for message in messages
                if isinstance(message, ModelRequest)
                for part in message.parts
                if isinstance(part, RetryPromptPart)
            )
            return _answer_part(info, "A graph-based RAG system.", ["paper.lightrag-2025"])

        dataset = _named_dataset(("case-a", "A graph-based RAG system.", ("paper.lightrag-2025",)))
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", sql=_sql_knowledge_base(gateway))

        run = await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=dataset, model=FunctionModel(respond))

        assert run.report.failures == []
        assert gateway.refused == refusals
        assert len(gateway.rows_calls) == 1
        assert len(retries) == refusals
        assert all("permission denied for table pages" in retry for retry in retries)

    async def test_sql_tools_cannot_run_under_delivered_evidence(self, tmp_path: Path):
        with pytest.raises(ValueError, match="the SQL tools cannot run under delivered evidence"):
            EvaluationRunConfig(model_spec="test-model", instructions="Cite.", sql=_sql_knowledge_base(FakeSqlGateway()), delivered_evidence_root=tmp_path)

    @pytest.mark.parametrize("column", ["collection", "src_collection", "dst_collection", "subject_collection"])
    async def test_a_case_whose_rows_name_another_collection_is_flagged(self, tmp_path: Path, column: str):
        gateway = FakeSqlGateway(
            routed={
                "api.edges": SqlResult(rows=({"id": "note.alpha", column: "other-notes"},), columns=("id", column)),
                "api.cards": SqlResult(rows=({"id": "note.beta", column: "research"},), columns=("id", column)),
            }
        )
        statements = {"question case-a": "SELECT * FROM api.edges", "question case-b": "SELECT * FROM api.cards"}

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if _tool_calls_made(messages) == 0:
                return ModelResponse(parts=[ToolCallPart("run_sql", {"sql": statements[_prompt_text(messages)]})])
            return _answer_part(info, "yes", [])

        results = tmp_path / "results.jsonl"
        dataset = _named_dataset(("case-a", "yes", ()), ("case-b", "yes", ()))
        config = EvaluationRunConfig(
            model_spec="test-model", instructions="Cite.", sql=_sql_knowledge_base(gateway, retry_refusals=False), results_path=results
        )

        await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=dataset, model=FunctionModel(respond))

        _header, lines = _read_results(results)
        assert lines["case-a"]["other_collection"] is True
        assert lines["case-b"]["other_collection"] is False


    @pytest.mark.parametrize(
        "statement",
        ["SELECT * FROM api.about", "SELECT * FROM api.authoring_rules", "SELECT * FROM api.page_types", "SELECT * FROM API.Schema_Guide"],
    )
    async def test_rows_from_a_view_that_lists_every_knowledge_base_do_not_flag_the_case(self, tmp_path: Path, statement: str):
        rows = SqlResult(rows=({"collection": "other-notes", "body": "Another knowledge base."},), columns=("collection", "body"))
        lines = await self._flags(tmp_path, FakeSqlGateway(result=rows), statement)

        assert lines["case-a"]["other_collection"] is False

    async def test_a_statement_that_joins_such_a_view_with_a_content_view_still_flags_the_case(self, tmp_path: Path):
        rows = SqlResult(rows=({"collection": "other-notes", "src_id": "note.alpha"},), columns=("collection", "src_id"))
        statement = "SELECT a.collection, e.src_id FROM api.about a JOIN api.edges e ON e.src_collection = a.collection"

        lines = await self._flags(tmp_path, FakeSqlGateway(routed={"api.about": rows}), statement)

        assert lines["case-a"]["other_collection"] is True

    @pytest.mark.parametrize(
        "statement",
        [
            "SELECT * FROM api.facets",
            "SELECT * FROM api.raw_chunks",
            "SELECT * FROM api.sections",
            "SELECT * FROM api.claims",
            "SELECT * FROM api.search('graph')",
            'SELECT * FROM "api"."cards"',
        ],
    )
    async def test_rows_from_a_content_view_naming_another_knowledge_base_flag_the_case(self, tmp_path: Path, statement: str):
        rows = SqlResult(rows=({"collection": "other-notes", "id": "note.alpha"},), columns=("collection", "id"))
        lines = await self._flags(tmp_path, FakeSqlGateway(result=rows), statement)

        assert lines["case-a"]["other_collection"] is True

    async def _flags(self, tmp_path: Path, gateway: FakeSqlGateway, statement: str) -> dict[str, dict[str, object]]:
        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if _tool_calls_made(messages) == 0:
                return ModelResponse(parts=[ToolCallPart("run_sql", {"sql": statement})])
            return _answer_part(info, "yes", [])

        results = tmp_path / "results.jsonl"
        config = EvaluationRunConfig(
            model_spec="test-model", instructions="Cite.", sql=_sql_knowledge_base(gateway, retry_refusals=False), results_path=results
        )

        await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=_named_dataset(("case-a", "yes", ())), model=FunctionModel(respond))

        _header, lines = _read_results(results)
        return lines


def _tool_returns(messages: list[ModelMessage]) -> list[object]:
    """The content of every tool return in `messages`, in order."""
    return [
        part.content for message in messages if isinstance(message, ModelRequest) for part in message.parts if isinstance(part, ToolReturnPart)
    ]


class TestCodeModeWithSql(_ResetsLogfire):
    async def test_the_sandbox_calls_run_sql_and_the_rows_it_reads_still_flag_the_case(self, tmp_path: Path):
        gateway = FakeSqlGateway(
            routed={
                "api.edges": SqlResult(rows=({"id": "note.alpha", "collection": "other-notes"},), columns=("id", "collection")),
                "api.cards": SqlResult(rows=({"id": "note.beta", "collection": "research"},), columns=("id", "collection")),
            }
        )
        statements = {"question case-a": "SELECT * FROM api.edges", "question case-b": "SELECT * FROM api.cards"}
        offered: list[list[str]] = []
        returned: list[object] = []

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            offered.append([tool.name for tool in info.function_tools])
            if _tool_calls_made(messages) == 0:
                code = f"rows = await run_sql(sql={statements[_prompt_text(messages)]!r})\nrows['rows'][0]['id']"
                return ModelResponse(parts=[ToolCallPart("run_code", {"code": code})])
            returned.extend(_tool_returns(messages))
            return _answer_part(info, "yes", [])

        results = tmp_path / "results.jsonl"
        dataset = _named_dataset(("case-a", "yes", ()), ("case-b", "yes", ()))
        config = EvaluationRunConfig(
            model_spec="test-model", instructions="Cite.", code_mode=True, sql=_sql_knowledge_base(gateway), results_path=results
        )

        run = await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=dataset, model=FunctionModel(respond))

        assert run.report.failures == []
        assert all(names == ["run_code"] for names in offered)
        assert sorted(str(content) for content in returned) == ["note.alpha", "note.beta"]
        assert len(gateway.rows_calls) == 2
        header, lines = _read_results(results)
        assert (header["sql"], header["code_mode"]) == (True, True)
        assert lines["case-a"]["other_collection"] is True
        assert lines["case-b"]["other_collection"] is False

    async def test_a_refused_statement_inside_run_code_comes_back_as_a_retry_and_the_case_finishes(self):
        gateway = _RefusingGateway(refusals=1)
        retries: list[str] = []

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if _tool_calls_made(messages) < 2:
                return ModelResponse(parts=[ToolCallPart("run_code", {"code": "await run_sql(sql='SELECT id FROM api.cards')"})])
            retries.extend(
                str(part.content)
                for message in messages
                if isinstance(message, ModelRequest)
                for part in message.parts
                if isinstance(part, RetryPromptPart)
            )
            return _answer_part(info, "yes", [])

        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", code_mode=True, sql=_sql_knowledge_base(gateway))

        run = await run_traced_eval_on_dataset(
            FakeContainer(), config=config, dataset=_named_dataset(("case-a", "yes", ())), model=FunctionModel(respond)
        )

        assert run.report.failures == []
        assert gateway.refused == 1
        assert len(gateway.rows_calls) == 1
        assert len(retries) == 1
        assert "permission denied for table pages" in retries[0]

    async def test_the_sandbox_reads_the_schema_through_describe_schema(self):
        gateway = FakeSqlGateway(
            schema=SchemaDescription(objects=(ObjectInfo(name="api.cards", kind="view", comment="One card per page.", columns=()),))
        )
        returned: list[object] = []

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if _tool_calls_made(messages) == 0:
                return ModelResponse(parts=[ToolCallPart("run_code", {"code": "await describe_schema()"})])
            returned.extend(_tool_returns(messages))
            return _answer_part(info, "yes", [])

        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", code_mode=True, sql=_sql_knowledge_base(gateway))

        run = await run_traced_eval_on_dataset(
            FakeContainer(), config=config, dataset=_named_dataset(("case-a", "yes", ())), model=FunctionModel(respond)
        )

        assert run.report.failures == []
        assert len(returned) == 1
        assert "- api.cards (view): One card per page." in str(returned[0])
        assert gateway.api_objects_calls == 1


class TestPinnedSearch(_ResetsLogfire):
    @pytest.mark.parametrize(("pinned", "offered"), [(True, False), (False, True)])
    async def test_a_pinned_run_offers_search_with_its_date_range_and_without_a_collection(self, pinned: bool, offered: bool):
        properties: list[set[str]] = []

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            search = next(tool for tool in info.function_tools if tool.name == "search")
            properties.append(set(search.parameters_json_schema.get("properties", {})))
            return _answer_part(info, "yes", [])

        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", pinned_search=pinned)

        await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=_golden_dataset(), model=FunctionModel(respond))

        assert properties
        assert all({"since", "until"} <= names for names in properties)
        assert all(("collection" in names) is offered for names in properties)


class TestSourceLevelRetrieval(_ResetsLogfire):
    async def test_cited_ids_map_to_sources_in_one_lookup_and_score_against_the_gold_sources(self, tmp_path: Path):
        hit_sources = _FakeHitSources(sources={"note.alpha": "src-alpha", "note.beta": "src-beta"})
        citations = {"question case-a": ["note.alpha", "note.beta#^c2", "src-gamma:4"], "question case-b": ["note.alpha"]}

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return _answer_part(info, "yes", citations[_prompt_text(messages)])

        results = tmp_path / "results.jsonl"
        dataset = _named_dataset(("case-a", "yes", ("src-beta", "src-delta")), ("case-b", "yes", ("src-alpha",)))
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", hit_sources=hit_sources, results_path=results)

        await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=dataset, model=FunctionModel(respond))

        _header, lines = _read_results(results)
        assert lines["case-a"]["sources"] == ["src-alpha", "src-beta", "src-gamma"]
        assert (lines["case-a"]["recall@4"], lines["case-a"]["recall@10"], lines["case-a"]["mrr"]) == (0.5, 0.5, 0.5)
        assert lines["case-b"]["sources"] == ["src-alpha"]
        assert (lines["case-b"]["recall@4"], lines["case-b"]["recall@10"], lines["case-b"]["mrr"]) == (1.0, 1.0, 1.0)
        assert [collection for collection, _ids in hit_sources.page_lookups] == ["research"]
        assert [collection for collection, _ids in hit_sources.source_lookups] == ["research"]


# -- seen recall: which gold sources the tool results the agent received named ----------


def _handlers_over(gateway: FakeSqlGateway) -> RetrievalHandlers:
    """The real retrieval handlers over `gateway`, so a scripted route answers the search or the fetch."""
    return RetrievalHandlers(
        search=SearchHandler(gateway, FakeEmbedding()),
        fetch=FetchHandler(gateway, "research"),
        graph_expand=GraphExpandHandler(gateway, "research"),
        find_paths=GraphPathsHandler(gateway, "research"),
    )


def _search_hits(*hits: tuple[str, str, str | None]) -> SqlResult:
    """The `api.search` rows for `(id, hit level, page id)` hits, in rank order."""
    return SqlResult(
        rows=tuple({"id": hit_id, "hit_level": level, "page_id": page_id, "score": 1.0 / rank} for rank, (hit_id, level, page_id) in enumerate(hits, start=1)),
        columns=("id", "hit_level", "page_id", "score"),
    )


def _calls_then_answers(*calls: ToolCallPart, cited: Sequence[str] = ()) -> FunctionModel:
    """A model that asks for `calls` one turn each, then answers `yes` citing `cited`."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        made = _tool_calls_made(messages)
        if made < len(calls):
            return ModelResponse(parts=[calls[made]])
        return _answer_part(info, "yes", cited)

    return FunctionModel(respond)


_SEEN_SOURCES = {"note.alpha": "src-alpha", "note.beta": "src-beta"}


class TestSeenRecall(_ResetsLogfire):
    async def test_a_case_whose_search_returned_a_gold_unit_but_that_cites_nothing_has_seen_recall_one_and_cited_recall_zero(self, tmp_path: Path):
        gateway = FakeSqlGateway(routed={"api.search": _search_hits(("note.alpha#s1", "section", "note.alpha"))})
        results = tmp_path / "results.jsonl"
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", hit_sources=_FakeHitSources(sources=_SEEN_SOURCES), results_path=results)

        await run_traced_eval_on_dataset(
            FakeContainer(handlers=_handlers_over(gateway)),
            config=config,
            dataset=_named_dataset(("case-a", "yes", ("src-alpha",))),
            model=_calls_then_answers(ToolCallPart("search", {"query": "alpha"})),
        )

        _header, lines = _read_results(results)
        assert (lines["case-a"]["seen_recall"], lines["case-a"]["seen_gold"]) == (1.0, ["src-alpha"])
        assert (lines["case-a"]["recall@4"], lines["case-a"]["recall@10"], lines["case-a"]["mrr"]) == (0.0, 0.0, 0.0)

    async def test_a_case_that_never_receives_a_gold_unit_has_seen_recall_zero(self, tmp_path: Path):
        gateway = FakeSqlGateway(routed={"api.search": _search_hits(("note.beta#s1", "section", "note.beta"), ("src-gamma:4", "raw", None))})
        results = tmp_path / "results.jsonl"
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", hit_sources=_FakeHitSources(sources=_SEEN_SOURCES), results_path=results)

        await run_traced_eval_on_dataset(
            FakeContainer(handlers=_handlers_over(gateway)),
            config=config,
            dataset=_named_dataset(("case-a", "yes", ("src-alpha",))),
            model=_calls_then_answers(ToolCallPart("search", {"query": "alpha"})),
        )

        _header, lines = _read_results(results)
        assert (lines["case-a"]["seen_recall"], lines["case-a"]["seen_gold"]) == (0.0, [])

    async def test_sql_rows_count_through_their_id_columns(self, tmp_path: Path):
        rows = SqlResult(rows=({"source_id": "src-alpha", "title": "src-gamma"}, {"page_id": "note.beta"}), columns=("source_id", "title", "page_id"))
        gateway = FakeSqlGateway(routed={"api.raw_chunks": rows})
        results = tmp_path / "results.jsonl"
        config = EvaluationRunConfig(
            model_spec="test-model",
            instructions="Cite.",
            sql=_sql_knowledge_base(gateway),
            hit_sources=_FakeHitSources(sources=_SEEN_SOURCES),
            results_path=results,
        )

        await run_traced_eval_on_dataset(
            FakeContainer(),
            config=config,
            dataset=_named_dataset(("case-a", "yes", ("src-alpha", "src-beta", "src-gamma"))),
            model=_calls_then_answers(ToolCallPart("run_sql", {"sql": "SELECT source_id, title FROM api.raw_chunks"})),
        )

        _header, lines = _read_results(results)
        assert lines["case-a"]["seen_gold"] == ["src-alpha", "src-beta"]
        assert lines["case-a"]["seen_recall"] == pytest.approx(2 / 3)

    async def test_sql_rows_that_name_a_gold_source_only_as_a_relation_end_do_not_count(self, tmp_path: Path):
        edges = SqlResult(
            rows=({"src_id": "note.alpha", "rel": "cites", "dst_id": "note.beta", "subject_id": "note.alpha"},),
            columns=("src_id", "rel", "dst_id", "subject_id"),
        )
        gateway = FakeSqlGateway(routed={"api.edges": edges})
        results = tmp_path / "results.jsonl"
        config = EvaluationRunConfig(
            model_spec="test-model",
            instructions="Cite.",
            sql=_sql_knowledge_base(gateway),
            hit_sources=_FakeHitSources(sources=_SEEN_SOURCES),
            results_path=results,
        )

        await run_traced_eval_on_dataset(
            FakeContainer(),
            config=config,
            dataset=_named_dataset(("case-a", "yes", ("src-alpha",))),
            model=_calls_then_answers(ToolCallPart("run_sql", {"sql": "SELECT src_id, rel, dst_id, subject_id FROM api.edges"})),
        )

        _header, lines = _read_results(results)
        assert (lines["case-a"]["seen_recall"], lines["case-a"]["seen_gold"]) == (0.0, [])

    async def test_a_fetched_unit_counts(self, tmp_path: Path):
        fetched = SqlResult(rows=({"id": "src-alpha:3", "level": "raw", "title": "Alpha", "body": "The chunk."},), columns=("id", "level", "title", "body"))
        gateway = FakeSqlGateway(routed={"api.fetch": fetched})
        results = tmp_path / "results.jsonl"
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", hit_sources=_FakeHitSources(sources=_SEEN_SOURCES), results_path=results)

        await run_traced_eval_on_dataset(
            FakeContainer(handlers=_handlers_over(gateway)),
            config=config,
            dataset=_named_dataset(("case-a", "yes", ("src-alpha",))),
            model=_calls_then_answers(ToolCallPart("fetch", {"unit_id": "src-alpha:3"})),
        )

        _header, lines = _read_results(results)
        assert (lines["case-a"]["seen_recall"], lines["case-a"]["seen_gold"]) == (1.0, ["src-alpha"])

    async def test_a_case_that_fails_keeps_what_its_tool_results_named_before_it_failed(self, tmp_path: Path):
        gateway = FakeSqlGateway(routed={"api.search": _search_hits(("note.alpha", "card", None))})
        results = tmp_path / "results.jsonl"
        config = EvaluationRunConfig(
            model_spec="test-model", instructions="Cite.", max_tool_calls=1, hit_sources=_FakeHitSources(sources=_SEEN_SOURCES), results_path=results
        )

        run = await run_traced_eval_on_dataset(
            FakeContainer(handlers=_handlers_over(gateway)),
            config=config,
            dataset=_named_dataset(("case-a", "yes", ("src-alpha",))),
            model=_calls_then_answers(ToolCallPart("search", {"query": "alpha"}), ToolCallPart("search", {"query": "again"})),
        )

        assert [failure.name for failure in run.report.failures] == ["case-a"]
        _header, lines = _read_results(results)
        assert (lines["case-a"]["failed"], lines["case-a"]["seen_recall"], lines["case-a"]["recall@4"]) == (True, 1.0, 0.0)

    async def test_in_code_mode_what_the_program_s_own_tool_calls_returned_counts_whatever_it_prints(self, tmp_path: Path):
        gateway = FakeSqlGateway(routed={"api.search": _search_hits(("note.alpha#s1", "section", "note.alpha"))})
        programs = {
            "question case-a": "found = await search(query='alpha')\nlen(found['hits'])",
            "question case-b": "found = await search(query='alpha')\nfound['hits'][0]['id']",
        }

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if _tool_calls_made(messages) == 0:
                return ModelResponse(parts=[ToolCallPart("run_code", {"code": programs[_prompt_text(messages)]})])
            return _answer_part(info, "yes", [])

        results = tmp_path / "results.jsonl"
        config = EvaluationRunConfig(
            model_spec="test-model", instructions="Cite.", code_mode=True, hit_sources=_FakeHitSources(sources=_SEEN_SOURCES), results_path=results
        )

        await run_traced_eval_on_dataset(
            FakeContainer(handlers=_handlers_over(gateway)),
            config=config,
            dataset=_named_dataset(("case-a", "yes", ("src-alpha",)), ("case-b", "yes", ("src-alpha",))),
            model=FunctionModel(respond),
        )

        _header, lines = _read_results(results)
        assert lines["case-a"]["seen_recall"] == 1.0
        assert lines["case-b"]["seen_recall"] == 1.0

    async def test_in_code_mode_a_gold_id_that_a_fetched_body_links_to_is_not_seen(self, tmp_path: Path):
        fetched = SqlResult(
            rows=({"id": "note.beta", "level": "card", "title": "Beta", "body": "Builds on [[note.alpha]]."},), columns=("id", "level", "title", "body")
        )
        gateway = FakeSqlGateway(routed={"api.fetch": fetched})
        results = tmp_path / "results.jsonl"
        config = EvaluationRunConfig(
            model_spec="test-model", instructions="Cite.", code_mode=True, hit_sources=_FakeHitSources(sources=_SEEN_SOURCES), results_path=results
        )

        await run_traced_eval_on_dataset(
            FakeContainer(handlers=_handlers_over(gateway)),
            config=config,
            dataset=_named_dataset(("case-a", "yes", ("src-alpha", "src-beta"))),
            model=_calls_then_answers(ToolCallPart("run_code", {"code": "unit = await fetch(unit_id='note.beta')\nunit['body']"})),
        )

        _header, lines = _read_results(results)
        assert (lines["case-a"]["seen_gold"], lines["case-a"]["seen_recall"]) == (["src-beta"], 0.5)

    async def test_a_program_result_keyed_by_a_number_leaves_an_answered_case_answered_and_a_transient_failure_transient(self, tmp_path: Path):
        gateway = FakeSqlGateway(routed={"api.search": _search_hits(("note.alpha#s1", "section", "note.alpha"))})
        program = "found = await search(query='alpha')\n{1: found['hits'][0]['id']}"
        dropped = ModelAPIError("deployment-a", "Connection error.")

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if _tool_calls_made(messages) == 0:
                return ModelResponse(parts=[ToolCallPart("run_code", {"code": program})])
            if _prompt_text(messages) == "question case-b":
                raise dropped
            return _answer_part(info, "yes", [])

        results = tmp_path / "results.jsonl"
        config = EvaluationRunConfig(
            model_spec="test-model", instructions="Cite.", code_mode=True, hit_sources=_FakeHitSources(sources=_SEEN_SOURCES), results_path=results
        )

        run = await run_traced_eval_on_dataset(
            FakeContainer(handlers=_handlers_over(gateway)),
            config=config,
            dataset=_named_dataset(("case-a", "yes", ("src-alpha",)), ("case-b", "yes", ("src-alpha",))),
            model=FunctionModel(respond),
        )

        assert [case.name for case in run.report.cases] == ["case-a"]
        _header, lines = _read_results(results)
        assert (lines["case-a"]["failed"], lines["case-a"]["seen_recall"]) == (False, 1.0)
        assert (lines["case-b"]["failed"], lines["case-b"]["transient"], lines["case-b"]["error"]) == (True, True, "ModelAPIError: Connection error.")

    async def test_the_seen_units_map_to_sources_in_the_one_lookup_the_citations_use(self, tmp_path: Path):
        gateway = FakeSqlGateway(routed={"api.search": _search_hits(("note.beta#s1", "section", "note.beta"))})
        hit_sources = _FakeHitSources(sources=_SEEN_SOURCES)
        results = tmp_path / "results.jsonl"
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", hit_sources=hit_sources, results_path=results)

        await run_traced_eval_on_dataset(
            FakeContainer(handlers=_handlers_over(gateway)),
            config=config,
            dataset=_named_dataset(("case-a", "yes", ("src-alpha", "src-beta"))),
            model=_calls_then_answers(ToolCallPart("search", {"query": "beta"}), cited=["note.alpha"]),
        )

        _header, lines = _read_results(results)
        assert (lines["case-a"]["sources"], lines["case-a"]["seen_gold"]) == (["src-alpha"], ["src-beta"])
        assert [set(ids) for _collection, ids in hit_sources.page_lookups] == [{"note.alpha", "note.beta"}]
        assert len(hit_sources.source_lookups) == 1

    async def test_a_run_that_maps_no_sources_has_no_seen_recall(self, tmp_path: Path):
        gateway = FakeSqlGateway(routed={"api.search": _search_hits(("note.alpha", "card", None))})
        results = tmp_path / "results.jsonl"
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", results_path=results)

        await run_traced_eval_on_dataset(
            FakeContainer(handlers=_handlers_over(gateway)),
            config=config,
            dataset=_named_dataset(("case-a", "yes", ("src-alpha",))),
            model=_calls_then_answers(ToolCallPart("search", {"query": "alpha"})),
        )

        _header, lines = _read_results(results)
        assert (lines["case-a"]["seen_recall"], lines["case-a"]["seen_gold"]) == (None, None)


class TestAnswerScoring(_ResetsLogfire):
    async def test_a_dataset_with_expected_answers_is_scored_for_answers_and_left_unchanged(self):
        dataset = _golden_dataset()
        evaluators_before = list(dataset.evaluators)
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.")

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return _answer_part(info, "A graph-based RAG system.", [])

        run = await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=dataset, model=FunctionModel(respond))

        scores = {case.name: case.scores for case in run.report.cases}
        assert scores["paper.lightrag-2025-0"]["answer_strict"].value == 1.0
        assert scores["paper.lightrag-2025-0"]["answer_official"].value == 1.0
        assert scores["paper.vector-cache-1"]["answer_strict"].value == 0.0
        assert dataset.evaluators == evaluators_before
        assert not any(isinstance(evaluator, AnswerAccuracy) for evaluator in dataset.evaluators)

    async def test_a_dataset_that_already_scores_answers_is_scored_once(self):
        dataset = _golden_dataset()
        dataset.evaluators.append(AnswerAccuracy())
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.")

        run = await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=dataset)

        assert all(case.evaluator_failures == [] for case in run.report.cases)
        assert all("answer_strict" in case.scores for case in run.report.cases)
        assert all("answer_strict_2" not in case.scores for case in run.report.cases)
        assert sum(isinstance(evaluator, AnswerAccuracy) for evaluator in dataset.evaluators) == 1

    async def test_a_dataset_with_no_expected_answers_is_not_scored_for_answers(self):
        dataset = _named_dataset(("case-a", "", ("paper.lightrag-2025",)))
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.")

        run = await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=dataset)

        assert "answer_strict" not in run.report.cases[0].scores


class TestResultsFile(_ResetsLogfire):
    async def test_the_header_records_how_the_run_was_set_up(self, tmp_path: Path):
        results = tmp_path / "runs" / "results.jsonl"
        dataset = _golden_dataset()
        container = FakeContainer()
        config = EvaluationRunConfig(
            model_spec="test-model",
            instructions="Cite the page ids.",
            retrieval_strategy="search-hybrid",
            sql=_sql_knowledge_base(FakeSqlGateway()),
            max_tool_calls=6,
            pinned_search=True,
            thinking="xhigh",
            results_path=results,
        )

        await run_traced_eval_on_dataset(container, config=config, dataset=dataset)

        header, lines = _read_results(results)
        started = header.pop("started")
        assert header == {
            "strategy": "search-hybrid",
            "sql": True,
            "code_mode": False,
            "model": "test-model",
            "thinking": "xhigh",
            "instructions_sha256": hashlib.sha256(b"Cite the page ids.").hexdigest(),
            "max_tool_calls": 6,
            "pinned_search": True,
            "search_date_range": True,
            "dataset": dataset.name,
            "dataset_sha256": dataset_sha256(dataset),
            "collection": "research",
            "knowledge_base_fingerprint": "spec-hash:research",
            "index_build": "7+500-1000 test-embedding",
            "source_lookup_error": None,
        }
        assert datetime.fromisoformat(str(started)).tzinfo is not None
        assert set(lines) == set(_CASE_NAMES)
        assert container.inventory.opened == 1

    async def test_a_run_with_no_strategy_records_the_default_one(self, tmp_path: Path):
        results = tmp_path / "results.jsonl"
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", results_path=results)

        await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=_golden_dataset())

        header, _lines = _read_results(results)
        assert (header["strategy"], header["sql"], header["max_tool_calls"], header["pinned_search"]) == ("full", False, None, False)
        assert (header["thinking"], header["code_mode"]) == (None, False)

    async def test_the_run_builds_its_model_at_the_thinking_level_it_was_given(self):
        config = EvaluationRunConfig(model_spec="gpt-4o-eval", instructions="Cite.", thinking="high")

        with pytest.raises(ValueError, match="gpt-4o-eval"):
            await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=_golden_dataset())

    async def test_the_fingerprint_is_read_before_the_first_case_runs(self, tmp_path: Path):
        container = FakeContainer()
        opened_when_asked: list[int] = []

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            opened_when_asked.append(container.inventory.opened)
            return _answer_part(info, "yes", [])

        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", results_path=tmp_path / "results.jsonl")

        await run_traced_eval_on_dataset(container, config=config, dataset=_golden_dataset(), model=FunctionModel(respond))

        assert opened_when_asked
        assert set(opened_when_asked) == {1}

    async def test_a_failed_source_lookup_still_writes_the_file_and_names_the_error(self, tmp_path: Path):
        results = tmp_path / "results.jsonl"
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", hit_sources=_UnreachableHitSources(), results_path=results)

        run = await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=_golden_dataset())

        assert run.report.failures == []
        header, lines = _read_results(results)
        assert header["source_lookup_error"] == "RuntimeError: the admin database is unreachable"
        assert set(lines) == set(_CASE_NAMES)
        for line in lines.values():
            assert (line["sources"], line["recall@4"], line["recall@10"], line["mrr"]) == (None, None, None, None)

    async def test_a_run_with_no_results_file_reads_no_fingerprint(self):
        container = FakeContainer()

        await run_traced_eval_on_dataset(container, config=EvaluationRunConfig(model_spec="test-model", instructions="Cite."), dataset=_golden_dataset())

        assert InventoryReaderFactory not in container.requested

    async def test_a_results_file_cannot_hold_a_repeated_case(self, tmp_path: Path):
        with pytest.raises(ValueError, match="a results file cannot hold a repeated case"):
            EvaluationRunConfig(model_spec="test-model", instructions="Cite.", repeat=2, results_path=tmp_path / "results.jsonl")


# -- resuming a run from its results file ------------------------------------------


_RESUMED_CASES = (("case-a", "yes", ("note.alpha",)), ("case-b", "yes", ("note.beta",)), ("case-c", "yes", ("note.gamma",)))


def _failing(failures: Mapping[str, BaseException]) -> FunctionModel:
    """A model that raises the scripted error for the cases named in `failures` and answers `yes` for the rest."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        failure = failures.get(_prompt_text(messages))
        if failure is not None:
            raise failure
        return _answer_part(info, "yes", ["note.alpha"])

    return FunctionModel(respond)


@dataclass
class _AskedModel:
    """A model that answers `yes` to every case and records each question it was asked."""

    asked: list[str] = field(default_factory=list)

    def model(self) -> FunctionModel:
        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            self.asked.append(_prompt_text(messages))
            return _answer_part(info, "yes", ["note.beta"])

        return FunctionModel(respond)


def _drop_header_key(path: Path, key: str) -> None:
    """Rewrite the results file at `path` with `key` left out of its header, as a file written before the key was recorded."""
    header, *rows = (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    del header["header"][key]
    path.write_text("".join(f"{json.dumps(row)}\n" for row in [header, *rows]), encoding="utf-8")


def _case_rows(path: Path) -> list[dict[str, object]]:
    """The results file's case lines in file order."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()[1:]]


def _raw_case_lines(path: Path) -> dict[str, str]:
    """The results file's case lines exactly as written, keyed by case name."""
    return {json.loads(line)["case"]: line for line in path.read_text(encoding="utf-8").split("\n")[1:] if line}


class TestResume(_ResetsLogfire):
    async def test_a_resumed_run_runs_only_the_case_that_failed_on_a_connection_error_and_writes_every_case_once(self, tmp_path: Path):
        results = tmp_path / "results.jsonl"
        dataset = _named_dataset(*_RESUMED_CASES)
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", results_path=results)
        failures = {"question case-b": ModelAPIError("deployment-a", "Connection error.")}
        await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=dataset, model=_failing(failures))
        first_header_line = results.read_text(encoding="utf-8").split("\n")[0]
        first_raw = _raw_case_lines(results)
        asked = _AskedModel()

        run = await run_traced_eval_on_dataset(FakeContainer(), config=replace(config, resume=True), dataset=dataset, model=asked.model())

        assert asked.asked == ["question case-b"]
        assert [case.name for case in run.report.cases] == ["case-b"]
        assert [row["case"] for row in _case_rows(results)] == ["case-a", "case-b", "case-c"]
        assert results.read_text(encoding="utf-8").split("\n")[0] == first_header_line
        raw = _raw_case_lines(results)
        assert (raw["case-a"], raw["case-c"]) == (first_raw["case-a"], first_raw["case-c"])
        _header, lines = _read_results(results)
        assert (lines["case-b"]["failed"], lines["case-b"]["transient"], lines["case-b"]["strict"]) == (False, False, True)

    async def test_a_resume_reads_the_current_fingerprint_and_accepts_one_that_differs_from_the_header(self, tmp_path: Path):
        results = tmp_path / "results.jsonl"
        dataset = _named_dataset(*_RESUMED_CASES)
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", results_path=results)
        failures = {"question case-b": ModelAPIError("deployment-a", "Connection error.")}
        first = FakeContainer(inventory=_FakeInventoryReaders(reader=_FakeCoverageReader(spec_hash="index-one")))
        await run_traced_eval_on_dataset(first, config=config, dataset=dataset, model=_failing(failures))
        first_raw = _raw_case_lines(results)
        reindexed = FakeContainer(inventory=_FakeInventoryReaders(reader=_FakeCoverageReader(spec_hash="index-two")))
        asked = _AskedModel()

        run = await run_traced_eval_on_dataset(reindexed, config=replace(config, resume=True), dataset=dataset, model=asked.model())

        assert asked.asked == ["question case-b"]
        assert reindexed.inventory.opened == 1
        assert run.knowledge_base_fingerprint == "index-two:research"
        header, _lines = _read_results(results)
        assert header["knowledge_base_fingerprint"] == "index-one:research"
        raw = _raw_case_lines(results)
        assert (raw["case-a"], raw["case-c"]) == (first_raw["case-a"], first_raw["case-c"])

    @pytest.mark.parametrize("guarded", [False, True])
    async def test_a_connection_error_is_written_as_a_transient_failure(self, tmp_path: Path, guarded: bool):
        results = tmp_path / "results.jsonl"
        config = EvaluationRunConfig(
            model_spec="test-model",
            instructions="Cite.",
            results_path=results,
            delivered_evidence_root=tmp_path / "evidence" if guarded else None,
        )
        failures = {"question case-b": ModelAPIError("deployment-a", "Connection error.")}

        await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=_named_dataset(*_RESUMED_CASES), model=_failing(failures))

        _header, lines = _read_results(results)
        assert (lines["case-b"]["failed"], lines["case-b"]["transient"]) == (True, True)
        assert lines["case-b"]["error"] == "ModelAPIError: Connection error."
        assert (lines["case-a"]["failed"], lines["case-a"]["transient"]) == (False, False)

    @pytest.mark.parametrize(
        ("failure", "runs_again"),
        [
            (ModelAPIError("deployment-a", "Request timed out."), True),
            (ModelHTTPError(429, "deployment-a"), True),
            (ModelHTTPError(503, "deployment-a"), True),
            (ModelHTTPError(400, "deployment-a"), False),
            (UnexpectedModelBehavior("The model refused to answer."), False),
        ],
        ids=["timeout", "429", "503", "400", "refusal"],
    )
    async def test_only_a_transient_failure_runs_again(self, tmp_path: Path, failure: BaseException, runs_again: bool):
        results = tmp_path / "results.jsonl"
        dataset = _named_dataset(*_RESUMED_CASES)
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", results_path=results)
        await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=dataset, model=_failing({"question case-c": failure}))
        _header, first_lines = _read_results(results)
        asked = _AskedModel()

        await run_traced_eval_on_dataset(FakeContainer(), config=replace(config, resume=True), dataset=dataset, model=asked.model())

        _header, lines = _read_results(results)
        assert asked.asked == (["question case-c"] if runs_again else [])
        assert lines["case-c"]["failed"] is not runs_again
        if not runs_again:
            assert lines["case-c"] == first_lines["case-c"]

    async def test_a_case_past_its_tool_call_budget_is_kept(self, tmp_path: Path):
        def search_forever(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if _prompt_text(messages) == "question case-a":
                return ModelResponse(parts=[ToolCallPart("search", {"query": "notes"})])
            return _answer_part(info, "yes", ["note.alpha"])

        results = tmp_path / "results.jsonl"
        dataset = _named_dataset(*_RESUMED_CASES)
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", max_tool_calls=2, results_path=results)
        await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=dataset, model=FunctionModel(search_forever))
        asked = _AskedModel()

        await run_traced_eval_on_dataset(FakeContainer(), config=replace(config, resume=True), dataset=dataset, model=asked.model())

        _header, lines = _read_results(results)
        assert asked.asked == []
        assert (lines["case-a"]["failed"], lines["case-a"]["transient"]) == (True, False)
        assert "tool_calls_limit of 2" in str(lines["case-a"]["error"])

    async def test_a_results_file_from_other_settings_is_refused_naming_the_fields_before_the_model_is_called(self, tmp_path: Path):
        results = tmp_path / "results.jsonl"
        dataset = _named_dataset(*_RESUMED_CASES)
        first = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", max_tool_calls=6, results_path=results)
        await run_traced_eval_on_dataset(FakeContainer(), config=first, dataset=dataset)
        written = results.read_text(encoding="utf-8")
        asked = _AskedModel()
        resumed = replace(first, instructions="Cite the page ids.", max_tool_calls=8, resume=True)

        with pytest.raises(ResultsFileNotResumableError) as refused:
            await run_traced_eval_on_dataset(FakeContainer(), config=resumed, dataset=dataset, model=asked.model())

        message = str(refused.value)
        assert str(results) in message
        assert "max_tool_calls (6 in the file, 8 now)" in message
        assert "instructions_sha256 (" in message
        assert "model (" not in message
        assert asked.asked == []
        assert results.read_text(encoding="utf-8") == written

    async def test_a_results_file_from_another_index_build_is_refused_naming_index_build_before_the_model_is_called(self, tmp_path: Path):
        results = tmp_path / "results.jsonl"
        dataset = _named_dataset(*_RESUMED_CASES)
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", results_path=results)
        failures = {"question case-b": ModelAPIError("deployment-a", "Connection error.")}
        await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=dataset, model=_failing(failures))
        written = results.read_text(encoding="utf-8")
        rechunked = FakeContainer(inventory=_FakeInventoryReaders(reader=_FakeCoverageReader(build="7+300-600 test-embedding")))
        asked = _AskedModel()

        with pytest.raises(ResultsFileNotResumableError) as refused:
            await run_traced_eval_on_dataset(rechunked, config=replace(config, resume=True), dataset=dataset, model=asked.model())

        assert "index_build (7+500-1000 test-embedding in the file, 7+300-600 test-embedding now)" in str(refused.value)
        assert asked.asked == []
        assert results.read_text(encoding="utf-8") == written

    async def test_a_results_file_whose_header_did_not_record_the_index_build_is_refused_as_unknown(self, tmp_path: Path):
        results = tmp_path / "results.jsonl"
        dataset = _named_dataset(*_RESUMED_CASES)
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", results_path=results)
        failures = {"question case-b": ModelAPIError("deployment-a", "Connection error.")}
        await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=dataset, model=_failing(failures))
        _drop_header_key(results, "index_build")
        asked = _AskedModel()

        with pytest.raises(ResultsFileNotResumableError) as refused:
            await run_traced_eval_on_dataset(FakeContainer(), config=replace(config, resume=True), dataset=dataset, model=asked.model())

        assert "index_build (unknown in the file, 7+500-1000 test-embedding now)" in str(refused.value)
        assert asked.asked == []

    async def test_an_older_pinned_results_file_is_refused_naming_search_date_range(self, tmp_path: Path):
        results = tmp_path / "results.jsonl"
        dataset = _named_dataset(*_RESUMED_CASES)
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", pinned_search=True, results_path=results)
        failures = {"question case-b": ModelAPIError("deployment-a", "Connection error.")}
        await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=dataset, model=_failing(failures))
        _drop_header_key(results, "search_date_range")
        written = results.read_text(encoding="utf-8")
        asked = _AskedModel()

        with pytest.raises(ResultsFileNotResumableError) as refused:
            await run_traced_eval_on_dataset(FakeContainer(), config=replace(config, resume=True), dataset=dataset, model=asked.model())

        assert "search_date_range (False in the file, True now)" in str(refused.value)
        assert asked.asked == []
        assert results.read_text(encoding="utf-8") == written

    async def test_an_older_unpinned_results_file_resumes(self, tmp_path: Path):
        results = tmp_path / "results.jsonl"
        dataset = _named_dataset(*_RESUMED_CASES)
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", results_path=results)
        failures = {"question case-b": ModelAPIError("deployment-a", "Connection error.")}
        await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=dataset, model=_failing(failures))
        _drop_header_key(results, "search_date_range")
        asked = _AskedModel()

        await run_traced_eval_on_dataset(FakeContainer(), config=replace(config, resume=True), dataset=dataset, model=asked.model())

        assert asked.asked == ["question case-b"]
        assert [row["case"] for row in _case_rows(results)] == ["case-a", "case-b", "case-c"]

    async def test_a_results_file_of_another_dataset_is_refused(self, tmp_path: Path):
        results = tmp_path / "results.jsonl"
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", results_path=results)
        await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=_named_dataset(*_RESUMED_CASES))

        with pytest.raises(ResultsFileNotResumableError, match="dataset_sha256"):
            await run_traced_eval_on_dataset(FakeContainer(), config=replace(config, resume=True), dataset=_golden_dataset())

    async def test_an_older_results_file_without_the_transient_key_resumes_from_each_error_text(self, tmp_path: Path):
        results = tmp_path / "results.jsonl"
        dataset = _named_dataset(*_RESUMED_CASES)
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", results_path=results)
        failures = {"question case-b": ModelAPIError("deployment-a", "Connection error."), "question case-c": ModelHTTPError(400, "deployment-a")}
        await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=dataset, model=_failing(failures))
        header, *rows = (json.loads(line) for line in results.read_text(encoding="utf-8").splitlines())
        for key in ("code_mode", "thinking"):
            del header["header"][key]
        for row in rows:
            del row["transient"]
        results.write_text("".join(f"{json.dumps(row)}\n" for row in [header, *rows]), encoding="utf-8")
        asked = _AskedModel()

        await run_traced_eval_on_dataset(FakeContainer(), config=replace(config, resume=True), dataset=dataset, model=asked.model())

        _header, lines = _read_results(results)
        assert asked.asked == ["question case-b"]
        assert (lines["case-b"]["failed"], lines["case-b"]["transient"]) == (False, False)
        assert (lines["case-c"]["failed"], lines["case-c"]["transient"]) == (True, False)
        assert all("transient" in row for row in _case_rows(results))

    async def test_a_resumed_run_with_no_results_file_runs_every_case(self, tmp_path: Path):
        results = tmp_path / "results.jsonl"
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", results_path=results, resume=True)

        run = await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=_golden_dataset())

        assert len(run.report.cases) == 2
        header, lines = _read_results(results)
        assert set(lines) == set(_CASE_NAMES)
        assert header["knowledge_base_fingerprint"] == "spec-hash:research"

    async def test_a_results_file_with_nothing_to_run_again_is_kept_whole(self, tmp_path: Path):
        results = tmp_path / "results.jsonl"
        dataset = _named_dataset(*_RESUMED_CASES)
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", results_path=results)
        await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=dataset, model=_failing({}))
        written = results.read_text(encoding="utf-8")
        container = FakeContainer()
        asked = _AskedModel()

        run = await run_traced_eval_on_dataset(container, config=replace(config, resume=True), dataset=dataset, model=asked.model())

        assert asked.asked == []
        assert (run.report.cases, run.report.failures) == ([], [])
        assert results.read_text(encoding="utf-8") == written
        assert run.knowledge_base_fingerprint == "spec-hash:research"

    async def test_without_resuming_an_existing_results_file_is_replaced(self, tmp_path: Path):
        results = tmp_path / "results.jsonl"
        dataset = _named_dataset(*_RESUMED_CASES)
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", results_path=results)
        failures = {"question case-b": ModelAPIError("deployment-a", "Connection error.")}
        await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=dataset, model=_failing(failures))
        asked = _AskedModel()

        await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=dataset, model=asked.model())

        assert sorted(asked.asked) == ["question case-a", "question case-b", "question case-c"]
        assert [(row["case"], row["failed"]) for row in _case_rows(results)] == [("case-a", False), ("case-b", False), ("case-c", False)]

    async def test_a_source_lookup_that_fails_on_resume_leaves_the_header_and_the_kept_lines_as_they_were(self, tmp_path: Path):
        results = tmp_path / "results.jsonl"
        dataset = _named_dataset(*_RESUMED_CASES)
        hit_sources = _FakeHitSources(sources={"note.alpha": "src-alpha"})
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", hit_sources=hit_sources, results_path=results)
        failures = {"question case-b": ModelAPIError("deployment-a", "Connection error.")}
        await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=dataset, model=_failing(failures))
        first_header_line = results.read_text(encoding="utf-8").split("\n")[0]
        first_raw = _raw_case_lines(results)

        resumed = replace(config, hit_sources=_UnreachableHitSources(), resume=True)
        run = await run_traced_eval_on_dataset(FakeContainer(), config=resumed, dataset=dataset, model=_AskedModel().model())

        assert run.source_lookup_error == "RuntimeError: the admin database is unreachable"
        assert results.read_text(encoding="utf-8").split("\n")[0] == first_header_line
        raw = _raw_case_lines(results)
        assert (raw["case-a"], raw["case-c"]) == (first_raw["case-a"], first_raw["case-c"])
        assert json.loads(raw["case-a"])["sources"] == ["src-alpha"]
        run_now = json.loads(raw["case-b"])
        assert (run_now["failed"], run_now["sources"], run_now["recall@4"], run_now["recall@10"], run_now["mrr"]) == (False, None, None, None, None)
        asked = _AskedModel()

        await run_traced_eval_on_dataset(FakeContainer(), config=replace(resumed, hit_sources=hit_sources), dataset=dataset, model=asked.model())

        assert asked.asked == []

    async def test_an_unreadable_results_file_is_refused(self, tmp_path: Path):
        results = tmp_path / "results.jsonl"
        results.write_text("not a results file\n", encoding="utf-8")
        config = EvaluationRunConfig(model_spec="test-model", instructions="Cite.", results_path=results, resume=True)

        with pytest.raises(ResultsFileNotResumableError, match="line 1 of"):
            await run_traced_eval_on_dataset(FakeContainer(), config=config, dataset=_golden_dataset())

    def test_resuming_needs_a_results_file(self):
        with pytest.raises(ValueError, match="a resumed run continues a results file"):
            EvaluationRunConfig(model_spec="test-model", instructions="Cite.", resume=True)

    def test_resuming_cannot_run_under_delivered_evidence(self, tmp_path: Path):
        with pytest.raises(ValueError, match="a resumed run cannot run under delivered evidence"):
            EvaluationRunConfig(
                model_spec="test-model", instructions="Cite.", results_path=tmp_path / "r.jsonl", resume=True, delivered_evidence_root=tmp_path
            )
