from pathlib import Path

from typer.testing import CliRunner

from harness.cli import app

WORKFLOW_FILES = Path(__file__).resolve().parents[2] / "workflows"


def test_import_prints_every_id_steps_first(tmp_path: Path, monkeypatch) -> None:
    written: list[str] = []

    async def fake_import_path(path: Path, *, by: str) -> tuple[str, ...]:
        assert path == WORKFLOW_FILES
        assert by == "owner:cli"
        ids = ("st.find_undigested.v1", "wf.research-digest.v1")
        written.extend(ids)
        return ids

    import harness.cli as cli_module

    monkeypatch.setattr(cli_module, "import_path_in_session", fake_import_path)
    result = CliRunner().invoke(app, ["import", str(WORKFLOW_FILES)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == list(written)
