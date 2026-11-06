from pathlib import Path

from typer.testing import CliRunner

from harness.cli import app
from harness.features.browsing.contracts import FolderImport, SkippedWorkflowFile

WORKFLOW_FILES = Path(__file__).resolve().parents[2] / "workflows"


def test_import_prints_every_id_steps_first_then_each_skipped_file_with_the_version_to_raise_it_to(monkeypatch) -> None:
    skipped = SkippedWorkflowFile(file=WORKFLOW_FILES / "workflows" / "source-remove.md", version=3, newer_version=4)

    async def fake_import_path(path: Path, *, by: str) -> FolderImport:
        assert path == WORKFLOW_FILES
        assert by == "owner:cli"
        return FolderImport(written=("st.find_undigested.v1", "wf.research-digest.v1"), skipped=(skipped,))

    import harness.cli as cli_module

    monkeypatch.setattr(cli_module, "import_path_in_session", fake_import_path)
    result = CliRunner().invoke(app, ["import", str(WORKFLOW_FILES)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "st.find_undigested.v1",
        "wf.research-digest.v1",
        f"skipped {skipped.file} (version 3): step maintenance saved version 4 in the library. Raise the file to version 4.",
    ]
