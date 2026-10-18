"""The code standard holds over every engram slice already converted to it."""

import subprocess
import sys

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CONVERTED = (
    "engram.sources",
    "engram.db",
    "engram.kb",
    "engram.indexing",
    "engram.retrieval",
    "engram.authoring",
    "engram.agent_memory",
    "engram.export",
    "engram.init",
    "engram.skills",
    "engram.testing",
    "engram.evals.shared",
    "engram.evals.harness",
    "engram.evals.outcomes",
    "engram.evals.rca",
    "engram.evals.tuning",
    "engram.evals.generation",
    "engram.evals.recommendation",
    "engram.evals.generator_check",
    "engram.evals.scoring",
    "engram.evals.judgments",
    "engram.evals.paired_comparison",
)
"""Every converted slice, by its dotted name. A slice joins when its conversion lands and never leaves."""
PINNED = ("skills/agent-threads",)
"""Folders under `engram/src/engram` that hold a pinned copy of an external skill, which the checks leave as shipped."""
DOCUMENTED: tuple[str, ...] = ("engram.config", "engram.identity", "engram.evals.commands", "engram.evals.handlers")
"""Every converted single module, by its dotted name. It is held to the docstring standard only."""
PROJECTS = ("core", "engram", "studio", "harness", "connectors")
"""Every project whose code may import engram, so a caller that bypasses a contracts file is found wherever it lives."""
CONSUMER_ASSEMBLY = (
    "harness.acp",
    "harness.adapters.wiring",
    "harness.cli",
    "harness.composition",
    "harness.container",
    "harness.main",
    "studio.knowledge_base.container",
    "studio.knowledge_base.cli",
    "studio.knowledge_base.wiring.conversion",
    "studio.knowledge_base.wiring.evals",
    "studio.knowledge_base.wiring.export",
    "studio.knowledge_base.wiring.indexing",
    "studio.knowledge_base.wiring.mirror",
    "studio.knowledge_base.wiring.sources",
    "studio.knowledge_base.wiring.sync",
    "studio.knowledge_base.wiring.watch_list",
)
"""The consumers' composition modules, which may also import an engram slice's infrastructure modules."""


def _run(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    """One of the repository's code checks, run on this interpreter."""
    command = [sys.executable, str(REPO / "scripts" / "code" / script), *arguments]
    return subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603


def _checked_files(slice_folder: Path) -> list[str]:
    """The slice's Python files, leaving out every pinned copy of an external skill."""
    pinned = [REPO / "engram" / "src" / "engram" / Path(*name.split("/")) for name in PINNED]
    return [str(path) for path in sorted(slice_folder.rglob("*.py")) if not any(path.is_relative_to(folder) for folder in pinned)]


def test_converted_slices_follow_the_docstring_standard() -> None:
    paths = [path for name in CONVERTED for path in _checked_files(REPO / "engram" / "src" / Path(*name.split(".")))]
    paths += [str(REPO / "engram" / "src" / Path(*name.split(".")).with_suffix(".py")) for name in DOCUMENTED]
    result = _run("check_docstrings.py", "--vocabulary", *paths)
    assert result.returncode == 0, result.stdout


def test_converted_slices_keep_their_boundaries() -> None:
    arguments = []
    for project in PROJECTS:
        arguments += ["--src", str(REPO / project / "src")]
    for name in CONVERTED:
        arguments += ["--slice", name]
    for name in CONSUMER_ASSEMBLY:
        arguments += ["--assembly", name]
    result = _run("check_slices.py", *arguments)
    assert result.returncode == 0, result.stdout
