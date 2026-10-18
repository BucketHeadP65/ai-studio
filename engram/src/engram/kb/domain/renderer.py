"""Render an `AuthoringPayload` or a parsed `KnowledgeBaseNote` to canonical markdown.

`render` returns a string and writes no file.
`parse` reads the output back into an equal `KnowledgeBaseNote`, and rendering that note again is byte-identical.
Sections come out in their page type's canonical H2 order, unknown H2s after them in input order, each H3 under its H2.
Claims add their claims section when the writer lists none.
A claim renders as `- {text} ^cN` with its fields as indented `key:: value` lines.
The built-in fields and edges come in a fixed order, the declared fields in the claim grammar's order, `status` and `disputed_by` last.
`payload_of` goes the other way: a page's markdown as the payload that renders it back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.markdown import heading_for_slug, heading_slug, render_frontmatter, split_frontmatter

from engram.kb.domain.note import BaseFrontmatter, ClaimBlock, KnowledgeBaseNote, Section, UnitLink, parse_frontmatter
from engram.kb.domain.parser import claim_value_read_back, heading_lines, parse
from engram.kb.domain.payload import AuthoringPayload, assign_anchors
from engram.kb.domain.quoting import quote
from engram.kb.domain.schema import KnowledgeBaseSchema, resolve_schema

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pydantic import JsonValue

    from engram.kb.domain.schema import ClaimGrammarSpec


# The render order of a claim's field lines.
# `edges` expands at its position into one `relation:: [[target]]` line per edge, in tuple order.
# `links` and `fields` expand into one line per link field and per text field, the claim grammar's declared names first
# in its order, then any other name the claim carries in the claim's own order.
_CLAIM_FIELD_ORDER: tuple[str, ...] = ("loc", "quote", "kind", "subject", "edges", "links", "fields", "status", "disputed_by")

# Claim fields rendered on the bullet line `- {text} ^cN` rather than as field lines.
# Together with `_CLAIM_FIELD_ORDER` it must cover every `ClaimBlock` field, or a field never renders.
_CLAIM_HEADER_FIELDS: frozenset[str] = frozenset({"text", "anchor"})

# Frontmatter keys that lead, in this order, when present. Every other key follows in model order.
_FRONTMATTER_LEAD_KEYS: tuple[str, ...] = ("id", "type", "title")

# H2 heading level: a section at this level opens a new canonical-ordering group.
_H2_LEVEL = 2

# One canonical-ordering group: an H2 `(heading, level)` paired with the H3 sections
# `(heading, level, body)` supplied under it, kept together so a group reorders atomically.
type _H2Group = tuple[tuple[str, int], list[tuple[str, int, str]]]


def render(source: AuthoringPayload | KnowledgeBaseNote, *, spec: KnowledgeBaseSchema | None = None) -> str:
    """Render a payload or a parsed note to canonical markdown text.

    A payload and the note parsed from its output render identically.

    Args:
        source: An `AuthoringPayload` or a `KnowledgeBaseNote` to render.
        spec: The knowledge base schema.

    Returns:
        The markdown text, frontmatter fence included.

    Raises:
        KnowledgeBaseSchemaMissingError: If `spec` is None.
    """
    resolved_spec = resolve_schema(spec)

    preamble_text: str | None = None
    if isinstance(source, KnowledgeBaseNote):
        frontmatter = source.frontmatter
        ordered = sorted(source.sections, key=lambda s: s.ord)
        preamble_text = next((s.body.strip() for s in ordered if s.kind == "preamble" and s.body.strip()), None)
        sections = [(s.heading, s.level, s.body) for s in ordered if s.kind != "preamble"]
        claims: tuple[ClaimBlock, ...] = source.claims
    else:
        frontmatter = source.validated_frontmatter()
        sections = [(s.heading, s.level, s.body) for s in source.sections]
        claims = tuple(claim.to_block() for claim in assign_anchors(source.claims))

    canon_order = resolved_spec.sections_for(frontmatter.type)
    canonical_headings = _canonical_headings(resolved_spec, frontmatter.type)
    raw_frontmatter = _frontmatter_dict(frontmatter, resolved_spec)
    body = _render_body(
        sections, claims, canon_order=canon_order, claim_grammar=resolved_spec.claims, canonical_headings=canonical_headings
    )
    if preamble_text is not None:
        body = "\n" + preamble_text + "\n\n" + body.removeprefix("\n")
    return render_frontmatter(raw_frontmatter, body)


def payload_of(text: str, *, source_path: str, spec: KnowledgeBaseSchema) -> dict[str, JsonValue]:
    """One page's markdown parsed into the write_note payload (frontmatter, sections, claims) that renders it back.

    Rendering the payload gives the same frontmatter values and the same body, byte for byte. Only the layout of the
    frontmatter YAML may differ. Each claim keeps its anchor and names each declared field as a key of its own, a link
    as a `"[[…]]"` string. The claims section's body is empty when the page has claims, because the renderer writes them.
    Each heading is a section of its own while that renders the page back. When it does not, a section the page type
    does not declare is carried, heading included and as written, in the body of the section before it, but only when
    the renderer would move it (it sits before a declared section) or change it (such as a heading with no blank line
    under it). Every other section stays a section of its own. Such a section is still refused when nothing before it
    can carry it: it sits before the first declared section, or right after a claims section that holds claims.

    Args:
        text: The page's whole markdown, frontmatter fence included.
        source_path: The page's path relative to the knowledge base root, named in a parse error.
        spec: The knowledge base schema.

    Returns:
        The payload, with `frontmatter`, `sections` and `claims`.

    Raises:
        NoteGrammarError: `text` does not parse under `spec`.
        ValueError: The payload would not carry the page whole, such as text before the first heading or a claim the
            grammar does not read back. The message names the first line that would change.
    """
    note = parse(text, source_path=source_path, spec=spec)
    claim_section = spec.claims.section if spec.claims is not None else None
    headed = [section for section in sorted(note.sections, key=lambda section: section.ord) if section.kind == "heading"]
    frontmatter = _frontmatter_dict(note.frontmatter, spec)
    scalar_links = spec.claims.link_scalar_names if spec.claims is not None else frozenset[str]()
    claims: list[JsonValue] = [_claim_payload(claim, scalar_links) for claim in note.claims]
    sections: list[JsonValue] = [
        {
            "heading": section.heading,
            "body": "" if note.claims and _slug(section.heading) == claim_section else section.body,
            "level": section.level,
        }
        for section in headed
    ]
    payload: dict[str, JsonValue] = {"frontmatter": frontmatter, "sections": sections, "claims": claims}
    written_values, written_body = split_frontmatter(text)
    rendered_values, rendered_body = _rendered_parts(payload, spec)
    if rendered_values != written_values:
        changed = sorted(key for key in {*written_values, *rendered_values} if written_values.get(key) != rendered_values.get(key))
        msg = f"{note.frontmatter.id} holds frontmatter a payload cannot carry whole: {', '.join(changed)}"
        raise ValueError(msg)
    if rendered_body != written_body and (
        carried := _carried_sections(headed, written_body, has_claims=bool(note.claims), claim_section=claim_section)
    ):
        payload["sections"] = carried
        rendered_body = _rendered_parts(payload, spec)[1]
    if rendered_body != written_body:
        first = _first_difference(written_body.splitlines(), rendered_body.splitlines())
        msg = f"{note.frontmatter.id} holds text a payload cannot carry whole, first at {first!r}"
        raise ValueError(msg)
    return payload


def _rendered_parts(payload: Mapping[str, JsonValue], spec: KnowledgeBaseSchema) -> tuple[dict[str, JsonValue], str]:
    """The frontmatter values and the body `payload` renders to."""
    return split_frontmatter(render(AuthoringPayload.model_validate({**payload, "knowledge_base_schema": spec}), spec=spec))


def _carried_sections(sections: Sequence[Section], body: str, *, has_claims: bool, claim_section: str | None) -> list[JsonValue]:
    """The sections with each undeclared one that would move or change carried as written in the section before it, or empty."""
    spans = heading_lines(body)
    if len(spans) != len(sections):
        return []
    lines = body.splitlines()
    last_declared = max((index for index, section in enumerate(sections) if section.canon is not None), default=-1)
    kept: list[int] = []
    for index, section in enumerate(sections):
        in_place = section.level != _H2_LEVEL or index > last_declared
        stays = section.canon is not None or (in_place and _renders_as_written(section, lines, spans, index))
        holder = sections[kept[-1]] if kept else None
        can_hold = holder is not None and not (has_claims and _slug(holder.heading) == claim_section)
        if stays or not can_hold:
            kept.append(index)
    carried: list[JsonValue] = []
    for position, index in enumerate(kept):
        section = sections[index]
        end = spans[kept[position + 1]][0] if position + 1 < len(kept) else len(lines)
        written = "\n".join(lines[spans[index][1] : end]).strip()
        empty = has_claims and _slug(section.heading) == claim_section
        carried.append({"heading": section.heading, "body": "" if empty else written, "level": section.level})
    return carried


def _renders_as_written(section: Section, lines: Sequence[str], spans: Sequence[tuple[int, int]], index: int) -> bool:
    """Whether the section, as a section of its own, renders to its own lines and the one blank line after them."""
    start, after = spans[index]
    end = spans[index + 1][0] if index + 1 < len(spans) else len(lines)
    own = "\n".join(lines[after:end]).strip()
    block = f"{'#' * section.level} {section.heading}" + (f"\n\n{own}" if own else "")
    return list(lines[start:end]) == [*block.splitlines(), *([""] if index + 1 < len(spans) else [])]


def _first_difference(written: Sequence[str], rendered: Sequence[str]) -> str:
    """The first line of `written` that `rendered` does not reproduce, or the first extra line of either."""
    for line, other in zip(written, rendered, strict=False):
        if line != other:
            return line
    longer = written if len(written) > len(rendered) else rendered
    return longer[min(len(written), len(rendered))]


@dataclass(frozen=True)
class UnreadClaim:
    """One payload claim that does not read back from its rendering: its anchor and the part that differs."""

    anchor: str
    part: str
    """The part that differs: `text`, a built-in field such as `quote`, or a declared field's name."""
    written: str
    """The part as the payload wrote it."""
    read_back: str | None
    """The part as it reads back, empty when the part is gone, None when the whole claim does not read back."""


def claims_not_read_back(payload: AuthoringPayload, note: KnowledgeBaseNote) -> tuple[UnreadClaim, ...]:
    """Every one of the payload's claims, anchored, that `note` does not carry as written, in payload order.

    `note` must be the payload rendered and parsed back. Each written part is read the way the parser reads it back
    (edge whitespace and comments dropped, a quote unwrapped) before it is compared. A claim whose text holds a blank
    line does not read back at all, and a field value with a line break reads back cut.

    Args:
        payload: The payload that was rendered.
        note: The note parsed from that rendering.

    Returns:
        One entry per claim that does not read back, each with the first part that differs. Empty when all read back.
    """
    read_back = {claim.anchor: claim for claim in note.claims}
    unread: list[UnreadClaim] = []
    for claim in (claim.to_block() for claim in assign_anchors(payload.claims)):
        parsed = read_back.get(claim.anchor)
        if parsed is None:
            unread.append(UnreadClaim(anchor=claim.anchor, part="text", written=claim.text, read_back=None))
            continue
        written, back = _claim_parts(claim), _claim_parts(parsed)
        differing = next((part for part in (*written, *back) if written.get(part) != back.get(part)), None)
        if differing is not None:
            unread.append(UnreadClaim(anchor=claim.anchor, part=differing, written=written.get(differing, ""), read_back=back.get(differing, "")))
    return tuple(unread)


def _claim_parts(claim: ClaimBlock) -> dict[str, str]:
    """Each set part of a claim as it reads back from a rendered page: the text, the built-in fields, then the declared fields."""
    values = {
        "text": claim.text,
        "loc": claim.loc,
        "quote": claim.quote or "",
        "kind": claim.kind,
        "status": claim.status or "",
        **claim.fields,
    }
    links = {
        "subject": _unit_link_str(claim.subject) if claim.subject is not None else "",
        "edges": ", ".join(f"{relation} {_unit_link_str(target)}" for relation, target in claim.edges),
        "disputed_by": ", ".join(_unit_link_str(link) for link in claim.disputed_by),
        **{name: ", ".join(_unit_link_str(link) for link in targets) for name, targets in claim.links.items()},
    }
    parts = {part: claim_value_read_back(part, value) for part, value in values.items() if value}
    parts.update({part: value for part, value in links.items() if value})
    return {part: value for part, value in parts.items() if value}


def _claim_payload(claim: ClaimBlock, scalar_links: frozenset[str]) -> dict[str, JsonValue]:
    """One claim as a payload claim: its set built-in fields, then each declared field as a key of its own."""
    payload: dict[str, JsonValue] = {"anchor": claim.anchor, "text": claim.text, "loc": claim.loc}
    if claim.quote is not None:
        payload["quote"] = claim.quote
    if claim.kind:
        payload["kind"] = claim.kind
    if claim.subject is not None:
        payload["subject"] = _unit_link_str(claim.subject)
    if claim.edges:
        payload["edges"] = [[relation, _unit_link_str(target)] for relation, target in claim.edges]
    if claim.status is not None:
        payload["status"] = claim.status
    if claim.disputed_by:
        payload["disputed_by"] = [_unit_link_str(link) for link in claim.disputed_by]
    for name, links in claim.links.items():
        as_text: list[JsonValue] = [_unit_link_str(link) for link in links]
        payload[name] = as_text[0] if name in scalar_links and len(as_text) == 1 else as_text
    payload.update(claim.fields)
    return payload


def _canonical_headings(spec: KnowledgeBaseSchema, page_type: str) -> Mapping[str, str]:
    """The page type's declared canonical headings by slug, or `{}` for an unknown page type."""
    pt = spec.page_type(page_type)
    return pt.canonical_headings if pt is not None else {}


def _frontmatter_dict(frontmatter: BaseFrontmatter, spec: KnowledgeBaseSchema | None = None) -> dict[str, JsonValue]:
    """Serialize frontmatter to an ordered, YAML-safe dict without unset keys, checked to parse back."""
    dumped = frontmatter.model_dump(exclude_defaults=True)
    dumped["type"] = frontmatter.type
    serialised = {key: _to_yaml_value(value) for key, value in dumped.items()}
    ordered = _order_frontmatter(serialised)
    parse_frontmatter(ordered, spec)  # round-trip guard: the emitted dict must re-discriminate cleanly
    return ordered


def _order_frontmatter(data: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Put `id`, `type`, `title` first, keep every other key in its incoming order."""
    ordered: dict[str, JsonValue] = {key: data[key] for key in _FRONTMATTER_LEAD_KEYS if key in data}
    ordered.update({key: value for key, value in data.items() if key not in ordered})
    return ordered


def _to_yaml_value(value: object) -> JsonValue:
    """Convert a dumped frontmatter value to a YAML-safe one (unit links to `"[[…]]"` strings, tuples to lists)."""
    if isinstance(value, tuple):
        return [_to_yaml_value(item) for item in value]
    if _is_unit_link_dict(value):
        return _unit_link_str(UnitLink.model_validate(value))
    return value  # pyright: ignore[reportReturnType]  # scalars are already JsonValue


def _is_unit_link_dict(value: object) -> bool:
    """Return True if a dumped value is a serialized `UnitLink` (a dict with a `target` key)."""
    return isinstance(value, dict) and "target" in value


def _render_body(
    sections: Sequence[tuple[str, int, str]],
    claims: Sequence[ClaimBlock],
    *,
    canon_order: tuple[str, ...],
    claim_grammar: ClaimGrammarSpec | None,
    canonical_headings: Mapping[str, str],
) -> str:
    """Assemble the markdown body from the ordered sections and the claim bullets."""
    claim_section = claim_grammar.section if claim_grammar is not None else None
    ordered = _order_sections(
        sections, has_claims=bool(claims), canon_order=canon_order, claim_section=claim_section, canonical_headings=canonical_headings
    )
    blocks = [
        _render_section(heading, level, body, claims if _slug(heading) == claim_section else (), claim_grammar)
        for heading, level, body in ordered
    ]
    return "\n" + "\n\n".join(blocks) + "\n"


def _order_sections(
    sections: Sequence[tuple[str, int, str]],
    *,
    has_claims: bool,
    canon_order: tuple[str, ...],
    claim_section: str | None,
    canonical_headings: Mapping[str, str],
) -> list[tuple[str, int, str]]:
    """Order sections canonically, adding an empty claims section when claims exist without one."""
    groups = _group_by_h2(sections)
    if has_claims and claim_section is not None and not any(_slug(heading) == claim_section for (heading, _level), _children in groups):
        synthesized = canonical_headings.get(claim_section, heading_for_slug(claim_section))
        groups.append(((synthesized, _H2_LEVEL), []))

    canon_index = {slug: i for i, slug in enumerate(canon_order)}

    def _sort_key(indexed_group: tuple[int, _H2Group]) -> tuple[int, int]:
        # Canonical sections first in canon order, unknown H2s after them, ties kept in input order.
        original_index, ((heading, _level), _children) = indexed_group
        return canon_index.get(_slug(heading), len(canon_order)), original_index

    ordered_groups = sorted(enumerate(groups), key=_sort_key)

    result: list[tuple[str, int, str]] = []
    for _, ((heading, level), children) in ordered_groups:
        body = _body_for(sections, heading, level)
        display_heading = canonical_headings.get(_slug(heading), heading) if level == _H2_LEVEL else heading
        result.append((display_heading, level, body))
        result.extend(children)
    return result


def _group_by_h2(sections: Sequence[tuple[str, int, str]]) -> list[_H2Group]:
    """Group sections so each H2 carries its following H3s in input order, for atomic reordering."""
    groups: list[_H2Group] = []
    for heading, level, body in sections:
        if level == _H2_LEVEL or not groups:
            groups.append(((heading, level), []))
        else:
            groups[-1][1].append((heading, level, body))
    return groups


def _body_for(sections: Sequence[tuple[str, int, str]], heading: str, level: int) -> str:
    """Return the supplied body for a heading (empty string for a synthesized Findings)."""
    for h, lvl, body in sections:
        if h == heading and lvl == level:
            return body
    return ""


def _render_section(heading: str, level: int, body: str, claims: Sequence[ClaimBlock], claim_grammar: ClaimGrammarSpec | None) -> str:
    """Render one section as its heading and body, or its heading and claim bullets when `claims` is not empty."""
    prefix = "#" * level
    parts = [f"{prefix} {heading}"]
    if claims:
        parts.append("\n".join(_render_claim(claim, claim_grammar) for claim in claims))
    elif body.strip():
        parts.append(body.strip())
    return "\n\n".join(parts)


def _render_claim(claim: ClaimBlock, claim_grammar: ClaimGrammarSpec | None) -> str:
    """Render one claim as `- {text} ^cN` with its 2-space-indented direct-child fields."""
    lines = [f"- {claim.text} {claim.anchor}"]
    for key in _CLAIM_FIELD_ORDER:
        lines.extend(f"    - {field}" for field in _render_field(key, claim, claim_grammar))
    return "\n".join(lines)


def _render_field(key: str, claim: ClaimBlock, claim_grammar: ClaimGrammarSpec | None) -> list[str]:
    """Return the `key:: value` line(s) for one claim field, or an empty list when unset."""
    if key == "edges":
        return [f"{relation}:: {_unit_link_str(target)}" for relation, target in claim.edges]
    if key == "links":
        declared = claim_grammar.link_fields if claim_grammar is not None else ()
        return [line for name in _in_declared_order(claim.links, declared) for line in _render_links(name, claim.links[name])]
    if key == "fields":
        declared = claim_grammar.text_fields if claim_grammar is not None else ()
        return [f"{name}:: {claim.fields[name]}" for name in _in_declared_order(claim.fields, declared) if claim.fields[name]]
    return _render_built_in(key, getattr(claim, key))


def _render_built_in(key: str, value: str | UnitLink | tuple[UnitLink, ...] | None) -> list[str]:
    """Return the `key:: value` line of one built-in claim field, or an empty list when unset."""
    if value is None or value == "":
        return []
    if isinstance(value, tuple):
        return _render_links(key, value)
    if isinstance(value, UnitLink):
        return [f"{key}:: {_unit_link_str(value)}"]
    return [f"quote:: {quote(value)}"] if key == "quote" else [f"{key}:: {value}"]


def _render_links(key: str, links: Sequence[UnitLink]) -> list[str]:
    """Return the one `key:: [[a]], [[b]]` line of a link field, or an empty list when it has no link."""
    return [f"{key}:: {', '.join(_unit_link_str(link) for link in links)}"] if links else []


def _in_declared_order(present: Mapping[str, object], declared: tuple[str, ...]) -> list[str]:
    """The names in `present`, those in `declared` first in its order, then the rest in their own order."""
    return [name for name in declared if name in present] + [name for name in present if name not in declared]


def _unit_link_str(link: UnitLink) -> str:
    """Render a `UnitLink` back to its `[[target(#…)(|alias)]]` source form."""
    inner = link.target
    if link.block is not None:
        inner += f"#^{link.block}"
    elif link.heading is not None:
        inner += f"#{link.heading}"
    if link.alias is not None:
        inner += f"|{link.alias}"
    return f"[[{inner}]]"


def _slug(heading: str) -> str:
    """Return the ltree-safe heading slug via `heading_slug` (transliterated, never empty)."""
    return heading_slug(heading)
