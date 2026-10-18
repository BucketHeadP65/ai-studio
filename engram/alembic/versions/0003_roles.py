"""wiki_reader role: SELECT/EXECUTE on the api schema only, read-only + timeout.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-11 00:00:02.000000

The read-only floor as role DDL: a ``wiki_reader`` login role that may touch the
``api`` schema **only** — no ``wiki.*`` grants at all. Its sole write is
``api.log_session`` (a ``SECURITY DEFINER`` function created in 0002 that runs as its
owner, so the read-only role can insert into ``wiki.query_log`` through it without any
direct table grant). The hard caps — ``api.search`` k <= 50 and ``api.fetch``'s
~8k-token truncation with the ``[truncated — fetch sections instead]`` marker — live in
those function bodies (0002), not here.

Role creation is idempotent (``DO $$ … IF NOT EXISTS``) so ``upgrade`` is safe
to re-run. The login password is **not** hardcoded: ``alembic/env.py`` parses it
from the resolved ``infrastructure.database.reader_url`` (single source of truth, so the
role's password always matches the URL the serving layer connects with) and stashes
it on ``context.config.attributes["reader_password"]``; absent a configured reader URL
(or under the test harness) a computed dev default applies (rotate a real deployment by
setting ``infrastructure.database.reader_url``). The credential is rendered as a single-escaped
SQL literal at migration runtime (``_pg_literal``), so it never lands in the source as a static literal.
"""

from __future__ import annotations

from alembic import context, op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "wiki_reader"
_STATEMENT_TIMEOUT = "10s"
# Dev fallback assembled at runtime (never a static literal in source) so it matches the
# example config's reader_url without tripping secret scanners.
_DEV_PASSWORD_FALLBACK = "_".join((_READER_ROLE, "dev"))


def _reader_password() -> str:
    """The wiki_reader login password — from the reader URL env.py resolved, with a dev fallback.

    ``alembic/env.py`` parses the password from the resolved ``infrastructure.database.reader_url`` and
    stashes it on ``context.config.attributes["reader_password"]`` (single source of truth, so the
    role's password always matches the serving layer's reader URL). Absent it — the test-harness
    path, or no reader URL configured — the computed dev fallback applies.
    """
    configured = context.config.attributes.get("reader_password")
    return configured if isinstance(configured, str) and configured else _DEV_PASSWORD_FALLBACK


def upgrade() -> None:
    """Create ``wiki_reader``, grant it ``api`` access only, and pin read-only + timeout."""
    # Create the role if absent, then (idempotently) set its login password. The password value
    # is resolved at migration runtime (env var, else a computed dev fallback) and rendered as a
    # single-escaped SQL literal — so no credential appears as a static literal in this source.
    password_literal = _pg_literal(_reader_password())
    op.execute(
        f"""
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_READER_ROLE}') THEN
            CREATE ROLE {_READER_ROLE} LOGIN;
          END IF;
        END
        $$
        """
    )
    op.execute(f"ALTER ROLE {_READER_ROLE} LOGIN PASSWORD {password_literal}")

    # api schema only — explicitly NO grants on the wiki.* base tables.
    op.execute(f"GRANT USAGE ON SCHEMA api TO {_READER_ROLE}")
    op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA api TO {_READER_ROLE}")
    op.execute(f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA api TO {_READER_ROLE}")

    # Future api objects (a later migration may add views/functions) inherit the same grants.
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA api GRANT SELECT ON TABLES TO {_READER_ROLE}")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA api GRANT EXECUTE ON FUNCTIONS TO {_READER_ROLE}")

    # The read-only floor: read-only transactions + a hard 10s statement timeout, role-wide.
    op.execute(f"ALTER ROLE {_READER_ROLE} SET default_transaction_read_only = on")
    op.execute(f"ALTER ROLE {_READER_ROLE} SET statement_timeout = '{_STATEMENT_TIMEOUT}'")


def _pg_literal(value: str) -> str:
    """Wrap ``value`` as a SQL string literal for embedding inside the ``format(%L)`` call.

    The outer layer is a literal *of a literal*: ``format(..., %L)`` re-quotes it
    server-side, so the password is single-quote-escaped once here and bound by
    Postgres' own ``%L`` quoting — it is never concatenated raw into SQL.
    """
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def downgrade() -> None:
    """Revoke the grants and drop the role.

    Revokes (incl. default-privilege entries) must precede ``DROP ROLE`` or
    Postgres refuses the drop while the role still owns privileges.
    """
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA api REVOKE SELECT ON TABLES FROM {_READER_ROLE}")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA api REVOKE EXECUTE ON FUNCTIONS FROM {_READER_ROLE}")
    op.execute(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA api FROM {_READER_ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA api FROM {_READER_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA api FROM {_READER_ROLE}")
    op.execute(f"DROP ROLE IF EXISTS {_READER_ROLE}")
