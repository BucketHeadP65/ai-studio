"""The unit ids the tool results of one agent run named, read from the run's own messages.

A unit counts only when a result carries something of it, never when it carries a bare id that points at it. A search
names each hit, a fetch the unit it read, a leveled fetch its own unit and every child it lists, and a graph answer each
node. A SQL row names the units in its `id`, `page_id`, `section_id`, `claim_id` and `source_id` columns, never the other
end of a relation such as `src_id`, `dst_id` or `subject_id`. A claim counts only when its text came with it: a claim hit
with its snippet, a fetched claim with its body, a SQL row with its `text`, `quote`, `snippet` or `body`. In code mode the
model reads the `run_code` output, so a `run_code` call names a unit only when one of its program's own tool calls
returned it, read by the same rules, and the output carries its id or its ref. Any other result, such as a schema
description or the receipt of the answer, names no unit. A tool return of any shape is read without raising.
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

    from core.ai.retrieval.contracts import GraphNode
    from pydantic_ai.messages import ModelMessage, ToolReturnContent

# The SQL columns that name the row's own unit or the unit that holds it.
_UNIT_COLUMNS = frozenset({"id", "page_id", "section_id", "claim_id", "source_id"})

# The SQL columns that carry a unit's own text.
_TEXT_COLUMNS = ("text", "quote", "snippet", "body")

# The level a search hit, a fetched unit or a SQL row gives a claim, and the columns that hold that level.
_CLAIM = "claim"
_LEVEL_COLUMNS = ("hit_level", "level")
_CLAIM_COLUMN = "claim_id"

# The key of a `run_code` return's metadata under which code mode records its program's tool returns.
_PROGRAM_RETURNS = "tool_returns"

# The kinds of ref that name a listing rather than a unit.
_LISTING_KINDS = frozenset({"collection", "group"})

# A word of output text runs between whitespace and the marks that never sit inside an id or a ref.
_WORD = re.compile(r"[^\s\[\](){}<>|,;'\"`]+")

# The marks a word may carry at its ends that are punctuation around an id, never part of it.
_EDGE_MARKS = ".:!?*"


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
        for unit in _units_of_return(part, collection)
    )
    return tuple(dict.fromkeys(named))


def _units_of_return(part: ToolReturnPart, collection: str) -> Iterator[str]:
    """The unit ids one tool return names, a `run_code` call's limited to those its output carries."""
    if part.tool_name != RUN_CODE:
        yield from _units_of(part.content, collection)
        return
    carried = frozenset(_carried_ids(part.content, collection))
    for returned in _program_returns(part):
        yield from (unit for unit in _units_of(returned.content, collection) if unit in carried)


def _program_returns(part: ToolReturnPart) -> Iterator[ToolReturnPart]:
    """Each tool return a `run_code` call's program received, as code mode recorded them on the call's return."""
    metadata = part.metadata
    program_returns = metadata.get(_PROGRAM_RETURNS) if isinstance(metadata, Mapping) else None
    if isinstance(program_returns, Mapping):
        yield from (returned for returned in program_returns.values() if isinstance(returned, ToolReturnPart))


def _units_of(content: ToolReturnContent, collection: str) -> Iterator[str]:
    """The unit ids one tool's result names."""
    if isinstance(content, SearchResult):
        yield from (hit.id for hit in content.hits if hit.hit_level != _CLAIM or _has_text(hit.snippet))
    elif isinstance(content, FetchResult):
        if content.level != _CLAIM or _has_text(content.body):
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
    """Every text cell under a unit column, at any depth of a JSON value, a claim's only when its row carries its text."""
    if isinstance(value, str):
        return
    if isinstance(value, Mapping):
        names_claim = any(value.get(column) == _CLAIM for column in _LEVEL_COLUMNS)
        has_text = any(_has_text(value.get(column)) for column in _TEXT_COLUMNS)
        for key, cell in value.items():
            if key in _UNIT_COLUMNS and isinstance(cell, str):
                is_claim = key == _CLAIM_COLUMN or (key == "id" and names_claim)
                if has_text or not is_claim:
                    yield cell
            else:
                yield from _unit_cells(cell)
    elif isinstance(value, Sequence):
        for item in value:
            yield from _unit_cells(item)


def _has_text(value: ToolReturnContent) -> bool:
    """Whether a field holds text with more than whitespace in it."""
    return isinstance(value, str) and bool(value.strip())


def _carried_ids(value: ToolReturnContent, collection: str) -> Iterator[str]:
    """Every id or ref a `run_code` output carries: each word of its text, and of its text keys, as written and as a ref."""
    if isinstance(value, str):
        words = (word.strip(_EDGE_MARKS) for word in _WORD.findall(value))
        for word in words:
            if word:
                yield word
                yield from _ref_units((word,), collection)
    elif isinstance(value, Mapping):
        for key, cell in value.items():
            yield from _carried_ids(key, collection)
            yield from _carried_ids(cell, collection)
    elif isinstance(value, Sequence):
        for item in value:
            yield from _carried_ids(item, collection)


def _ref_units(refs: Iterable[str], collection: str) -> Iterator[str]:
    """The bare unit id each ref names, leaving out a listing and anything that is no ref."""
    for ref in refs:
        try:
            resolved = parse_ref(ref, collection)
        except BadRefError:
            continue
        if resolved.kind not in _LISTING_KINDS:
            yield resolved.unit_id
