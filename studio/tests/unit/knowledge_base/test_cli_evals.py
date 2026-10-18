r"""Unit tests for the `studio evals run` CLI command (`src/studio/knowledge_base/cli.py`).

Driven by :class:`typer.testing.CliRunner` with the container *factory* injected
through `ctx.obj` (no monkeypatching), exactly as
`test_cli_serve.py` does for search/fetch.

The factory builds the **real** wireup container (so the command's
`container.get(RetrievalHandlers)` — from which `run_eval` builds the per-run
toolset — flows through the real evals DI providers), then persistently overrides
the read-side dependencies the retrieval handlers sit on —
:class:`~studio.knowledge_base.wiring.serve.ReaderSqlGateway`→:class:`FakeSqlGateway` and the
:class:`~core.ai.embedding.BatchEmbedder`→:class:`FakeEmbedding` — so resolving
the handlers touches no Postgres, Azure, or network.

The dataset is a tiny one-case golden YAML written into `tmp_path` via the slice's
own :func:`~engram.evals.generation.contracts.build_dataset` / :func:`~engram.evals.generation.contracts.save_dataset`.
`--model test-model` selects a network-free model; the command builds it as
`TestModel(call_tools=[])` so the agent synthesizes a structured `RetrievalAnswer`
without tripping `fetch`'s `ModelRetry`-on-empty (a bare `TestModel` would). The
assertion: the command exits 0 and the rendered report prints the metric labels
(`recall@…` / `mrr`).
"""

from __future__ import annotations

import asyncio
import json

from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import typer

from core.ai.embedding import BatchEmbedder
from core.cqrs.contracts import JobQueries, MemoryJobs, MessageBus
from core.wiring.contracts import ContainerResolver
from engram.evals.commands import (
    CheckGenerator,
    ComputeCoverage,
    CoverageComputedReport,
    DatasetFrozenReport,
    DatasetScored,
    FreezeDataset,
    GenerateEvalDataset,
    GenerateEvalDatasetReport,
    GeneratorChecked,
    InventoryReaderFactory,
    RollbackPolicy,
    ScoreDataset,
    TuneRetrieval,
)
from engram.evals.generation.contracts import AuthoredCase, Stratum, TopUpPlan, build_dataset, save_dataset
from engram.evals.generation.domain.allocation import CellQuota
from engram.evals.generation.domain.datasets import RetirementRecord
from engram.evals.generator_check.contracts import RANK_DECIMALS, generator_check
from engram.evals.handlers import GenerateEvalDatasetHandler, RollbackPolicyHandler
from engram.evals.harness.contracts import ExperimentAxes, ExperimentVariant, expand_variants, run_experiment
from engram.evals.scoring.contracts import HitSources
from engram.evals.shared.domain import Archetype, ExpectedQa, QaCaseMeta, QaInput
from engram.evals.tuning.policy import DEFAULT_INSTRUCTIONS, baseline_policy, load_policy, save_policy
from engram.evals.tuning.strategy import DEFAULT_STRATEGY
from engram.indexing.contracts import KnowledgeBaseReader
from engram.kb.contracts import apply_retrieval_policy
from engram.retrieval.contracts import SqlKnowledgeBase
from engram.retrieval.domain import SqlResult
from engram.testing.fakes.bus_spy import RecordingCommandHandler
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.gateway import FakeSqlGateway
from engram.testing.fakes.knowledge_base_reader import FakeKnowledgeBaseReader
from engram.testing.specs import research_spec
from pydantic_evals.reporting import EvaluationReport
from typer.testing import CliRunner
from wireup import AsyncContainer
from wireup.errors import UnknownServiceRequestedError

from studio.knowledge_base.cli import app
from studio.knowledge_base.cli_evals import (
    _EXPERIMENT_STRATEGY_OPTION,
    _build_tune_command,
    _dataset_slug,
    _default_run_instructions,
    _experiment_axes,
    _experiment_evaluator,
    _parse_hold,
    _parse_strategies,
    _skill_md_path,
    _variant_evaluator,
)
from studio.knowledge_base.settings import ApplicationSettings, EvalsSettings, KnowledgeBaseSettings, LogfireSettings, TuningSettings
from studio.knowledge_base.wiring.contracts import AdminSqlGateway, EvaluationSqlTools, ReaderSqlGateway
from tests.support.http_app import write_settings_json

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping, Sequence
    from contextlib import AbstractContextManager

    from core.cqrs.contracts import Command, Event
    from core.resolver import Resolver
    from engram.evals.harness.contracts import EvaluationRunConfig, EvaluationRunReport, ExperimentAxis
    from typer.testing import Result

runner = CliRunner()


@pytest.fixture(autouse=True)
def _ambient_knowledge_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this file runs inside a real knowledge base by cwd, its spec the research template's own verbatim.

    A test that names its own `--knowledge-base` is unaffected: this only supplies the fallback a bare
    `evals ...` invocation resolves to when no flag is given.
    """
    root = tmp_path / "ambient-knowledge-base"
    root.mkdir()
    (root / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
    monkeypatch.chdir(root)


def _knowledge_base_root(tmp_path: Path) -> Path:
    """A research knowledge base on disk, the shape `KnowledgeBaseSettings` accepts."""
    knowledge_base_root = tmp_path / "knowledge_base"
    for required in ("Units", "_meta"):
        (knowledge_base_root / required).mkdir(parents=True, exist_ok=True)
    (knowledge_base_root / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
    return knowledge_base_root


def _settings(tmp_path: Path) -> ApplicationSettings:
    """A settings tree read from a real file under `tmp_path` — no env, no network."""
    return ApplicationSettings(json_file=write_settings_json(tmp_path / "application_settings.json"))


class _FakeCoverageReader:
    """Answers a knowledge base fingerprint of `spec-hash:{collection}`, the shape the real reader reads."""

    async def knowledge_base_fingerprint(self, *, collection: str) -> str:
        return f"spec-hash:{collection}"


class _FakeInventoryReaders:
    """Opens the scripted coverage reader in place of an owner session."""

    @asynccontextmanager
    async def __call__(self) -> AsyncGenerator[_FakeCoverageReader]:
        yield _FakeCoverageReader()


class _ReindexedCoverageReader:
    """Answers the fingerprint of a knowledge base indexed again since the first run."""

    async def knowledge_base_fingerprint(self, *, collection: str) -> str:
        return f"reindexed-hash:{collection}"


class _ReindexedInventoryReaders:
    """Opens the coverage reader of a knowledge base indexed again since the first run."""

    @asynccontextmanager
    async def __call__(self) -> AsyncGenerator[_ReindexedCoverageReader]:
        yield _ReindexedCoverageReader()


def _factory(tmp_path: Path):
    """A zero-arg `ctx.obj` factory returning the real container with inert read-side fakes.

    The retrieval toolset resolves through the real evals DI providers, so the
    :class:`~studio.knowledge_base.wiring.serve.ReaderSqlGateway` and :class:`~core.ai.embedding.BatchEmbedder`
    the handlers depend on are persistently overridden with inert fakes — the toolset builds with no
    Postgres/Azure (`test_container.py`'s override pattern, made persistent for the command run). The
    coverage reader a results file's fingerprint is read through is a fake too.
    """

    def build() -> object:
        from studio.knowledge_base.container import create_container

        container = create_container(_settings(tmp_path), knowledge_base=KnowledgeBaseSettings(path=_knowledge_base_root(tmp_path)))
        container.override.set(target=ReaderSqlGateway, new=FakeSqlGateway())
        container.override.set(target=BatchEmbedder, new=FakeEmbedding())
        container.override.set(target=AdminSqlGateway, new=FakeSqlGateway())
        container.override.set(target=JobQueries, new=MemoryJobs())
        container.override.set(target=InventoryReaderFactory, new=_FakeInventoryReaders())
        return container

    return build


def _write_golden(tmp_path: Path, name: str = "seed.yaml") -> Path:
    """Write a tiny one-case golden dataset to `tmp_path` via the slice's own builders."""
    record = AuthoredCase(
        question=QaInput(question="What is LightRAG?"),
        expected=ExpectedQa(answer="A graph-based RAG system.", expected_source_ids=("paper.lightrag-2025",)),
        meta=QaCaseMeta(anchor_page_id="paper.lightrag-2025", difficulty="easy"),
    )
    path = tmp_path / name
    save_dataset(build_dataset([record]), path)
    return path


class TestEvalsRun:
    def test_runs_the_test_model_and_prints_metric_labels(self, tmp_path: Path):
        dataset_path = _write_golden(tmp_path)

        result = runner.invoke(app, ["evals", "run", "--model", "test-model", "--dataset", str(dataset_path)], obj=_factory(tmp_path))

        assert result.exit_code == 0, result.stdout
        assert "recall@1" in result.stdout
        assert "mrr" in result.stdout

    def test_passing_instructions_still_produces_a_report(self, tmp_path: Path):
        dataset_path = _write_golden(tmp_path)

        result = runner.invoke(
            app,
            [
                "evals",
                "run",
                "--model",
                "test-model",
                "--dataset",
                str(dataset_path),
                "--instructions",
                "Answer concisely and cite page ids.",
            ],
            obj=_factory(tmp_path),
        )

        assert result.exit_code == 0, result.stdout
        assert "recall@1" in result.stdout

    def test_an_explicit_knowledge_base_is_the_one_resolved(self, tmp_path: Path):
        named_knowledge_base = tmp_path / "named-knowledge-base"
        for required in ("Units", "_meta"):
            (named_knowledge_base / required).mkdir(parents=True)
        (named_knowledge_base / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
        dataset_path = _write_golden(tmp_path)

        result = runner.invoke(
            app,
            ["evals", "run", "--model", "test-model", "--dataset", str(dataset_path), "--knowledge-base", str(named_knowledge_base)],
            obj=_factory(tmp_path),
        )

        assert result.exit_code == 0, result.stdout
        assert f"at {named_knowledge_base.resolve()}" in result.stdout

    def test_no_knowledge_base_named_and_no_knowledge_base_underfoot_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        empty_dir = tmp_path / "not-a-knowledge-base"
        empty_dir.mkdir()
        monkeypatch.chdir(empty_dir)
        dataset_path = _write_golden(tmp_path)

        result = runner.invoke(
            app, ["evals", "run", "--model", "test-model", "--dataset", str(dataset_path)], obj=_factory(tmp_path)
        )

        assert result.exit_code != 0
        assert "No knowledge base named" in result.stdout

    def test_source_level_with_no_knowledge_base_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        empty_dir = tmp_path / "not-a-knowledge-base"
        empty_dir.mkdir()
        monkeypatch.chdir(empty_dir)
        dataset_path = _write_golden(tmp_path)

        result = runner.invoke(
            app,
            ["evals", "run", "--model", "test-model", "--dataset", str(dataset_path), "--source-level", "--results", str(tmp_path / "r.jsonl")],
            obj=_factory(tmp_path),
        )

        assert result.exit_code == 1
        assert "No knowledge base named" in result.stdout
        assert not (tmp_path / "r.jsonl").exists()

    def test_source_level_without_a_results_file_is_refused(self, tmp_path: Path):
        dataset_path = _write_golden(tmp_path)

        result = runner.invoke(
            app, ["evals", "run", "--model", "test-model", "--dataset", str(dataset_path), "--source-level"], obj=_factory(tmp_path)
        )

        assert result.exit_code == 1
        assert "--source-level" in result.stdout
        assert "--results" in result.stdout
        assert "recall@1" not in result.stdout

    def test_results_writes_the_file_at_the_given_path(self, tmp_path: Path):
        dataset_path = _write_golden(tmp_path)
        results = tmp_path / "runs" / "results.jsonl"

        result = runner.invoke(
            app, ["evals", "run", "--model", "test-model", "--dataset", str(dataset_path), "--results", str(results)], obj=_factory(tmp_path)
        )

        assert result.exit_code == 0, result.stdout
        header, *lines = (json.loads(line) for line in results.read_text(encoding="utf-8").splitlines())
        assert header["header"]["collection"] == "research"
        assert header["header"]["knowledge_base_fingerprint"] == "spec-hash:research"
        assert (header["header"]["sql"], header["header"]["pinned_search"], header["header"]["max_tool_calls"]) == (False, False, None)
        assert [line["case"] for line in lines] == ["paper.lightrag-2025-0"]
        assert lines[0]["sources"] is None
        assert "source-level retrieval" not in result.stdout

    def test_sql_a_pinned_search_a_budget_and_source_level_reach_the_results_file(self, tmp_path: Path):
        dataset_path = _write_golden(tmp_path)
        results = tmp_path / "results.jsonl"

        result = runner.invoke(
            app,
            [
                "evals",
                "run",
                "--model",
                "test-model",
                "--dataset",
                str(dataset_path),
                "--sql",
                "--pinned-search",
                "--max-tool-calls",
                "4",
                "--source-level",
                "--results",
                str(results),
            ],
            obj=_factory(tmp_path),
        )

        assert result.exit_code == 0, result.stdout
        header, *lines = (json.loads(line) for line in results.read_text(encoding="utf-8").splitlines())
        assert (header["header"]["sql"], header["header"]["pinned_search"], header["header"]["max_tool_calls"]) == (True, True, 4)
        assert isinstance(lines[0]["sources"], list)


    def test_code_mode_and_a_thinking_level_reach_the_results_file(self, tmp_path: Path):
        dataset_path = _write_golden(tmp_path)
        results = tmp_path / "results.jsonl"

        result = runner.invoke(
            app,
            [
                "evals",
                "run",
                "--model",
                "test-model",
                "--dataset",
                str(dataset_path),
                "--code-mode",
                "--thinking",
                "xhigh",
                "--results",
                str(results),
            ],
            obj=_factory(tmp_path),
        )

        assert result.exit_code == 0, result.stdout
        header, *_lines = (json.loads(line) for line in results.read_text(encoding="utf-8").splitlines())
        assert (header["header"]["code_mode"], header["header"]["thinking"]) == (True, "xhigh")

    def test_a_thinking_level_off_the_scale_is_a_usage_error(self, tmp_path: Path):
        dataset_path = _write_golden(tmp_path)

        result = runner.invoke(
            app, ["evals", "run", "--model", "test-model", "--dataset", str(dataset_path), "--thinking", "max"], obj=_factory(tmp_path)
        )

        assert result.exit_code == 2
        assert "'max' is not one of 'low', 'medium', 'high', 'xhigh'" in " ".join(result.stderr.replace("│", " ").split())

    def test_source_level_prints_the_mean_source_level_retrieval_and_says_the_table_above_does_not_apply(self, tmp_path: Path):
        dataset_path = _write_golden(tmp_path)
        results = tmp_path / "results.jsonl"

        result = runner.invoke(
            app,
            ["evals", "run", "--model", "test-model", "--dataset", str(dataset_path), "--source-level", "--results", str(results)],
            obj=_factory(tmp_path),
        )

        assert result.exit_code == 0, result.stdout
        assert "source-level retrieval over 1 case(s) with gold ids: recall@4 0.000, recall@10 0.000, mrr 0.000\n" in result.stdout
        assert (
            "The recall, precision and mrr in the table above compare cited page ids with the gold ids, "
            "so they do not apply when the gold ids are source ids.\n"
        ) in result.stdout

    def test_source_level_says_when_the_source_lookup_failed(self, tmp_path: Path):
        dataset_path = _write_golden(tmp_path)
        build = _factory(tmp_path)

        def unreachable_sources() -> object:
            container = build()
            assert isinstance(container, AsyncContainer)
            container.override.set(target=HitSources, new=_UnreachableHitSources())
            return container

        result = runner.invoke(
            app,
            ["evals", "run", "--model", "test-model", "--dataset", str(dataset_path), "--source-level", "--results", str(tmp_path / "r.jsonl")],
            obj=unreachable_sources,
        )

        assert result.exit_code == 0, result.stdout
        assert "source-level retrieval: none, the source lookup failed (RuntimeError: the admin database is unreachable)\n" in result.stdout


def _write_three_cases(tmp_path: Path) -> Path:
    """Write a three-case dataset, `case-a`, `case-b` and `case-c`, each with an expected answer and one gold id."""
    records = [
        AuthoredCase(
            question=QaInput(question=f"Which note covers topic {letter}?"),
            expected=ExpectedQa(answer=f"note {letter}", expected_source_ids=(f"note.topic-{letter}",)),
            meta=QaCaseMeta(),
            case_name=f"case-{letter}",
        )
        for letter in ("a", "b", "c")
    ]
    path = tmp_path / "three.yaml"
    save_dataset(build_dataset(records), path)
    return path


def _rewrite_case_rows(results: Path, *, transient: str, missing: str) -> list[dict[str, object]]:
    """Mark one case line as a transient failure and drop another, as a run cut short by the model would leave them."""
    header, *rows = (json.loads(line) for line in results.read_text(encoding="utf-8").splitlines())
    kept_rows: list[dict[str, object]] = []
    for row in rows:
        if row["case"] == missing:
            continue
        if row["case"] == transient:
            row.update(failed=True, error="ModelAPIError: Connection error.", transient=True, answer=None, cited=[], strict=False, official=False)
        kept_rows.append(row)
    results.write_text("".join(f"{json.dumps(row)}\n" for row in [header, *kept_rows]), encoding="utf-8")
    return kept_rows


class TestEvalsRunResume:
    def test_resume_without_a_results_file_is_refused(self, tmp_path: Path):
        dataset_path = _write_golden(tmp_path)

        result = runner.invoke(app, ["evals", "run", "--model", "test-model", "--dataset", str(dataset_path), "--resume"], obj=_factory(tmp_path))

        assert result.exit_code == 1
        assert "--resume continues a results file, so pass --results PATH with it." in result.stdout
        assert "recall@1" not in result.stdout

    def test_resume_runs_only_the_missing_case_and_the_transient_failure_and_prints_how_many_were_kept_and_run(self, tmp_path: Path):
        dataset_path = _write_three_cases(tmp_path)
        results = tmp_path / "results.jsonl"
        command = ["evals", "run", "--model", "test-model", "--dataset", str(dataset_path), "--source-level", "--results", str(results)]
        first = runner.invoke(app, command, obj=_factory(tmp_path))
        assert first.exit_code == 0, first.stdout
        first_header = results.read_text(encoding="utf-8").splitlines()[0]
        kept_rows = _rewrite_case_rows(results, transient="case-b", missing="case-c")

        kept_line = results.read_text(encoding="utf-8").split("\n")[1]

        result = runner.invoke(app, [*command, "--resume"], obj=_factory(tmp_path))

        assert result.exit_code == 0, result.stdout
        assert "kept 1 case(s) from the results file and ran 2. The table above covers the 2 case(s) run now.\n" in result.stdout
        assert "results file over 3 case(s): 0 failed, strict accuracy " in result.stdout
        assert "source-level retrieval over 3 case(s) with gold ids" in result.stdout
        assert "warning" not in result.stdout
        assert "source scores missing" not in result.stdout
        header, *rows = results.read_text(encoding="utf-8").split("\n")
        assert header == first_header
        assert [json.loads(row)["case"] for row in rows if row] == ["case-a", "case-b", "case-c"]
        assert rows[0] == kept_line
        assert json.loads(rows[0]) == kept_rows[0]
        assert json.loads(rows[1])["failed"] is False

    def test_a_source_lookup_that_fails_on_resume_prints_that_the_cases_run_now_miss_their_source_scores(self, tmp_path: Path):
        dataset_path = _write_three_cases(tmp_path)
        results = tmp_path / "results.jsonl"
        command = ["evals", "run", "--model", "test-model", "--dataset", str(dataset_path), "--source-level", "--results", str(results)]
        first = runner.invoke(app, command, obj=_factory(tmp_path))
        assert first.exit_code == 0, first.stdout
        first_header = results.read_text(encoding="utf-8").split("\n")[0]
        _rewrite_case_rows(results, transient="case-b", missing="case-c")
        kept_line = results.read_text(encoding="utf-8").split("\n")[1]
        build = _factory(tmp_path)

        def unreachable_sources() -> object:
            container = build()
            assert isinstance(container, AsyncContainer)
            container.override.set(target=HitSources, new=_UnreachableHitSources())
            return container

        result = runner.invoke(app, [*command, "--resume"], obj=unreachable_sources)

        assert result.exit_code == 0, result.stdout
        assert (
            "source scores missing for the 2 case(s) run now: the source lookup failed "
            "(RuntimeError: the admin database is unreachable). The kept cases keep theirs.\n"
        ) in result.stdout
        assert "source-level retrieval over 1 case(s) with gold ids" in result.stdout
        header, kept, *_run_now = results.read_text(encoding="utf-8").split("\n")
        assert (header, kept) == (first_header, kept_line)

    def test_a_resume_on_a_knowledge_base_indexed_again_prints_one_warning_and_still_runs(self, tmp_path: Path):
        dataset_path = _write_three_cases(tmp_path)
        results = tmp_path / "results.jsonl"
        command = ["evals", "run", "--model", "test-model", "--dataset", str(dataset_path), "--results", str(results)]
        first = runner.invoke(app, command, obj=_factory(tmp_path))
        assert first.exit_code == 0, first.stdout
        first_header = results.read_text(encoding="utf-8").split("\n")[0]
        _rewrite_case_rows(results, transient="case-b", missing="case-c")
        build = _factory(tmp_path)

        def reindexed() -> object:
            container = build()
            assert isinstance(container, AsyncContainer)
            container.override.set(target=InventoryReaderFactory, new=_ReindexedInventoryReaders())
            return container

        result = runner.invoke(app, [*command, "--resume"], obj=reindexed)

        assert result.exit_code == 0, result.stdout
        warnings = [line for line in result.stdout.splitlines() if line.startswith("warning:")]
        assert warnings == [
            (
                "warning: the knowledge base was indexed again since the results file was started (spec-hash:research in the file, "
                "reindexed-hash:research now), so the 2 case(s) run now read another index than the kept ones."
            )
        ]
        assert "kept 1 case(s) from the results file and ran 2." in result.stdout
        assert results.read_text(encoding="utf-8").split("\n")[0] == first_header

    def test_resume_refuses_a_results_file_from_other_settings_and_leaves_it_as_it_was(self, tmp_path: Path):
        dataset_path = _write_three_cases(tmp_path)
        results = tmp_path / "results.jsonl"
        command = ["evals", "run", "--model", "test-model", "--dataset", str(dataset_path), "--results", str(results)]
        first = runner.invoke(app, [*command, "--max-tool-calls", "4"], obj=_factory(tmp_path))
        assert first.exit_code == 0, first.stdout
        written = results.read_text(encoding="utf-8")

        result = runner.invoke(app, [*command, "--max-tool-calls", "6", "--resume"], obj=_factory(tmp_path))

        assert result.exit_code == 1
        assert "evals run failed: the results file" in result.stdout
        assert "max_tool_calls (4 in the file, 6 now)" in " ".join(result.stdout.split())
        assert results.read_text(encoding="utf-8") == written


class _UnreachableHitSources:
    """A page-to-source lookup whose database cannot be reached."""

    async def page_ids(self, collection: str, ids: Sequence[str]) -> frozenset[str]:
        msg = "the admin database is unreachable"
        raise RuntimeError(msg)

    async def source_ids(self, collection: str, page_ids: Sequence[str]) -> Mapping[str, str]:
        msg = "the admin database is unreachable"
        raise RuntimeError(msg)


class TestEvaluationSqlTools:
    async def test_the_retrying_sql_tools_resolve_only_under_their_own_type(self, tmp_path: Path):
        container = _factory(tmp_path)()
        assert isinstance(container, AsyncContainer)

        tools = await container.get(EvaluationSqlTools)

        assert tools.capability.retry_refusals is True
        with pytest.raises(UnknownServiceRequestedError):
            await container.get(SqlKnowledgeBase)


class TestHelp:
    def test_evals_group_is_listed_at_top_level(self):
        result = runner.invoke(app, ["--help"])

        assert result.exit_code == 0
        assert "evals" in result.stdout

    def test_run_help_lists_the_flags(self):
        result = runner.invoke(app, ["evals", "run", "--help"])

        assert result.exit_code == 0
        for flag in (
            "--model",
            "--dataset",
            "--instructions",
            "--retrieval-strategy",
            "--code-mode",
            "--thinking",
            "--disclosure",
            "--sql",
            "--max-tool-calls",
            "--pinned-search",
            "--source-level",
            "--results",
            "--resume",
        ):
            assert flag in result.stdout

    def test_run_under_a_retrieval_strategy_produces_a_report(self, tmp_path: Path):
        dataset_path = _write_golden(tmp_path)

        result = runner.invoke(
            app,
            ["evals", "run", "--model", "test-model", "--dataset", str(dataset_path), "--retrieval-strategy", "graph-only"],
            obj=_factory(tmp_path),
        )

        assert result.exit_code == 0, result.stdout
        assert "recall@1" in result.stdout

    def test_run_with_disclosure_produces_a_report(self, tmp_path: Path):
        # `--disclosure` adds the `disclose` tool + `ProgressiveDisclosure` capability; the run's
        # `EnvelopeHandler` resolves over the same overridden `FakeSqlGateway`, no real DB.
        dataset_path = _write_golden(tmp_path)

        result = runner.invoke(
            app, ["evals", "run", "--model", "test-model", "--dataset", str(dataset_path), "--disclosure"], obj=_factory(tmp_path)
        )

        assert result.exit_code == 0, result.stdout
        assert "recall@1" in result.stdout


class TestGenDatasetCli:
    def test_slug_drops_prefix_and_replaces_dots(self):
        assert _dataset_slug("paper.lightrag-2025") == "lightrag-2025"
        assert _dataset_slug("paper.cag.2024") == "cag-2024"
        assert _dataset_slug("doc.scifact-4983") == "scifact-4983"

    def test_gen_dataset_help_lists_options(self):
        result = runner.invoke(app, ["evals", "gen-dataset", "--help"])

        assert result.exit_code == 0
        for flag in ("--page", "--collection", "--archetype", "--count", "--model", "--out", "--push", "--top-up", "--dry-run", "--salt"):
            assert flag in result.stdout


class _GenContainer:
    """Fake async DI container backing the gen-dataset bus dispatch.

    The command resolves the identity's admin gateway and job store first, then
    `GenerateEvalDatasetHandler` (to trigger the late route registration), then `MessageBus`, and
    `EvalsSettings` for the default `--out` path. This fake returns the recording handler, the
    pre-wired bus, and the tmp-dir settings respectively.
    """

    def __init__(self, bus: MessageBus, handler: object, settings: EvalsSettings) -> None:
        self._bus = bus
        self._handler = handler
        self._settings = settings
        self._gateway = FakeSqlGateway()
        self._reader: KnowledgeBaseReader = FakeKnowledgeBaseReader()
        self._jobs = MemoryJobs()

    async def get(self, key: type) -> object:
        if key is MessageBus:
            return self._bus
        if key is GenerateEvalDatasetHandler:
            return self._handler
        if key is EvalsSettings:
            return self._settings
        if key is AdminSqlGateway:
            return self._gateway
        if key is KnowledgeBaseReader:
            return self._reader
        if key is JobQueries:
            return self._jobs
        msg = f"_GenContainer has no registration for {key!r}"
        raise KeyError(msg)


class _GenFactory:
    """Zero-arg `ctx.obj` factory returning a container around a recording gen-dataset handler."""

    def __init__(self, report: GenerateEvalDatasetReport, datasets_dir: Path) -> None:
        self.handler = RecordingCommandHandler(result=report)
        self._bus = MessageBus({GenerateEvalDataset: self.handler}, {})
        self._settings = EvalsSettings(datasets_dir=datasets_dir)

    def __call__(self) -> _GenContainer:
        return _GenContainer(self._bus, self.handler, self._settings)


def _gen_report(out: str, *, n_cases: int = 3, archetypes: tuple[Archetype, ...] = (Archetype.PINPOINT,)) -> GenerateEvalDatasetReport:
    """A successful structural-generation report the recording handler returns."""
    return GenerateEvalDatasetReport(out=out, n_cases=n_cases, requested_archetypes=archetypes, duration_seconds=0.1)


class TestGenDatasetPipeline:
    """The rewired `gen-dataset` dispatches a `GenerateEvalDataset` through the bus."""

    def test_archetypes_collection_and_no_page_are_plumbed_to_the_command(self, tmp_path: Path):
        factory = _GenFactory(_gen_report(str(tmp_path / "research-structural.yaml")), tmp_path)

        result = runner.invoke(
            app, ["evals", "gen-dataset", "--archetype", "pinpoint", "--archetype", "survey", "--collection", "research"], obj=factory
        )

        assert result.exit_code == 0, result.stdout
        command = factory.handler.handled[0]
        assert isinstance(command, GenerateEvalDataset)
        assert command.archetypes == (Archetype.PINPOINT, Archetype.SURVEY)
        assert command.collection == "research"
        assert command.page_ids == ()

    def test_page_sets_page_ids_and_default_out_uses_the_slug(self, tmp_path: Path):
        factory = _GenFactory(_gen_report(str(tmp_path / "x.yaml")), tmp_path)

        result = runner.invoke(app, ["evals", "gen-dataset", "--page", "paper.x"], obj=factory)

        assert result.exit_code == 0, result.stdout
        command = factory.handler.handled[0]
        assert isinstance(command, GenerateEvalDataset)
        assert command.page_ids == ("paper.x",)
        assert command.out.endswith("x.yaml")

    def test_no_page_default_out_is_the_collection_structural_file(self, tmp_path: Path):
        factory = _GenFactory(_gen_report(str(tmp_path / "research-structural.yaml")), tmp_path)

        result = runner.invoke(app, ["evals", "gen-dataset", "--collection", "research"], obj=factory)

        assert result.exit_code == 0, result.stdout
        command = factory.handler.handled[0]
        assert isinstance(command, GenerateEvalDataset)
        assert command.out.endswith("research-structural.yaml")

    def test_count_overrides_the_total(self, tmp_path: Path):
        factory = _GenFactory(_gen_report(str(tmp_path / "research-structural.yaml")), tmp_path)

        result = runner.invoke(app, ["evals", "gen-dataset", "--count", "7"], obj=factory)

        assert result.exit_code == 0, result.stdout
        command = factory.handler.handled[0]
        assert isinstance(command, GenerateEvalDataset)
        assert command.count == 7

    def test_empty_archetypes_stay_empty(self, tmp_path: Path):
        factory = _GenFactory(_gen_report(str(tmp_path / "research-structural.yaml")), tmp_path)

        runner.invoke(app, ["evals", "gen-dataset"], obj=factory)

        command = factory.handler.handled[0]
        assert isinstance(command, GenerateEvalDataset)
        assert command.archetypes == ()
        assert command.count is None

    def test_an_unknown_archetype_fails_cleanly_without_dispatch(self, tmp_path: Path):
        factory = _GenFactory(_gen_report(str(tmp_path / "research-structural.yaml")), tmp_path)

        result = runner.invoke(app, ["evals", "gen-dataset", "--archetype", "bogus"], obj=factory)

        assert result.exit_code == 1
        assert factory.handler.handled == []

    def test_success_echo_includes_n_cases_and_out(self, tmp_path: Path):
        out = str(tmp_path / "research-structural.yaml")
        factory = _GenFactory(_gen_report(out, n_cases=5), tmp_path)

        result = runner.invoke(app, ["evals", "gen-dataset", "--collection", "research"], obj=factory)

        assert result.exit_code == 0, result.stdout
        assert "5 golden case(s)" in result.stdout
        assert out in result.stdout

    def test_top_up_dry_run_and_salt_are_plumbed_to_the_command(self, tmp_path: Path):
        factory = _GenFactory(_gen_report(str(tmp_path / "research-structural.yaml")), tmp_path)

        result = runner.invoke(app, ["evals", "gen-dataset", "--top-up", "current", "--dry-run", "--salt", "s1"], obj=factory)

        assert result.exit_code == 0, result.stdout
        command = factory.handler.handled[0]
        assert isinstance(command, GenerateEvalDataset)
        assert command.top_up_from == "current"
        assert command.dry_run is True
        assert command.salt == "s1"

    def test_no_top_up_leaves_the_command_in_fresh_mode(self, tmp_path: Path):
        factory = _GenFactory(_gen_report(str(tmp_path / "research-structural.yaml")), tmp_path)

        runner.invoke(app, ["evals", "gen-dataset"], obj=factory)

        command = factory.handler.handled[0]
        assert isinstance(command, GenerateEvalDataset)
        assert command.top_up_from is None
        assert command.dry_run is False
        assert command.salt is None

    def test_dry_run_echo_prints_the_generate_and_retire_plan(self, tmp_path: Path):
        plan = TopUpPlan(
            parent_version="research.v1.yaml",
            generate=(CellQuota(stratum=Stratum(archetype=Archetype.SURVEY, difficulty="hard", anchor_page_type="hub"), quota=2),),
            retire=(RetirementRecord(case_name="pinpoint:c9#0", seed_key="pinpoint:c9", reason="displaced"),),
        )
        report = GenerateEvalDatasetReport(
            out="research.v1.yaml", n_cases=5, requested_archetypes=(Archetype.PINPOINT,), retired=1, plan=plan
        )
        factory = _GenFactory(report, tmp_path)

        result = runner.invoke(app, ["evals", "gen-dataset", "--top-up", "current", "--dry-run"], obj=factory)

        assert result.exit_code == 0, result.stdout
        assert "dry run, nothing written" in result.stdout
        assert "survey/hard/hub x2" in result.stdout  # the generate cell
        assert "pinpoint:c9#0 (displaced)" in result.stdout  # the retirement

    def test_applied_top_up_echo_reports_the_retired_count(self, tmp_path: Path):
        out = str(tmp_path / "research.v2.yaml")
        report = GenerateEvalDatasetReport(
            out=out, n_cases=3, requested_archetypes=(Archetype.PINPOINT,), version="research.v2.yaml", retired=2
        )
        factory = _GenFactory(report, tmp_path)

        result = runner.invoke(app, ["evals", "gen-dataset", "--top-up", "current"], obj=factory)

        assert result.exit_code == 0, result.stdout
        assert "3 golden case(s)" in result.stdout
        assert "2 retired" in result.stdout
        assert out in result.stdout


class _CoverageContainer:
    """Fake async DI container backing the `evals coverage` bus dispatch.

    The command resolves the identity's admin gateway and job store, `EvalsSettings` (for the
    default `--out` directory), and `MessageBus` (the dispatch seam), so this fake returns those in
    turn, the pre-wired bus, the tmp-dir settings, an empty admin gateway, and an in-memory job store.
    """

    def __init__(self, bus: MessageBus, settings: EvalsSettings) -> None:
        self._bus = bus
        self._settings = settings
        self._gateway = FakeSqlGateway()
        self._reader: KnowledgeBaseReader = FakeKnowledgeBaseReader()
        self._jobs = MemoryJobs()

    async def get(self, key: type) -> object:
        if key is MessageBus:
            return self._bus
        if key is EvalsSettings:
            return self._settings
        if key is AdminSqlGateway:
            return self._gateway
        if key is KnowledgeBaseReader:
            return self._reader
        if key is JobQueries:
            return self._jobs
        msg = f"_CoverageContainer has no registration for {key!r}"
        raise KeyError(msg)


class _CoverageFactory:
    """Zero-arg `ctx.obj` factory returning a container around a recording coverage handler."""

    def __init__(self, report: CoverageComputedReport, datasets_dir: Path) -> None:
        self.handler = RecordingCommandHandler(result=report)
        self._bus = MessageBus({ComputeCoverage: self.handler}, {})
        self._settings = EvalsSettings(datasets_dir=datasets_dir)

    def __call__(self) -> _CoverageContainer:
        return _CoverageContainer(self._bus, self._settings)


def _coverage_report(
    out: str, *, gaps: tuple[str, ...] = (), debt: float = 0.0, dataset_version: str | None = None
) -> CoverageComputedReport:
    """A successful coverage report the recording handler returns."""
    return CoverageComputedReport(
        out=out, markdown=out.replace(".yaml", ".md"), collection="research", gaps=gaps, debt=debt, dataset_version=dataset_version
    )


class TestCoverageCli:
    """`evals coverage` dispatches a `ComputeCoverage` through the bus."""

    def test_coverage_help_lists_options(self):
        result = runner.invoke(app, ["evals", "coverage", "--help"])

        assert result.exit_code == 0
        for flag in ("--collection", "--dataset", "--out"):
            assert flag in result.stdout

    def test_collection_and_dataset_are_plumbed_to_the_command(self, tmp_path: Path):
        factory = _CoverageFactory(_coverage_report(str(tmp_path / "coverage" / "research.coverage.yaml")), tmp_path / "datasets")

        result = runner.invoke(app, ["evals", "coverage", "--collection", "research", "--dataset", "current"], obj=factory)

        assert result.exit_code == 0, result.stdout
        command = factory.handler.handled[0]
        assert isinstance(command, ComputeCoverage)
        assert command.collection == "research"
        assert command.dataset == "current"

    def test_default_out_lands_under_the_coverage_dir_and_dataset_is_none(self, tmp_path: Path):
        # datasets_dir is `<tmp>/evals/datasets`, so the default coverage dir is its `../coverage` sibling.
        factory = _CoverageFactory(_coverage_report("x"), tmp_path / "evals" / "datasets")

        result = runner.invoke(app, ["evals", "coverage"], obj=factory)

        assert result.exit_code == 0, result.stdout
        command = factory.handler.handled[0]
        assert isinstance(command, ComputeCoverage)
        assert command.out == str(tmp_path / "evals" / "coverage")
        assert command.dataset is None  # inventory-only by default

    def test_success_echo_reports_debt_gaps_and_the_written_path(self, tmp_path: Path):
        out = str(tmp_path / "coverage" / "research.coverage.yaml")
        factory = _CoverageFactory(_coverage_report(out, gaps=("survey/hard/hub",), debt=0.5), tmp_path / "datasets")

        result = runner.invoke(app, ["evals", "coverage"], obj=factory)

        assert result.exit_code == 0, result.stdout
        assert "debt 0.500" in result.stdout
        assert "survey/hard/hub" in result.stdout
        assert out in result.stdout


def _identity_gateway() -> FakeSqlGateway:
    """A gateway answering the identity read with no revision and no index history."""
    return FakeSqlGateway(
        routed={
            "to_regclass": SqlResult(rows=({"present": False},), columns=("present",)),
            "index_fingerprint": SqlResult(rows=(), columns=("indexed_at",)),
        }
    )


class _FreezeContainer:
    """Fake async DI container backing the `evals freeze` bus dispatch.

    Also serves the `evals rollback-policy` identity announce (`AdminSqlGateway`, `JobQueries`),
    shared over the same container shape.
    """

    def __init__(self, bus: MessageBus) -> None:
        self._bus = bus
        self._gateway = _identity_gateway()
        self._reader: KnowledgeBaseReader = FakeKnowledgeBaseReader()
        self._jobs = MemoryJobs()

    async def get(self, key: type) -> object:
        if key is MessageBus:
            return self._bus
        if key is AdminSqlGateway:
            return self._gateway
        if key is KnowledgeBaseReader:
            return self._reader
        if key is JobQueries:
            return self._jobs
        msg = f"_FreezeContainer has no registration for {key!r}"
        raise KeyError(msg)


class _FreezeFactory:
    """Zero-arg `ctx.obj` factory returning a container around a recording freeze handler."""

    def __init__(self, report: DatasetFrozenReport) -> None:
        self.handler = RecordingCommandHandler(result=report)
        self._bus = MessageBus({FreezeDataset: self.handler}, {})

    def __call__(self) -> _FreezeContainer:
        return _FreezeContainer(self._bus)


def _freeze_report(
    sidecar: str, *, frozen_version: int = 1, n_frozen: int = 4, dataset_version: str = "research.v1.yaml", stale_fraction: float = 0.0
) -> DatasetFrozenReport:
    """A successful freeze report the recording handler returns."""
    return DatasetFrozenReport(
        dataset_version=dataset_version,
        frozen_version=frozen_version,
        n_frozen=n_frozen,
        per_stratum={"pinpoint/easy": 2, "survey/hard": 2},
        staleness_at_freeze=stale_fraction,
        sidecar=sidecar,
    )


class TestTheOldCommandName:
    def test_bless_is_no_longer_a_command(self):
        result = runner.invoke(app, ["evals", "bless", "--help"])

        assert result.exit_code != 0
        assert "No such command" in result.output


class TestFreezeCli:
    """`evals freeze` dispatches a `FreezeDataset` through the bus."""

    def test_freeze_help_lists_options(self):
        result = runner.invoke(app, ["evals", "freeze", "--help"])

        assert result.exit_code == 0
        for flag in ("--dataset", "--fraction", "--collection"):
            assert flag in result.stdout

    def test_flags_are_plumbed_to_the_command(self):
        factory = _FreezeFactory(_freeze_report("evals/datasets/research.frozen.v1.json"))

        result = runner.invoke(
            app, ["evals", "freeze", "--dataset", "research:v2", "--fraction", "0.3", "--collection", "research"], obj=factory
        )

        assert result.exit_code == 0, result.stdout
        command = factory.handler.handled[0]
        assert isinstance(command, FreezeDataset)
        assert command.dataset == "research:v2"
        assert command.fraction == 0.3
        assert command.collection == "research"

    def test_default_dataset_and_fraction_are_none(self):
        factory = _FreezeFactory(_freeze_report("x"))

        result = runner.invoke(app, ["evals", "freeze"], obj=factory)

        assert result.exit_code == 0, result.stdout
        command = factory.handler.handled[0]
        assert isinstance(command, FreezeDataset)
        assert command.dataset is None  # the collection's current version by default
        assert command.fraction is None  # the configured frozen_fraction by default

    def test_success_echo_reports_the_frozen_version_stratum_counts_and_sidecar(self):
        sidecar = "evals/datasets/research.frozen.v1.json"
        factory = _FreezeFactory(_freeze_report(sidecar, frozen_version=1, n_frozen=4, stale_fraction=0.25))

        result = runner.invoke(app, ["evals", "freeze"], obj=factory)

        assert result.exit_code == 0, result.stdout
        assert "frozen v1" in result.stdout
        assert "4 case(s)" in result.stdout
        assert "25.0% stale" in result.stdout
        assert "pinpoint/easy: 2" in result.stdout
        assert sidecar in result.stdout


class _TuneSettingsResolver:
    """Minimal resolver backing `_build_tune_command`'s default `--out` resolution.

    The command-assembly helper resolves only `EvalsSettings` (for the tuning-policy default
    path), so this fake returns the tmp-dir settings and refuses anything else loudly.
    """

    def __init__(self, settings: EvalsSettings) -> None:
        self._settings = settings

    async def get[T](self, klass: type[T]) -> T:
        services: dict[type, object] = {EvalsSettings: self._settings}
        service = services.get(klass)
        if not isinstance(service, klass):
            msg = f"_TuneSettingsResolver has no registration for {klass!r}"
            raise KeyError(msg)
        return service


class TestTuneCommandAssembly:
    """`_build_tune_command` assembles a `TuneRetrieval` from the flags."""

    def test_flags_are_plumbed_to_the_command(self, tmp_path: Path):
        resolver = _TuneSettingsResolver(EvalsSettings(datasets_dir=tmp_path / "datasets"))
        out = str(tmp_path / "winner.yaml")

        command = asyncio.run(
            _build_tune_command(
                resolver, dataset="seed.yaml", model="gpt-4.1", collection="cooking", out=out, max_rounds=5, token_budget=2000
            )
        )

        assert isinstance(command, TuneRetrieval)
        assert command.dataset == "seed.yaml"
        assert command.model == "gpt-4.1"
        assert command.collection == "cooking"
        assert command.out == out
        assert command.max_rounds == 5
        assert command.token_budget == 2000

    def test_default_out_lands_under_the_tuning_dir(self, tmp_path: Path):
        resolver = _TuneSettingsResolver(EvalsSettings(tuning=TuningSettings(policies_dir=tmp_path / "tuning")))

        command = asyncio.run(
            _build_tune_command(
                resolver, dataset="seed.yaml", model="test-model", collection="research", out=None, max_rounds=None, token_budget=None
            )
        )

        assert command.out == str(tmp_path / "tuning" / "research.yaml")
        assert command.max_rounds is None
        assert command.token_budget is None

    def test_tune_help_lists_the_flags(self):
        result = runner.invoke(app, ["evals", "tune", "--help"])

        assert result.exit_code == 0
        for flag in ("--dataset", "--model", "--collection", "--out", "--max-rounds", "--token-budget"):
            assert flag in result.stdout


class TestApplyPolicyCli:
    """`evals apply-policy` loads a winning policy and writes its channels (working tree only)."""

    def test_apply_policy_help_lists_the_flags(self):
        result = runner.invoke(app, ["evals", "apply-policy", "--help"])

        assert result.exit_code == 0
        for flag in ("--policy", "--knowledge-base"):
            assert flag in result.stdout

    def test_skill_md_path_resolves_to_the_shipped_skill_with_the_sentinels(self):
        # The path math (package root from policy.__file__, plugin/ sibling of src/) must land on the
        # real shipped skill that already carries the managed-section sentinels apply-policy rewrites.
        skill = _skill_md_path()

        assert skill.is_file()
        text = skill.read_text(encoding="utf-8")
        assert "<!-- BEGIN ENGRAM TUNED INSTRUCTIONS -->" in text
        assert "<!-- END ENGRAM TUNED INSTRUCTIONS -->" in text

    def test_a_missing_policy_file_fails_cleanly_without_touching_tracked_files(self, tmp_path: Path):
        # load_policy runs before any write, so a missing --policy is a clean exit-1 and no tracked
        # file (the real policy.py / SKILL.md) is mutated.
        before = _skill_md_path().read_text(encoding="utf-8")

        result = runner.invoke(app, ["evals", "apply-policy", "--policy", str(tmp_path / "missing.yaml"), "--knowledge-base", str(tmp_path)])

        assert result.exit_code == 1
        assert "evals apply-policy failed" in result.stdout
        assert _skill_md_path().read_text(encoding="utf-8") == before


class TestExperimentCli:
    def test_experiment_help_lists_options(self):
        result = runner.invoke(app, ["evals", "experiment", "--help"])

        assert result.exit_code == 0
        for flag in ("--hold", "--model", "--instructions", "--dataset", "--retrieval-strategy"):
            assert flag in result.stdout

    def test_parse_hold_accepts_the_four_axes(self):
        assert _parse_hold("model") == "model"
        assert _parse_hold("instructions") == "instructions"
        assert _parse_hold("dataset") == "dataset"
        assert _parse_hold("retrieval_strategy") == "retrieval_strategy"

    def test_parse_hold_rejects_an_unknown_axis(self):
        result = runner.invoke(app, ["evals", "experiment", "--hold", "bogus"])

        assert result.exit_code != 0

    def test_the_strategy_option_defaults_to_the_default_strategy(self):
        assert _EXPERIMENT_STRATEGY_OPTION.default == [DEFAULT_STRATEGY.value]

    def test_parse_strategies_returns_the_known_names(self):
        assert _parse_strategies(["search-hybrid", "full"]) == ("search-hybrid", "full")

    def test_parse_strategies_refuses_an_unknown_name(self):
        with pytest.raises(typer.Exit) as refusal:
            _parse_strategies(["bogus"])

        assert refusal.value.exit_code == 1

    def test_parse_strategies_refuses_an_empty_axis(self):
        with pytest.raises(typer.Exit):
            _parse_strategies([])

    def test_an_unknown_strategy_never_reaches_the_settings_or_the_container(self, tmp_path: Path):
        # The refusal is parsed from the flag itself, before the knowledge base is resolved, so the
        # recording factory is never called and no database is touched.
        factory = _RecordingFactory()

        result = runner.invoke(
            app,
            ["evals", "experiment", "--hold", "model", "--retrieval-strategy", "bogus", "--knowledge-base", str(tmp_path)],
            obj=factory,
        )

        assert result.exit_code == 1
        assert "--retrieval-strategy must be one of" in result.stdout
        assert "bogus" in result.stdout
        assert factory.calls == 0

    def test_the_axes_builder_refuses_an_unknown_strategy_cleanly(self):
        with pytest.raises(typer.Exit):
            _experiment_axes(models=("test-model",), instructions=("x",), datasets=("p.yaml",), retrieval_strategies=("bogus",))

    def test_the_axes_builder_carries_every_axis(self):
        axes = _experiment_axes(
            models=("test-model",), instructions=("x",), datasets=("p.yaml",), retrieval_strategies=("search-hybrid", "full")
        )

        assert axes.retrieval_strategy == ("search-hybrid", "full")


class _RecordingFactory:
    """A `ctx.obj` factory that records being called instead of building anything."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        msg = "the command built a container when it should have refused first"
        raise AssertionError(msg)


class _UnusedResolver:
    """A `Resolver` the recording run never asks anything of."""

    async def get[T](self, klass: type[T]) -> T:
        msg = f"the variant evaluator resolved {klass!r} when the run was faked"
        raise AssertionError(msg)


@dataclass
class _RecordingVariantRun:
    """A `VariantRun` that records the config of every variant instead of scoring one."""

    configs: list[EvaluationRunConfig] = field(default_factory=list)
    datasets: list[Path] = field(default_factory=list)

    async def __call__(self, container: Resolver, *, config: EvaluationRunConfig, dataset_path: Path) -> EvaluationRunReport:
        self.configs.append(config)
        self.datasets.append(dataset_path)
        return EvaluationReport(name="recorded", cases=[], failures=[])


@dataclass
class _RecordingVariantSpan:
    """A `VariantSpan` that records what each variant's run was tagged with, opening nothing."""

    tags: list[dict[str, str]] = field(default_factory=list)

    def __call__(self, *, model: str, instructions: str, dataset: str, retrieval_strategy: str) -> AbstractContextManager[object]:
        self.tags.append({"model": model, "instructions": instructions, "dataset": dataset, "retrieval_strategy": retrieval_strategy})
        return nullcontext()


class TestVariantEvaluator:
    """Every axis of a variant reaches the run it configures and the span that tags it."""

    _AXES = ExperimentAxes(
        model=("test-model", "other-model"),
        instructions=("prompt A", "prompt B"),
        dataset=("first.yaml", "second.yaml"),
        retrieval_strategy=("search-hybrid", "full"),
    )

    async def test_one_variant_reaches_the_run_config_and_the_span(self):
        run = _RecordingVariantRun()
        span = _RecordingVariantSpan()
        variant = ExperimentVariant(model="test-model", instructions="prompt A", dataset="first.yaml", retrieval_strategy="search-hybrid")

        await _experiment_evaluator(_UnusedResolver(), variant, run=run, span=span)

        assert run.configs[0].model_spec == "test-model"
        assert run.configs[0].instructions == "prompt A"
        assert run.configs[0].retrieval_strategy == "search-hybrid"
        assert run.datasets == [Path("first.yaml")]
        assert span.tags == [
            {"model": "test-model", "instructions": "prompt A", "dataset": "first.yaml", "retrieval_strategy": "search-hybrid"}
        ]

    @pytest.mark.parametrize("hold", ["model", "instructions", "dataset", "retrieval_strategy"])
    async def test_every_variant_of_every_hold_carries_its_own_strategy(self, hold: ExperimentAxis):
        run = _RecordingVariantRun()
        span = _RecordingVariantSpan()

        runs = await run_experiment(self._AXES, hold=hold, evaluate=_variant_evaluator(_UnusedResolver(), run=run, span=span))

        expected = [variant.retrieval_strategy for variant in expand_variants(self._AXES, hold=hold)]
        assert [config.retrieval_strategy for config in run.configs] == expected
        assert [tag["retrieval_strategy"] for tag in span.tags] == expected
        assert [experiment.variant.retrieval_strategy for experiment in runs] == expected

    def test_holding_the_strategy_pins_one_surface_across_the_sweep(self):
        variants = expand_variants(self._AXES, hold="retrieval_strategy")

        assert {variant.retrieval_strategy for variant in variants} == {"search-hybrid"}


# A saved-SQL file in the rca.py on-disk format (a `-- name:` block per query). Two of the D10 triad
# so the degraded path surfaces real loaded names.
_RCA_QUERIES_SQL = (
    "-- name: slowest_tool_spans\n"
    "SELECT span_name, duration FROM records ORDER BY duration DESC LIMIT 20\n"
    "\n"
    "-- name: lowest_recall_cases\n"
    "SELECT case_name FROM records WHERE recall_at_5 = 0\n"
)


def _rca_settings(tmp_path: Path, *, read_token: str = "", queries_path: Path | None = None) -> ApplicationSettings:
    """A settings tree for the RCA command: a custom `evals.rca_queries_path` + a controllable token.

    `read_token` empty (the default) drives the degraded, MCP-driven path — the command must never
    open a socket; a non-empty token would select the live Query-API path (not exercised in unit tests).
    `queries_path` overrides the saved-SQL location (point it at an absent file to test that degrade).
    """
    rca_path = queries_path if queries_path is not None else tmp_path / "evals" / "rca" / "queries.sql"
    base = ApplicationSettings()
    return ApplicationSettings(
        evals=EvalsSettings(rca_queries_path=rca_path),
        infrastructure=base.infrastructure.model_copy(update={"logfire": LogfireSettings(read_token=read_token)}),
    )


def _rca_factory(tmp_path: Path, *, read_token: str = "", queries_path: Path | None = None):
    """A zero-arg `ctx.obj` factory, the real container over RCA-shaped settings.

    The `rca` command resolves only `EvalsSettings` (a registered singleton), the identity's admin
    gateway and job store, and the Logfire resource (best-effort), so unlike the `run` factory this
    needs no reader/embedding overrides, only the admin-side ones the identity announce reads.
    """

    def build() -> object:
        from studio.knowledge_base.container import create_container

        container = create_container(
            _rca_settings(tmp_path, read_token=read_token, queries_path=queries_path),
            knowledge_base=KnowledgeBaseSettings(path=_knowledge_base_root(tmp_path)),
        )
        container.override.set(target=AdminSqlGateway, new=FakeSqlGateway())
        container.override.set(target=JobQueries, new=MemoryJobs())
        return container

    return build


def _write_rca_queries(tmp_path: Path) -> Path:
    """Write the saved-SQL file to the default `rca_queries_path` location under `tmp_path`."""
    path = tmp_path / "evals" / "rca" / "queries.sql"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_RCA_QUERIES_SQL, encoding="utf-8")
    return path


class TestRcaCli:
    def test_rca_help_lists_the_flags(self):
        result = runner.invoke(app, ["evals", "rca", "--help"])

        assert result.exit_code == 0
        for flag in ("--query", "--focus", "--prompt"):
            assert flag in result.stdout

    def test_prompt_flag_prints_the_rca_prompt(self, tmp_path: Path):
        result = runner.invoke(app, ["evals", "rca", "--prompt"], obj=_rca_factory(tmp_path))

        assert result.exit_code == 0, result.stdout
        # Key instruction text from build_rca_prompt — the two D10 questions and the MCP grounding.
        assert "root-cause-analysis assistant" in result.stdout
        assert "Logfire MCP" in result.stdout
        assert "recall@5 == 0" in result.stdout

    def test_prompt_flag_weaves_in_the_focus(self, tmp_path: Path):
        result = runner.invoke(app, ["evals", "rca", "--prompt", "--focus", "the lightrag run"], obj=_rca_factory(tmp_path))

        assert result.exit_code == 0, result.stdout
        assert "the lightrag run" in result.stdout

    def test_empty_token_degrades_to_prompt_plus_saved_query_names(self, tmp_path: Path):
        _write_rca_queries(tmp_path)

        result = runner.invoke(app, ["evals", "rca"], obj=_rca_factory(tmp_path, read_token=""))

        assert result.exit_code == 0, result.stdout
        # Degraded, MCP-driven path: the prompt is emitted and the loaded query names are surfaced.
        assert "root-cause-analysis assistant" in result.stdout
        assert "slowest_tool_spans" in result.stdout
        assert "lowest_recall_cases" in result.stdout
        # No live report ran — the rendered markdown report header must be absent (no network attempted).
        assert "# RCA report" not in result.stdout

    def test_empty_token_with_query_filter_lists_only_selected_names(self, tmp_path: Path):
        _write_rca_queries(tmp_path)

        result = runner.invoke(app, ["evals", "rca", "--query", "lowest_recall_cases"], obj=_rca_factory(tmp_path, read_token=""))

        assert result.exit_code == 0, result.stdout
        assert "lowest_recall_cases" in result.stdout

    def test_missing_saved_query_file_degrades_with_a_clear_message(self, tmp_path: Path):
        absent = tmp_path / "evals" / "rca" / "does-not-exist.sql"

        result = runner.invoke(app, ["evals", "rca"], obj=_rca_factory(tmp_path, queries_path=absent))

        # A missing saved-SQL file is a degrade, not a crash: clean exit, the prompt still emitted.
        assert result.exit_code == 0, result.stdout
        assert str(absent) in result.stdout
        assert "root-cause-analysis assistant" in result.stdout


def _resolver_container(tmp_path: Path) -> ContainerResolver:
    """The `_factory` container wrapped in the typed `ContainerResolver` port.

    `_factory`'s `build()` deliberately returns `object` (the `ctx.obj` contract); helper-level tests
    call `_default_run_instructions` directly, so they hand it the same `Resolver` adapter the
    commands build. Building it also materialises the knowledge base's `Memory/` + `_meta/` directories the
    active-policy file lives in.
    """
    from studio.knowledge_base.container import create_container

    container = create_container(_settings(tmp_path), knowledge_base=KnowledgeBaseSettings(path=_knowledge_base_root(tmp_path)))
    container.override.set(target=ReaderSqlGateway, new=FakeSqlGateway())
    container.override.set(target=BatchEmbedder, new=FakeEmbedding())
    return ContainerResolver(container)


class TestRunDefaultInstructions:
    """`evals run` defaults its instructions from the knowledge base's active policy.

    The helper `_default_run_instructions` is the seam: an active policy's `instructions` win over
    the shipped `DEFAULT_INSTRUCTIONS`, an absent file keeps the shipped default, and a malformed
    file exits cleanly via `_fail`, mirroring `studio mcp`.
    """

    async def test_active_policy_instructions_become_the_run_default(self, tmp_path: Path):
        container = _resolver_container(tmp_path)  # materialises the knowledge base's _meta/ the policy file lives in
        save_policy(
            baseline_policy().model_copy(update={"instructions": "TUNED INSTRUCTIONS"}),
            tmp_path / "knowledge_base" / "_meta" / "active-policy.yaml",
        )

        resolved = await _default_run_instructions(container)

        assert resolved == "TUNED INSTRUCTIONS"

    async def test_without_an_active_policy_the_shipped_default_applies(self, tmp_path: Path):
        container = _resolver_container(tmp_path)

        resolved = await _default_run_instructions(container)

        assert resolved == DEFAULT_INSTRUCTIONS

    async def test_a_malformed_active_policy_fails_cleanly(self, tmp_path: Path):
        container = _resolver_container(tmp_path)
        (tmp_path / "knowledge_base" / "_meta" / "active-policy.yaml").write_text(
            "strategy: not-a-real-strategy\ninstructions: i\n", encoding="utf-8"
        )

        with pytest.raises(typer.Exit):
            await _default_run_instructions(container)


class _RollbackFactory:
    """Zero-arg `ctx.obj` factory around the REAL rollback handler in a minimal routed bus.

    The rollback handler is dependency-free filesystem arithmetic, so the CLI spec drives the real
    thing (mirroring `_FreezeFactory`'s container shape) without building the wireup graph.
    """

    def __init__(self) -> None:
        self._bus = MessageBus({RollbackPolicy: RollbackPolicyHandler()}, {})

    def __call__(self) -> _FreezeContainer:
        return _FreezeContainer(self._bus)


class TestRollbackPolicyCli:
    """`evals rollback-policy` + the tune runtime flags."""

    def test_tune_help_lists_the_runtime_flags(self):
        result = runner.invoke(app, ["evals", "tune", "--help"])

        assert result.exit_code == 0
        for flag in ("--apply-runtime", "--knowledge-base"):
            assert flag in result.stdout

    def test_rollback_policy_help_lists_knowledge_base(self):
        result = runner.invoke(app, ["evals", "rollback-policy", "--help"])

        assert result.exit_code == 0
        assert "--knowledge-base" in result.stdout

    def test_rollback_restores_the_previous_policy(self, tmp_path: Path):
        knowledge_base = tmp_path / "pkb"
        apply_retrieval_policy(baseline_policy().model_copy(update={"instructions": "A"}), knowledge_base_path=knowledge_base)
        apply_retrieval_policy(baseline_policy().model_copy(update={"instructions": "B"}), knowledge_base_path=knowledge_base)
        (knowledge_base / "Units").mkdir(parents=True, exist_ok=True)
        (knowledge_base / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")

        result = runner.invoke(app, ["evals", "rollback-policy", "--knowledge-base", str(knowledge_base)], obj=_RollbackFactory())

        assert result.exit_code == 0, result.stdout
        assert "restored" in result.stdout
        assert load_policy(knowledge_base / "_meta" / "active-policy.yaml").instructions == "A"

    def test_rollback_without_history_exits_cleanly(self, tmp_path: Path):
        empty = tmp_path / "empty"
        (empty / "Units").mkdir(parents=True)
        (empty / "_meta").mkdir(parents=True)
        (empty / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")

        result = runner.invoke(app, ["evals", "rollback-policy", "--knowledge-base", str(empty)], obj=_RollbackFactory())

        assert result.exit_code == 1
        assert "no policy history" in result.stdout

    def test_rollback_announces_the_flag_knowledge_base(self, tmp_path: Path):
        knowledge_base = tmp_path / "alpha"
        apply_retrieval_policy(baseline_policy().model_copy(update={"instructions": "A"}), knowledge_base_path=knowledge_base)
        apply_retrieval_policy(baseline_policy().model_copy(update={"instructions": "B"}), knowledge_base_path=knowledge_base)
        (knowledge_base / "Units").mkdir(parents=True, exist_ok=True)
        # slug "alpha" (not the research template's own) so the announced identity is this test's,
        # proving the banner reads the flag knowledge base's own spec, not some other one.
        (knowledge_base / "knowledge-base-schema.json").write_text(
            research_spec().model_copy(update={"slug": "alpha"}).model_dump_json(), encoding="utf-8"
        )

        result = runner.invoke(app, ["evals", "rollback-policy", "--knowledge-base", str(knowledge_base)], obj=_RollbackFactory())

        assert result.exit_code == 0, result.stdout
        assert result.stdout.splitlines()[0] == f"knowledge base alpha at {knowledge_base}"


class TestImportJudgmentsCli:
    """`evals import-judgments` turns a relevance judgments file into an evals dataset, with no knowledge base."""

    def test_it_writes_the_dataset_and_prints_the_counts(self, tmp_path: Path):
        source = tmp_path / "judgments.test.jsonl"
        source.write_text(
            '{"query_id": "q1", "question": "Q?", "judgments": {"d1": 1}}\n{"query_id": "q2", "question": "R?", "judgments": {"d2": 0}}\n',
            encoding="utf-8",
        )

        result = runner.invoke(app, ["evals", "import-judgments", "--file", str(source), "--out", str(tmp_path / "set.yaml")])

        assert result.exit_code == 0, result.stdout
        assert "1 case(s)" in result.stdout
        assert "1 skipped" in result.stdout
        assert (tmp_path / "set.yaml").is_file()

    def test_it_samples_per_query_type_and_prints_the_count_of_each(self, tmp_path: Path):
        source = tmp_path / "judgments.jsonl"
        lines = [
            '{"query_id": "q1", "question": "Q?", "judgments": {"d1": 1}, "answer": "Yes", "query_type": "alpha"}',
            '{"query_id": "q2", "question": "R?", "judgments": {"d1": 1}, "answer": "No", "query_type": "alpha"}',
            '{"query_id": "q3", "question": "S?", "judgments": {}, "answer": "no answer", "query_type": "beta"}',
            '{"query_id": "q4", "question": "T?", "judgments": {}, "query_type": "beta"}',
        ]
        source.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = runner.invoke(
            app, ["evals", "import-judgments", "--file", str(source), "--out", str(tmp_path / "set.yaml"), "--per-type", "1", "--salt", "s"]
        )

        assert result.exit_code == 0, result.stdout
        assert "Imported 2 case(s), 1 skipped" in result.stdout
        assert "  alpha: 1 case(s)\n" in result.stdout
        assert "  beta: 1 case(s)\n" in result.stdout
        assert "no query type" not in result.stdout

    def test_it_prints_the_count_of_the_cases_with_no_query_type_when_there_are_any(self, tmp_path: Path):
        source = tmp_path / "judgments.jsonl"
        lines = [
            '{"query_id": "q1", "question": "Q?", "judgments": {"d1": 1}, "query_type": "alpha"}',
            '{"query_id": "q2", "question": "R?", "judgments": {"d1": 1}}',
            '{"query_id": "q3", "question": "S?", "judgments": {"d1": 1}}',
        ]
        source.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = runner.invoke(app, ["evals", "import-judgments", "--file", str(source), "--out", str(tmp_path / "set.yaml")])

        assert result.exit_code == 0, result.stdout
        assert "  alpha: 1 case(s)\n  no query type: 2 case(s)\n" in result.stdout

    def test_a_missing_file_fails_cleanly(self, tmp_path: Path):
        result = runner.invoke(app, ["evals", "import-judgments", "--file", str(tmp_path / "missing.jsonl"), "--out", str(tmp_path / "set.yaml")])

        assert result.exit_code == 1
        assert "evals import-judgments failed" in result.stdout


def _write_results_file(path: Path, *, strict: tuple[bool, ...], model: str = "test-model") -> None:
    header = {
        "strategy": "search-hybrid",
        "sql": False,
        "model": model,
        "instructions_sha256": "a" * 64,
        "max_tool_calls": 8,
        "pinned_search": True,
        "dataset": "sample",
        "dataset_sha256": "b" * 64,
        "collection": "notes",
        "knowledge_base_fingerprint": "hash:notes",
        "started": "2026-10-13T09:00:00+00:00",
        "source_lookup_error": None,
    }
    lines = [
        {
            "case": f"c{index}",
            "query_type": "alpha",
            "strict": correct,
            "official": correct,
            "failed": False,
            "error": None,
            "answer": "yes",
            "cited": [],
            "sources": None,
            "recall@4": None,
            "recall@10": None,
            "mrr": None,
            "other_collection": False,
        }
        for index, correct in enumerate(strict)
    ]
    path.write_text("".join(f"{json.dumps(row)}\n" for row in [{"header": header}, *lines]), encoding="utf-8")


class TestCompareCli:
    """`evals compare` compares two runs of one dataset case by case from their results files, with no knowledge base."""

    def test_compare_help_lists_the_flags_and_the_comma_between_the_two_paths(self):
        result = runner.invoke(app, ["evals", "compare", "--help"])

        assert result.exit_code == 0
        assert "--pair" in result.stdout
        assert "BASE,CANDIDATE" in result.stdout
        assert "separated by a comma" in " ".join(result.stdout.split())
        assert "--out" in result.stdout

    def test_it_compares_every_pair_prints_each_and_writes_the_report_and_its_yaml_twin(self, tmp_path: Path):
        _write_results_file(tmp_path / "a.jsonl", strict=(True, False, False, False))
        _write_results_file(tmp_path / "b.jsonl", strict=(True, True, True, False))
        _write_results_file(tmp_path / "c.jsonl", strict=(True, True, True, False))
        out = tmp_path / "reports" / "compare.md"
        arguments = ["evals", "compare", "--pair", f"{tmp_path / 'a.jsonl'},{tmp_path / 'b.jsonl'}"]
        arguments += ["--pair", f"{tmp_path / 'b.jsonl'},{tmp_path / 'c.jsonl'}", "--out", str(out)]

        result = runner.invoke(app, arguments)

        assert result.exit_code == 0, result.stdout
        assert (
            f"{tmp_path / 'b.jsonl'} against {tmp_path / 'a.jsonl'}: strict 25.0% -> 75.0% over 4 case(s), "
            "difference +50.0 points (95% interval -6.6 to +106.6), McNemar p 0.5000, Holm p 1.0000\n"
        ) in result.stdout
        assert f"{tmp_path / 'c.jsonl'} against {tmp_path / 'b.jsonl'}: strict 75.0% -> 75.0%" in result.stdout
        assert f"Report -> {out}, YAML twin -> {out.with_suffix('.yaml')}\n" in result.stdout
        assert out.is_file()
        assert out.with_suffix(".yaml").is_file()

    def test_a_refused_pair_exits_one_naming_the_field_and_writes_nothing(self, tmp_path: Path):
        _write_results_file(tmp_path / "a.jsonl", strict=(True, False))
        _write_results_file(tmp_path / "b.jsonl", strict=(True, True), model="other-model")
        out = tmp_path / "compare.md"

        result = runner.invoke(app, ["evals", "compare", "--pair", f"{tmp_path / 'a.jsonl'},{tmp_path / 'b.jsonl'}", "--out", str(out)])

        assert result.exit_code == 1
        assert "evals compare failed" in result.stdout
        assert "differ in model" in result.stdout
        assert not out.exists()

    @pytest.mark.parametrize("value", ["a.jsonl", "a.jsonl,b.jsonl,c.jsonl", "a.jsonl,", ",b.jsonl"])
    def test_a_pair_that_is_not_two_comma_separated_paths_is_a_usage_error(self, tmp_path: Path, value: str):
        out = tmp_path / "compare.md"

        result = runner.invoke(app, ["evals", "compare", "--pair", value, "--out", str(out)])

        assert result.exit_code == 2
        assert "exactly two paths" in " ".join(result.stderr.replace("│", " ").split())
        assert not out.exists()

    def test_an_out_that_is_not_markdown_is_a_usage_error_before_any_results_file_is_read(self, tmp_path: Path):
        out = tmp_path / "compare.yaml"

        result = runner.invoke(app, ["evals", "compare", "--pair", f"{tmp_path / 'missing-a.jsonl'},{tmp_path / 'missing-b.jsonl'}", "--out", str(out)])

        assert result.exit_code == 2
        assert "--out must end in .md" in " ".join(result.stderr.replace("│", " ").split())
        assert not out.exists()


class TestScoreCli:
    def test_score_help_lists_the_flags(self):
        result = runner.invoke(app, ["evals", "score", "--help"])

        assert result.exit_code == 0
        for flag in ("--dataset", "--collection", "--setup", "--out", "--knowledge-base"):
            assert flag in result.stdout

    def test_it_scores_under_hybrid_when_no_setup_is_named(self, tmp_path: Path):
        handler = RecordingCommandHandler(result=self._report(tmp_path, unmappable_gold=0, cases_without_gold=0))

        result = self._invoke(tmp_path, handler)

        assert result.exit_code == 0, result.stdout
        assert [command.setup for command in handler.handled if isinstance(command, ScoreDataset)] == ["hybrid"]

    def test_it_scores_under_the_named_setup_and_prints_it(self, tmp_path: Path):
        report = self._report(tmp_path, unmappable_gold=0, cases_without_gold=0).model_copy(update={"setup": "keyword"})
        handler = RecordingCommandHandler(result=report)

        result = self._invoke(tmp_path, handler, "--setup", "keyword")

        assert result.exit_code == 0, result.stdout
        assert [command.setup for command in handler.handled if isinstance(command, ScoreDataset)] == ["keyword"]
        assert "Score [research] set.yaml under keyword: 3 case(s)" in result.stdout

    def test_an_unknown_setup_fails_naming_every_setup_before_it_reads_the_knowledge_base(self, tmp_path: Path):
        handler = RecordingCommandHandler(result=self._report(tmp_path, unmappable_gold=0, cases_without_gold=0))

        result = self._invoke(tmp_path, handler, "--setup", "dense")

        assert result.exit_code == 1
        assert result.stdout == (
            "evals score failed: unknown search setup 'dense', "
            "choose one of hybrid, keyword, hybrid-raw, keyword-raw, hybrid-card, hybrid-section, hybrid-claim\n"
        )
        assert handler.handled == []

    @pytest.mark.parametrize(("unmappable_gold", "cases_without_gold"), [(2, 1), (2, 0), (0, 1)])
    def test_it_prints_both_gold_counts_when_either_is_above_zero(self, tmp_path: Path, unmappable_gold: int, cases_without_gold: int):
        result = self._scored(tmp_path, unmappable_gold=unmappable_gold, cases_without_gold=cases_without_gold)

        assert result.exit_code == 0, result.stdout
        assert "Score [research] set.yaml under hybrid: 3 case(s)" in result.stdout
        assert f"  gold ids that name a page with no source document: {unmappable_gold}\n" in result.stdout
        assert f"  cases left out because no gold id names a source document: {cases_without_gold}\n" in result.stdout

    def test_it_prints_no_gold_lines_when_every_gold_id_named_a_source(self, tmp_path: Path):
        result = self._scored(tmp_path, unmappable_gold=0, cases_without_gold=0)

        assert result.exit_code == 0, result.stdout
        assert "gold ids that name a page" not in result.stdout
        assert "cases left out" not in result.stdout

    @classmethod
    def _scored(cls, tmp_path: Path, *, unmappable_gold: int, cases_without_gold: int) -> Result:
        report = cls._report(tmp_path, unmappable_gold=unmappable_gold, cases_without_gold=cases_without_gold)
        return cls._invoke(tmp_path, RecordingCommandHandler(result=report))

    @staticmethod
    def _report(tmp_path: Path, *, unmappable_gold: int, cases_without_gold: int) -> DatasetScored:
        return DatasetScored(
            dataset="set.yaml",
            collection="research",
            setup="hybrid",
            cases=3,
            means={"recall@1": 0.5},
            unmapped_hits=0,
            unmappable_gold=unmappable_gold,
            cases_without_gold=cases_without_gold,
            out=str(tmp_path / "scores" / "set.score.yaml"),
            markdown=str(tmp_path / "scores" / "set.score.md"),
        )

    @staticmethod
    def _invoke(tmp_path: Path, handler: RecordingCommandHandler, *extra: str) -> Result:
        knowledge_base = tmp_path / "research"
        (knowledge_base / "Units").mkdir(parents=True)
        (knowledge_base / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
        bus = MessageBus({ScoreDataset: handler}, {})
        arguments = ["evals", "score", "--dataset", "set.yaml", "--out", str(tmp_path / "scores"), "--knowledge-base", str(knowledge_base)]
        return runner.invoke(app, [*arguments, *extra], obj=lambda: _FreezeContainer(bus))


class _RefusingHandler:
    """A command handler that refuses every command with the scripted message."""

    def __init__(self, message: str) -> None:
        self.events: list[Event] = []
        self.message = message

    async def __call__(self, command: Command) -> object:
        del command
        raise ValueError(self.message)


_DESCENDING = {
    "hybrid": 0.7,
    "keyword": 0.6,
    "hybrid-raw": 0.5,
    "keyword-raw": 0.4,
    "hybrid-card": 0.3,
    "hybrid-section": 0.2,
    "hybrid-claim": 0.1,
}


class TestCheckGeneratorCli:
    def test_check_generator_help_lists_the_flags(self):
        result = runner.invoke(app, ["evals", "check-generator", "--help"])

        assert result.exit_code == 0
        for flag in ("--human", "--generated", "--collection", "--out", "--knowledge-base"):
            assert flag in result.stdout
        assert f"nDCG@10 rounded to {RANK_DECIMALS} decimals" in " ".join(result.stdout.split())

    def test_it_sends_both_datasets_the_collection_and_the_out_directory(self, tmp_path: Path):
        handler = RecordingCommandHandler(result=self._report(tmp_path, _DESCENDING))

        result = self._invoke(tmp_path, handler)

        assert result.exit_code == 0, result.stdout
        assert handler.handled == [
            CheckGenerator(human="human.yaml", generated="generated.yaml", collection="research", out=str(tmp_path / "scores"))
        ]

    def test_a_passing_check_prints_every_setup_tau_b_the_pairs_and_the_files(self, tmp_path: Path):
        result = self._invoke(tmp_path, RecordingCommandHandler(result=self._report(tmp_path, _DESCENDING)))

        assert result.exit_code == 0, result.stdout
        assert "Generator check [research] generated.yaml against human.yaml: passed\n" in result.stdout
        assert "  hybrid: human 0.7000 (rank 1), generated 0.7000 (rank 1)\n" in result.stdout
        assert "  hybrid-claim: human 0.1000 (rank 7), generated 0.1000 (rank 7)\n" in result.stdout
        assert "  Kendall's tau-b: 1.0000\n" in result.stdout
        assert "  setup pairs both datasets order the same way: 21 of 21\n" in result.stdout
        assert str(tmp_path / "scores" / "generated.generator-check.yaml") in result.stdout

    def test_a_failed_check_is_printed_as_failed_and_still_exits_zero(self, tmp_path: Path):
        swapped = {**_DESCENDING, "hybrid": 0.6, "keyword": 0.7}

        result = self._invoke(tmp_path, RecordingCommandHandler(result=self._report(tmp_path, swapped)))

        assert result.exit_code == 0, result.stdout
        assert "Generator check [research] generated.yaml against human.yaml: failed\n" in result.stdout
        assert "  hybrid: human 0.7000 (rank 1), generated 0.6000 (rank 2)\n" in result.stdout
        assert "  setup pairs both datasets order the same way: 20 of 21\n" in result.stdout

    def test_a_refused_check_exits_one_with_the_refusal(self, tmp_path: Path):
        result = self._invoke(tmp_path, _RefusingHandler("the generated dataset gives every search setup the same nDCG@10"))

        assert result.exit_code == 1
        assert "evals check-generator failed: the generated dataset gives every search setup the same nDCG@10" in result.stdout

    @staticmethod
    def _report(tmp_path: Path, generated: dict[str, float]) -> GeneratorChecked:
        return GeneratorChecked(
            human="human.yaml",
            generated="generated.yaml",
            collection="research",
            check=generator_check(_DESCENDING, generated),
            out=str(tmp_path / "scores" / "generated.generator-check.yaml"),
            markdown=str(tmp_path / "scores" / "generated.generator-check.md"),
        )

    @staticmethod
    def _invoke(tmp_path: Path, handler: RecordingCommandHandler | _RefusingHandler) -> Result:
        knowledge_base = tmp_path / "research"
        (knowledge_base / "Units").mkdir(parents=True)
        (knowledge_base / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
        bus = MessageBus({CheckGenerator: handler}, {})
        arguments = [
            "evals",
            "check-generator",
            "--human",
            "human.yaml",
            "--generated",
            "generated.yaml",
            "--out",
            str(tmp_path / "scores"),
            "--knowledge-base",
            str(knowledge_base),
        ]
        return runner.invoke(app, arguments, obj=lambda: _FreezeContainer(bus))
