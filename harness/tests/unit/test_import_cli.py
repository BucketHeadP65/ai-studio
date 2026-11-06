from pathlib import Path

from core.ai.workflows.contracts import StepVersionChangedError
from typer.testing import CliRunner

from harness.cli import app
from harness.features.browsing.contracts import FolderImport, SkippedWorkflowFile

WORKFLOW_FILES = Path(__file__).resolve().parents[2] / "workflows"


def test_import_prints_every_id_steps_first_then_each_skipped_file_with_why_it_was_skipped(monkeypatch) -> None:
    skipped = SkippedWorkflowFile(
        file=WORKFLOW_FILES / "workflows" / "source-remove.md", version=3, reason="moved_past", newer_id="wf.source-remove.v4", newer_version=4
    )
    archived = SkippedWorkflowFile(file=WORKFLOW_FILES / "workflows" / "source-remove.md", version=5, reason="archived", newer_id=None, newer_version=None)

    async def fake_import_path(path: Path, *, by: str) -> FolderImport:
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


def test_an_import_refusal_prints_as_one_line_with_its_notes_and_exits_1(monkeypatch) -> None:
    refusal = StepVersionChangedError("st.find_undigested.v1")
    refusal.add_note("The step file is workflows/steps/find-undigested.md.")

    async def refusing_import_path(path: Path, *, by: str) -> FolderImport:
        del path, by
        raise refusal

    import harness.cli as cli_module

    monkeypatch.setattr(cli_module, "import_path_in_session", refusing_import_path)
    result = CliRunner().invoke(app, ["import", str(WORKFLOW_FILES)])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit), "no traceback"
    assert result.output.splitlines() == [
        (
            "st.find_undigested.v1 already holds different content. Raise version in the step file and import again. "
            "The step file is workflows/steps/find-undigested.md."
        )
    ]
