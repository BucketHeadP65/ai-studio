"""The unit ids the tool results of one agent run named, read from the run's own messages.

A unit counts only when a result carries something of it, never when it carries a bare id that points at it. A search
names each hit, a fetch the unit it read, a leveled fetch its own unit and every child it lists, and a graph answer each
node. A SQL row names the units in its `id`, `page_id`, `section_id`, `claim_id` and `source_id` columns, never the other
end of a relation such as `src_id`, `dst_id` or `subject_id`. A `run_code` call names what its program's own tool calls
returned, read by the same rules, and never what the program prints or returns. Any other result, such as a schema
description or the receipt of the answer, names no unit. A tool return of any shape is read without raising.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from core.ai.disclosure.contracts import DisclosureEnvelope
from core.ai.retrieval.contracts import RUN_CODE, FetchResult, GraphPathsResult, GraphResult, SearchResult
from pydantic_ai.messages import ModelRequest, ToolReturnPart

from engram.evals.shared.contracts import BadRefError, parse_ref

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from core.ai.retrieval.contracts import GraphNode
    from pydantic_ai.messages import ModelMessage, ToolReturnContent

# The SQL columns that name the row's own unit or the unit that holds it.
_UNIT_COLUMNS = frozenset({"id", "page_id", "section_id", "claim_id", "source_id"})

# The key of a `run_code` return's metadata under which code mode records its program's tool returns.
_PROGRAM_RETURNS = "tool_returns"

# The kinds of ref that name a listing rather than a unit.
_LISTING_KINDS = frozenset({"collection", "group"})


def seen_units(messages: Sequence[ModelMessage], *, collection: str) -> tuple[str, ...]:
    """Every unit id the tool results in `messages` named, each once, in the order the run received them.

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
        for received in _received(part)
        for unit in _units_of(received.content, collection)
    )
    return tuple(dict.fromkeys(named))


def _received(part: ToolReturnPart) -> Iterator[ToolReturnPart]:
    """The tool return itself, or for a `run_code` call each tool return its program received, as code mode recorded them."""
    if part.tool_name != RUN_CODE:
        yield part
        return
    metadata = part.metadata
    program_returns = metadata.get(_PROGRAM_RETURNS) if isinstance(metadata, Mapping) else None
    if isinstance(program_returns, Mapping):
        yield from (returned for returned in program_returns.values() if isinstance(returned, ToolReturnPart))


def _units_of(content: ToolReturnContent, collection: str) -> Iterator[str]:
    """The unit ids one tool's result names."""
    if isinstance(content, SearchResult):
        yield from (hit.id for hit in content.hits)
    elif isinstance(content, FetchResult):
        yield content.id
    elif isinstance(content, DisclosureEnvelope):
        yield from _ref_units((content.ref, *(child.ref for child in content.children)), collection)
    elif isinstance(content, GraphResult):
        yield from _node_ids(content.nodes)
    elif isinstance(content, GraphPathsResult):
        for path in content.paths:
            yield from _node_ids(path.nodes)
    else:
        yield from _unit_cells(content)


def _node_ids(nodes: Iterable[GraphNode]) -> Iterator[str]:
    """Each node's page id."""
    return (node.id for node in nodes)


def _unit_cells(value: ToolReturnContent) -> Iterator[str]:
    """Every text cell under a unit column, at any depth of a JSON value, whatever its keys are."""
    if isinstance(value, str):
        return
    if isinstance(value, Mapping):
        for key, cell in value.items():
            if key in _UNIT_COLUMNS and isinstance(cell, str):
                yield cell
            else:
                yield from _unit_cells(cell)
    elif isinstance(value, Sequence):
        for item in value:
            yield from _unit_cells(item)


def _ref_units(refs: Iterable[str], collection: str) -> Iterator[str]:
    """The bare unit id each ref names, leaving out a listing and anything that is no ref."""
    for ref in refs:
        try:
            resolved = parse_ref(ref, collection)
        except BadRefError:
            continue
        if resolved.kind not in _LISTING_KINDS:
            yield resolved.unit_id
