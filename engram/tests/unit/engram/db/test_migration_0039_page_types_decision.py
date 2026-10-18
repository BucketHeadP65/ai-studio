"""0039 must keep `api.page_types` guarded: `api.visible_collection` stays in both the upgrade and the downgrade body.

Pure Python, no DB, no Docker -- mirrors `tests/unit/engram/retrieval/test_queries.py`'s
`_load_migration_module`, kept local so the unit tier never imports from the integration tree.
0037_library_privacy.py added the visibility guard to `api.page_types`; 0039 replaces that same
view to add a `decision` column, so both the SQL it installs (`upgrade`) and the SQL its downgrade
restores must carry the guard forward, or a knowledge base declaring `private: true` would leak its page
types to every other session the moment 0039 lands.
"""

from __future__ import annotations

import importlib.util

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

# `engram/alembic/versions` -- four levels up from `tests/unit/engram/db/`.
_ALEMBIC_VERSIONS_DIR = Path(__file__).resolve().parents[4] / "alembic" / "versions"

_GUARD = "api.visible_collection("


def _load_migration_module(revision: str) -> ModuleType:
    """Import an alembic version file as a plain module, found by its revision-number filename prefix."""
    matches = sorted(_ALEMBIC_VERSIONS_DIR.glob(f"{revision}_*.py"))
    assert len(matches) == 1, f"expected exactly one {revision}_*.py under {_ALEMBIC_VERSIONS_DIR}, found {matches}"
    spec = importlib.util.spec_from_file_location(f"_migration_{revision}", matches[0])
    assert spec is not None, f"no import spec for {matches[0]}"
    assert spec.loader is not None, f"import spec for {matches[0]} carries no loader"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_upgrade_view_body_keeps_the_visibility_guard() -> None:
    module = _load_migration_module("0039")

    assert _GUARD in module._VIEW_BODY
    assert "decision" in module._VIEW_BODY


def test_the_upgrade_view_body_folds_the_compiled_json_null_into_a_real_sql_null() -> None:
    """A page_type with no declared decision still carries the key with a JSON null value, not an absent key."""
    module = _load_migration_module("0039")

    assert "NULLIF(t.contract -> 'decision', 'null'::jsonb)" in module._VIEW_BODY


def test_the_downgrade_restore_body_keeps_the_visibility_guard() -> None:
    module = _load_migration_module("0039")

    assert _GUARD in module._VIEW_BODY_PRE_0039


def test_the_downgrade_restore_body_does_not_carry_the_decision_column() -> None:
    """The restore target is 0037's guarded view, before 0039 ever added the column."""
    module = _load_migration_module("0039")

    assert "decision" not in module._VIEW_BODY_PRE_0039
