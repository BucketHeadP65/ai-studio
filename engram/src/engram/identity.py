"""Which knowledge base a command or a retrieval tool is acting on, and how far it has moved since it was indexed.

Two values, one builder each. `KnowledgeBaseIdentity` is what a command prints before it does any work. `KnowledgeBaseStamp`
(from `core.ai.retrieval`) rides every search and fetch answer. You must read both through the admin gateway. The index
manifest and fingerprint live in the `kb` schema, which the reader role cannot see.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from core.ai.retrieval.contracts import KnowledgeBaseStamp
from pydantic import BaseModel, ConfigDict

from engram.indexing.contracts import content_hash

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.ai.retrieval.contracts import SqlGateway

    from engram.indexing.contracts import KnowledgeBaseReader

IndexOutcome = Literal["ok", "failed", "never"]

# The manifest holds one row per indexed file, so the read must never be trimmed by the gateway's row cap.
_WHOLE_MANIFEST = 1_000_000

_VERSION_TABLE_SQL = "SELECT to_regclass('alembic_version') IS NOT NULL AS present"
_REVISION_SQL = "SELECT version_num FROM alembic_version"
_INDEXED_AT_SQL = "SELECT indexed_at FROM kb.index_fingerprint WHERE collection = :collection"
_MANIFEST_SQL = "SELECT path, content_hash FROM kb.index_manifest WHERE collection = :collection"


class LastRun(BaseModel):
    """The newest recorded refresh run, supplied by a host that keeps a refresh history."""

    model_config = ConfigDict(frozen=True)

    finished_at: datetime
    outcome: Literal["ok", "failed"]
    detail: str


class KnowledgeBaseIdentity(BaseModel):
    """What a knowledge-base-touching command is about to act on, stated before it acts."""

    model_config = ConfigDict(frozen=True)

    slug: str
    root: Path
    database: str
    collection: str
    db_revision: str | None
    code_revision: str | None
    last_indexed_at: datetime | None
    last_index_outcome: IndexOutcome
    last_index_reason: str = ""
    changed_since: int


async def read_identity(
    gateway: SqlGateway,
    *,
    on_disk: Mapping[str, str],
    slug: str,
    root: Path,
    database: str,
    collection: str,
    code_revision: str | None,
    last_run: LastRun | None = None,
) -> KnowledgeBaseIdentity:
    """Assemble the identity from what the caller knows and the reads against the database and the disk.

    Args:
        gateway: The admin gateway, which can see `alembic_version` and the `kb` schema.
        on_disk: The knowledge base's current content hashes by path (`disk_content_hashes` over its reader), walked by
            the caller, so a file that cannot be read is never mistaken for a database that cannot be reached.
        slug: The knowledge base slug from its `knowledge-base-schema.json`.
        root: The knowledge base root on disk.
        database: The database as a credential-free `host:port/dbname`.
        collection: The partition whose last index is reported.
        code_revision: The alembic head the installed code declares.
        last_run: The newest refresh run the host recorded, when it keeps a history. A failed run that
            finished after the last successful index is the outcome the identity reports.

    Returns:
        The identity, with `changed_since` counting the files that differ from the stored manifest.

    Raises:
        RuntimeError: When the stored manifest holds more rows than one read returns, which would make `changed_since` wrong.
    """
    db_revision = await _db_revision(gateway)
    indexed_at = await _last_indexed_at(gateway, collection)
    outcome, reason = _outcome(indexed_at, last_run)
    stored = await _manifest_hashes(gateway, collection)
    return KnowledgeBaseIdentity(
        slug=slug,
        root=root,
        database=database,
        collection=collection,
        db_revision=db_revision,
        code_revision=code_revision,
        last_indexed_at=indexed_at,
        last_index_outcome=outcome,
        last_index_reason=reason,
        changed_since=changed_count(stored, on_disk),
    )


async def read_stamp(gateway: SqlGateway, reader: KnowledgeBaseReader, *, slug: str, collection: str) -> KnowledgeBaseStamp:
    """Stamp one answer with the knowledge base slug, its last successful index, and how many files moved since.

    The count is the files added, edited or removed since the manifest was written, the same set an incremental
    index starts from. Every call reads every file the reader yields, so the count is never stale and its cost
    grows with the size of the knowledge base.

    Args:
        gateway: The admin gateway, which can see the `kb` schema.
        reader: The reader over the knowledge base's files on disk.
        slug: The knowledge base slug from its `knowledge-base-schema.json`.
        collection: The partition whose last index is reported.

    Returns:
        The stamp for this answer.

    Raises:
        RuntimeError: When the stored manifest holds more rows than one read returns, which would make the count wrong.
    """
    indexed_at = await _last_indexed_at(gateway, collection)
    stored = await _manifest_hashes(gateway, collection)
    return KnowledgeBaseStamp(slug=slug, indexed_at=indexed_at, changed_since=changed_count(stored, disk_content_hashes(reader)))


def disk_content_hashes(reader: KnowledgeBaseReader) -> dict[str, str]:
    """Every unit note and source mirror the reader yields, as `path` to its content hash.

    Args:
        reader: The reader over the knowledge base's files on disk.

    Returns:
        The content hash of each file, keyed by its path.
    """
    return {document.path: content_hash(document.text) for document in (*reader.unit_documents(), *reader.source_mirrors())}


def changed_count(stored: Mapping[str, str], current: Mapping[str, str]) -> int:
    """How many paths differ between the stored manifest and the disk, counting an addition, an edit and a removal alike.

    Args:
        stored: The content hashes the index manifest holds, keyed by path.
        current: The content hashes on disk now, keyed by path.

    Returns:
        The number of paths whose hash differs or that exist on one side only.
    """
    return sum(1 for path in set(stored) | set(current) if stored.get(path) != current.get(path))


def _outcome(indexed_at: datetime | None, last_run: LastRun | None) -> tuple[IndexOutcome, str]:
    if last_run is not None and last_run.outcome == "failed" and (indexed_at is None or last_run.finished_at > indexed_at):
        return "failed", last_run.detail
    if indexed_at is None:
        return "never", ""
    return "ok", ""


async def _db_revision(gateway: SqlGateway) -> str | None:
    present = await gateway.rows(_VERSION_TABLE_SQL, cap=1)
    if not present.rows or present.rows[0].get("present") is not True:
        return None
    result = await gateway.rows(_REVISION_SQL, cap=1)
    if not result.rows:
        return None
    value = result.rows[0].get("version_num")
    return value if isinstance(value, str) else None


async def _last_indexed_at(gateway: SqlGateway, collection: str) -> datetime | None:
    result = await gateway.rows(_INDEXED_AT_SQL, {"collection": collection}, cap=1)
    if not result.rows:
        return None
    value = result.rows[0].get("indexed_at")
    return value if isinstance(value, datetime) else None


async def _manifest_hashes(gateway: SqlGateway, collection: str) -> dict[str, str]:
    result = await gateway.rows(_MANIFEST_SQL, {"collection": collection}, cap=_WHOLE_MANIFEST)
    if result.truncated_message is not None:
        msg = f"the index manifest for {collection!r} holds more than {_WHOLE_MANIFEST} rows, the freshness count would be wrong"
        raise RuntimeError(msg)
    return {str(row["path"]): str(row["content_hash"]) for row in result.rows}
