"""The files plugin's `search_files` runs on the `rg` the harness's own environment carries, never on the POSIX grep fallback.

Live, the harness's environment had no `rg`, so every search ran the fallback. It refused `(?i)proposition` with advice
to pass `literal=True`, which `search_files` does not take, and it failed the whole search on a git-tracked symlink to a
folder (`engram/plugin/skills/agent-threads`). `uv run` puts the environment's `bin` first on the harness's `PATH`, and
every workspace inherits that `PATH`.
"""

from __future__ import annotations

import shutil
import sys

from pathlib import Path

import pytest

from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.workspaces.local import LocalWorkspaceBackend
from pydantic_ai_harness.filesystem import FileSystem, FileSystemToolset

_SYSTEM_PATH = "/usr/bin:/bin"
"""Where `git`, `sh` and the core utilities a search runs live, with no `rg`."""

_ENVIRONMENT_BIN = str(Path(sys.prefix) / "bin")
"""The harness environment's own executables."""


def test_the_harness_environment_carries_rg() -> None:
    assert shutil.which("rg", path=_ENVIRONMENT_BIN) is not None, "pydantic-ai-harness's `coder` extra installs `rg` here"


@pytest.fixture
async def tree(tmp_path: Path) -> Path:
    """A git work tree with a match at the top, one in a folder, and a tracked symlink to that folder."""
    root = tmp_path.resolve()
    (root / "skills").mkdir()
    (root / "skills" / "SKILL.md").write_text("A Proposition in the folder\n")
    (root / "notes.md").write_text("A proposition at the top\n")
    (root / "linked-skills").symlink_to("skills")
    workspace = LocalWorkspaceBackend(root, env={"PATH": _SYSTEM_PATH})
    for command in (["git", "init", "-q"], ["git", "add", "-A"]):
        done = await workspace.run(command, timeout=30)
        assert done.exit_code == 0, done.stderr
    return root


async def _search(root: Path, pattern: str, *, path: str) -> str:
    """`search_files` over `root`, in a workspace whose commands see only `path`."""
    toolset = FileSystem[None](root_dir=root).get_toolset()
    assert isinstance(toolset, FileSystemToolset)
    return await toolset.search_files(pattern, workspace=LocalWorkspaceBackend(root, env={"PATH": path}))


@pytest.mark.parametrize(
    ("pattern", "refusal"),
    [("(?i)proposition", "Use literal=True"), ("Proposition", "Is a directory")],
)
async def test_without_rg_the_fallback_fails_both_searches(tree: Path, pattern: str, refusal: str) -> None:
    """The control: the tree reproduces both fallback defects, so the search below proves `rg` is what fixes them."""
    with pytest.raises(ModelRetry, match=refusal):
        await _search(tree, pattern, path=_SYSTEM_PATH)


async def test_with_the_environment_on_path_an_inline_flag_searches_past_a_symlinked_folder(tree: Path) -> None:
    found = await _search(tree, "(?i)proposition", path=f"{_ENVIRONMENT_BIN}:{_SYSTEM_PATH}")

    assert found.splitlines() == ["notes.md:1:A proposition at the top", "skills/SKILL.md:1:A Proposition in the folder"]
