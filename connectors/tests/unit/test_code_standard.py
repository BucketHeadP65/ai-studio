"""The code standard holds over every connectors slice already converted to it."""

import subprocess
import sys

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CONVERTED = ("engram_connectors.conversion", "engram_connectors.multihop_rag", "engram_connectors.source_queue", "engram_connectors.x", "engram_connectors.youtube", "engram_connectors.zotero", "engram_connectors.testing")
"""Every converted slice, by its dotted name. A slice joins when its conversion lands and never leaves."""
DOCUMENTED: tuple[str, ...] = ("engram_connectors.xai",)
"""Every converted single module, by its dotted name. It is held to the docstring standard only."""
ASSEMBLY: tuple[str, ...] = ()
"""The composition modules, which may also import a slice's wiring, controller and infrastructure modules."""


def _run(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    """One of the repository's code checks, run on this interpreter."""
    command = [sys.executable, str(REPO / "scripts" / "code" / script), *arguments]
    return subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603


def test_converted_slices_follow_the_docstring_standard() -> None:
    paths = [str(REPO / "connectors" / "src" / Path(*name.split("."))) for name in CONVERTED]
    paths += [str(REPO / "connectors" / "src" / Path(*name.split(".")).with_suffix(".py")) for name in DOCUMENTED]
    result = _run("check_docstrings.py", "--vocabulary", *paths)
    assert result.returncode == 0, result.stdout


def test_converted_slices_keep_their_boundaries() -> None:
    arguments = ["--src", str(REPO / "connectors" / "src")]
    for name in CONVERTED:
        arguments += ["--slice", name]
    for name in ASSEMBLY:
        arguments += ["--assembly", name]
    result = _run("check_slices.py", *arguments)
    assert result.returncode == 0, result.stdout
