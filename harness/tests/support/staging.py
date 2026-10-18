"""Records the temp names that file writers swap into place, for tests of how they stage a write."""

from __future__ import annotations

import os

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


def staged_names(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the file name of every source passed to `os.replace` from here on, and still perform each swap."""
    names: list[str] = []
    real_replace = os.replace

    def replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        names.append(Path(source).name)
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    return names
