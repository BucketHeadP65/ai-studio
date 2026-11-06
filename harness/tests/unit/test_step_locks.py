"""The step lock file beside every `steps/` folder: it pins each step file's version and content address."""

from __future__ import annotations

import json
import os

from pathlib import Path

import pytest

from core.ai.workflows.contracts import DocumentError, load_step_document, step_content_address
from engram.indexing.service.row_builder import _jsonable_frontmatter_value

from harness.features.browsing.contracts import StepLock, StepLockEntry, StepLockRefusedError, locked_steps, read_lock, write_lock
from harness.features.browsing.step_locks import as_stored

REPO = Path(__file__).resolve().parents[3]

STEPS_FOLDERS = (REPO / "harness" / "workflows", *sorted(steps.parent for steps in REPO.glob("templates/*/workflows/steps")))
"""Every folder in the repository whose `steps/` holds reusable step files."""

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

EDITED = STEP.replace('"a"', '"aa"')

NULL_DEFAULT = STEP.replace("  output: {json_schema: {type: string}}", "  output: {json_schema: {type: string, default: null, enum: []}}")


def _folder(tmp_path: Path, text: str) -> Path:
    (tmp_path / "steps").mkdir(exist_ok=True)
    (tmp_path / "steps" / "step-a.md").write_text(text, encoding="utf-8")
    return tmp_path


def _stale(folder: Path) -> list[str]:
    """One line per step whose entry in the folder's lock differs from its step file, saying what to do."""
    held = read_lock(folder).steps
    current = locked_steps(folder).steps
    where = folder.relative_to(REPO)
    command = f"uv run harness steps lock {os.path.relpath(folder, REPO / 'harness')}"
    lines = [
        f"{where}/steps: step `{name}` is at version {entry.version}, and {where}/steps.lock "
        + (f"records version {held[name].version} with another content address." if name in held else "has no entry for it.")
        + f" If you changed the step, raise `version` in its step file. Then run `{command}` from harness/."
        for name, entry in sorted(current.items())
        if held.get(name) != entry
    ]
    lines += [
        f"{where}/steps.lock names `{name}`, which no step file in {where}/steps holds. Run `{command}` from harness/."
        for name in sorted(held.keys() - current.keys())
    ]
    return lines


def test_the_repository_s_steps_folders_are_the_ones_the_lock_test_reads() -> None:
    assert REPO / "harness" / "workflows" in STEPS_FOLDERS
    assert REPO / "templates" / "research" / "workflows" in STEPS_FOLDERS
    assert REPO / "templates" / "news-benchmark" / "workflows" not in STEPS_FOLDERS, "a folder with no steps/ has no lock"


@pytest.mark.parametrize("folder", STEPS_FOLDERS, ids=lambda folder: str(folder.relative_to(REPO)))
def test_every_step_file_matches_its_folder_s_step_lock(folder: Path) -> None:
    assert read_lock(folder) == locked_steps(folder), "\n".join(_stale(folder))


def test_the_lock_records_each_step_s_version_and_content_address(tmp_path: Path) -> None:
    folder = _folder(tmp_path, STEP)

    lock = write_lock(folder)

    expected = StepLock(steps={"step_a": StepLockEntry(version=1, step_hash=step_content_address(load_step_document(STEP).step))})
    assert lock == expected
    assert read_lock(folder) == expected
    assert json.loads((folder / "steps.lock").read_text(encoding="utf-8")) == expected.model_dump(mode="json")


def test_an_edited_step_at_an_unchanged_version_is_refused_by_name(tmp_path: Path) -> None:
    folder = _folder(tmp_path, STEP)
    held = write_lock(folder)
    _folder(tmp_path, EDITED)

    with pytest.raises(StepLockRefusedError, match="step_a") as refused:
        write_lock(folder)

    assert refused.value.steps == ("step_a",)
    assert read_lock(folder) == held, "a refused lock writes nothing"


def test_raising_the_version_records_the_new_version_and_content_address(tmp_path: Path) -> None:
    folder = _folder(tmp_path, STEP)
    write_lock(folder)
    raised = EDITED.replace("version: 1", "version: 2")
    _folder(tmp_path, raised)

    lock = write_lock(folder)

    assert lock.steps["step_a"] == StepLockEntry(version=2, step_hash=step_content_address(load_step_document(raised).step))
    assert read_lock(folder) == lock


def test_writing_a_field_at_its_default_leaves_the_lock_unchanged(tmp_path: Path) -> None:
    folder = _folder(tmp_path, STEP)
    held = write_lock(folder)
    _folder(tmp_path, STEP.replace("  name: step_a\n", "  name: step_a\n  tools: []\n"))

    assert locked_steps(folder) == held
    assert write_lock(folder) == held


def test_a_step_the_import_refuses_cannot_be_locked(tmp_path: Path) -> None:
    folder = _folder(tmp_path, NULL_DEFAULT)

    with pytest.raises(DocumentError, match=r"step-a\.md.*`output\.json_schema\.default`, `output\.json_schema\.enum`"):
        write_lock(folder)

    assert not (folder / "steps.lock").exists()


def test_the_drop_rule_is_the_one_engram_stores_frontmatter_with() -> None:
    files = [file for folder in STEPS_FOLDERS for file in sorted((folder / "steps").glob("*.md"))]
    dumps = [load_step_document(file.read_text(encoding="utf-8")).step.model_dump(mode="json") for file in files]
    dumps.append(load_step_document(NULL_DEFAULT).step.model_dump(mode="json"))

    assert len(dumps) > 30, "every shipped step file is read"
    for dump in dumps:
        assert as_stored(dump) == _jsonable_frontmatter_value(dump), dump["name"]


def test_a_folder_with_no_lock_reads_as_an_empty_one(tmp_path: Path) -> None:
    folder = _folder(tmp_path, STEP)

    assert read_lock(folder) == StepLock(steps={})
