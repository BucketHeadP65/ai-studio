"""Revision 0062 publishes `api.graph_reach` to the reader, and one step down and back restores it.

What this pins:

* at head, `api.graph_reach` and its walk exist, and `kb_reader` may run `api.graph_reach`;
* one step down drops both and puts 0053's comments back on the two walks;
* the step back up publishes them again with the grant.
"""

from __future__ import annotations

import asyncpg

from engram.testing.infrastructure.knowledge_base_db import run_downgrade, run_upgrade_head
from tests.integration.test_migrations import _asyncpg_dsn, _load_migration_module

_STATE = """
SELECT to_regprocedure('api.graph_reach(text, text[], text[], text, boolean)') IS NOT NULL AS published,
       to_regprocedure('kb.graph_reach_walk(text, text[], text[], text, boolean)') IS NOT NULL AS walk,
       obj_description('api.graph_expand(text, text[], integer, text[], integer, boolean)'::regprocedure, 'pg_proc') AS expand,
       obj_description('api.graph_paths(text, text, text, integer, text[], integer, boolean)'::regprocedure, 'pg_proc') AS paths
"""
_GRANTED = "SELECT has_function_privilege('kb_reader', 'api.graph_reach(text, text[], text[], text, boolean)', 'EXECUTE')"


async def _state(url: str) -> dict[str, object]:
    """Whether the reach function and its walk exist, and the two walks' comments."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        row = await connection.fetchrow(_STATE)
        granted = await connection.fetchval(_GRANTED) if row is not None and row["published"] else False
    finally:
        await connection.close()
    assert row is not None
    return {**dict(row), "granted": granted}


def _comment_text(statement: str) -> str:
    """The text a `COMMENT ON FUNCTION ... IS '...'` statement sets."""
    return statement.split(" IS '", 1)[1].removesuffix("'").replace("''", "'")


async def test_head_publishes_the_reach_function_to_the_reader(migrated: str):
    state = await _state(migrated)

    assert (state["published"], state["walk"], state["granted"]) == (True, True, True)
    assert "at most 6" in str(state["expand"])


async def test_one_step_down_and_back_restores_the_reach_function(migrated: str):
    revision = _load_migration_module("0062")

    run_downgrade(migrated, "0061")
    below = await _state(migrated)
    run_upgrade_head(migrated)
    above = await _state(migrated)

    assert (below["published"], below["walk"]) == (False, False)
    assert below["expand"] == _comment_text(revision._PREVIOUS_EXPAND_COMMENT)
    assert below["paths"] == _comment_text(revision._PREVIOUS_PATHS_COMMENT)
    assert (above["published"], above["walk"], above["granted"]) == (True, True, True)
