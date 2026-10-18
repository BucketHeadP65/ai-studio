"""The code standard holds over every core slice already converted to it."""

import subprocess
import sys

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CONVERTED = (
    "core.wiring",
    "core.cqrs",
    "core.ai.disclosure",
    "core.ai.format_maps",
    "core.ai.evaluation",
    "core.ai.retrieval",
    "core.ai.inspection",
    "core.ai.workflows",
    "core.ai.history",
    "core.ai.transcripts",
)
"""Every converted slice, by its dotted name. A slice joins when its conversion lands and never leaves."""
DOCUMENTED: tuple[str, ...] = (
    "core.fingerprints",
    "core.statistics",
    "core.column_types",
    "core.net",
    "core.settings",
    "core.supervisor",
    "core.testing.fakes",
    "core.ai.empty_answer",
    "core.ai.filesystem",
    "core.ai.tool_output_limits",
    "core.ai.artifacts",
    "core.ai.dedup",
    "core.ai.spend_store",
    "core.ai.embedding",
    "core.ai.models",
    "core.ai.voice",
    "core.ai.registry",
    "core.db",
    "core.files",
    "core.markdown",
    "core.observability",
    "core.resolver",
)
"""Every converted single module, by its dotted name. It is held to the docstring standard only."""
ASSEMBLY: tuple[str, ...] = ()
"""The composition modules, which may also import a slice's wiring, controller and infrastructure modules."""
PROJECTS = ("core", "engram", "studio", "harness", "connectors")
"""Every project whose code may import core, so a caller that bypasses a contracts file is found wherever it lives."""
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
    "studio.knowledge_base.wiring.serve",
    "studio.knowledge_base.wiring.sources",
    "studio.knowledge_base.wiring.sync",
    "studio.knowledge_base.wiring.watch_list",
)
"""The consumers' composition modules, which may also import a core slice's infrastructure modules."""


def _run(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    """One of the repository's code checks, run on this interpreter."""
    command = [sys.executable, str(REPO / "scripts" / "code" / script), *arguments]
    return subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603


def test_converted_slices_follow_the_docstring_standard() -> None:
    paths = [str(REPO / "core" / "src" / Path(*name.split("."))) for name in CONVERTED]
    paths += [str(REPO / "core" / "src" / Path(*name.split(".")).with_suffix(".py")) for name in DOCUMENTED]
    result = _run("check_docstrings.py", "--vocabulary", *paths)
    assert result.returncode == 0, result.stdout


def test_converted_slices_keep_their_boundaries() -> None:
    arguments = []
    for project in PROJECTS:
        arguments += ["--src", str(REPO / project / "src")]
    for name in CONVERTED:
        arguments += ["--slice", name]
    for name in (*ASSEMBLY, *CONSUMER_ASSEMBLY):
        arguments += ["--assembly", name]
    result = _run("check_slices.py", *arguments)
    assert result.returncode == 0, result.stdout
