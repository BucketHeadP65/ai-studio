"""Reading an indexed `KnowledgeBaseSchema` back from the database, for a deployment with no checkout on disk.

Every index stamps the validated `KnowledgeBaseSchema` it ran with onto that collection's
`kb.index_fingerprint` row. Readers reach it only through `api.indexed_schema(p_collection)`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.kb.domain.schema import KnowledgeBaseSchema
from engram.kb.exceptions import KnowledgeBaseSchemaMissingError

if TYPE_CHECKING:
    from engram.kb.internal_contracts import IndexedSchemaQueries

_INDEXED_SCHEMA_SQL = "SELECT api.indexed_schema(:collection) AS spec"


async def load_indexed_schema(reader: IndexedSchemaQueries, collection: str) -> KnowledgeBaseSchema:
    """`collection`'s validated `KnowledgeBaseSchema`, read from the database it last indexed into.

    Use it in place of `load_knowledge_base_schema` when there is no checkout on disk. It reads through
    `api.indexed_schema`, the only access the `kb_reader` role has to the fingerprint table.

    Args:
        reader: The capped-read gateway to query through.
        collection: The knowledge base collection to load.

    Returns:
        The `KnowledgeBaseSchema` the collection's most recent index ran with.

    Raises:
        KnowledgeBaseSchemaMissingError: `collection` has no indexed schema on record. The message names
            the collection and `studio index`.
    """
    result = await reader.rows(_INDEXED_SCHEMA_SQL, {"collection": collection}, cap=1)
    raw = result.rows[0].get("spec") if result.rows else None
    if raw is None:
        msg = f"{collection!r} has no indexed knowledge_base_schema on record, run studio index to write one"
        raise KnowledgeBaseSchemaMissingError(msg)
    return KnowledgeBaseSchema.model_validate(raw)
