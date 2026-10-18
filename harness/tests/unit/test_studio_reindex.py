"""`StudioReindex`: `studio index --knowledge-base <path>` through an injectable command runner."""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.authoring.contracts import AuthoringKnowledgeBase
from engram.testing.specs import research_spec

from harness.adapters.knowledge_base import StudioReindex

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class RecordingRunner:
    """Records every argv it is asked to run and answers with a scripted exit code and output tail."""

    def __init__(self, exit_code: int = 0, output: str = "") -> None:
        self.exit_code = exit_code
        self.output = output
        self.argv: list[Sequence[str]] = []

    async def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        self.argv.append(argv)
        return self.exit_code, self.output


_VALIDATION_TAIL = """Indexed in 1.2s: 10 pages, 5 sections, 3 claims, 2 edges
Pruned: 0 | Embedded: 5, reused: 5
Validation: 1 error(s), 0 warning(s)
  ERROR paper.one: missing-quote: claim quote absent from its mirror"""

_STANDING_ONLY_TAIL = """Indexed in 1.2s: 10 pages, 5 sections, 3 claims, 2 edges
Pruned: 0 | Embedded: 5, reused: 5
Standing in the knowledge base: 1 error(s), 0 warning(s)
  ERROR ent.x: dangling-link: links to a page that does not exist
Validation: 0 error(s), 0 warning(s)"""

_STANDING_AND_RUN_TAIL = """Indexed in 1.2s: 10 pages, 5 sections, 3 claims, 2 edges
Pruned: 0 | Embedded: 5, reused: 5
Standing in the knowledge base: 2 error(s), 0 warning(s)
  ERROR ent.x: dangling-link: links to a page that does not exist
Validation: 1 error(s), 0 warning(s)
  ERROR paper.one: missing-quote: claim quote absent from its mirror"""


async def test_reindex_runs_studio_index_on_the_target_path(tmp_path: Path) -> None:
    runner = RecordingRunner()
    knowledge_base = AuthoringKnowledgeBase(name="research", collection="research", path=tmp_path, schema=research_spec())

    result = await StudioReindex(run=runner)(knowledge_base)

    assert result.outcome == "clean"
    assert result.detail == f"reindexed {tmp_path}"
    argv = runner.argv[0]
    assert argv[:2] == ("uv", "run")
    assert "studio" in argv
    assert "index" in argv
    assert "--knowledge-base" in argv
    assert str(tmp_path) in argv


async def test_a_failed_index_with_validation_findings_in_the_tail_reports_validation_errors(tmp_path: Path) -> None:
    knowledge_base = AuthoringKnowledgeBase(name="research", collection="research", path=tmp_path, schema=research_spec())
    runner = RecordingRunner(exit_code=1, output=_VALIDATION_TAIL)

    result = await StudioReindex(run=runner)(knowledge_base)

    assert result.outcome == "validation_errors"
    assert result.findings_total == 1
    assert result.findings[0].page_id == "paper.one"
    assert result.findings[0].rule == "missing-quote"


async def test_an_exit_on_an_error_standing_on_an_unchanged_page_reports_clean(tmp_path: Path) -> None:
    knowledge_base = AuthoringKnowledgeBase(name="research", collection="research", path=tmp_path, schema=research_spec())
    runner = RecordingRunner(exit_code=1, output=_STANDING_ONLY_TAIL)

    result = await StudioReindex(run=runner)(knowledge_base)

    assert result.outcome == "clean"
    assert result.detail == f"reindexed {tmp_path}"


async def test_a_run_error_beside_a_standing_error_reports_only_the_run_error(tmp_path: Path) -> None:
    knowledge_base = AuthoringKnowledgeBase(name="research", collection="research", path=tmp_path, schema=research_spec())
    runner = RecordingRunner(exit_code=1, output=_STANDING_AND_RUN_TAIL)

    result = await StudioReindex(run=runner)(knowledge_base)

    assert result.outcome == "validation_errors"
    assert result.findings_total == 1
    assert [(finding.page_id, finding.rule) for finding in result.findings] == [("paper.one", "missing-quote")]


async def test_a_failed_index_with_no_parseable_findings_reports_not_indexed(tmp_path: Path) -> None:
    knowledge_base = AuthoringKnowledgeBase(name="research", collection="research", path=tmp_path, schema=research_spec())
    runner = RecordingRunner(exit_code=1, output="IntegrityError: update or delete on table violates foreign key")

    result = await StudioReindex(run=runner)(knowledge_base)

    assert result.outcome == "not_indexed"
    assert "exit 1" in result.detail
    assert "foreign key" in result.detail


async def test_a_failed_index_with_no_output_reports_not_indexed_with_the_bare_code(tmp_path: Path) -> None:
    knowledge_base = AuthoringKnowledgeBase(name="research", collection="research", path=tmp_path, schema=research_spec())

    result = await StudioReindex(run=RecordingRunner(exit_code=3))(knowledge_base)

    assert result.outcome == "not_indexed"
    assert result.detail == "studio index for research failed with exit 3"
