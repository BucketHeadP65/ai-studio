"""A private collection is visible only to a session that logs in as its own reader role.

Revision ID: 0055
Revises: 0054
Create Date: 2026-10-07 00:00:00.000000

`api.visible_collection` trusted `app.collection`, a setting any reader can write with `set_config`, so one
statement could claim a private collection and read it. Visibility now rests on the login role, which a reader
cannot change: a private collection is visible to a session whose `session_user` has a row in the new
`kb.reader_scopes` table for it, and to a role that can read the `kb` tables directly, which gains nothing from
hiding `api` rows. A shared collection, and one with no fingerprint, stays visible to every session.

`kb.reader_scopes` maps a reader role to the collection it may read. The indexer creates each private
collection's role, makes it a member of `kb_reader` so it holds every `api` grant, copies the reader's settings
onto it and writes its row. No reader is granted the table. `app.collection` stays the routing default of
`api.search`'s collection argument and grants nothing.

Downgrade restores 0047's predicate with the search path 0054 pinned and 0053's comment, and drops the table. The
reader roles the indexer created stay, since roles belong to the whole server, and read nothing private once the
table is gone.
"""

from __future__ import annotations

from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_SCOPES_TABLE = """
CREATE TABLE kb.reader_scopes (
    role_name text NOT NULL,
    collection text NOT NULL,
    CONSTRAINT reader_scopes_pkey PRIMARY KEY (role_name, collection)
)
"""

_SCOPES_COMMENT = (
    "COMMENT ON TABLE kb.reader_scopes IS "
    "'Which private collection each reader role may read. The indexer writes a row when it gives a private "
    "collection its own login role, and api.visible_collection reads it by session_user. No reader is granted it.'"
)

_VISIBLE_COLLECTION = """
CREATE OR REPLACE FUNCTION api.visible_collection(p_collection text)
  RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, kb, public, pg_temp AS $$
    SELECT p_collection IS NOT NULL
       AND (NOT EXISTS (
              SELECT 1 FROM kb.index_fingerprint f
              WHERE f.collection = p_collection AND f.switches ->> 'private' = 'true'
            )
            OR EXISTS (
              SELECT 1 FROM kb.reader_scopes s
              WHERE s.role_name = session_user::text AND s.collection = p_collection
            )
            OR has_table_privilege(session_user, 'kb.index_fingerprint', 'SELECT'))
$$
"""

_PREVIOUS_VISIBLE_COLLECTION = """
CREATE OR REPLACE FUNCTION api.visible_collection(p_collection text)
  RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, kb, public, pg_temp AS $$
    SELECT p_collection IS NOT NULL
       AND (p_collection IS NOT DISTINCT FROM current_setting('app.collection', true)
            OR NOT EXISTS (
              SELECT 1 FROM kb.index_fingerprint f
              WHERE f.collection = p_collection AND f.switches ->> 'private' = 'true'
            ))
$$
"""

_VISIBLE_COMMENT = (
    "COMMENT ON FUNCTION api.visible_collection(text) IS "
    "'Whether this session may read one collection: true for every collection whose indexed switches do not say "
    "private: true, and for a private collection only when the session logs in as that collection''s own reader "
    "role. A collection this answers false for behaves throughout the api surface exactly like one that does not "
    "exist. Example: SELECT api.visible_collection(''<collection>'');'"
)

# 0053's comment, byte-identical: what `downgrade` restores.
_PREVIOUS_VISIBLE_COMMENT = (
    "COMMENT ON FUNCTION api.visible_collection(text) IS "
    "'Whether this session may read one collection: true for the session''s own collection (the app.collection "
    "setting a reader connection carries) and for every collection whose indexed switches do not say private: "
    "true. A collection this answers false for behaves throughout the api surface exactly like one that does not "
    "exist. Example: SELECT api.visible_collection(''<collection>'');'"
)


def upgrade() -> None:
    """Create the scope table and key private visibility on the login role."""
    op.execute(_SCOPES_TABLE)
    op.execute(_SCOPES_COMMENT)
    op.execute(_VISIBLE_COLLECTION)
    op.execute(_VISIBLE_COMMENT)


def downgrade() -> None:
    """Put 0047's setting-based predicate and 0053's comment back, and drop the scope table."""
    op.execute(_PREVIOUS_VISIBLE_COLLECTION)
    op.execute(_PREVIOUS_VISIBLE_COMMENT)
    op.execute("DROP TABLE kb.reader_scopes")
