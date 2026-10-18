"""The database says private: ``api.visible_collection`` reads the inverted switch, and a queued refresh is ``kb.refresh``.

Revision ID: 0047
Revises: 0046
Create Date: 2026-09-04 00:00:00.000000

A knowledge base declares itself private (``retrieval.private: true``), and the indexed switches
carry that declaration as ``private: true`` rather than ``library: false``. The stored JSON moves
with the name: a row saying ``library: false`` gains ``private: true``, every other row simply
loses the key, and an absent key reads as shared on both sides of the inversion, so the four
collections that carry no switches at all stay shared and the one that opted out stays private.

``api.visible_collection`` is replaced to read the new key. The predicate is otherwise unchanged:
a collection is visible when it is the session's own (``app.collection``, the setting a reader
connection carries) or when its fingerprint does not say it is private. Every ``api`` view and
read function calls this function rather than reading the flag itself, so nothing else changes.

The comment on ``api.retrieval_switches`` is rewritten because ``describe_schema`` serves it to an
agent, and it names the flag.

Queued work moves with the names: a ``coordination.jobs`` row of kind ``knowledge_base.ingest``
becomes ``kb.refresh``, except a live keyed row whose new kind already holds a live row for that
key, which stays put rather than tripping the live-key unique index.

Every step is guarded on the object it needs, so a database that carries no knowledge base tables
skips it, and on the key it moves, so a second run is a no-op. Downgrade is the mirror.
"""

from __future__ import annotations

from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def _pg_literal(value: str) -> str:
    """Wrap ``value`` as a single-escaped SQL string literal, so it is never concatenated raw."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


_VISIBLE_COLLECTION = """
CREATE OR REPLACE FUNCTION api.visible_collection(p_collection text)
  RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = kb, public, pg_catalog AS $$
    SELECT p_collection IS NOT NULL
       AND (p_collection IS NOT DISTINCT FROM current_setting('app.collection', true)
            OR NOT EXISTS (
              SELECT 1 FROM kb.index_fingerprint f
              WHERE f.collection = p_collection AND f.switches ->> 'private' = 'true'
            ))
$$
"""

_PREVIOUS_VISIBLE_COLLECTION = """
CREATE OR REPLACE FUNCTION api.visible_collection(p_collection text)
  RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = kb, public, pg_catalog AS $$
    SELECT p_collection IS NOT NULL
       AND (p_collection IS NOT DISTINCT FROM current_setting('app.collection', true)
            OR NOT EXISTS (
              SELECT 1 FROM kb.index_fingerprint f
              WHERE f.collection = p_collection AND f.switches ->> 'library' = 'false'
            ))
$$
"""

_VISIBLE_COMMENT = (
    "COMMENT ON FUNCTION api.visible_collection(text) IS "
    "'Whether this session may read one collection: true for the session''s own collection "
    "(the app.collection setting a reader connection carries) and for every collection whose "
    "indexed switches do not say private: true. A collection this answers false for behaves "
    "throughout the api surface exactly like one that does not exist. Example: "
    "SELECT api.visible_collection(''research'');'"
)

_PREVIOUS_VISIBLE_COMMENT = (
    "COMMENT ON FUNCTION api.visible_collection(text) IS "
    "'Whether this session may read one collection: true for the session''s own collection "
    "(the app.collection setting a reader connection carries) and for every collection whose "
    "compiled switches do not say library: false. A collection this answers false for behaves "
    "throughout the api surface exactly like one that does not exist. Example: "
    "SELECT api.visible_collection(''research'');'"
)

_SWITCHES_COMMENT = (
    "COMMENT ON FUNCTION api.retrieval_switches(text) IS "
    "'The indexed retrieval switches for one collection (embeddings/sql/graph, each boolean, plus "
    "private: true when the collection keeps out of the library over several knowledge bases), or "
    "an empty object when that collection has never been indexed or is not visible to this "
    "session. The one reader-granted read onto index_fingerprint, the table itself stays "
    "ungranted. Example: SELECT api.retrieval_switches(''research'');'"
)

_PREVIOUS_SWITCHES_COMMENT = (
    "COMMENT ON FUNCTION api.retrieval_switches(text) IS "
    "'The compiled retrieval switches for one collection (embeddings/sql/graph, each boolean, plus "
    "library: false when the collection keeps out of the shared library), or an empty object when "
    "that collection has never compiled or is not visible to this session. The one reader-granted "
    "door onto index_fingerprint -- the table itself stays ungranted. Example: "
    "SELECT api.retrieval_switches(''research'');'"
)


def _invert_switch(*, old: str, new: str, was: str, becomes: str) -> str:
    """DDL moving the privacy declaration on every stored switches object from one key to the other.

    A row whose `old` key carries `was` gains `new` set to `becomes`, and every other row carrying
    `old` simply loses it, since an absent key reads the same on both sides. Guarded on the table
    and on the key, so a database without the knowledge base tables skips it and a second run is a
    no-op.

    Args:
        old: The JSON key the declaration is read from.
        new: The JSON key it is written to.
        was: The `old` value that means private, as a JSON literal.
        becomes: The `new` value that means private, as a JSON literal.
    """
    return f"""
DO $mig$
BEGIN
  IF to_regclass('kb.index_fingerprint') IS NOT NULL THEN
    UPDATE kb.index_fingerprint
       SET switches = CASE
             WHEN switches ->> {_pg_literal(old)} = {_pg_literal(was)}
             THEN (switches - {_pg_literal(old)}) || jsonb_build_object({_pg_literal(new)}, {_pg_literal(becomes)}::jsonb)
             ELSE switches - {_pg_literal(old)}
           END
     WHERE switches ? {_pg_literal(old)};
  END IF;
END
$mig$
"""


def _replace_visible_collection(*, definition: str, comment: str) -> str:
    """DDL replacing `api.visible_collection` and its comment, skipped where the `api` schema is absent."""
    return f"""
DO $mig$
BEGIN
  IF to_regnamespace('api') IS NOT NULL THEN
    EXECUTE {_pg_literal(definition)};
    EXECUTE {_pg_literal(comment)};
  END IF;
END
$mig$
"""


def _set_switches_comment(comment: str) -> str:
    """DDL rewriting the comment on `api.retrieval_switches`, skipped where that function is absent."""
    return f"""
DO $mig$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'api' AND p.proname = 'retrieval_switches'
  ) THEN
    EXECUTE {_pg_literal(comment)};
  END IF;
END
$mig$
"""


def _move_job_kind(*, old: str, new: str) -> str:
    """DDL moving every ``coordination.jobs`` row of one kind to the other, minus the ones that would collide.

    ``ux_jobs_live_key`` is UNIQUE on ``(kind, key)`` while a job is live (queued, running or
    failed) and keyed, so a keyed live row whose target kind already holds a live row for that key
    stays on its old kind rather than failing the whole migration.

    Args:
        old: The kind rows are moved off.
        new: The kind rows are moved onto.
    """
    return (
        f"UPDATE coordination.jobs AS j SET kind = {_pg_literal(new)} WHERE j.kind = {_pg_literal(old)}"
        " AND NOT (j.state IN ('queued', 'running', 'failed') AND j.key <> ''"
        f" AND EXISTS (SELECT 1 FROM coordination.jobs o WHERE o.kind = {_pg_literal(new)}"
        " AND o.key = j.key AND o.state IN ('queued', 'running', 'failed')))"
    )


def upgrade() -> None:
    """Invert the privacy switch, teach `api.visible_collection` the new key, and move queued refresh jobs."""
    op.execute(_invert_switch(old="library", new="private", was="false", becomes="true"))
    op.execute(_replace_visible_collection(definition=_VISIBLE_COLLECTION, comment=_VISIBLE_COMMENT))
    op.execute(_set_switches_comment(_SWITCHES_COMMENT))
    op.execute(_move_job_kind(old="knowledge_base.ingest", new="kb.refresh"))


def downgrade() -> None:
    """Put the flag, the function and the job kind back."""
    op.execute(_invert_switch(old="private", new="library", was="true", becomes="false"))
    op.execute(_replace_visible_collection(definition=_PREVIOUS_VISIBLE_COLLECTION, comment=_PREVIOUS_VISIBLE_COMMENT))
    op.execute(_set_switches_comment(_PREVIOUS_SWITCHES_COMMENT))
    op.execute(_move_job_kind(old="kb.refresh", new="knowledge_base.ingest"))
