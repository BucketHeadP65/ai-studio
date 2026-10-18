"""The reindex port `studio mcp`'s multi-knowledge_base `AuthoringTools` is wired with.

`_BusReindex` (this process's own container) is correct only for the session's own knowledge base: its
`KnowledgeBaseReader` is bound to that knowledge base's root alone. `SubprocessReindex` indexes any OTHER
knowledge base correctly by spawning a fresh `studio index --knowledge-base <path>` process, which resolves its
own root from the flag. `_MultiKnowledgeBaseReindex` routes a knowledge base to whichever of the two is correct
for it, mirroring `harness.adapters.knowledge_base.StudioReindex`'s injectable-runner shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from core.cqrs.contracts import MemoryOutbox, MessageBus
from engram.authoring.contracts import AuthoringKnowledgeBase, ReindexResult
from engram.indexing.contracts import IndexKnowledgeBase, IndexReport, ValidationIssue, ValidationReport
from engram.testing.specs import research_spec

from studio.knowledge_base.cli import _BusReindex, _MultiKnowledgeBaseReindex
from studio.knowledge_base.cli_support import _print_index_report
from studio.knowledge_base.reindex import INDEX_ERROR_TAIL, SubprocessReindex

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    import pytest

    from core.cqrs.contracts import Command, Event


class RecordingRunner:
    """Records every argv it is asked to run and answers with a scripted exit code and output tail."""

    def __init__(self, exit_code: int = 0, output: str = "") -> None:
        self.exit_code = exit_code
        self.output = output
        self.argv: list[Sequence[str]] = []

    async def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        self.argv.append(argv)
        return self.exit_code, self.output


@dataclass
class _RecordingReindex:
    """A `Reindex` fake recording every knowledge base it was asked to reindex, always answering clean."""

    calls: list[AuthoringKnowledgeBase] = field(default_factory=list)
    reply: str = "reindexed"

    async def __call__(self, knowledge_base: AuthoringKnowledgeBase) -> ReindexResult:
        self.calls.append(knowledge_base)
        return ReindexResult(outcome="clean", detail=self.reply)


def _target(name: str, path: Path) -> AuthoringKnowledgeBase:
    return AuthoringKnowledgeBase(name=name, collection=name, path=path, schema=research_spec())


_VALIDATION_TAIL = """Indexed in 1.2s: 10 pages, 5 sections, 3 claims, 2 edges
Pruned: 0 | Embedded: 5, reused: 5
Validation: 2 error(s), 0 warning(s)
  ERROR paper.one: missing-quote: claim quote absent from its mirror
  ERROR paper.two: missing-quote: claim quote absent from its mirror"""


class _ReportingHandler:
    """A command handler that always answers with the scripted `IndexReport`, raising no events."""

    def __init__(self, report: IndexReport) -> None:
        self.events: list[Event] = []
        self._report = report

    async def __call__(self, command: Command) -> object:
        del command
        return self._report


class _BusContainer:
    """A resolver that only ever hands back the one bus it was built with."""

    def __init__(self, bus: MessageBus) -> None:
        self._bus = bus

    async def get[T](self, klass: type[T]) -> T:
        if klass is MessageBus:
            return cast("T", self._bus)
        msg = f"_BusContainer has no registration for {klass!r}"
        raise KeyError(msg)


def _bus_reindex(report: IndexReport) -> _BusReindex:
    bus = MessageBus({IndexKnowledgeBase: _ReportingHandler(report)}, {}, outbox=MemoryOutbox())
    return _BusReindex(_BusContainer(bus))


class TestBusReindex:
    async def test_a_clean_report_reports_clean(self, tmp_path: Path):
        result = await _bus_reindex(IndexReport(pages=1))(_target("research", tmp_path))

        assert result.outcome == "clean"
        assert result.detail == f"reindexed {tmp_path}"
        assert result.findings == ()

    async def test_a_report_with_validation_errors_reports_validation_errors_read_off_the_report_itself(self, tmp_path: Path):
        validation = ValidationReport(
            errors=(
                ValidationIssue(severity="error", rule="missing-quote", page_id="paper.one", message="claim quote absent from its mirror"),
            )
        )
        report = IndexReport(pages=1, validation=validation)

        result = await _bus_reindex(report)(_target("research", tmp_path))

        assert result.outcome == "validation_errors"
        assert result.findings_total == 1
        assert result.findings[0].page_id == "paper.one"
        assert result.findings[0].rule == "missing-quote"
        assert "1 validation error(s)" in result.detail


class TestSubprocessReindex:
    async def test_a_clean_exit_reports_clean(self, tmp_path: Path):
        runner = RecordingRunner()

        result = await SubprocessReindex(run=runner)(_target("research", tmp_path))

        assert result.outcome == "clean"
        assert result.detail == f"reindexed {tmp_path}"
        (argv,) = runner.argv
        assert "index" in argv
        assert "--knowledge-base" in argv
        assert str(tmp_path) in argv

    async def test_a_failed_index_with_validation_findings_in_the_tail_reports_validation_errors(self, tmp_path: Path):
        runner = RecordingRunner(exit_code=1, output=_VALIDATION_TAIL)

        result = await SubprocessReindex(run=runner)(_target("research", tmp_path))

        assert result.outcome == "validation_errors"
        assert result.findings_total == 2
        assert [(f.page_id, f.rule) for f in result.findings] == [("paper.one", "missing-quote"), ("paper.two", "missing-quote")]
        assert "2 validation error(s)" in result.detail

    async def test_a_failed_index_with_no_parseable_findings_reports_not_indexed(self, tmp_path: Path):
        result = await SubprocessReindex(run=RecordingRunner(exit_code=2, output="IntegrityError: duplicate key"))(
            _target("research", tmp_path)
        )

        assert result.outcome == "not_indexed"
        assert "exit 2" in result.detail
        assert "duplicate key" in result.detail
        assert result.findings == ()

    async def test_a_failed_index_with_no_output_reports_not_indexed_with_the_bare_code(self, tmp_path: Path):
        result = await SubprocessReindex(run=RecordingRunner(exit_code=3))(_target("research", tmp_path))

        assert result.outcome == "not_indexed"
        assert result.detail == "studio index for research failed with exit 3"


def _issue(page_id: str, rule: str = "dangling-link") -> ValidationIssue:
    return ValidationIssue(severity="error", rule=rule, page_id=page_id, message="links to a page that does not exist")


def _printed_tail(report: IndexReport, capsys: pytest.CaptureFixture[str]) -> str:
    """What `SubprocessReindex` reads back from `studio index` for `report`: the printed summary's last characters."""
    _print_index_report(report)
    return capsys.readouterr().out.strip()[-INDEX_ERROR_TAIL:]


class TestSubprocessReindexAgreesWithTheBus:
    """`studio index` exits 1 on an error standing anywhere, and the subprocess reindex still reports only the run's own errors."""

    async def test_an_error_standing_on_an_unchanged_page_reports_clean(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        report = IndexReport(pages=1, mode="incremental", standing_validation=ValidationReport(errors=(_issue("ent.x"),)))
        runner = RecordingRunner(exit_code=1, output=_printed_tail(report, capsys))

        result = await SubprocessReindex(run=runner)(_target("research", tmp_path))

        assert result == await _bus_reindex(report)(_target("research", tmp_path))
        assert result.outcome == "clean"

    async def test_standing_errors_longer_than_the_tail_still_report_clean(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        standing = tuple(_issue(f"ent.page-{number}") for number in range(40))
        report = IndexReport(pages=1, mode="incremental", standing_validation=ValidationReport(errors=standing))
        runner = RecordingRunner(exit_code=1, output=_printed_tail(report, capsys))

        result = await SubprocessReindex(run=runner)(_target("research", tmp_path))

        assert result.outcome == "clean"

    async def test_a_run_error_beside_standing_errors_reports_only_the_run_error(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        own = _issue("paper.one", rule="missing-quote")
        report = IndexReport(
            pages=1,
            mode="incremental",
            validation=ValidationReport(errors=(own,)),
            standing_validation=ValidationReport(errors=(own, _issue("ent.x"))),
        )
        runner = RecordingRunner(exit_code=1, output=_printed_tail(report, capsys))

        result = await SubprocessReindex(run=runner)(_target("research", tmp_path))

        assert result == await _bus_reindex(report)(_target("research", tmp_path))
        assert [(finding.page_id, finding.rule) for finding in result.findings] == [("paper.one", "missing-quote")]


class TestMultiKnowledgeBaseReindex:
    async def test_the_named_own_target_reindexs_in_process(self, tmp_path: Path):
        own = _RecordingReindex(reply="own reply")
        other = _RecordingReindex(reply="other reply")
        reindex = _MultiKnowledgeBaseReindex("research", own, other)

        said = await reindex(_target("research", tmp_path))

        assert said.detail == "own reply"
        assert [knowledge_base.name for knowledge_base in own.calls] == ["research"]
        assert other.calls == []

    async def test_every_other_target_reindexs_the_other_way(self, tmp_path: Path):
        own = _RecordingReindex(reply="own reply")
        other = _RecordingReindex(reply="other reply")
        reindex = _MultiKnowledgeBaseReindex("research", own, other)

        said = await reindex(_target("repo-docs", tmp_path))

        assert said.detail == "other reply"
        assert [knowledge_base.name for knowledge_base in other.calls] == ["repo-docs"]
        assert own.calls == []
