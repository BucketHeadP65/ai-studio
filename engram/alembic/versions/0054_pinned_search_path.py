"""Every `api` and `kb` function that pins a `search_path` reads `pg_catalog` first and `pg_temp` last.

Revision ID: 0054
Revises: 0053
Create Date: 2026-10-07 00:00:00.000000

Every `SECURITY DEFINER` function the builders generated, and every hand-written one beside them, pinned
`<schemas>, public, pg_catalog`. Postgres searches an unlisted `pg_temp` first, so a temporary object a
caller created could shadow a name the function uses, and an object in a listed schema could shadow a
built-in. Read-only transactions stop the reader from creating either today, so this is hardening.

Each function whose configuration sets `search_path` keeps its own schemas in their order, with
`pg_catalog` moved to the front and `pg_temp` added at the end. `ALTER FUNCTION ... SET` changes only the
setting, so bodies, grants and comments stay. The builders render the same order for every function
they generate from now on. Downgrade moves `pg_catalog` back to the end and drops `pg_temp`, which is
the one shape every pinned function carried at 0053.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Callable

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_PINNED = text(
    """
    SELECT p.oid::regprocedure::text AS signature, c.setting
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    CROSS JOIN LATERAL unnest(p.proconfig) AS c(setting)
    WHERE n.nspname IN ('api', 'kb') AND c.setting LIKE 'search_path=%'
    ORDER BY 1
    """
)

_ENDS = ("pg_catalog", "pg_temp")


def _first_and_last(schemas: list[str]) -> list[str]:
    """`pg_catalog`, the other schemas in their order, then `pg_temp`."""
    return ["pg_catalog", *(schema for schema in schemas if schema not in _ENDS), "pg_temp"]


def _catalog_last(schemas: list[str]) -> list[str]:
    """The other schemas in their order, then `pg_catalog`, with no `pg_temp`."""
    return [*(schema for schema in schemas if schema not in _ENDS), "pg_catalog"]


def _repin(order: Callable[[list[str]], list[str]]) -> None:
    """Set every pinned `search_path` in `api` and `kb` to `order` of its own schemas."""
    bind = op.get_bind()
    for signature, setting in bind.execute(_PINNED).all():
        schemas = [schema.strip() for schema in setting.removeprefix("search_path=").split(",") if schema.strip()]
        op.execute(f"ALTER FUNCTION {signature} SET search_path = {', '.join(order(schemas))}")


def upgrade() -> None:
    """Pin `pg_catalog` first and `pg_temp` last on every function that pins a `search_path`."""
    _repin(_first_and_last)


def downgrade() -> None:
    """Put `pg_catalog` back at the end and drop `pg_temp`."""
    _repin(_catalog_last)
