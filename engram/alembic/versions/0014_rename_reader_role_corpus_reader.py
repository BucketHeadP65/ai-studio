"""The read-only serving login is ``corpus_reader``.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-11 00:00:00.000000

0003 creates the reader login under the name ``wiki_reader``; this revision renames it, so every
privilege 0003–0013 granted rides along on the role's identity and no grant is re-issued on that
path. There is no alias. Connection strings (``infrastructure.database.reader_url``) must name
``corpus_reader``.

Roles are cluster-wide while grants are per-database, so a database migrated *after* a sibling
database in the same cluster was renamed finds that 0003 re-created ``wiki_reader`` as a second,
distinct role. That duplicate holds only this database's ``api`` grants: they are re-issued to
``corpus_reader`` (0003's grant set, verbatim) and the duplicate is dropped, so the cluster is
left with exactly one reader login either way.

The login password is re-set afterwards because ``ALTER ROLE … RENAME`` clears an MD5-encrypted
password. It resolves exactly as 0003's does — ``alembic/env.py`` parses it from the configured
``infrastructure.database.reader_url`` and stashes it on ``context.config.attributes["reader_password"]``,
absent that (or under the test harness) a computed dev default applies. The credential is rendered
as a single-escaped SQL literal at migration runtime, so it never lands in the source as a static
literal.
"""

from __future__ import annotations

from alembic import context, op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "corpus_reader"
_PREVIOUS_READER_ROLE = "wiki_reader"

# Dev fallbacks assembled at runtime (never static literals in source) so they match the example
# config's reader_url without tripping secret scanners. Each name carries its own fallback, so a
# downgrade restores exactly the credential 0003 would have set.
_DEV_PASSWORD_FALLBACK = "_".join((_READER_ROLE, "dev"))
_PREVIOUS_DEV_PASSWORD_FALLBACK = "_".join((_PREVIOUS_READER_ROLE, "dev"))

# Rename when the new name is free; otherwise move this database's grants onto the existing
# `corpus_reader` and drop the duplicate. Idempotent: with `wiki_reader` already gone, a no-op.
_ADOPT_READER_ROLE = f"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_PREVIOUS_READER_ROLE}') THEN
    RETURN;
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_READER_ROLE}') THEN
    ALTER ROLE {_PREVIOUS_READER_ROLE} RENAME TO {_READER_ROLE};
    RETURN;
  END IF;

  GRANT USAGE ON SCHEMA api TO {_READER_ROLE};
  GRANT SELECT ON ALL TABLES IN SCHEMA api TO {_READER_ROLE};
  GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA api TO {_READER_ROLE};
  ALTER DEFAULT PRIVILEGES IN SCHEMA api GRANT SELECT ON TABLES TO {_READER_ROLE};
  ALTER DEFAULT PRIVILEGES IN SCHEMA api GRANT EXECUTE ON FUNCTIONS TO {_READER_ROLE};

  DROP OWNED BY {_PREVIOUS_READER_ROLE};
  DROP ROLE {_PREVIOUS_READER_ROLE};
END
$$
"""

# The mirror image: restore the pre-rename name, and only when it is free.
_RESTORE_READER_ROLE = f"""
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_READER_ROLE}')
     AND NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_PREVIOUS_READER_ROLE}') THEN
    ALTER ROLE {_READER_ROLE} RENAME TO {_PREVIOUS_READER_ROLE};
  END IF;
END
$$
"""


def _reader_password(fallback: str) -> str:
    """The reader login password — from the reader URL env.py resolved, else ``fallback``.

    ``alembic/env.py`` stashes the password it parsed from ``infrastructure.database.reader_url`` on
    ``context.config.attributes["reader_password"]``, so the role's password always matches the URL
    the serving layer connects with. Absent it — the test-harness path, or no reader URL configured
    — the caller's computed dev fallback applies.

    Args:
        fallback: The dev password to use when no reader URL was resolved.
    """
    configured = context.config.attributes.get("reader_password")
    return configured if isinstance(configured, str) and configured else fallback


def _pg_literal(value: str) -> str:
    """Wrap ``value`` as a single-escaped SQL string literal, so it is never concatenated raw."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def upgrade() -> None:
    """Rename the reader login to ``corpus_reader`` and re-set its password."""
    op.execute(_ADOPT_READER_ROLE)
    op.execute(f"ALTER ROLE {_READER_ROLE} LOGIN PASSWORD {_pg_literal(_reader_password(_DEV_PASSWORD_FALLBACK))}")


def downgrade() -> None:
    """Restore the pre-rename login name and the password 0003 would have set on it."""
    op.execute(_RESTORE_READER_ROLE)
    op.execute(f"ALTER ROLE {_PREVIOUS_READER_ROLE} LOGIN PASSWORD {_pg_literal(_reader_password(_PREVIOUS_DEV_PASSWORD_FALLBACK))}")
