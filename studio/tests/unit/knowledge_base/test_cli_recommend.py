r"""Unit tests for the `studio evals recommend` CLI command (`src/studio/knowledge_base/cli_evals.py`).

Drives the command through :class:`typer.testing.CliRunner` with a container *factory* injected
through `ctx.obj` (no monkeypatching), exactly as `test_cli_evals.py` stubs the
gen-dataset bus dispatch with `_GenFactory` / `_GenContainer`. The fake container serves the
pre-routed `MessageBus` and the `EvalsSettings` the default `--out` resolution reads, and the fake
command handler records the dispatched `RecommendStructure` and returns a scripted `RecommendationReport`,
so the CLI is exercised with no database, model, or network.

`--simulate` is a plain pass-through onto the command (the shadow split runs inside the real
handler), so the CLI specs assert the flag lands on the dispatched `RecommendStructure` and that the
printed summary carries the report's simulated-proposal count only when the flag was given.
"""

from __future__ import annotations

import asyncio
import json

from typing import TYPE_CHECKING, Literal

import pytest

from core.cqrs.contracts import JobQueries, MemoryJobs, MessageBus
from engram.evals.commands import RecommendationReport, RecommendStructure
from engram.indexing.contracts import KnowledgeBaseReader
from engram.testing.fakes.gateway import FakeSqlGateway
from engram.testing.fakes.knowledge_base_reader import FakeKnowledgeBaseReader
from typer.testing import CliRunner

from studio.knowledge_base.cli import app
from studio.knowledge_base.cli_evals import _build_recommend_command
from studio.knowledge_base.settings import EvalsSettings, RecommendationSettings
from studio.knowledge_base.wiring.contracts import AdminSqlGateway

if TYPE_CHECKING:
    from pathlib import Path

    from core.cqrs.contracts import Command, Event

runner = CliRunner()


@pytest.fixture(autouse=True)
def _ambient_knowledge_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this file runs inside a real knowledge base by cwd, the second of the two ways a command finds one."""
    root = tmp_path / "ambient-knowledge-base"
    root.mkdir()
    (root / "knowledge-base-schema.json").write_text(json.dumps({"slug": "ambient", "page_types": [{"page_type": "note"}]}), encoding="utf-8")
    monkeypatch.chdir(root)


def _recommendation_report(
    out: str,
    *,
    adr: str | None = None,
    evidence_grade: Literal["residual-of-tuned", "raw-baseline"] = "raw-baseline",
    proposals: tuple[str, ...] = ("add-relation-cites-on-paper", "restructure-sections-on-paper"),
    n_findings: int = 1,
    n_simulated: int = 0,
) -> RecommendationReport:
    """A successful recommendation report the recording handler returns."""
    return RecommendationReport(
        recommendation_path=out,
        adr_path=adr if adr is not None else out.replace(".yaml", ".adr.md"),
        collection="research",
        dataset="seed.yaml",
        evidence_grade=evidence_grade,
        n_proposals=len(proposals),
        n_findings=n_findings,
        n_simulated=n_simulated,
        top_proposals=proposals,
        run_id="run-1",
        duration_seconds=0.1,
    )


class _RecordingRecommendHandler:
    """Records dispatched commands and returns the scripted report."""

    def __init__(self, result: RecommendationReport) -> None:
        self.events: list[Event] = []
        self.result = result
        self.handled: list[Command] = []

    async def __call__(self, command: Command) -> object:
        self.handled.append(command)
        return self.result


class _RecommendContainer:
    """Fake async DI container backing the recommend bus dispatch.

    The command resolves the identity's admin gateway and job store, `EvalsSettings` for the
    default `--out` path (only when `--out` is omitted), and then `MessageBus` for the dispatch, so
    this fake serves those four and refuses anything else.
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
        msg = f"_RecommendContainer has no registration for {key!r}"
        raise KeyError(msg)


class _RecommendFactory:
    """Zero-arg `ctx.obj` factory returning a container around a recording recommend handler."""

    def __init__(self, report: RecommendationReport, recommendations_dir: Path) -> None:
        self.handler = _RecordingRecommendHandler(result=report)
        self._bus = MessageBus({RecommendStructure: self.handler}, {})
        self._settings = EvalsSettings(recommendation=RecommendationSettings(recommendations_dir=recommendations_dir))

    def __call__(self) -> _RecommendContainer:
        return _RecommendContainer(self._bus, self._settings)


class _RecommendSettingsResolver:
    """Minimal resolver backing `_build_recommend_command`'s default `--out` resolution.

    The command-assembly helper resolves only `EvalsSettings` (for the recommendations default path), so this
    fake returns the tmp-dir settings and refuses anything else loudly (mirrors `_TuneSettingsResolver`).
    """

    def __init__(self, settings: EvalsSettings) -> None:
        self._settings = settings

    async def get[T](self, klass: type[T]) -> T:
        services: dict[type, object] = {EvalsSettings: self._settings}
        service = services.get(klass)
        if not isinstance(service, klass):
            msg = f"_RecommendSettingsResolver has no registration for {klass!r}"
            raise KeyError(msg)
        return service


class TestRecommendCommandAssembly:
    """`_build_recommend_command` assembles a `RecommendStructure` from the flags."""

    def test_flags_are_plumbed_to_the_command(self, tmp_path: Path):
        resolver = _RecommendSettingsResolver(EvalsSettings(datasets_dir=tmp_path / "datasets"))
        out = str(tmp_path / "recommendation.yaml")

        command = asyncio.run(
            _build_recommend_command(
                resolver, dataset="seed.yaml", model="gpt-4.1", collection="cooking", policy="pol.yaml", out=out, simulate=False
            )
        )

        assert isinstance(command, RecommendStructure)
        assert command.dataset == "seed.yaml"
        assert command.model == "gpt-4.1"
        assert command.collection == "cooking"
        assert command.policy == "pol.yaml"
        assert command.out == out
        assert command.simulate is False

    def test_default_out_lands_under_the_recommendations_dir(self, tmp_path: Path):
        resolver = _RecommendSettingsResolver(EvalsSettings(recommendation=RecommendationSettings(recommendations_dir=tmp_path / "recommendations")))

        command = asyncio.run(
            _build_recommend_command(
                resolver, dataset="seed.yaml", model="test-model", collection="research", policy=None, out=None, simulate=False
            )
        )

        assert command.out == str(tmp_path / "recommendations" / "research.yaml")
        assert command.policy is None
        assert command.simulate is False


class TestRecommendCli:
    """`evals recommend` dispatches a `RecommendStructure` through the bus and echoes the report."""

    def test_recommend_help_lists_the_flags(self):
        result = runner.invoke(app, ["evals", "recommend", "--help"])

        assert result.exit_code == 0
        for flag in ("--dataset", "--model", "--collection", "--policy", "--out", "--simulate"):
            assert flag in result.stdout

    def test_dispatches_and_prints_the_report_fields(self, tmp_path: Path):
        out = str(tmp_path / "research.yaml")
        factory = _RecommendFactory(_recommendation_report(out), tmp_path / "recommendations")

        result = runner.invoke(app, ["evals", "recommend", "--dataset", "seed.yaml", "--collection", "research", "--out", out], obj=factory)

        assert result.exit_code == 0, result.stdout
        command = factory.handler.handled[0]
        assert isinstance(command, RecommendStructure)
        assert command.dataset == "seed.yaml"
        assert command.collection == "research"
        assert command.out == out
        # The printed summary carries the recommendation path, the draft-ADR path, the evidence grade, the
        # proposal count, and the top proposal ids.
        assert out in result.stdout
        assert out.replace(".yaml", ".adr.md") in result.stdout
        assert "raw-baseline" in result.stdout
        assert "2 proposal(s)" in result.stdout
        assert "add-relation-cites-on-paper" in result.stdout

    def test_default_out_through_the_cli_uses_the_recommendations_dir(self, tmp_path: Path):
        recommendations_dir = tmp_path / "recommendations"
        expected = str(recommendations_dir / "research.yaml")
        factory = _RecommendFactory(_recommendation_report(expected), recommendations_dir)

        result = runner.invoke(app, ["evals", "recommend", "--dataset", "seed.yaml", "--collection", "research"], obj=factory)

        assert result.exit_code == 0, result.stdout
        command = factory.handler.handled[0]
        assert isinstance(command, RecommendStructure)
        assert command.out == expected

    def test_policy_is_forwarded_onto_the_command(self, tmp_path: Path):
        out = str(tmp_path / "research.yaml")
        factory = _RecommendFactory(_recommendation_report(out), tmp_path / "recommendations")

        result = runner.invoke(
            app, ["evals", "recommend", "--dataset", "seed.yaml", "--out", out, "--policy", "_meta/active-policy.yaml"], obj=factory
        )

        assert result.exit_code == 0, result.stdout
        command = factory.handler.handled[0]
        assert isinstance(command, RecommendStructure)
        assert command.policy == "_meta/active-policy.yaml"

    def test_simulate_is_plumbed_and_the_count_is_printed(self, tmp_path: Path):
        out = str(tmp_path / "research.yaml")
        factory = _RecommendFactory(_recommendation_report(out, n_simulated=1), tmp_path / "recommendations")

        result = runner.invoke(app, ["evals", "recommend", "--dataset", "seed.yaml", "--out", out, "--simulate"], obj=factory)

        assert result.exit_code == 0, result.stdout
        command = factory.handler.handled[0]
        assert isinstance(command, RecommendStructure)
        assert command.simulate is True
        assert "1 simulated" in result.stdout

    def test_the_simulated_count_is_not_printed_without_the_flag(self, tmp_path: Path):
        out = str(tmp_path / "research.yaml")
        factory = _RecommendFactory(_recommendation_report(out), tmp_path / "recommendations")

        result = runner.invoke(app, ["evals", "recommend", "--dataset", "seed.yaml", "--out", out], obj=factory)

        assert result.exit_code == 0, result.stdout
        # Without the flag the summary goes straight from the finding count to the proposal ids.
        assert "finding(s):" in result.stdout
        assert "0 simulated" not in result.stdout
