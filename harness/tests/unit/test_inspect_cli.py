"""`harness inspect` -- the exclusivity guard over `--run` and `--since`, exercised through `CliRunner`.

Both cases fail before the container or the database are ever reached, so neither points the app
at a real store: the guard itself is what is under test.
"""

from __future__ import annotations

from typer.testing import CliRunner

from harness.cli import app

runner = CliRunner()


def test_neither_flag_is_refused() -> None:
    result = runner.invoke(app, ["inspect"])

    assert result.exit_code == 1
    assert "give exactly one of --run and --since" in result.output


def test_both_flags_are_refused() -> None:
    result = runner.invoke(app, ["inspect", "--run", "some-run-id", "--since", "24"])

    assert result.exit_code == 1
    assert "give exactly one of --run and --since" in result.output
