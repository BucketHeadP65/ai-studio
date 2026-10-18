"""The unit ids the tool results of one agent run named, read from the run's own messages.

A search names each hit, a fetch the unit it read, a leveled fetch its own unit and every child it lists, and a graph
answer each node and each provenance claim. A SQL row names each column called `id` or ending in `_id`. Code mode's
`run_code` output is read whole, the way the model reads it: every value under such a key, and every word of any other
text that carries a `.`, a `:` or a `#`, one of which every page, section, claim and chunk id carries. Any other result,
such as a schema description or the receipt of the answer, names no unit.
"""

from __future__ import annotations

import re

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from core.ai.disclosure.contracts import DisclosureEnvelope
from core.ai.retrieval.contracts import RUN_CODE, FetchResult, GraphPathsResult, GraphResult, SearchResult
from pydantic_ai.messages import ModelRequest, ToolReturnPart

from engram.evals.shared.contracts import BadRefError, parse_ref

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from core.ai.retrieval.contracts import GraphEdge, GraphNode
    from pydantic_ai.messages import ModelMessage, ToolReturnContent

# A word of text runs between whitespace and the marks that never sit inside an id.
_WORD = re.compile(r"[^\s\[\](){}<>|,;'\"`]+")

# The marks a word may carry at its ends that are punctuation around an id, never part of it.
_EDGE_MARKS = ".:!?*"

# Every page id carries a `.`, every section and claim id a `#`, and every chunk id a `:`.
_ID_MARKS = (".", ":", "#")

# The kinds of ref that name a listing rather than a unit.
_LISTING_KINDS = frozenset({"collection", "group"})


def seen_units(messages: Sequence[ModelMessage], *, collection: str) -> tuple[str, ...]:
    """Every unit id the tool results in `messages` named, each once, in the order the model received them.

    Args:
        messages: One agent run's messages, its tool returns among them.
        collection: The knowledge base the run read, which owns a ref that names no knowledge base of its own.

    Returns:
        The bare unit ids: page, section, claim, raw chunk and source ids alike.
    """
    named = (
        unit
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
        for unit in _units_of(part, collection)
    )
    return tuple(dict.fromkeys(named))


def _units_of(part: ToolReturnPart, collection: str) -> Iterator[str]:
    """The unit ids one tool return names, its `run_code` output read whole."""
    content = part.content
    if part.tool_name == RUN_CODE:
        yield from _read_whole(content, collection)
    elif isinstance(content, SearchResult):
        yield from (hit.id for hit in content.hits)
    elif isinstance(content, FetchResult):
        yield content.id
    elif isinstance(content, DisclosureEnvelope):
        yield from _ref_units((content.ref, *(child.ref for child in content.children)), collection)
    elif isinstance(content, GraphResult):
        yield from _graph_units(content.nodes, content.edges)
    elif isinstance(content, GraphPathsResult):
        for path in content.paths:
            yield from _graph_units(path.nodes, path.edges)
    else:
        yield from _id_cells(content)


def _graph_units(nodes: Iterable[GraphNode], edges: Iterable[GraphEdge]) -> Iterator[str]:
    """Each node's page id, then each edge's provenance claim."""
    yield from (node.id for node in nodes)
    yield from (edge.provenance_claim for edge in edges if edge.provenance_claim is not None)


def _id_cells(value: ToolReturnContent) -> Iterator[str]:
    """Every text cell under a key called `id` or ending in `_id`, at any depth of a JSON value."""
    if isinstance(value, str):
        return
    if isinstance(value, Mapping):
        for key, cell in value.items():
            if _is_id_key(key) and isinstance(cell, str):
                yield cell
            else:
                yield from _id_cells(cell)
    elif isinstance(value, Sequence):
        for item in value:
            yield from _id_cells(item)


def _read_whole(value: ToolReturnContent, collection: str) -> Iterator[str]:
    """Every id cell of a JSON value, and every word of its other text that could be an id or a ref."""
    if isinstance(value, str):
        words = (word.strip(_EDGE_MARKS) for word in _WORD.findall(value))
        yield from _ref_units((word for word in words if any(mark in word for mark in _ID_MARKS)), collection)
    elif isinstance(value, Mapping):
        for key, cell in value.items():
            if _is_id_key(key) and isinstance(cell, str):
                yield cell
            else:
                yield from _read_whole(cell, collection)
    elif isinstance(value, Sequence):
        for item in value:
            yield from _read_whole(item, collection)


def _ref_units(refs: Iterable[str], collection: str) -> Iterator[str]:
    """The bare unit id each ref names, leaving out a listing and a word that is no ref."""
    for ref in refs:
        try:
            resolved = parse_ref(ref, collection)
        except BadRefError:
            continue
        if resolved.kind not in _LISTING_KINDS:
            yield resolved.unit_id


def _is_id_key(key: str) -> bool:
    """Whether a column or key names a unit: `id` itself or a name ending in `_id`."""
    return key == "id" or key.endswith("_id")
