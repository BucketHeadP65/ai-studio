"""A price registry that carries no provider, for a test of what this app answers when the registry knows nothing."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from genai_prices import data_snapshot
from genai_prices.data_snapshot import DataSnapshot

if TYPE_CHECKING:
    from collections.abc import Generator


@contextmanager
def empty_price_registry() -> Generator[None]:
    """Answer every registry lookup with a miss inside the block, then hand the bundled registry back."""
    data_snapshot.set_custom_snapshot(DataSnapshot(providers=[], from_auto_update=False))
    try:
        yield
    finally:
        data_snapshot.set_custom_snapshot(None)
