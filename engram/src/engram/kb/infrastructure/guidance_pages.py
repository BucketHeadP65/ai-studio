"""Documentation and compounding guidance: policy prose in two `_meta` pages, merged into the schema at load time.

`_meta/documentation-policy.md` carries authoring guidance per page type and canonical section.
`_meta/compounding-policy.md` carries guidance per compounding entry. Either page's prose wins over
the schema's own `guidance` field wherever the page names that page_type, section, or entry. A page
that is missing, unreadable, or whose frontmatter cannot be trusted leaves the schema's own guidance
as the fallback.
"""

from __future__ import annotations

import re

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

import yaml

from core.markdown import heading_for_slug, heading_slug, render_frontmatter, split_frontmatter
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from engram.kb.domain.schema import KnowledgeBaseSchema

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

DOCUMENTATION_POLICY_PATH = "_meta/documentation-policy.md"
"""Where a knowledge base keeps its page-type and section authoring guidance, relative to the knowledge base root."""

COMPOUNDING_POLICY_PATH = "_meta/compounding-policy.md"
"""Where a knowledge base keeps its compounding-entry guidance, relative to the knowledge base root."""

_DOCUMENTATION_ID = "meta.documentation-policy"
_DOCUMENTATION_TITLE = "Documentation policy"
_COMPOUNDING_ID = "meta.compounding-policy"
_COMPOUNDING_TITLE = "Compounding policy"

_HEADING = re.compile(r"^(#{1,3}) (.+)$")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")

_KIND_HEADING = 2  # `_walk_page`'s level for an H2 (a page type or a compounding entry), an H3 (a section) is 3


class PageTypeGuidance(BaseModel):
    """One page type's guidance as the documentation page declares it: the page_type's own note, and each section's, by slug."""

    model_config = ConfigDict(frozen=True)

    guidance: str = ""
    sections: Mapping[str, str] = Field(default_factory=dict)


def _read_body(path: Path) -> str | None:
    """The page's body past its frontmatter, or None when the page is absent, unreadable, or its frontmatter is empty or broken."""
    if not path.is_file():
        return None
    try:
        front, body = split_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return None
    if not front:
        return None
    return body


def _walk_page(body: str) -> Iterator[tuple[int, str, str]]:
    """Yield `(level, heading, prose)` for every H2 and H3 block in `body` outside fenced code, in document order."""
    level: int | None = None
    heading = ""
    lines: list[str] = []
    for fenced, line in _fenced_lines(body):
        mark = None if fenced else _HEADING.match(line)
        if mark is None:
            lines.append(line)
            continue
        depth = len(mark.group(1))
        if depth < _KIND_HEADING:
            continue
        if level is not None:
            yield level, heading, "\n".join(lines).strip()
        level, heading, lines = depth, mark.group(2).strip(), []
    if level is not None:
        yield level, heading, "\n".join(lines).strip()


def _fenced_lines(body: str) -> Iterator[tuple[bool, str]]:
    """Yield `(fenced, line)` per line of `body`, `fenced` True for a fence delimiter and every line between a pair."""
    fence: str | None = None
    for line in body.splitlines():
        mark = _FENCE.match(line)
        if fence is None:
            fence = mark.group(1) if mark is not None else None
            yield fence is not None, line
            continue
        if mark is not None and mark.group(1)[0] == fence[0] and len(mark.group(1)) >= len(fence):
            fence = None
        yield True, line


def read_documentation_guidance(knowledge_base_path: Path, spec: KnowledgeBaseSchema) -> Mapping[str, PageTypeGuidance]:
    """Read the documentation policy page: per-page_type and per-section guidance, keyed by page_type slug.

    An H2 naming a page_type `spec` does not declare is dropped along with any H3s under it. An H3
    naming a section its page_type does not declare is dropped. A missing or untrustworthy page answers
    empty.

    Args:
        knowledge_base_path: The knowledge base root.
        spec: The knowledge base's schema, naming the page types and sections to keep.

    Returns:
        Each named page type's guidance, keyed by page_type slug.
    """
    body = _read_body(knowledge_base_path / DOCUMENTATION_POLICY_PATH)
    if body is None:
        return {}
    guidance: dict[str, str] = {}
    sections: dict[str, dict[str, str]] = {}
    current_page_type: str | None = None
    current_sections: frozenset[str] = frozenset()
    for level, heading, prose in _walk_page(body):
        if level == _KIND_HEADING:
            page_type = spec.page_type(heading)
            if page_type is None:
                current_page_type = None
                current_sections = frozenset()
                continue
            current_page_type = heading
            current_sections = frozenset(section.slug for section in page_type.canonical_sections)
            guidance[current_page_type] = prose
            sections.setdefault(current_page_type, {})
            continue
        if current_page_type is None:
            continue
        slug = heading_slug(heading)
        if slug in current_sections:
            sections[current_page_type][slug] = prose
    return {page_type: PageTypeGuidance(guidance=text, sections=sections.get(page_type, {})) for page_type, text in guidance.items()}


def read_compounding_guidance(knowledge_base_path: Path, spec: KnowledgeBaseSchema) -> Mapping[str, str]:
    """Read the compounding policy page: guidance per compounding entry, keyed by entry name.

    An H2 naming an entry `spec` does not declare is dropped. A missing or untrustworthy page
    answers empty.

    Args:
        knowledge_base_path: The knowledge base root.
        spec: The knowledge base's schema, naming the compounding entries to keep.

    Returns:
        Each named entry's guidance, keyed by entry name.
    """
    body = _read_body(knowledge_base_path / COMPOUNDING_POLICY_PATH)
    if body is None:
        return {}
    declared = {entry.name for entry in spec.compounding.entries}
    return {heading: prose for level, heading, prose in _walk_page(body) if level == _KIND_HEADING and heading in declared}


def apply_guidance(spec: KnowledgeBaseSchema, knowledge_base_path: Path) -> KnowledgeBaseSchema:
    """Merge `knowledge_base_path`'s guidance pages into `spec`: page prose wins per page_type, section, and entry it names.

    `spec`'s own `guidance` fields are the fallback wherever a page is silent or absent. `spec` itself
    is never changed.

    Args:
        spec: The knowledge base's schema.
        knowledge_base_path: The knowledge base root holding the guidance pages.

    Returns:
        A new schema carrying the merged guidance.
    """
    doc_guidance = read_documentation_guidance(knowledge_base_path, spec)
    compounding_guidance = read_compounding_guidance(knowledge_base_path, spec)
    data: dict[str, JsonValue] = spec.model_dump(mode="json", by_alias=True)
    for page_type in cast("list[dict[str, JsonValue]]", data["page_types"]):
        page_type_guidance = doc_guidance.get(cast("str", page_type["page_type"]))
        if page_type_guidance is None:
            continue
        page_type["guidance"] = page_type_guidance.guidance
        section_entries = cast("list[JsonValue]", page_type["canonical_sections"])
        for index, entry in enumerate(section_entries):
            merged: dict[str, JsonValue]
            if isinstance(entry, str):
                slug = entry
                merged = {"slug": entry}
            elif isinstance(entry, dict):
                merged = dict(entry)
                slug = cast("str", merged["slug"])
            else:
                continue
            section_guidance = page_type_guidance.sections.get(slug)
            if section_guidance is None:
                continue
            merged["guidance"] = section_guidance
            section_entries[index] = merged
    compounding = cast("dict[str, JsonValue]", data["compounding"])
    for entry in cast("list[dict[str, JsonValue]]", compounding["entries"]):
        entry_guidance = compounding_guidance.get(cast("str", entry["name"]))
        if entry_guidance is not None:
            entry["guidance"] = entry_guidance
    return KnowledgeBaseSchema.model_validate(data)


def render_documentation_policy(spec: KnowledgeBaseSchema) -> str:
    """Render the documentation policy page for `spec`'s guidance: an H2 per page_type, an H3 per section.

    A page_type that carries no guidance of its own and whose sections carry none either is omitted.
    Within a page_type, only its sections that carry guidance get an H3.

    Args:
        spec: The knowledge base's schema.

    Returns:
        The page's markdown, frontmatter included.
    """
    lines: list[str] = [f"# {_DOCUMENTATION_TITLE}"]
    for page_type in spec.page_types:
        sections = [
            (section.heading or heading_for_slug(section.slug), section.guidance)
            for section in page_type.canonical_sections
            if section.guidance
        ]
        if not page_type.guidance and not sections:
            continue
        lines.extend(("", f"## {page_type.page_type}"))
        if page_type.guidance:
            lines.append(page_type.guidance)
        for heading, section_guidance in sections:
            lines.extend(("", f"### {heading}", section_guidance))
    front = {"id": _DOCUMENTATION_ID, "type": "meta", "title": _DOCUMENTATION_TITLE}
    return render_frontmatter(front, "\n".join(lines) + "\n")


def render_compounding_policy(spec: KnowledgeBaseSchema) -> str:
    """Render the compounding policy page for `spec`'s guidance: an H2 per entry that carries guidance.

    Args:
        spec: The knowledge base's schema.

    Returns:
        The page's markdown, frontmatter included.
    """
    lines: list[str] = [f"# {_COMPOUNDING_TITLE}"]
    for entry in spec.compounding.entries:
        if not entry.guidance:
            continue
        lines.extend(("", f"## {entry.name}", entry.guidance))
    front = {"id": _COMPOUNDING_ID, "type": "meta", "title": _COMPOUNDING_TITLE}
    return render_frontmatter(front, "\n".join(lines) + "\n")
