from pathlib import Path

import pytest

from core.ai.workflows.contracts import DocumentError, PresentationRefusedError, SaveRefusedError, StepVersionChangedError
from core.ai.workflows.domain.validator import ValidationReport
from typer.testing import CliRunner

from harness.cli import LibraryAccess, LibraryOpener, app
from harness.features.browsing.contracts import FolderImport, SkippedWorkflowFile
from tests.support.flags import STUDIO_DOWN, studio_down

WORKFLOW_FILES = Path(__file__).resolve().parents[2] / "workflows"


def test_import_prints_every_id_steps_first_then_each_skipped_file_with_why_it_was_skipped(monkeypatch) -> None:
    skipped = SkippedWorkflowFile(
        file=WORKFLOW_FILES / "workflows" / "source-remove.md", version=3, reason="moved_past", newer_id="wf.source-remove.v4", newer_version=4
    )
    archived = SkippedWorkflowFile(file=WORKFLOW_FILES / "workflows" / "source-remove.md", version=5, reason="archived", newer_id=None, newer_version=None)

    async def fake_import_path(path: Path, *, by: str, open_library: LibraryOpener) -> FolderImport:
        del open_library
        assert path == WORKFLOW_FILES
        assert by == "owner:cli"
        return FolderImport(written=("st.find_undigested.v1", "wf.research-digest.v1"), skipped=(skipped, archived))

    import harness.cli as cli_module

    monkeypatch.setattr(cli_module, "import_path_in_session", fake_import_path)
    result = CliRunner().invoke(app, ["import", str(WORKFLOW_FILES)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "st.find_undigested.v1",
        "wf.research-digest.v1",
        (
            f"skipped {skipped.file} (version 3): step maintenance saved wf.source-remove.v4 in the library. "
            "Bring the file up to wf.source-remove.v4, its content and not only its version number."
        ),
        f"skipped {archived.file} (version 5): the library archived version 5, so it stays archived.",
    ]


def _noted(refusal: Exception, note: str) -> Exception:
    refusal.add_note(note)
    return refusal


@pytest.mark.parametrize(
    ("refusal", "line"),
    [
        (
            _noted(StepVersionChangedError("st.find_undigested.v1"), "The step file is workflows/steps/find-undigested.md."),
            (
                "st.find_undigested.v1 already holds different content. Raise version in the step file and import again. "
                "The step file is workflows/steps/find-undigested.md."
            ),
        ),
        (
            _noted(SaveRefusedError(ValidationReport(issues=()), ("quarter",)), "The workflow file is workflows/workflows/digest.md."),
            "refused to save an unexecutable workflow: parameters nothing supplies: quarter The workflow file is workflows/workflows/digest.md.",
        ),
        (
            _noted(PresentationRefusedError("presentation input 'quarter' names no parameter"), "The workflow file is workflows/workflows/digest.md."),
            "presentation input 'quarter' names no parameter The workflow file is workflows/workflows/digest.md.",
        ),
        (
            DocumentError("workflows/steps/broken.md: the file has no frontmatter"),
            "workflows/steps/broken.md: the file has no frontmatter",
        ),
    ],
    ids=["a library refusal", "an unrunnable workflow", "a presentation", "a document"],
)
def test_an_import_refusal_prints_as_one_line_with_its_notes_and_exits_1(monkeypatch, refusal: Exception, line: str) -> None:
    async def refusing_import_path(path: Path, *, by: str, open_library: LibraryOpener) -> FolderImport:
        del path, by, open_library
        raise refusal

    import harness.cli as cli_module

    monkeypatch.setattr(cli_module, "import_path_in_session", refusing_import_path)
    result = CliRunner().invoke(app, ["import", str(WORKFLOW_FILES)])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit), "no traceback"
    assert result.output.splitlines() == [line]


def test_a_library_that_does_not_open_prints_what_did_not_answer_in_one_line_and_exits_1() -> None:
    result = CliRunner().invoke(app, ["import", str(WORKFLOW_FILES)], obj=LibraryAccess(open_library=studio_down))

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit), "no traceback"
    assert result.output.splitlines() == [f"The workflow library did not open: {STUDIO_DOWN}"]
