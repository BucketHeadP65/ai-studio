"""No retired environment name survives anywhere this deployment is described or read."""

from __future__ import annotations

import re

from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
REPO = HERE.parents[3]
SEARCHED = (REPO / "harness", REPO / "scripts" / "stack")
"""The two trees whole, with no list of files inside them: a document, a decision record, a script or
a module that names a retired export fails this wherever it sits."""

NOT_SOURCE = frozenset({".venv", "__pycache__", ".state", "node_modules", ".pytest_cache", ".ruff_cache"})
"""Installed packages and build output, which are not this repository's own text."""

READ_SUFFIXES = frozenset({".py", ".md", ".json", ".yml", ".yaml", ".toml", ".sh", ".txt"})

RETIRED = (
    "APP_RUN_ENGINE",
    "APP_RUN_ARCHIVE_DAYS",
    "APP_TEMPORAL_ADDRESS",
    "APP_TEMPORAL_NAMESPACE",
    "APP_TEMPORAL_TASK_QUEUE",
    "APP_HYBRID_SEARCH",
    "APP_CUSTOM_ROOT",
    "APP_TERMINAL_ENABLED",
    "TAVILY_API_KEY",
    "APP_STUDIO_BASE_URL",
    "APP_KNOWLEDGE_BASE_GUIDANCE",
    "APP_CROSS_KNOWLEDGE_BASE_SEARCH",
    "APP_DEEPSEEK_GATEWAY_ROUTE",
    "APP_MAX_TOKENS",
)


def whole_name(name: str) -> re.Pattern[str]:
    """`name` matched as a whole export, so a longer name that contains it does not count.

    Every replacement spells a section and a field with a double underscore between them, and three
    of the retired names are prefixes of their own replacement (`TAVILY_API_KEY` inside
    `APP_WEB_SEARCH__TAVILY_API_KEY`, `APP_KNOWLEDGE_BASE_GUIDANCE` inside
    `APP_KNOWLEDGE_BASE_GUIDANCE__GUIDANCE`, `APP_TERMINAL_ENABLED` beside `APP_TERMINAL__ENABLED`).
    A substring search would find the new spelling and refuse it.
    """
    return re.compile(rf"(?<![A-Z0-9_]){re.escape(name)}(?![A-Z0-9_])")


def is_source(path: Path) -> bool:
    """Whether `path` is this repository's own text rather than something installed or generated.

    This file is skipped by its own path: it is where the retired names are written down, and it
    would otherwise refuse every one of them on its own lines.
    """
    return path.is_file() and path != HERE and path.suffix in READ_SUFFIXES and NOT_SOURCE.isdisjoint(path.parts)


def searched_files() -> list[Path]:
    """Every file the names are read out of, so a new one joins by itself."""
    return [path for where in SEARCHED for path in where.rglob("*") if is_source(path)]


@pytest.mark.parametrize("name", RETIRED)
def test_no_file_still_names_it(name: str) -> None:
    pattern = whole_name(name)
    offenders = sorted(
        f"{path.relative_to(REPO)}:{lineno}"
        for path in searched_files()
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1)
        if pattern.search(line)
    )

    assert offenders == [], f"{name} was retired, these still name it:\n" + "\n".join(offenders)


def test_the_search_actually_read_something() -> None:
    assert len(searched_files()) > 50


def test_a_replacement_is_not_read_as_its_own_predecessor() -> None:
    """The three pairs the boundary rule exists for."""
    pairs = (
        ("TAVILY_API_KEY", "APP_WEB_SEARCH__TAVILY_API_KEY"),
        ("APP_KNOWLEDGE_BASE_GUIDANCE", "APP_KNOWLEDGE_BASE_GUIDANCE__GUIDANCE"),
        ("APP_TERMINAL_ENABLED", "APP_TERMINAL__ENABLED"),
    )

    assert [retired for retired, replacement in pairs if whole_name(retired).search(replacement)] == []
