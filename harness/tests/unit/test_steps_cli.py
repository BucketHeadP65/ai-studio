"""`harness steps lock <folder>` writes the folder's step lock file and refuses a changed step at an unchanged version."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.workflows.contracts import load_step_document, step_content_address
from typer.testing import CliRunner

from harness.cli import app
from harness.features.browsing.contracts import read_lock

if TYPE_CHECKING:
    from pathlib import Path

STEP = """---
title: Step A
summary: Says a.
version: 1
step:
  name: step_a
  source: |
    "a"
  output: {json_schema: {type: string}}
---

Use first.
"""


def _folder(tmp_path: Path, text: str) -> Path:
    (tmp_path / "steps").mkdir(exist_ok=True)
    (tmp_path / "steps" / "step-a.md").write_text(text, encoding="utf-8")
    return tmp_path


def test_steps_lock_writes_the_lock_and_prints_each_recorded_step(tmp_path: Path) -> None:
    folder = _folder(tmp_path, STEP)

    result = CliRunner().invoke(app, ["steps", "lock", str(folder)])

    assert result.exit_code == 0, result.output
    step_hash = step_content_address(load_step_document(STEP).step)
    assert result.output.splitlines() == [f"step_a v1 {step_hash}"]
    assert read_lock(folder).steps["step_a"].step_hash == step_hash


def test_steps_lock_exits_1_with_the_refusal_on_a_changed_step_at_an_unchanged_version(tmp_path: Path) -> None:
    folder = _folder(tmp_path, STEP)
    assert CliRunner().invoke(app, ["steps", "lock", str(folder)]).exit_code == 0
    held = (folder / "steps.lock").read_text(encoding="utf-8")
    _folder(tmp_path, STEP.replace('"a"', '"aa"'))

    result = CliRunner().invoke(app, ["steps", "lock", str(folder)])

    assert result.exit_code == 1, result.output
    assert "step_a" in result.output
    assert "Raise `version`" in result.output
    assert (folder / "steps.lock").read_text(encoding="utf-8") == held


def test_steps_lock_refuses_a_folder_with_no_steps_folder(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["steps", "lock", str(tmp_path)])

    assert result.exit_code == 1, result.output
    assert "steps/" in result.output
    assert not (tmp_path / "steps.lock").exists()
