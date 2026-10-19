"""The code standard holds over every studio slice already converted to it."""

import subprocess
import sys

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CONVERTED = (
    "studio.knowledge_base.http",
    "studio.knowledge_base.indexing",
    "studio.knowledge_base.refresh",
    "studio.knowledge_base.retention",
)
"""Every converted slice, by its dotted name. A slice joins when its conversion lands and never leaves."""
DOCUMENTED: tuple[str, ...] = (
    "studio.knowledge_base.wiring.__init__",
    "studio.knowledge_base.wiring.contracts",
    "studio.knowledge_base.wiring.conversion",
    "studio.knowledge_base.wiring.evals",
    "studio.knowledge_base.wiring.export",
    "studio.knowledge_base.wiring.indexing",
    "studio.knowledge_base.wiring.mirror",
    "studio.knowledge_base.wiring.refresh",
    "studio.knowledge_base.wiring.serve",
    "studio.knowledge_base.wiring.sources",
    "studio.knowledge_base.wiring.sync",
    "studio.knowledge_base.wiring.watch_list",
    "studio.knowledge_base.cli",
    "studio.knowledge_base.cli_decisions",
    "studio.knowledge_base.cli_evals",
    "studio.knowledge_base.cli_feedback",
    "studio.knowledge_base.cli_init",
    "studio.knowledge_base.cli_note",
    "studio.knowledge_base.cli_source_queue",
    "studio.knowledge_base.cli_support",
    "studio.knowledge_base.cli_vocab",
    "studio.knowledge_base.cli_watch_list",
    "studio.knowledge_base.container",
    "studio.knowledge_base.qa_routing",
    "studio.knowledge_base.reindex",
    "studio.knowledge_base.settings",
    "studio.knowledge_base.source_queue",
    "studio.knowledge_base.template_guidance",
    "studio.knowledge_base.watch_list",
)
"""Every converted single module, by its dotted name. It is held to the docstring standard only."""
ASSEMBLY = (
    "studio.knowledge_base.cli",
    "studio.knowledge_base.container",
    "studio.knowledge_base.wiring.conversion",
    "studio.knowledge_base.wiring.evals",
    "studio.knowledge_base.wiring.export",
    "studio.knowledge_base.wiring.indexing",
    "studio.knowledge_base.wiring.mirror",
    "studio.knowledge_base.wiring.refresh",
    "studio.knowledge_base.wiring.serve",
    "studio.knowledge_base.wiring.sources",
    "studio.knowledge_base.wiring.sync",
    "studio.knowledge_base.wiring.watch_list",
)
"""The composition modules, which may also import a slice's wiring, controller and infrastructure modules."""


def _run(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    """One of the repository's code checks, run on this interpreter."""
    command = [sys.executable, str(REPO / "scripts" / "code" / script), *arguments]
    return subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603


def test_converted_slices_follow_the_docstring_standard() -> None:
    paths = [str(REPO / "studio" / "src" / Path(*name.split("."))) for name in CONVERTED]
    paths += [str(REPO / "studio" / "src" / Path(*name.split(".")).with_suffix(".py")) for name in DOCUMENTED]
    result = _run("check_docstrings.py", "--vocabulary", *paths)
    assert result.returncode == 0, result.stdout


def test_converted_slices_keep_their_boundaries() -> None:
    arguments = ["--src", str(REPO / "studio" / "src")]
    for name in CONVERTED:
        arguments += ["--slice", name]
    for name in ASSEMBLY:
        arguments += ["--assembly", name]
    result = _run("check_slices.py", *arguments)
    assert result.returncode == 0, result.stdout
