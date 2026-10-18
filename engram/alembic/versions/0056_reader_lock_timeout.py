"""The reader roles wait at most five seconds for a lock.

Revision ID: 0056
Revises: 0055
Create Date: 2026-10-10 00:00:00.000000

`kb_reader` stopped a statement after ten seconds but would wait that whole time behind a lock another session held.
It now gives up on a lock after five seconds, so a reader queued behind the indexer fails fast with a lock timeout
instead of using up its statement budget. Every private reader role, each a member of `kb_reader`, carries the same
setting, and the indexer copies `kb_reader`'s settings onto a role it creates later. Downgrade removes the setting
from all of them.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_LOCK_TIMEOUT = "5s"

_READER_ROLES = text(
    """
    SELECT r.rolname FROM pg_roles r
    WHERE r.rolname = 'kb_reader'
       OR EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
                  WHERE m.member = r.oid AND g.rolname = 'kb_reader')
    ORDER BY 1
    """
)


def _reader_roles() -> list[str]:
    """`kb_reader` and every role that belongs to it."""
    return [row[0] for row in op.get_bind().execute(_READER_ROLES).all()]


def upgrade() -> None:
    """Give `kb_reader` and every private reader role a five-second lock timeout."""
    for role in _reader_roles():
        op.execute(f"ALTER ROLE {role} SET lock_timeout = '{_LOCK_TIMEOUT}'")


def downgrade() -> None:
    """Remove the lock timeout from `kb_reader` and every private reader role."""
    for role in _reader_roles():
        op.execute(f"ALTER ROLE {role} RESET lock_timeout")
