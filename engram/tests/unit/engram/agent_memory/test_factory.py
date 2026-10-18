"""`embedded_memory_store`'s pool-sizing parameters: the seam `_open_pool` threads to `asyncpg.create_pool`."""

from __future__ import annotations

import inspect

from engram.agent_memory.contracts import embedded_memory_store


def test_pool_sizes_default_well_under_asyncpgs_own_defaults() -> None:
    """asyncpg.create_pool defaults to min_size=10, max_size=10; a store opened once per knowledge base must not inherit that."""
    parameters = inspect.signature(embedded_memory_store).parameters
    assert parameters["pool_min_size"].default == 1
    assert parameters["pool_max_size"].default == 2
