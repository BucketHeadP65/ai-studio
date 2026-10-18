r"""The pure mirror chunker: a raw `Sources/` mirror body cut into flat `MirrorChunk` drafts sized for retrieval.

A mirror is cut at its headings into heading sections. A heading section within the maximum chunk size is one
chunk, so a mirror with no heading that fits stays whole. A longer heading section is filled into several chunks
paragraph by paragraph, and each of them keeps its heading and section path. Every chunk body is the mirror's own
text, so the chunks hold every word of the mirror in order.

A heading opening with a numbered token (`4` / `3.1` / `7.3.1` / lettered appendix `A.1`) indexes to an
ltree `section_path` (`s4` / `s3.s1` / `s7.s3.s1` / `sA.s1`) using the same `s`-prefixed segments
as `resolver.loc_to_ltree`, so the verification join `raw_chunks.section_path @> claims.loc_path`
matches. Unnumbered headings and the title or preamble before the first heading
carry `section_path = None`. The Load stage assigns `source_id`, `id` and `seq`.
"""

from __future__ import annotations

import re

from itertools import pairwise
from typing import TYPE_CHECKING

from core.markdown import split_frontmatter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from engram.indexing.domain.card import EMBED_TOKEN_BUDGET, content_hash, token_count

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

# A heading line in a mirror: one to four `#` then the heading text.
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,4})\s+(?P<text>.+?)\s*$")

# A numbered-heading prefix: a dotted sequence of integers *or* a lettered appendix label
# (`4`, `3.1`, `7.3.1`, `A`, `A.1`), optionally dot-terminated, then whitespace. The remainder is
# the human heading title. The captured number becomes the `s`-prefixed ltree path.
_NUMBER_PREFIX_RE = re.compile(r"^(?P<number>(?:\d+|[A-Z])(?:\.\d+)*)\.?\s+(?P<title>.*)$")

# A sentence end: a full stop, question or exclamation mark, any closing quotes or brackets, then whitespace,
# or a full-width sentence mark, which needs no whitespace after it.
_SENTENCE_END_RE = re.compile(r"[.!?][\"'\u201d\u2019)\]]*(?=\s)|[\u3002\uff01\uff1f]")

_WORD_RE = re.compile(r"\S+")

MIRROR_CHUNK_TOKEN_BUDGET = EMBED_TOKEN_BUDGET - 500
"""The largest maximum chunk size: the embed budget less room for the title and heading breadcrumb."""


class ChunkSizes(BaseModel):
    """How many tokens a chunk of a mirror aims for and may hold.

    Filling closes a chunk before it would pass `target_tokens`, and no chunk passes `max_tokens`.
    The target must not exceed the maximum, and the maximum must not exceed `MIRROR_CHUNK_TOKEN_BUDGET`.
    """

    model_config = ConfigDict(frozen=True)

    target_tokens: int = Field(gt=0)
    """The tokens filling aims for in one chunk."""
    max_tokens: int = Field(gt=0, le=MIRROR_CHUNK_TOKEN_BUDGET)
    """The most tokens one chunk holds. A heading section within it stays one chunk."""

    @model_validator(mode="after")
    def _target_within_maximum(self) -> ChunkSizes:
        """Refuse a target over the maximum."""
        if self.target_tokens > self.max_tokens:
            msg = f"chunk target_tokens {self.target_tokens} exceeds max_tokens {self.max_tokens}"
            raise ValueError(msg)
        return self


DEFAULT_CHUNK_SIZES = ChunkSizes(target_tokens=400, max_tokens=800)
"""The chunk sizes the indexer uses when its host names none: a 400-token target and an 800-token maximum."""

DEFAULT_PLANNER_CEILING_TOKENS = 12_000
"""The most tokens of text the chunk planner is shown at once when its host names no other ceiling. Longer text is filled."""


class MirrorChunk(BaseModel):
    """A draft raw-chunk from a mirror body, the pre-Load shape of `kb.raw_chunks`.

    `section_path` is the numbered heading indexed to an ltree label, or `None`
    for unnumbered headings and the preamble. `heading` is the verbatim heading text
    (`None` for the preamble). `ord` is the dense document-order index, the Load
    stage turns it into `seq` and mints the `<source_id>:<seq>` id.
    """

    model_config = ConfigDict(frozen=True)

    ord: int
    section_path: str | None
    heading: str | None
    body: str


type _Span = tuple[int, int]
"""The start and end offsets of a span of text."""


def _section_path(heading_text: str) -> str | None:
    """The ltree `section_path` for a heading, or `None` when it is not numbered."""
    match = _NUMBER_PREFIX_RE.match(heading_text)
    if match is None:
        return None
    return ".".join(f"s{segment}" for segment in match.group("number").split("."))


def _fits(text: str, max_tokens: int) -> bool:
    """Whether `text` holds no more than `max_tokens` tokens."""
    return token_count(text) <= max_tokens


def _trimmed(text: str, start: int, end: int) -> _Span:
    """The span from `start` to `end` without the whitespace at either edge."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


PARAGRAPH_NUMBERING_VERSION = 1
"""The version of how `_paragraph_spans` splits and numbers paragraphs.

You must raise it whenever `_paragraph_spans` splits text differently, so every stored cut plan is chosen again.
"""


def cut_plan_identity(planner_identity: str) -> str:
    """The identity a cut plan is stored under and the index fingerprint names: the chunk planner's identity and the paragraph numbering version.

    Args:
        planner_identity: The chunk planner's identity, its model name, prompt version and thinking level.

    Returns:
        The cut plan identity, such as `gpt-6-luna/prompt-1/thinking-low/paragraphs-1`.
    """
    return f"{planner_identity}/paragraphs-{PARAGRAPH_NUMBERING_VERSION}"


def _paragraph_spans(text: str) -> list[_Span]:
    """The paragraphs of `text` in reading order, split at blank lines outside code fences."""
    spans: list[_Span] = []
    start: int | None = None
    end = 0
    in_fence = False
    offset = 0
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if line.strip():
            if start is None:
                start = offset + len(line) - len(line.lstrip())
            end = offset + len(line.rstrip())
        elif not in_fence and start is not None:
            spans.append((start, end))
            start = None
        offset += len(line)
    if start is not None:
        spans.append((start, end))
    return spans


def _sentence_spans(text: str, span: _Span) -> list[_Span]:
    """The sentences of one paragraph of `text`, each ending at a sentence end or at the paragraph's end."""
    start, end = span
    spans: list[_Span] = []
    for match in _SENTENCE_END_RE.finditer(text, start, end):
        spans.append(_trimmed(text, start, match.end()))
        start = match.end()
    spans.append(_trimmed(text, start, end))
    return [(first, last) for first, last in spans if first < last]


def _line_spans(text: str, span: _Span) -> list[_Span]:
    """The lines of one span of `text`, without blank ones."""
    start, end = span
    spans: list[_Span] = []
    line_end = text.find("\n", start, end)
    while line_end != -1:
        spans.append(_trimmed(text, start, line_end))
        start = line_end + 1
        line_end = text.find("\n", start, end)
    spans.append(_trimmed(text, start, end))
    return [(first, last) for first, last in spans if first < last]


def _longest_fit(text: str, start: int, ends: Sequence[int], max_tokens: int) -> int:
    """How many of `ends` may close a span from `start` within `max_tokens`, at least one."""
    low, high = 1, len(ends)
    while low < high:
        middle = (low + high + 1) // 2
        if _fits(text[start : ends[middle - 1]], max_tokens):
            low = middle
        else:
            high = middle - 1
    return low


def _cut_word(text: str, start: int, end: int, max_tokens: int) -> list[_Span]:
    """Cut one word of `text` over `max_tokens` into pieces within it, each as long as fits."""
    pieces: list[_Span] = []
    while start < end:
        # Double a window from `max_tokens` characters until it no longer fits, so no probe reads far past the piece.
        reach = min(end, start + max_tokens)
        while reach < end and _fits(text[start:reach], max_tokens):
            reach = min(end, start + 2 * (reach - start))
        taken = _longest_fit(text, start, range(start + 1, reach + 1), max_tokens)
        pieces.append((start, start + taken))
        start += taken
    return pieces


def _cut_at_maximum(text: str, span: _Span, max_tokens: int) -> list[_Span]:
    """Cut a span of `text` into pieces within `max_tokens` at its last word break, or inside a word over it."""
    pieces: list[_Span] = []
    words = [(match.start(), match.end()) for match in _WORD_RE.finditer(text, *span)]
    first = 0
    while first < len(words):
        start, word_end = words[first]
        if not _fits(text[start:word_end], max_tokens):
            pieces.extend(_cut_word(text, start, word_end, max_tokens))
            first += 1
            continue
        # A word holds at least one token, so a piece never holds more words than the maximum.
        ends = [end for _, end in words[first : first + max_tokens]]
        taken = _longest_fit(text, start, ends, max_tokens)
        pieces.append((start, ends[taken - 1]))
        first += taken
    return pieces


def _units(text: str, max_tokens: int) -> list[_Span]:
    """The spans of `text` filling joins, each within `max_tokens`: paragraphs, else sentences, else lines, else pieces of a line."""
    units: list[_Span] = []
    for paragraph in _paragraph_spans(text):
        if _fits(text[slice(*paragraph)], max_tokens):
            units.append(paragraph)
            continue
        for sentence in _sentence_spans(text, paragraph):
            if _fits(text[slice(*sentence)], max_tokens):
                units.append(sentence)
                continue
            for line in _line_spans(text, sentence):
                if _fits(text[slice(*line)], max_tokens):
                    units.append(line)
                else:
                    units.extend(_cut_at_maximum(text, line, max_tokens))
    return units


def _fill(text: str, sizes: ChunkSizes) -> list[str]:
    """Fill `text` into chunks in reading order, each closed before the next span would pass the target."""
    spans: list[_Span] = []
    for start, end in _units(text, sizes.max_tokens):
        if spans and _fits(text[spans[-1][0] : end], sizes.target_tokens):
            spans[-1] = (spans[-1][0], end)
        else:
            spans.append((start, end))
    return [text[start:end] for start, end in spans]


def planner_paragraphs(section: str) -> list[str]:
    """The paragraphs of a heading section's text as the chunk planner is shown them, numbered from 1 by their place.

    Paragraphs are split at blank lines outside code fences, each the section's own text with edge whitespace removed.

    Args:
        section: The text of one heading section, its heading left out.

    Returns:
        The paragraphs in reading order.
    """
    return [section[start:end] for start, end in _paragraph_spans(section)]


def accepted_starts(answer: Sequence[int], paragraph_count: int) -> tuple[int, ...] | None:
    """The paragraph numbers where a new chunk starts, from a chunk planner's answer, or `None` when it is refused.

    An answer is accepted only when it is strictly increasing and every number names a paragraph, from 1 to
    `paragraph_count`. Paragraph 1 always starts the first chunk, so it is dropped. An empty answer is accepted.

    Args:
        answer: The paragraph numbers the chunk planner answered.
        paragraph_count: How many paragraphs the chunk planner was shown.

    Returns:
        The accepted numbers without 1, or `None` for a refused answer.
    """
    if any(number < 1 or number > paragraph_count for number in answer):
        return None
    if any(earlier >= later for earlier, later in pairwise(answer)):
        return None
    return tuple(number for number in answer if number != 1)


def heading_sections_for_planner(text: str, sizes: ChunkSizes, *, ceiling_tokens: int) -> list[str]:
    """The text of each heading section of a mirror the chunk planner is asked to cut, its heading left out.

    Such a heading section is over `sizes.max_tokens`, within `ceiling_tokens` and of at least two paragraphs. Each
    text is exactly the text `chunk_mirror` looks up in its cut plans by content hash.

    Args:
        text: The raw mirror file text, frontmatter included.
        sizes: How many tokens a chunk aims for and may hold.
        ceiling_tokens: The most tokens of text the chunk planner is shown at once.

    Returns:
        The heading sections' texts in document order.
    """
    _, body = split_frontmatter(text)
    return [
        content
        for _, content in _heading_sections(body)
        if not _fits(content, sizes.max_tokens) and _fits(content, ceiling_tokens) and len(_paragraph_spans(content)) > 1
    ]


def _cut_at_starts(text: str, starts: tuple[int, ...], sizes: ChunkSizes) -> list[str]:
    """Cut `text` before each paragraph `starts` numbers, filling any chunk over the maximum."""
    spans = _paragraph_spans(text)
    bounds = [0, *(number - 1 for number in starts), len(spans)]
    chunks: list[str] = []
    for first, end in pairwise(bounds):
        chunk = text[spans[first][0] : spans[end - 1][1]]
        chunks.extend([chunk] if _fits(chunk, sizes.max_tokens) else _fill(chunk, sizes))
    return chunks


def _cut_unheaded(text: str, sizes: ChunkSizes, answer: Sequence[int] | None) -> list[str]:
    """The chunks of a heading section's text that does not fit one chunk: cut by its cut plan, else filled."""
    starts = accepted_starts(answer, len(_paragraph_spans(text))) if answer is not None else None
    return _fill(text, sizes) if starts is None else _cut_at_starts(text, starts, sizes)


def _heading_sections(body: str) -> Iterator[tuple[str | None, str]]:
    """Each heading section of `body` as its heading and its text, the text before the first heading only when it holds text."""
    heading: str | None = None
    lines: list[str] = []
    for line in body.splitlines():
        match = _HEADING_RE.match(line)
        if match is None:
            lines.append(line)
            continue
        content = "\n".join(lines).strip()
        if heading is not None or content:
            yield heading, content
        heading = match.group("text")
        lines = []
    content = "\n".join(lines).strip()
    if heading is not None or content:
        yield heading, content


def chunk_mirror(text: str, sizes: ChunkSizes, cut_plans: Mapping[str, Sequence[int]] | None = None) -> Iterator[MirrorChunk]:
    """Cut a raw mirror's body into `MirrorChunk` drafts sized for retrieval.

    Frontmatter is stripped first. The body is cut at its headings: every heading opens a heading section, even
    one with an empty body, and the text before the first heading is one only when it holds text. A section within
    `sizes.max_tokens` is one chunk, so a body with no heading that fits stays whole. A longer section with a cut
    plan is cut before each paragraph the plan numbers, from 1, and a chunk of it over the maximum is filled. A plan
    that is not strictly increasing paragraph numbers is refused. A longer section with no plan, or a refused one, is
    filled paragraph by paragraph, each chunk closed before the next paragraph would pass `sizes.target_tokens`. A
    paragraph over the maximum is cut at its sentence ends, a sentence over it at its line breaks, a line over it at
    its last word break within it, and a word over it inside the word. No chunk passes the maximum. Each chunk keeps
    its section's heading and section path, and each body is a piece of the section's own text, edge whitespace removed.

    Args:
        text: The raw mirror file text, frontmatter included.
        sizes: How many tokens a chunk aims for and may hold.
        cut_plans: The cut plans by the content hash of the section text each cuts, as `heading_sections_for_planner` yields that
            text. `None` fills every longer section.

    Returns:
        The chunks in document order, with dense `ord` values from `0`.
    """
    _, body = split_frontmatter(text)
    plans = cut_plans or {}
    chunks: list[MirrorChunk] = []
    for heading, content in _heading_sections(body):
        section_path = _section_path(heading) if heading else None
        parts = [content] if _fits(content, sizes.max_tokens) else _cut_unheaded(content, sizes, plans.get(content_hash(content)))
        for part in parts:
            chunks.append(MirrorChunk(ord=len(chunks), section_path=section_path, heading=heading, body=part))
    return iter(chunks)
