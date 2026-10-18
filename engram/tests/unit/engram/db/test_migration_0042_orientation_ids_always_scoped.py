"""0042's id expression must stay in step with `orientation_page_id`, the function it exists to serve.

Pure Python, no DB, no Docker — reuses `test_migration_0039_page_types_decision`'s
`_load_migration_module` rather than adding a fourth copy (`tests/integration/test_migrations.py`
and `tests/unit/engram/retrieval/test_queries.py` each already carry their own). Every assertion
here is derived from the production constants (`orientation_page_id`, `ORIENTATION_PAGES`) rather
than a second hand-typed literal, the way `TestBakedOverrideConstantsMatchTheMigration` in
`test_queries.py` derives its expected text from the migration it guards: a literal-vs-literal
comparison would stay green even if `orientation_page_id`'s format changed underneath it, while
`api.about` / `api.authoring_rules` silently stopped matching the rows the indexer writes.
"""

from __future__ import annotations

from engram.indexing.service.orientation import AUTHORING_AUDIENCE, ORIENTATION_PAGES, QUERY_AUDIENCE, orientation_page_id
from tests.unit.engram.db.test_migration_0039_page_types_decision import _load_migration_module

# A collection name distinctive enough that it can only appear in the id expression's suffix
# position, never as a substring of the literal SQL surrounding it.
_SENTINEL_COLLECTION = "sentinel-collection-0042"


def _expected_id_prefix(base_id: str) -> str:
    """The `'<base_id>@'` fragment 0042's SQL must bake, derived from `orientation_page_id` itself.

    Calls the real function with a sentinel collection and strips it back off, so a future change
    to `orientation_page_id`'s format (not only its base ids) fails this test rather than leaving a
    hand-typed SQL literal green.
    """
    computed = orientation_page_id(base_id, _SENTINEL_COLLECTION)
    assert computed.endswith(_SENTINEL_COLLECTION), f"orientation_page_id no longer suffixes with the collection: {computed!r}"
    return computed.removesuffix(_SENTINEL_COLLECTION)


def test_the_about_view_and_function_bake_orientation_page_id_s_expression() -> None:
    module = _load_migration_module("0042")
    base_id, _title = ORIENTATION_PAGES[QUERY_AUDIENCE]
    prefix = _expected_id_prefix(base_id)

    assert f"'{prefix}' || p.collection" in module._ABOUT
    assert f"'{prefix}' || p_collection" in module._COLLECTION_ABOUT


def test_the_authoring_rules_view_and_function_bake_orientation_page_id_s_expression() -> None:
    module = _load_migration_module("0042")
    base_id, _title = ORIENTATION_PAGES[AUTHORING_AUDIENCE]
    prefix = _expected_id_prefix(base_id)

    assert f"'{prefix}' || p.collection" in module._AUTHORING_RULES
    assert f"'{prefix}' || p_collection" in module._COLLECTION_AUTHORING_RULES


def test_the_migration_s_base_ids_are_orientation_pages_own_base_ids() -> None:
    """`_ORIENTATION_BASE_IDS` (the rename source list) must name the same two ids `ORIENTATION_PAGES` declares."""
    module = _load_migration_module("0042")
    expected = {base_id for base_id, _title in ORIENTATION_PAGES.values()}

    assert set(module._ORIENTATION_BASE_IDS) == expected
