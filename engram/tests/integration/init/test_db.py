"""Integration test for `assert_schema_at_head` against a real Postgres.

Proves the real wiring end to end: `ScriptDirectory` reading the code's alembic head,
`MigrationContext` reading the database's `alembic_version` row over a live connection. A
database one revision short of head raises `SchemaBehindHeadError` naming both revisions; a
database migrated all the way to head passes silently.

The whole module skips when Docker is unavailable (the `pg` fixture raises `pytest.skip`). The
tests are synchronous: each wraps its one async setup call in `asyncio.run` (legal — the sync
test body has no running loop), matching `tests/integration/test_describe_schema.py`.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

import pytest

from engram.init.contracts import SchemaBehindHeadError, assert_schema_at_head
from engram.testing.infrastructure.knowledge_base_db import BED_GRAPH_BACKEND, new_test_database, run_upgrade

if TYPE_CHECKING:
    from collections.abc import Coroutine


def _run_sync[T](coro: Coroutine[object, object, T]) -> T:
    """Drive a coroutine to completion from a synchronous test body (no loop is running)."""
    return asyncio.run(coro)


class TestAssertSchemaAtHead:
    def test_a_database_one_revision_short_of_head_raises(self, pg: str):
        target_url = _run_sync(new_test_database(pg))
        run_upgrade(target_url, "0024")

        with pytest.raises(SchemaBehindHeadError, match="0024"):
            assert_schema_at_head(target_url)

    def test_a_database_migrated_to_head_passes(self, pg: str):
        if BED_GRAPH_BACKEND != "cte":
            pytest.skip(f"bed graph backend is {BED_GRAPH_BACKEND!r}, not cte — 0025 needs KNOWLEDGE_BASE_TEST_DIALECT=paradedb to reach head")
        target_url = _run_sync(new_test_database(pg))
        run_upgrade(target_url, "head")

        # The guard is that this does not raise.
        assert_schema_at_head(target_url)
