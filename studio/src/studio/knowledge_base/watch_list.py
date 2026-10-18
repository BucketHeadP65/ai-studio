"""Where a knowledge base keeps its watch list."""

from __future__ import annotations

from pathlib import PurePosixPath

WATCH_LIST_PATH = PurePosixPath("_meta") / "watch-list.yaml"
"""The watch-list file, relative to a knowledge base's folder."""
