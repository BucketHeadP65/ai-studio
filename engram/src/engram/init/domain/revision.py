"""Where a database's alembic revision stands against the code's migration head."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

RevisionStanding = Literal["at_head", "behind", "ahead"]


def revision_standing(db_rev: str | None, head: str | None, *, known: Callable[[str], bool]) -> RevisionStanding:
    """Where the database's revision stands against the code's `head`.

    Args:
        db_rev: The database's current revision, or `None` when `alembic_version` carries no row.
        head: The code's migration head.
        known: Whether the code's migration scripts contain a given revision.

    Returns:
        `at_head` when they match. `behind` when the database is unmigrated or at a revision the code
        knows. `ahead` when the database names a revision the code has never seen, which means the
        checkout is older than the database.
    """
    if db_rev == head:
        return "at_head"
    if db_rev is None or known(db_rev):
        return "behind"
    return "ahead"
