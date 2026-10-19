"""The unit ids the tool results of one agent run carried something of, read from the run's own messages.

A search names each hit and each folded unit that carries a snippet, a fetch the unit it read, a leveled fetch its own
unit and every child it lists, and a graph answer each node. A SQL row names the units in its `id`, `page_id`,
`section_id`, `claim_id` and `source_id` columns, never a relation end such as `src_id`. A unit's text is a hit's
snippet, a folded unit's snippet, a fetched body, a leveled fetch's content, a child's summary or a SQL row's `text`,
`quote`, `snippet` or `body`, and a claim counts only with its text. A text holding only whitespace and the ellipsis
that ends a cut snippet is no text. In code mode
a unit any program tool call of the case returned counts when a `run_code` output carries the first 60 characters of its
text, or, once every returned text the output carries is cut out, its id, ref or page id as a word. Any other result
names no unit, and a tool return of any shape is read without raising.
"""

from __future__ import annotations

import re

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.ai.disclosure.contracts import DisclosureEnvelope
from core.ai.retrieval.contracts import RUN_CODE, FetchResult, GraphPathsResult, GraphResult, SearchResult
from pydantic_ai.messages import ModelRequest, ToolReturnPart

from engram.evals.outcomes.contracts import page_of
from engram.evals.shared.contracts import BadRefError, parse_ref

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from core.ai.retrieval.contracts import GraphNode
    from pydantic_ai.messages import ModelMessage, ToolReturnContent

# The SQL columns that name the row's own unit or the unit that holds it.
_UNIT_COLUMNS = frozenset({"id", "page_id", "section_id", "claim_id", "source_id"})

# The SQL columns that carry a unit's own text, in the order they are read.
_TEXT_COLUMNS = ("text", "quote", "snippet", "body")

# The level a search hit, a fetched unit or a SQL row gives a claim, and the columns that hold that level.
_CLAIM = "claim"
_LEVEL_COLUMNS = ("hit_level", "level")
_CLAIM_COLUMN = "claim_id"

# The key of a `run_code` return's metadata under which code mode records its program's tool returns.
_PROGRAM_RETURNS = "tool_returns"

# The kinds of ref that name a listing rather than a unit.
_LISTING_KINDS = frozenset({"collection", "group"})

# The mark a cut snippet ends with. A snippet that holds nothing else carries no text.
_ELLIPSIS = "\u2026"

# How many leading characters of a unit's text, whitespace collapsed, an output must carry for the unit to count.
_TEXT_KEY_CHARS = 60

# The escapes a printed Python value or a JSON text shows in place of a character: a surrogate pair, a code point, a
# quote mark or a backslash, then a line break or a tab, which reads as a space.
_ESCAPE = re.compile(r"\\u(d[89ab][0-9a-f]{2})\\u(d[c-f][0-9a-f]{2})|\\u([0-9a-f]{4})|\\(['\"\\])|\\[nrt]", re.IGNORECASE)

# A word of output text runs between whitespace and the marks that never sit inside an id or a ref.
_WORD = re.compile(r"[^\s\[\](){}<>|,;'\"`=]+")

# The marks a word may carry at its ends that are punctuation around an id, never part of it.
_EDGE_MARKS = ".:!?*"


@dataclass(frozen=True)
class _Received:
    """One unit a tool result carried: its id, the page that holds it, and the text of it the result gave, if any."""

    unit: str
    page: str
    text: str | None


def seen_units(messages: Sequence[ModelMessage], *, collection: str) -> tuple[str, ...]:
    """Every unit id the tool results in `messages` carried something of, each once, in the order the run received them.

    Args:
        messages: One agent run's messages, its tool returns among them.
        collection: The knowledge base the run read, which owns a ref that names no knowledge base of its own.

    Returns:
        The bare unit ids: page, section, claim, raw chunk and source ids alike.
    """
    seen: dict[str, None] = {}
    program_received: list[_Received] = []
    program_texts: list[str] = []
    for part in _tool_returns(messages):
        if part.tool_name != RUN_CODE:
            seen.update(dict.fromkeys(received.unit for received in _received(part.content, collection)))
            continue
        for returned in _program_returns(part):
            program_received.extend(_received(returned.content, collection))
            program_texts.extend(_texts(returned.content))
        seen.update(dict.fromkeys(_carried(part.content, program_received, program_texts, collection)))
    return tuple(seen)


def bare_unit_id(ref: str, *, collection: str) -> str:
    """The bare unit id a ref names, or the ref itself when it is already bare, names a listing or is no ref.

    Args:
        ref: A bare id, or a collection-qualified ref such as `page:<collection>/<id>`, `section:` or `unit:`.
        collection: The knowledge base that owns a ref naming no knowledge base of its own.

    Returns:
        The unit id, such as `<id>` for `page:<collection>/<id>`.
    """
    unit = _unit_of_ref(ref, collection)
    return unit if unit is not None else ref


def _tool_returns(messages: Sequence[ModelMessage]) -> Iterator[ToolReturnPart]:
    """Every tool return the run received, in order."""
    for message in messages:
        if isinstance(message, ModelRequest):
            yield from (part for part in message.parts if isinstance(part, ToolReturnPart))


def _program_returns(part: ToolReturnPart) -> Iterator[ToolReturnPart]:
    """Each tool return a `run_code` call's program received, as code mode recorded them on the call's return."""
    metadata = part.metadata
    program_returns = metadata.get(_PROGRAM_RETURNS) if isinstance(metadata, Mapping) else None
    if isinstance(program_returns, Mapping):
        yield from (returned for returned in program_returns.values() if isinstance(returned, ToolReturnPart))


def _received(content: ToolReturnContent, collection: str) -> Iterator[_Received]:
    """Each unit one tool's result carried something of, with its page and its text."""
    if isinstance(content, SearchResult):
        yield from _hits_received(content)
    elif isinstance(content, FetchResult):
        if content.level != _CLAIM or _has_text(content.body):
            yield _Received(unit=content.id, page=page_of(content.id), text=content.body)
    elif isinstance(content, DisclosureEnvelope):
        yield from _ref_received(content.ref, content.content, collection)
        for child in content.children:
            yield from _ref_received(child.ref, child.summary, collection)
    elif isinstance(content, GraphResult):
        yield from _node_received(content.nodes)
    elif isinstance(content, GraphPathsResult):
        yield from _node_received(node for path in content.paths for node in path.nodes)
    else:
        yield from _row_received(content)


def _hits_received(result: SearchResult) -> Iterator[_Received]:
    """Each hit of a search with its snippet, a claim hit only when its snippet came with it, then its folded units that carry one."""
    for hit in result.hits:
        if hit.hit_level != _CLAIM or _has_text(hit.snippet):
            yield _Received(unit=hit.id, page=hit.page_id or page_of(hit.id), text=hit.snippet)
        for unit in hit.folded_units:
            if _has_text(unit.snippet):
                yield _Received(unit=unit.id, page=page_of(unit.id), text=unit.snippet)


def _ref_received(ref: str, text: str | None, collection: str) -> Iterator[_Received]:
    """The unit a disclosed ref names, with its text, unless the ref names a listing."""
    unit = _unit_of_ref(ref, collection)
    if unit is not None:
        yield _Received(unit=unit, page=page_of(unit), text=text)


def _node_received(nodes: Iterable[GraphNode]) -> Iterator[_Received]:
    """Each node's page."""
    return (_Received(unit=node.id, page=node.id, text=None) for node in nodes)


def _row_received(value: ToolReturnContent) -> Iterator[_Received]:
    """Every unit a JSON value's rows name in a unit column, with the row's text, a claim's only when its row carries text."""
    if isinstance(value, str):
        return
    if isinstance(value, Mapping):
        names_claim = any(value.get(column) == _CLAIM for column in _LEVEL_COLUMNS)
        text = next((cell for column in _TEXT_COLUMNS if _has_text(cell := value.get(column))), None)
        for key, cell in value.items():
            if key in _UNIT_COLUMNS and isinstance(cell, str):
                is_claim = key == _CLAIM_COLUMN or (key == "id" and names_claim)
                if text is not None or not is_claim:
                    yield _Received(unit=cell, page=page_of(cell), text=text)
            else:
                yield from _row_received(cell)
    elif isinstance(value, Sequence):
        for item in value:
            yield from _row_received(item)


def _texts(content: ToolReturnContent) -> Iterator[str]:
    """Every text one tool's result returned, a SQL row's text columns included whether or not the row names a unit."""
    if isinstance(content, SearchResult):
        for hit in content.hits:
            if hit.snippet is not None:
                yield hit.snippet
            yield from (unit.snippet for unit in hit.folded_units if unit.snippet is not None)
    elif isinstance(content, FetchResult):
        if content.body is not None:
            yield content.body
    elif isinstance(content, DisclosureEnvelope):
        if content.content is not None:
            yield content.content
        yield from (child.summary for child in content.children)
    elif not isinstance(content, GraphResult | GraphPathsResult):
        yield from _row_texts(content)


def _row_texts(value: ToolReturnContent) -> Iterator[str]:
    """Every text column cell of a JSON value's rows, at any depth."""
    if isinstance(value, str):
        return
    if isinstance(value, Mapping):
        for key, cell in value.items():
            if key in _TEXT_COLUMNS and isinstance(cell, str):
                yield cell
            else:
                yield from _row_texts(cell)
    elif isinstance(value, Sequence):
        for item in value:
            yield from _row_texts(item)


def _carried(output: ToolReturnContent, received: Sequence[_Received], texts: Sequence[str], collection: str) -> Iterator[str]:
    """The received units a `run_code` output carries: by their text, then by an id, ref or page id outside every returned text."""
    text = _collapsed(" ".join(_ESCAPE.sub(_unescaped, string) for string in _strings(output)))
    by_text = {unit.unit for unit in received if (key := _text_key(unit.text)) and key in text}
    cut = sorted({whole for whole in map(_collapsed, texts) if _has_text(whole) and whole[:_TEXT_KEY_CHARS] in text}, key=len, reverse=True)
    words = frozenset(_words(_without(text, cut), collection))
    carried = by_text | {unit.unit for unit in received if unit.unit in words or unit.page in words}
    yield from (unit.unit for unit in received if unit.unit in carried)


def _strings(value: ToolReturnContent) -> Iterator[str]:
    """Every text of a JSON value, its text keys included, in order."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, cell in value.items():
            yield from _strings(key)
            yield from _strings(cell)
    elif isinstance(value, Sequence):
        for item in value:
            yield from _strings(item)


def _unescaped(escape: re.Match[str]) -> str:
    """The character an escape stands for, a space for an escaped line break or tab."""
    high, low, code_point, mark = escape.groups()
    if high is not None and low is not None:
        return chr(0x10000 + ((int(high, 16) - 0xD800) << 10) + (int(low, 16) - 0xDC00))
    if code_point is not None:
        return chr(int(code_point, 16))
    return mark if mark is not None else " "


def _collapsed(text: str) -> str:
    """The text with every run of whitespace collapsed to one space."""
    return " ".join(text.split())


def _text_key(text: str | None) -> str:
    """The leading characters of a unit's text an output must carry, empty when the unit came with no text."""
    return _collapsed(text)[:_TEXT_KEY_CHARS] if text is not None and _has_text(text) else ""


def _without(text: str, texts: Sequence[str]) -> str:
    """The text with every run that starts like one of `texts` and follows it cut out, the longest texts first."""
    for whole in texts:
        key = whole[:_TEXT_KEY_CHARS]
        start = text.find(key)
        while start >= 0:
            end = start
            while end < len(text) and end - start < len(whole) and text[end] == whole[end - start]:
                end += 1
            text = f"{text[:start]} {text[end:]}"
            start = text.find(key, start + 1)
    return text


def _words(text: str, collection: str) -> Iterator[str]:
    """Each word of the text, as written and as the bare unit id it names as a ref."""
    for word in (raw.strip(_EDGE_MARKS) for raw in _WORD.findall(text)):
        if word:
            yield word
            yield bare_unit_id(word, collection=collection)


def _has_text(value: ToolReturnContent) -> bool:
    """Whether a field holds text with more than whitespace and the ellipsis that ends a cut snippet in it."""
    return isinstance(value, str) and bool(value.replace(_ELLIPSIS, "").strip())


def _unit_of_ref(ref: str, collection: str) -> str | None:
    """The bare unit id a ref names, or None for a listing or anything that is no ref."""
    try:
        resolved = parse_ref(ref, collection)
    except BadRefError:
        return None
    return None if resolved.kind in _LISTING_KINDS else resolved.unit_id
