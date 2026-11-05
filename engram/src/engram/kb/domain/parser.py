"""Parse markdown text into a `KnowledgeBaseNote`.

Claims written inside fenced code, an HTML comment or an Obsidian `%%` comment are never read.
Sections come from H2 and H3 headings, and an H3 nests under the preceding H2 as a dotted ltree path.
Claims are the top-level bullets under the claims section whose text ends in a `^cN` anchor.
A claim's fields are its direct `key:: value` sub-bullets only, and a `key::` nested deeper is ignored.
Each key is a built-in claim field, an edge relation, or a text or link field the schema's claim grammar declares.
Parsing refuses only a claim without `loc::`, a link-scalar field with more than one link, a multilink field whose text
holds no `[[page]]` link, and a key that is none of those.
A field line whose value runs onto a further line is not read. Its claim names it in `unread_fields` instead, with an
empty `loc` when that line is the `loc::` one.
Vocabulary, `quote::` presence, edge qualifiers and unread fields are checked by the validate stage.
"""

from __future__ import annotations

import re

from typing import TYPE_CHECKING

from core.markdown import heading_slug, split_frontmatter
from markdown_it import MarkdownIt

from engram.kb.domain.note import ClaimBlock, KnowledgeBaseNote, Section, UnitLink, parse_frontmatter
from engram.kb.domain.quoting import quote, unquote
from engram.kb.exceptions import ClaimParseError

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from markdown_it.token import Token

    from engram.kb.domain.schema import ClaimGrammarSpec, KnowledgeBaseSchema


_ANCHOR_RE = re.compile(r"\s*\^(?P<id>c\d+)\s*$")
_FIELD_RE = re.compile(r"^(?P<key>[a-z_]+)::\s*(?P<value>.*)$")
# A `key::` field line whose value runs onto a further line, which `_FIELD_RE` does not match.
_BROKEN_FIELD_RE = re.compile(r"^(?P<key>[a-z_]+)::[^\n]*\n")
_UNIT_LINK_TOKEN_RE = re.compile(r"\[\[[^\]]+?\]\]")
# Paired Obsidian `%%comment%%` inline spans, stripped from captured text defensively.
_OBSIDIAN_INLINE_COMMENT_RE = re.compile(r"%%.*?%%", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# The built-in claim fields whose value is kept as written. `quote` is unwrapped, `subject` and `disputed_by` are links.
_BUILT_IN_TEXT_FIELDS = frozenset({"loc", "kind", "status"})

# A claim bullet sits one bullet list deep under the claims section, its direct field sub-bullets one level deeper.
_CLAIM_ITEM_DEPTH = 1
_FIELD_ITEM_DEPTH = 2


def parse(text: str, *, source_path: str, spec: KnowledgeBaseSchema) -> KnowledgeBaseNote:
    """Parse a note's full markdown text into a typed `KnowledgeBaseNote`.

    Args:
        text: The complete file contents, the `---` frontmatter fence and the body.
        source_path: The path relative to the knowledge base root, carried on the note for validation messages.
        spec: The knowledge base schema.

    Returns:
        The parsed note with its frontmatter, sections and claims.

    Raises:
        FrontmatterError: If the frontmatter `type` is missing or unknown, or a field breaks the page type's schema.
        ClaimParseError: If a claim omits `loc::` or a link-scalar field holds more than one link.
        NoteGrammarError: If a link field is malformed.
    """
    data, body = split_frontmatter(text)
    frontmatter = parse_frontmatter(data, spec)

    body_lines = body.splitlines()
    tokens = MarkdownIt("commonmark").parse(body)

    canon_set = spec.canon_set_for(frontmatter.type)

    sections = _parse_sections(tokens, body_lines, canon_set)
    claims = _parse_claims(tokens, source_path, spec.claims) if spec.claims is not None else []

    return KnowledgeBaseNote(frontmatter=frontmatter, sections=tuple(sections), claims=tuple(claims), source_path=source_path)


def heading_lines(body: str) -> tuple[tuple[int, int], ...]:
    """Where each H2 and H3 heading sits in a page body, in the order `parse` reads them as sections.

    Args:
        body: The page's markdown body, the frontmatter fence left out.

    Returns:
        One pair per heading: the index of its first line and the index of the first line after it, counted from zero.
    """
    tokens = MarkdownIt("commonmark").parse(body)
    return tuple((token.map[0], token.map[1]) for _index, token in _headings(tokens) if token.map is not None)


def title_heading(body: str) -> tuple[str, int, int] | None:
    """The H1 heading that `parse` reads a page's preamble after: the first H1 before the first H2 or H3.

    Args:
        body: The page's markdown body, the frontmatter fence left out.

    Returns:
        The heading's text, the index of its line and the index of the first line after it, counted from zero.
        None when no H1 comes before the first H2 or H3.
    """
    tokens = MarkdownIt("commonmark").parse(body)
    title = _title_token(tokens, _preamble_end(_headings(tokens), len(body.splitlines())))
    if title is None:
        return None
    index, token = title
    if token.map is None:
        return None
    return tokens[index + 1].content, token.map[0], token.map[1]


def _headings(tokens: Sequence[Token]) -> list[tuple[int, Token]]:
    """Each H2 and H3 heading's opening token with its index, in source order."""
    return [(i, t) for i, t in enumerate(tokens) if t.type == "heading_open" and t.tag in {"h2", "h3"}]


def _parse_sections(tokens: Sequence[Token], body_lines: Sequence[str], canon_set: frozenset[str]) -> list[Section]:
    """Turn H2 and H3 heading tokens into sections, the preamble first when there is one."""
    headings = _headings(tokens)
    preamble = _preamble_section(tokens, body_lines, headings)
    sections: list[Section] = [preamble] if preamble is not None else []
    current_h2_slug: str | None = None
    used_paths: set[str] = set()

    def _unique(label: str) -> str:
        """Suffix `_2`, `_3`, etc. so two same-slug headings never collide on `(page, path)` at load."""
        if label not in used_paths:
            used_paths.add(label)
            return label
        n = 2
        while f"{label}_{n}" in used_paths:
            n += 1
        used_paths.add(f"{label}_{n}")
        return f"{label}_{n}"

    ordinal_offset = len(sections)
    for heading_ord, (token_index, open_token) in enumerate(headings):
        is_h2 = open_token.tag == "h2"
        level = 2 if is_h2 else 3
        heading_text = tokens[token_index + 1].content
        slug = _slug(heading_text)

        if is_h2:
            slug = _unique(slug)
            current_h2_slug = slug
            path = slug
            canon = slug if slug in canon_set else None
        else:
            path = _unique(f"{current_h2_slug}.{slug}" if current_h2_slug else slug)
            canon = None  # an H3 is never canonical

        body = _section_body(open_token, headings, heading_ord, body_lines)
        sections.append(Section(heading=heading_text, level=level, path=path, canon=canon, body=body, ord=heading_ord + ordinal_offset))

    return sections


def _preamble_section(tokens: Sequence[Token], body_lines: Sequence[str], headings: Sequence[tuple[int, Token]]) -> Section | None:
    """Build the reserved preamble section from meaningful source before the first H2/H3."""
    end = _preamble_end(headings, len(body_lines))
    title = _title_token(tokens, end)
    start = title[1].map[1] if title is not None and title[1].map is not None else 0

    body = _strip_preamble_comments(tokens, body_lines, start, end)
    if not body:
        return None
    return Section(kind="preamble", heading="Introduction", level=0, path="__preamble__", canon=None, body=body, ord=0)


def _preamble_end(headings: Sequence[tuple[int, Token]], line_count: int) -> int:
    """The index of the first H2 or H3 heading's line, or `line_count` when there is none."""
    if headings and headings[0][1].map is not None:
        return headings[0][1].map[0]
    return line_count


def _title_token(tokens: Sequence[Token], end: int) -> tuple[int, Token] | None:
    """The index and opening token of the first H1 heading that starts before line `end`, or None."""
    titles = (
        (index, token)
        for index, token in enumerate(tokens)
        if token.type == "heading_open" and token.tag == "h1" and token.map is not None and token.map[0] < end
    )
    return next(titles, None)


def _section_body(open_token: Token, headings: Sequence[tuple[int, Token]], ord_: int, body_lines: Sequence[str]) -> str:
    """Return the body text of one section: source lines after the heading up to the next heading."""
    if open_token.map is None:
        return ""
    start = open_token.map[1]  # first line after the heading line
    end = len(body_lines)
    if ord_ + 1 < len(headings):
        next_open = headings[ord_ + 1][1]
        if next_open.map is not None:
            end = next_open.map[0]
    raw = "\n".join(body_lines[start:end]).strip()
    return _strip_obsidian_comments(raw)


def _parse_claims(tokens: Sequence[Token], source_path: str, grammar: ClaimGrammarSpec) -> list[ClaimBlock]:
    """Parse the `^cN`-anchored top-level bullets under the claims section into claims."""
    findings_span = _findings_token_span(tokens, grammar.section)
    if findings_span is None:
        return []
    start, stop = findings_span

    claims: list[ClaimBlock] = []
    depth = 0
    item_depth_stack: list[int] = []
    pending: _PendingClaim | None = None

    for i in range(start, stop):
        token = tokens[i]
        if token.type == "bullet_list_open":
            depth += 1
        elif token.type == "bullet_list_close":
            depth -= 1
        elif token.type == "list_item_open":
            item_depth_stack.append(depth)
        elif token.type == "list_item_close":
            item_depth_stack.pop()
        elif token.type == "inline" and tokens[i - 1].type == "paragraph_open":
            item_depth = item_depth_stack[-1] if item_depth_stack else 0
            pending = _consume_inline(token.content, item_depth, pending, claims, source_path, grammar)

    _flush(pending, claims, source_path)
    return claims


class _PendingClaim:
    """Mutable accumulator for one claim while its direct child fields stream in."""

    __slots__ = ("anchor", "built_in", "edges", "fields", "links", "text", "unread_fields")

    def __init__(self, anchor: str, text: str) -> None:
        self.anchor = anchor
        self.text = text
        self.built_in: dict[str, object] = {}
        self.edges: list[tuple[str, UnitLink]] = []
        self.fields: dict[str, str] = {}
        self.links: dict[str, tuple[UnitLink, ...]] = {}
        self.unread_fields: list[str] = []


def _consume_inline(
    content: str, item_depth: int, pending: _PendingClaim | None, claims: list[ClaimBlock], source_path: str, grammar: ClaimGrammarSpec
) -> _PendingClaim | None:
    """Fold one inline paragraph into the claim state and return the open claim, naming a field over several lines unread."""
    anchor_match = _ANCHOR_RE.search(content)
    if item_depth == _CLAIM_ITEM_DEPTH and anchor_match is not None:
        _flush(pending, claims, source_path)
        return _PendingClaim(anchor=f"^{anchor_match.group('id')}", text=_read_value(content[: anchor_match.start()]))

    if item_depth == _FIELD_ITEM_DEPTH and pending is not None:
        if (broken := _BROKEN_FIELD_RE.match(content.strip())) is not None:
            pending.unread_fields.append(broken.group("key"))
        else:
            _absorb_field(content, pending, source_path, grammar)

    return pending


def _absorb_field(content: str, pending: _PendingClaim, source_path: str, grammar: ClaimGrammarSpec) -> None:
    """Record one `key:: value` sub-bullet on the open claim, refusing an undeclared key or a link field it cannot hold."""
    field_match = _FIELD_RE.match(content.strip())
    if field_match is None:
        return  # a free-prose sub-bullet is allowed and ignored
    key = field_match.group("key")
    value = _read_value(field_match.group("value"))

    if key in grammar.edge_relations:
        tokens = list(_split_links(value)) or [value]
        pending.edges.extend((key, _link(token)) for token in tokens)
    elif key == "quote":
        pending.built_in[key] = unquote(value)
    elif key in _BUILT_IN_TEXT_FIELDS:
        pending.built_in[key] = value
    elif key == "subject":
        pending.built_in[key] = _one_link(key, value)
    elif key == "disputed_by":
        pending.built_in[key] = _links(key, value, f"claim {pending.anchor} in {source_path}")
    elif key in grammar.link_scalar_names:
        pending.links[key] = (_one_link(key, value),)
    elif key in grammar.link_fields:
        pending.links[key] = _links(key, value, f"claim {pending.anchor} in {source_path}")
    elif key in grammar.text_fields:
        pending.fields[key] = value
    else:
        msg = f"claim {pending.anchor} in {source_path}: {grammar.undeclared_field_refusal(key)}"
        raise ClaimParseError(msg)


def claim_value_read_back(part: str, written: str) -> str:
    """The value the parser reads back for one claim part the renderer wrote from `written`.

    Edge whitespace and Obsidian `%%…%%` comments are dropped, and a quote comes back unwrapped, exactly as the parser
    reads a claim's text and each of its `key:: value` lines.

    Args:
        part: The claim part: `text`, `quote`, or any other field's name.
        written: The value as the writer gave it.

    Returns:
        The value as it reads back from the rendered page.
    """
    if part == "quote":
        return unquote(_read_value(quote(written)))
    return _read_value(written)


def _read_value(raw: str) -> str:
    """One claim text or field value as the parser keeps it: edge whitespace and Obsidian comments dropped."""
    return _strip_obsidian_comments(raw.strip())


def _one_link(key: str, value: str) -> UnitLink:
    """Parse the one link of a link-scalar field, refusing a value with several links."""
    tokens = list(_split_links(value))
    if len(tokens) > 1:
        msg = f"field {key!r} takes one link, got {len(tokens)}"
        raise ClaimParseError(msg)
    return _link(value)


def _links(key: str, value: str, claim: str) -> tuple[UnitLink, ...]:
    """Parse every link of a multilink field in written order, refusing a value that holds text but no link."""
    links = tuple(_link(part) for part in _split_links(value))
    if value and not links:
        msg = f"{claim}: field {key!r} takes [[page]] links, got {value!r}"
        raise ClaimParseError(msg)
    return links


def _flush(pending: _PendingClaim | None, claims: list[ClaimBlock], source_path: str) -> None:
    """Materialize a pending claim into a `ClaimBlock`, refusing one with no `loc::` line and leaving an unread `loc` empty."""
    if pending is None:
        return
    if "loc" not in pending.built_in and "loc" not in pending.unread_fields:
        msg = f"claim {pending.anchor} in {source_path} is missing the required loc:: field"
        raise ClaimParseError(msg)

    claims.append(
        ClaimBlock.model_validate(
            {
                "anchor": pending.anchor,
                "text": pending.text,
                "edges": tuple(pending.edges),
                "loc": "",
                **pending.built_in,
                "fields": pending.fields,
                "links": pending.links,
                "unread_fields": tuple(pending.unread_fields),
            }
        )
    )


def _findings_token_span(tokens: Sequence[Token], section: str) -> tuple[int, int] | None:
    """Return the token range `[start, stop)` from the claims section's H2 to the next H2, or None."""
    start: int | None = None
    for i, token in enumerate(tokens):
        if token.type == "heading_open" and token.tag == "h2":
            heading_text = tokens[i + 1].content
            if start is not None:
                return start, i  # the next H2 ends the claims span
            if _slug(heading_text) == section:
                start = i
    return (start, len(tokens)) if start is not None else None


def _split_links(value: str) -> Iterator[str]:
    """Split a multi-link value such as `[[a]], [[b]]` into its link strings, commas inside an alias kept."""
    return iter(_UNIT_LINK_TOKEN_RE.findall(value))


def _link(value: str) -> UnitLink:
    """Parse a single `[[…]]` qualifier/edge value into a `UnitLink`."""
    return UnitLink.parse(value)


def _slug(heading: str) -> str:
    """Return the ltree-safe heading slug via `heading_slug` (transliterated, never empty)."""
    return heading_slug(heading)


def _strip_obsidian_comments(text: str) -> str:
    """Remove paired `%%…%%` Obsidian comment spans from text."""
    return _OBSIDIAN_INLINE_COMMENT_RE.sub("", text).strip()


def _strip_comments(text: str) -> str:
    """Remove Obsidian and HTML comments from captured prose."""
    return _strip_obsidian_comments(_HTML_COMMENT_RE.sub("", text))


def _strip_preamble_comments(tokens: Sequence[Token], body_lines: Sequence[str], start: int, end: int) -> str:
    """Remove comments from preamble prose while preserving fenced-code source ranges."""
    lines = list(body_lines[start:end])
    fences: list[tuple[str, str]] = []
    for index, token in enumerate(tokens):
        if token.type != "fence" or token.map is None:
            continue
        fence_start, fence_end = token.map
        if fence_start < start or fence_end > end:
            continue
        marker = f"\ue000engram-fence-{index}\ue001"
        fences.append((marker, "\n".join(body_lines[fence_start:fence_end])))
        lines[fence_start - start] = marker
        for line_index in range(fence_start - start + 1, fence_end - start):
            lines[line_index] = ""

    body = _strip_comments("\n".join(lines))
    for marker, fence in fences:
        body = body.replace(marker, fence)
    return body
