"""The code standard holds over every harness slice already converted to it."""

import subprocess
import sys

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CONVERTED = (
    "harness.features.scheduling",
    "harness.features.browsing",
    "harness.features.running",
    "harness.features.evals",
    "harness.features.sessions",
    "harness.features.selfauthoring",
    "harness.features.projects",
    "harness.features.inspection",
    "harness.features.followme",
    "harness.features.terminal",
    "harness.domain",
    "harness.migrations",
    "harness.features.authoring",
    "harness.adapters",
)
"""Every converted slice, by its dotted name. A slice joins when its conversion lands and never leaves."""
DOCUMENTED: tuple[str, ...] = (
    "harness.rendering",
    "harness.acp",
    "harness.cli",
    "harness.composition",
    "harness.container",
    "harness.knowledge_base",
    "harness.main",
    "harness.plugins",
    "harness.sandbox",
    "harness.session",
    "harness.settings",
    "harness.workspace",
    "harness.tracing",
)
"""Every converted single module, by its dotted name. It is held to the docstring standard only."""
ASSEMBLY = ("harness.acp", "harness.adapters.wiring", "harness.cli", "harness.composition", "harness.container", "harness.main")
"""The composition modules, which may also import a slice's wiring, controller and infrastructure modules."""
INFRASTRUCTURE = ("harness.adapters",)
"""The converted slices that are the harness's infrastructure, where every module may import database and network code."""


def _run(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    """One of the repository's code checks, run on this interpreter."""
    command = [sys.executable, str(REPO / "scripts" / "code" / script), *arguments]
    return subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603


def test_converted_slices_follow_the_docstring_standard() -> None:
    paths = [str(REPO / "harness" / "src" / Path(*name.split("."))) for name in CONVERTED]
    paths += [str(REPO / "harness" / "src" / Path(*name.split(".")).with_suffix(".py")) for name in DOCUMENTED]
    result = _run("check_docstrings.py", "--vocabulary", *paths)
    assert result.returncode == 0, result.stdout


def test_converted_slices_keep_their_boundaries() -> None:
    arguments = ["--src", str(REPO / "harness" / "src")]
    for name in CONVERTED:
        arguments += ["--slice", name]
    for name in ASSEMBLY:
        arguments += ["--assembly", name]
    for name in INFRASTRUCTURE:
        arguments += ["--infrastructure", name]
    result = _run("check_slices.py", *arguments)
    assert result.returncode == 0, result.stdout
