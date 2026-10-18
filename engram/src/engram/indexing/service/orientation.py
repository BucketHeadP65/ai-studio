"""The orientation pages: synthetic `kb.pages` rows assembled from a knowledge base's `_meta/` orientation documents."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.markdown import split_frontmatter
from core.observability import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from engram.indexing.internal_contracts import KnowledgeBaseReader

_logger = get_logger(__name__)

# The `_meta/` orientation pages whose text feeds the orientation rows, in reading order.
_ORIENTATION_SOURCES: tuple[str, ...] = ("schema", "purpose", "retrieval-policy")

QUERY_AUDIENCE = "query"
AUTHORING_AUDIENCE = "authoring"
BOTH_AUDIENCES = "both"
_AUDIENCES: tuple[str, ...] = (QUERY_AUDIENCE, AUTHORING_AUDIENCE)
_AUDIENCE_FIELD = "audience"

ORIENTATION_PAGES: Mapping[str, tuple[str, str]] = {
    QUERY_AUDIENCE: ("meta.about", "About this memory"),
    AUTHORING_AUDIENCE: ("meta.authoring", "Authoring rules for this memory"),
}
"""The unscoped id and the title of the synthetic `kb.pages` row for each audience. These rows own no source file."""

def orientation_page_id(base_id: str, collection: str) -> str:
    """The stored id of a synthetic orientation page, scoped by its collection.

    Args:
        base_id: The unscoped orientation id from `ORIENTATION_PAGES` (`meta.about` or `meta.authoring`).
        collection: The indexing knowledge base's collection.

    Returns:
        `f"{base_id}@{collection}"`.
    """
    return f"{base_id}@{collection}"


def orientation_page_ids(collection: str) -> frozenset[str]:
    """Every synthetic orientation page id an index of `collection` can write.

    Args:
        collection: The indexing knowledge base's collection.

    Returns:
        The scoped id of each page in `ORIENTATION_PAGES`.
    """
    return frozenset(orientation_page_id(page_id, collection) for page_id, _title in ORIENTATION_PAGES.values())


def _audiences_of(text: str, name: str) -> tuple[str, ...]:
    """The audiences one orientation document's `audience` field names, `query` alone when absent or unknown."""
    declared = split_frontmatter(text)[0].get(_AUDIENCE_FIELD)
    if declared == BOTH_AUDIENCES:
        return _AUDIENCES
    if declared in _AUDIENCES:
        return (str(declared),)
    if declared is not None:
        _logger.warning(
            "_meta/{page}.md declares {field}: {declared}. Expected query, authoring or both, treating as query",
            page=name,
            field=_AUDIENCE_FIELD,
            declared=repr(declared),
        )
    return (QUERY_AUDIENCE,)


def read_orientation(reader: KnowledgeBaseReader) -> dict[str, str]:
    """One orientation text per audience, joined from the `_meta/` orientation documents in a fixed order.

    Each document contributes its body without its frontmatter. A document's `audience` frontmatter field
    (`query`, `authoring` or `both`) picks the texts it joins, and a document without one joins `query` alone.
    A missing document is skipped with a warning. The same inputs always give byte-identical texts.

    Args:
        reader: Reads the knowledge base's `_meta/` documents.

    Returns:
        The text for each audience, keyed by `query` and `authoring`.
    """
    parts: dict[str, list[str]] = {audience: [] for audience in _AUDIENCES}
    for name in _ORIENTATION_SOURCES:
        try:
            text = reader.meta_document(name).text
        except (OSError, FileNotFoundError) as exc:  # a sparse knowledge base may lack an orientation page
            _logger.warning("Skipping missing _meta/{page}.md: {reason}", page=name, reason=str(exc))
            continue
        body = split_frontmatter(text)[1].strip()
        for audience in _audiences_of(text, name):
            parts[audience].append(body)
    return {audience: "\n\n".join(texts) for audience, texts in parts.items()}
