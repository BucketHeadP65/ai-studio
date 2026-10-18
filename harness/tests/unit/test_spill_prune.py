"""The spill folders of processes that have ended are pruned, and every live or unknown folder is kept."""

from __future__ import annotations

import os
import subprocess
import sys

from typing import TYPE_CHECKING

from harness.adapters.environment import prune_ended_spill_folders

if TYPE_CHECKING:
    from pathlib import Path


def _ended_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_prune_removes_only_the_folders_of_processes_that_have_ended(tmp_path: Path) -> None:
    ended = _ended_pid()
    for name in (str(ended), f"{ended}-maps", str(os.getpid()), f"{os.getpid()}-maps", "notes"):
        (tmp_path / name).mkdir()

    removed = prune_ended_spill_folders(tmp_path)

    assert removed == 2
    assert sorted(folder.name for folder in tmp_path.iterdir()) == sorted([str(os.getpid()), f"{os.getpid()}-maps", "notes"])


def test_prune_of_an_absent_directory_removes_nothing(tmp_path: Path) -> None:
    assert prune_ended_spill_folders(tmp_path / "absent") == 0
