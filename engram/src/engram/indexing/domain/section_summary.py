r"""Derive a section's indexed summary (`kb.sections.summary`) from its own body.

`derive_section_summary` applies the first rule that matches:

1. A heading with no body before its first subheading: its child headings, comma-joined.
2. A body no longer than `spec.inline_gate_chars`: the body verbatim.
3. A longer body opening with prose: its leading `spec.leading_sentences` sentences.
4. A longer body opening with a list: its leading `spec.leading_sentences` items, flattened and joined with `"; "`.
5. A longer body opening with a table or a fenced code block: an empty summary.

Sentences split on `.`, `!` or `?` followed by whitespace or the end of the text, skipping a fixed list of common
abbreviations. An abbreviation missing from that list still splits, and a mark followed directly by a closing quote or
parenthesis does not.
"""

from __future__ import annotations

import re

from typing import TYPE_CHECKING, Literal

from markdown_it import MarkdownIt

if TYPE_CHECKING:
    from collections.abc import Sequence

    from markdown_it.token import Token

    from engram.kb.contracts import Section, SectionSummarySpec

# The commonmark preset with GFM tables on, so a leading pipe table parses as `table_open`, not as a paragraph.
_PARSER = MarkdownIt("commonmark").enable("table")

_LIST_ITEM_JOIN = "; "

# Abbreviations whose trailing period never ends a sentence. They are lower-cased and matched against the
# lower-cased whitespace-delimited token ending at the candidate split point.
_ABBREVIATIONS = frozenset(
    {
        "e.g.",
        "i.e.",
        "etc.",
        "vs.",
        "cf.",
        "approx.",
        "dr.",
        "mr.",
        "mrs.",
        "ms.",
        "prof.",
        "fig.",
        "st.",
        "jr.",
        "sr.",
        "no.",
        "vol.",
        "inc.",
        "ltd.",
        "co.",
        "corp.",
        "u.s.",
        "u.k.",
    }
)

# A run of sentence-ending punctuation followed by whitespace or the end, so "3.14" and "Node.js" stay whole.
_SENTENCE_END_RE = re.compile(r"[.!?]+(?=\s|$)")


def derive_section_summary(section: Section, sections: Sequence[Section], spec: SectionSummarySpec) -> str:
    """Derive one section's indexed summary from its own body, or from its child headings when the body is empty.

    Args:
        section: The section to summarize.
        sections: Every section of the section's own page, in parse order (`KnowledgeBaseNote.sections`).
            Read only to find the child headings of a section with an empty body.
        spec: The knowledge base's `SectionSummarySpec` (leading-sentence count, inline gate).

    Returns:
        The derived summary, or `""` when the body has neither prose nor a list and the section has
        no children.
    """
    body = section.body.strip()
    if not body:
        return ", ".join(_child_headings(section, sections))

    if len(body) <= spec.inline_gate_chars:
        return body

    tokens = _PARSER.parse(body)
    leading = _leading_block_kind(tokens)
    if leading == "prose":
        return _leading_sentences(tokens, spec.leading_sentences)
    if leading == "list":
        return _LIST_ITEM_JOIN.join(_leading_list_items(tokens, spec.leading_sentences))
    return ""


def _child_headings(section: Section, sections: Sequence[Section]) -> list[str]:
    """The headings of every other section whose ltree path is a direct descendant of `section`'s."""
    prefix = f"{section.path}."
    return [other.heading for other in sections if other.path.startswith(prefix)]


def _leading_block_kind(tokens: Sequence[Token]) -> Literal["prose", "list", "other"]:
    """Classify a body's first top-level block token: a paragraph, a list, or anything else."""
    if not tokens:
        return "other"
    first = tokens[0]
    if first.type == "paragraph_open":
        return "prose"
    if first.type in {"bullet_list_open", "ordered_list_open"}:
        return "list"
    return "other"


def _leading_sentences(tokens: Sequence[Token], n: int) -> str:
    """The leading `n` sentences of the body's leading run of top-level paragraphs, joined with a space."""
    parts: list[str] = []
    i = 0
    while i < len(tokens) and tokens[i].type == "paragraph_open" and tokens[i].level == 0:
        parts.append(tokens[i + 1].content)
        i += 3  # paragraph_open, inline, paragraph_close
    sentences = _split_sentences(" ".join(parts))
    return " ".join(sentences[:n])


def _leading_list_items(tokens: Sequence[Token], n: int) -> list[str]:
    """The text of the leading list's first `n` items, each flattened (nested markup dropped)."""
    items: list[str] = []
    i = 0
    while i < len(tokens) and len(items) < n:
        token = tokens[i]
        if token.type == "list_item_open" and token.level == 1:
            text, i = _flatten_list_item(tokens, i + 1)
            items.append(text)
        else:
            i += 1
    return items


def _flatten_list_item(tokens: Sequence[Token], start: int) -> tuple[str, int]:
    """The joined text of every `inline` token inside one list item, and the index after its close."""
    depth = 1
    parts: list[str] = []
    i = start
    while i < len(tokens) and depth > 0:
        token = tokens[i]
        if token.type == "list_item_open":
            depth += 1
        elif token.type == "list_item_close":
            depth -= 1
        elif token.type == "inline":
            parts.append(token.content)
        i += 1
    return " ".join(part.strip() for part in parts if part.strip()), i


def _split_sentences(text: str) -> list[str]:
    """Split `text` into sentences on `.`, `!` or `?` followed by whitespace or the end, skipping known abbreviations."""
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(text):
        end = match.end()
        if _ends_in_abbreviation(text[start:end]):
            continue
        sentence = text[start:end].strip()
        if sentence:
            sentences.append(sentence)
        start = end
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def _ends_in_abbreviation(text: str) -> bool:
    """Whether `text`'s last whitespace-delimited token is a known abbreviation (case-insensitive)."""
    stripped = text.strip()
    if not stripped:
        return False
    return stripped.rsplit(maxsplit=1)[-1].lower() in _ABBREVIATIONS
