"""Format outliners: pure `(content) -> map` and `(content, part) -> slice` functions per format.

An outliner turns one format's text into a map of named parts and serves any named part back as
text. Outliners are framework-free: no store, no pydantic-ai types, deterministic on the same
content, so a map computed from one tool invocation names parts a later invocation can slice.
"""

from __future__ import annotations

import csv
import io
import json
import re

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Required, TypedDict

from markdown_it import MarkdownIt

if TYPE_CHECKING:
    from pydantic import JsonValue


class MapStub(TypedDict, total=False):
    """One named part of a map: its ready-to-pass token plus format-specific hints.

    `size_chars` accompanies every stub whose content is withheld, so the model can price the slice.
    A stub carrying its whole `value` omits it, and `type` too, because both are visible in the value.
    """

    part: Required[str]
    size_chars: int
    level: int
    snippet: str
    summary: str
    changes: str
    type: str
    value: JsonValue
    keys: list[str]
    element_keys: list[str]
    first_item: JsonValue


class ColumnInfo(TypedDict):
    """One CSV column: its header name and inferred type (`int`, `float`, or `text`)."""

    name: str
    type: str


class FormatMapResult(TypedDict, total=False):
    """A format's map: the `parts` stubs plus format-specific extras."""

    parts: Required[list[MapStub]]
    columns: list[ColumnInfo]
    row_count: int
    sample_rows: list[str]
    item_count: int
    file_count: int


class Outliner(Protocol):
    """One format's mapping and slicing behavior."""

    def map(self, content: str) -> FormatMapResult | None:
        """The map of the named parts of `content`.

        Args:
            content: The text to map.

        Returns:
            The map, or `None` when this content has no parts worth mapping.
        """

    def slice(self, content: str, part: str) -> str | None:
        """The text of one named part of `content`.

        Args:
            content: The text the part is taken from.
            part: A part token from the map of the same content.

        Returns:
            The part's text, or `None` when `part` names nothing in this content.
        """


_ATX_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]")
"""A heading-shaped line, skipped when scanning a section's body for its snippet."""

_FRONTMATTER_FENCE = "---"
"""A leading YAML frontmatter block's delimiter. A `#` inside the block is a YAML comment, never a heading."""

_INTRO_PART = "(intro)"
"""Part token for text that precedes the first heading."""

_MIN_HEADINGS = 2
"""Fewer headings than this and the content is served whole rather than mapped."""

_MIN_CSV_RECORDS = 2
"""A header and one data row, the least a CSV maps with."""

_MIN_DIFF_SECTIONS = 2
"""Fewer file sections than this and the patch is served whole."""

_SNIPPET_MARKER = "..."
"""Visible marker appended to a snippet that was cut at `snippet_chars`."""


@dataclass(frozen=True)
class _Section:
    """One markdown part: its token, heading level, owned text (subsections included), and snippet."""

    token: str
    level: int
    text: str
    snippet: str | None


@dataclass(frozen=True)
class _Heading:
    """One CommonMark heading: the source lines it spans (end exclusive), its level, and title."""

    start: int
    end: int
    level: int
    title: str


@dataclass(frozen=True)
class MarkdownOutliner:
    """Maps markdown by its headings, where a part is a section with its subsections.

    Headings are found by a CommonMark tokenizer, so ATX and setext headings count and nothing inside a code block does.
    A leading `---` frontmatter block is metadata, not structure. Text before the first heading maps as the `(intro)`
    part, and a repeated title gets an occurrence suffix such as ` (2)` on its part token.
    """

    snippet_chars: int = 80
    """The longest `snippet`, a section's first body line, before it is cut and marked. A bodyless section has none."""

    def map(self, content: str) -> FormatMapResult | None:
        """The document's sections, each with its level, size and snippet.

        Args:
            content: The markdown to map.

        Returns:
            The map, or `None` when the document has fewer than two headings.
        """
        sections = self._sections(content)
        if sections is None:
            return None
        stubs: list[MapStub] = []
        for section in sections:
            stub: MapStub = {"part": section.token, "level": section.level, "size_chars": len(section.text)}
            if section.snippet is not None:
                snippet = section.snippet
                stub["snippet"] = snippet if len(snippet) <= self.snippet_chars else snippet[: self.snippet_chars] + _SNIPPET_MARKER
            stubs.append(stub)
        return {"parts": stubs}

    def slice(self, content: str, part: str) -> str | None:
        """The named section's text, subsections included.

        Args:
            content: The markdown the section is taken from.
            part: A section's part token from the document's map.

        Returns:
            The section's text, or `None` for an unknown part.
        """
        sections = self._sections(content)
        if sections is None:
            return None
        return next((section.text for section in sections if section.token == part), None)

    def _sections(self, content: str) -> list[_Section] | None:
        """Parse the document into sections, or `None` when it has too few headings to map."""
        lines = content.splitlines()
        headings = _headings(lines)
        if len(headings) < _MIN_HEADINGS:
            return None
        sections: list[_Section] = []
        first = headings[0].start
        if any(line.strip() for line in lines[:first]):
            intro = lines[:first]
            sections.append(_Section(token=_INTRO_PART, level=1, text="\n".join(intro), snippet=_first_body_line(_past_frontmatter(intro))))
        seen: dict[str, int] = {}
        for index, heading in enumerate(headings):
            boundary = next((h.start for h in headings[index + 1 :] if h.level <= heading.level), len(lines))
            occurrence = seen.get(heading.title, 0) + 1
            seen[heading.title] = occurrence
            token = heading.title if occurrence == 1 else f"{heading.title} ({occurrence})"
            sections.append(
                _Section(
                    token=token,
                    level=heading.level,
                    text="\n".join(lines[heading.start : boundary]),
                    snippet=_first_body_line(lines[heading.end : boundary]),
                )
            )
        return sections


def _headings(lines: list[str]) -> list[_Heading]:
    """The document's CommonMark headings, their line spans counted over the whole of `lines`."""
    # The tokenizer reads a frontmatter block as a thematic break and a setext heading, so it is left out and its length added back.
    offset = _frontmatter_end(lines)
    tokens = MarkdownIt().parse("\n".join(lines[offset:]))
    found: list[_Heading] = []
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.map is None:
            continue
        follower = tokens[index + 1] if index + 1 < len(tokens) else None
        title = follower.content.strip() if follower is not None and follower.type == "inline" else ""
        found.append(_Heading(start=token.map[0] + offset, end=token.map[1] + offset, level=int(token.tag[1:]), title=title))
    return found


def _frontmatter_end(lines: list[str]) -> int:
    """The line index just past a leading `---` frontmatter block, or 0 when there is none."""
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        return 0
    for number, line in enumerate(lines[1:], start=1):
        if line.strip() == _FRONTMATTER_FENCE:
            return number + 1
    return 0


def _past_frontmatter(lines: list[str]) -> list[str]:
    """`lines` with a leading frontmatter block removed, so an intro snippet is prose, not metadata."""
    return lines[_frontmatter_end(lines) :]


def _first_body_line(lines: list[str]) -> str | None:
    """The first non-blank, non-heading line, the free approximation of a summary."""
    for line in lines:
        if not line.strip() or _ATX_HEADING.match(line):
            continue
        return line.strip()
    return None


_ROWS_PART = re.compile(r"rows (\d+)-(\d+)")
"""The CSV part grammar: a 1-based inclusive data-row range."""


@dataclass(frozen=True)
class CsvOutliner:
    """Maps CSV by its header: columns with inferred types, the row count, and parts that are ranges of rows.

    A part is written `rows A-B`, 1-based and inclusive over the data rows. Any range inside the data slices, and an end
    past the last row is clamped. Every slice repeats the header. Content is parsed as CSV records, so a quoted field
    with an embedded newline counts as one row.
    """

    chunk_rows: int = 200
    """How many rows each part the map lists covers."""

    sample_data_rows: int = 5
    """How many data rows the map shows after the header."""

    type_sample_rows: int = 50
    """How many data rows a column's type (`int`, `float` or `text`) is inferred from, skipping empty cells."""

    def map(self, content: str) -> FormatMapResult | None:
        """The CSV's columns, row count, sample rows and ranges of rows.

        Args:
            content: The CSV text to map.

        Returns:
            The map, or `None` without at least a header and one data row.
        """
        records = _records(content)
        if len(records) < _MIN_CSV_RECORDS:
            return None
        header, data = records[0], records[1:]
        columns: list[ColumnInfo] = [
            {"name": name, "type": _column_type(data[: self.type_sample_rows], index)} for index, name in enumerate(header)
        ]
        row_count = len(data)
        parts: list[MapStub] = []
        for start in range(1, row_count + 1, self.chunk_rows):
            end = min(start + self.chunk_rows - 1, row_count)
            parts.append({"part": f"rows {start}-{end}", "size_chars": len(_serialize([header, *data[start - 1 : end]]))})
        return {
            "parts": parts,
            "columns": columns,
            "row_count": row_count,
            "sample_rows": [_serialize([record]) for record in records[: self.sample_data_rows + 1]],
        }

    def slice(self, content: str, part: str) -> str | None:
        """The header plus the named range of data rows.

        Args:
            content: The CSV text the rows are taken from.
            part: A range written `rows A-B`, 1-based and inclusive.

        Returns:
            The header and the rows as CSV text, or `None` for a part outside the grammar or the range.
        """
        match = _ROWS_PART.fullmatch(part)
        if match is None:
            return None
        records = _records(content)
        if len(records) < _MIN_CSV_RECORDS:
            return None
        start, end = int(match.group(1)), int(match.group(2))
        row_count = len(records) - 1
        if start < 1 or start > row_count or end < start:
            return None
        return _serialize([records[0], *records[start : min(end, row_count) + 1]])


def _records(content: str) -> list[list[str]]:
    """The content's CSV records. A quoted field with an embedded newline stays one record."""
    return list(csv.reader(io.StringIO(content)))


def _serialize(records: list[list[str]]) -> str:
    """`records` re-serialized as CSV text, one line per record (quoting where needed)."""
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(records)
    return buffer.getvalue().rstrip("\n")


def _column_type(rows: list[list[str]], index: int) -> str:
    """`int`, `float`, or `text`, inferred from the column's non-empty sampled cells."""
    values = [row[index] for row in rows if index < len(row) and row[index] != ""]
    if values and all(_parses(value, int) for value in values):
        return "int"
    if values and all(_parses(value, float) for value in values):
        return "float"
    return "text"


def _parses(value: str, kind: type[int | float]) -> bool:
    """Whether `value` parses as `kind`."""
    try:
        kind(value)
    except ValueError:
        return False
    return True


_ITEM_PART = re.compile(r"item (\d+)")
"""The listing part grammar for one item: 1-based over the listing's items."""

_ITEMS_PART = re.compile(r"items (\d+)-(\d+)")
"""The listing part grammar for a range of items: 1-based, inclusive."""


@dataclass(frozen=True)
class ListingOutliner:
    """Maps a JSON array of objects by each item's own summary line, where a part is one item or a range of items.

    Every object must carry `summary_field` as a non-empty string, or the array is not mapped at all. Part tokens are
    `item N` and `items A-B`, 1-based and inclusive, with the end clamped. A sliced item answers as its JSON text.
    """

    summary_field: str = "summary"
    """The field every item must carry as its summary line."""

    def map(self, content: str) -> FormatMapResult | None:
        """The listing's items, each with its own summary line.

        Args:
            content: The JSON listing to map.

        Returns:
            The map, or `None` when the content is not a conforming listing.
        """
        items = self._items(content)
        if items is None:
            return None
        parts: list[MapStub] = []
        for number, (item, summary) in enumerate(items, start=1):
            parts.append({"part": f"item {number}", "size_chars": len(_json_text(item)), "summary": summary})
        return {"parts": parts, "item_count": len(items)}

    def slice(self, content: str, part: str) -> str | None:
        """One item's JSON, or a range of items as a JSON array.

        Args:
            content: The JSON listing the items are taken from.
            part: `item N` or `items A-B`, 1-based and inclusive.

        Returns:
            The JSON text, or `None` for a part outside the grammar or the range.
        """
        items = self._items(content)
        if items is None:
            return None
        if (single := _ITEM_PART.fullmatch(part)) is not None:
            number = int(single.group(1))
            if 1 <= number <= len(items):
                return _json_text(items[number - 1][0])
            return None
        if (ranged := _ITEMS_PART.fullmatch(part)) is not None:
            start, end = int(ranged.group(1)), int(ranged.group(2))
            if 1 <= start <= len(items) and end >= start:
                chosen: list[JsonValue] = [item for item, _summary in items[start - 1 : min(end, len(items))]]
                return _json_text(chosen)
        return None

    def _items(self, content: str) -> list[tuple[dict[str, JsonValue], str]] | None:
        """The items paired with their summary lines, or `None` when the content does not conform."""
        try:
            parsed: JsonValue = json.loads(content)
        except ValueError:
            return None
        if not isinstance(parsed, list) or not parsed:
            return None
        items: list[tuple[dict[str, JsonValue], str]] = []
        for entry in parsed:
            if not isinstance(entry, dict):
                return None
            summary = entry.get(self.summary_field)
            if not isinstance(summary, str) or not summary:
                return None
            items.append((entry, summary))
        return items


def _json_text(value: JsonValue) -> str:
    """`value` as JSON text, the shape a sliced item answers in."""
    return json.dumps(value, ensure_ascii=False)


_DIFF_MARKER = "diff --git "
"""The line prefix that starts one file's section in a unified git diff."""

_DIFF_HEADER = re.compile(r"diff --git a/(\S+) b/(\S+)")
"""The standard section header. Quoted or otherwise unusual paths fall back to positional tokens."""


@dataclass(frozen=True)
class DiffOutliner:
    """Maps a git diff by its file sections, where a part is one file's complete patch.

    It maps text with at least two `diff --git` sections, so a patch of one file is served whole. Text before the first
    section, such as the commit header `git show` prints, maps as the `(intro)` part. A part token is the file's b-side
    path, or `file N` when the header cannot be parsed, with an occurrence suffix such as ` (2)` on a repeated path.
    Each file's stub carries `changes` as `+added -removed` line counts.
    """

    def map(self, content: str) -> FormatMapResult | None:
        """The patch's file sections, each with its added and removed line counts.

        Args:
            content: The unified git diff to map.

        Returns:
            The map, or `None` under two file sections.
        """
        parsed = self._sections(content)
        if parsed is None:
            return None
        intro, sections = parsed
        stubs: list[MapStub] = []
        if intro is not None:
            stubs.append({"part": _INTRO_PART, "size_chars": len(intro)})
        for token, text in sections:
            plus = sum(1 for line in text.splitlines() if line.startswith("+") and not line.startswith("+++"))
            minus = sum(1 for line in text.splitlines() if line.startswith("-") and not line.startswith("---"))
            stubs.append({"part": token, "size_chars": len(text), "changes": f"+{plus} -{minus}"})
        return {"parts": stubs, "file_count": len(sections)}

    def slice(self, content: str, part: str) -> str | None:
        """One file's patch, or the text before the first file.

        Args:
            content: The unified git diff the patch is taken from.
            part: A file's b-side path, `file N`, or `(intro)`.

        Returns:
            The patch text, or `None` for an unknown part.
        """
        parsed = self._sections(content)
        if parsed is None:
            return None
        intro, sections = parsed
        if part == _INTRO_PART:
            return intro
        return next((text for token, text in sections if token == part), None)

    def _sections(self, content: str) -> tuple[str | None, list[tuple[str, str]]] | None:
        """The intro text (or `None`) and the `(token, text)` file sections, or `None` under two."""
        lines = content.splitlines()
        starts = [number for number, line in enumerate(lines) if line.startswith(_DIFF_MARKER)]
        if len(starts) < _MIN_DIFF_SECTIONS:
            return None
        intro_text = "\n".join(lines[: starts[0]])
        intro = intro_text if intro_text.strip() else None
        sections: list[tuple[str, str]] = []
        seen: dict[str, int] = {}
        bounds = [*starts, len(lines)]
        for index, (start, end) in enumerate(zip(starts, bounds[1:], strict=True), start=1):
            header = _DIFF_HEADER.match(lines[start])
            base = header.group(2) if header is not None else f"file {index}"
            occurrence = seen.get(base, 0) + 1
            seen[base] = occurrence
            token = base if occurrence == 1 else f"{base} ({occurrence})"
            sections.append((token, "\n".join(lines[start:end])))
        return intro, sections


@dataclass(frozen=True)
class JsonOutliner:
    """Maps a JSON object by its top-level keys, where a part is one key's value.

    It maps a non-empty object only, since an array is a listing. A small value arrives whole under `value`. A larger
    value's stub carries its `type`, its `size_chars` and a preview: an object's `keys`, an array's `first_item` with
    the `element_keys` of an object element, or a string's `snippet`. A slice is never mapped again.
    """

    small_chars: int = 200
    """The largest value, measured as JSON text, a stub carries whole under `value`."""

    snippet_chars: int = 90
    """The longest string a stub shows as `snippet` before it is cut and marked."""

    def map(self, content: str) -> FormatMapResult | None:
        """The object's top-level keys, each with its value or a preview of it.

        Args:
            content: The JSON object to map.

        Returns:
            The map, or `None` when the content is not a non-empty JSON object.
        """
        obj = _json_object(content)
        if obj is None:
            return None
        stubs: list[MapStub] = []
        for key, value in obj.items():
            text = _json_text(value)
            if len(text) <= self.small_chars:
                stubs.append({"part": key, "value": value})
                continue
            stub: MapStub = {"part": key, "size_chars": len(text), "type": _json_kind(value)}
            if isinstance(value, dict):
                stub["keys"] = list(value)
            elif isinstance(value, list):
                first = value[0]
                if isinstance(first, dict):
                    stub["element_keys"] = list(first)
                stub["first_item"] = first
            elif isinstance(value, str):
                stub["snippet"] = value if len(value) <= self.snippet_chars else value[: self.snippet_chars] + _SNIPPET_MARKER
            stubs.append(stub)
        return {"parts": stubs}

    def slice(self, content: str, part: str) -> str | None:
        """The JSON of one top-level key's value.

        Args:
            content: The JSON object the value is taken from.
            part: A top-level key.

        Returns:
            The value's JSON text, or `None` for an unknown key or content that is not an object.
        """
        obj = _json_object(content)
        if obj is None or part not in obj:
            return None
        return _json_text(obj[part])


def _json_object(content: str) -> dict[str, JsonValue] | None:
    """The content as a non-empty JSON object, or `None` when it is not one."""
    try:
        parsed: JsonValue = json.loads(content)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or not parsed:
        return None
    return parsed


def _json_kind(value: JsonValue) -> str:
    """A short shape label: `array[N]`, `object[N keys]`, or the scalar's type name."""
    if isinstance(value, list):
        return f"array[{len(value)}]"
    if isinstance(value, dict):
        return f"object[{len(value)} keys]"
    return type(value).__name__


def default_outliners() -> dict[str, Outliner]:
    """The stock outliners by format name.

    Returns:
        A fresh registry of `markdown`, `csv`, `listing`, `diff` and `json`.
    """
    return {
        "markdown": MarkdownOutliner(),
        "csv": CsvOutliner(),
        "listing": ListingOutliner(),
        "diff": DiffOutliner(),
        "json": JsonOutliner(),
    }
