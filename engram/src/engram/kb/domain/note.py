"""Typed, frozen models of one note: page types, frontmatter, sections, claims and relations.

Parsing and authoring both bind to these models.
They enforce only structure: the id prefix of the page type, the page type's closed `status` values, immutability and the required claim field `loc::`.
They do not check vocabulary membership or the `summary` length. The validate stage checks those against a `VocabularyIndex`.
Frontmatter keys these models do not type are kept verbatim through `extra="allow"` and reach `pages.frontmatter` unchanged.
"""

from __future__ import annotations

import re

from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from engram.kb.exceptions import FrontmatterError, NoteGrammarError

if TYPE_CHECKING:
    from engram.kb.domain.schema import FrontmatterFieldSpec, KnowledgeBaseSchema

DEFAULT_STATUSES: frozenset[str] = frozenset(
    {"stub", "draft", "synthesized", "verified", "active", "superseded", "outdated", "pending"}
)
"""The statuses a page type allows when its `PageTypeSpec.status_values` is empty.

It holds two lifecycles that share the `pages.status` column.
The authoring lifecycle is `stub`, `draft`, `synthesized` and `verified`, the only four the authoring JSON Schema offers.
The disclosure lifecycle is `active`, `superseded`, `outdated` and `pending`, matching `core.ai.disclosure.contracts.DisclosureStatus`.
A page's status comes from one lifecycle, never both.
"""

Status = str
"""A page's status value, checked against its page type's `status_values` or `DEFAULT_STATUSES` when frontmatter is parsed."""

_UNIT_LINK_RE = re.compile(r"^\[\[(?P<body>(?:(?!\]\]).)+?)\]\]$", re.DOTALL)
"""One whole value that is exactly one `[[...]]` link, whose body never contains `]]`."""


# One line of letters, digits and the characters `._-@/+:` with no whitespace, quotes or brackets.
_PAGE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@/+:-]*")


class UnitLink(BaseModel):
    """An Obsidian unit link as a value object: `[[target]]` and its decorations.

    It supports `[[target]]`, `[[target|alias]]`, `[[target#heading]]`, `[[target#^block]]` and their combinations.
    `target` is the link text as written, a file name or an alias, never resolved to a page id here.
    """

    model_config = ConfigDict(frozen=True)

    target: str
    alias: str | None = None
    heading: str | None = None
    block: str | None = None

    @classmethod
    def parse(cls, raw: str) -> UnitLink:
        """Parse a `[[…]]` string into a `UnitLink`.

        Args:
            raw: The link text, surrounding whitespace allowed.

        Returns:
            The parsed link.

        Raises:
            NoteGrammarError: If `raw` is not a `[[…]]`-bracketed link or has an empty target.
        """
        match = _UNIT_LINK_RE.match(raw.strip())
        if match is None:
            msg = f"not a unit link: {raw!r}"
            raise NoteGrammarError(msg)

        body = match.group("body")
        link_part, _, alias = body.partition("|")
        target_part, sep, fragment = link_part.partition("#")

        target = target_part.strip()
        if not target:
            msg = f"unit link has no target: {raw!r}"
            raise NoteGrammarError(msg)

        heading: str | None = None
        block: str | None = None
        if sep:
            fragment = fragment.strip()
            if fragment.startswith("^"):
                block = fragment[1:].strip() or None
            else:
                heading = fragment or None

        return cls(target=target, alias=alias.strip() or None if alias else None, heading=heading, block=block)


_UNIT_LINK_TOKEN_RE = re.compile(_UNIT_LINK_RE.pattern.removeprefix("^").removesuffix("$"), re.DOTALL)
"""The same link grammar as `_UNIT_LINK_RE`, unanchored, so it finds links embedded anywhere in text."""


def unit_links_in(text: str) -> tuple[UnitLink, ...]:
    """Every unit link written anywhere in `text`, parsed, in the order each is written.

    Unlike `UnitLink.parse`, `text` may hold any amount of prose around its links.

    Args:
        text: The text to search, such as a section body.

    Returns:
        The parsed links in written order, empty when there are none.
    """
    return tuple(UnitLink.parse(match.group(0)) for match in _UNIT_LINK_TOKEN_RE.finditer(text))


def _coerce_unit_link(value: object) -> object:
    """Turn a `"[[…]]"` string into a `UnitLink` and pass anything else through."""
    if isinstance(value, str):
        return UnitLink.parse(value)
    return value


UnitLinkField = Annotated[UnitLink, BeforeValidator(_coerce_unit_link)]
"""A `UnitLink` field that also accepts a raw `"[[…]]"` string."""


class BaseFrontmatter(BaseModel):
    """The frontmatter every page carries, with every other key kept verbatim.

    It is frozen and hashable.
    Build it through `parse_frontmatter`, which also checks the id prefix and the status against the schema.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    id: str
    type: str  # any page type the schema declares
    title: str
    summary: str | None = None  # validation warns above 400 chars at the validate stage, not here
    status: Status
    superseded_by: UnitLinkField | None = None
    aliases: tuple[str, ...] = ()

    def facet_values(self, key: str) -> tuple[str, ...]:
        """Return the authored values for facet `key`, a string read as one value and a list as its values.

        Args:
            key: A `FacetSpec.key`, the facet's sole address.

        Returns:
            The facet's authored values in order, or `()` when `key` is absent.
        """
        value = getattr(self, key, None)
        if isinstance(value, str):
            return (value,)
        if isinstance(value, (tuple, list)):
            return tuple(v for v in value if isinstance(v, str))
        return ()


def _coerce_generic_links(data: Mapping[str, object], field_specs: tuple[FrontmatterFieldSpec, ...]) -> dict[str, object]:
    """Return a copy of `data` with its link and link-list fields turned into `UnitLink` objects."""
    coerced: dict[str, object] = dict(data)
    for fs in field_specs:
        if fs.name not in coerced:
            continue
        raw_val = coerced[fs.name]
        if fs.type == "link" and isinstance(raw_val, str):
            coerced[fs.name] = UnitLink.parse(raw_val)
        elif fs.type == "link-list" and isinstance(raw_val, list):
            coerced[fs.name] = tuple(UnitLink.parse(v) if isinstance(v, str) else v for v in raw_val)
    return coerced


def _is_blank(value: object) -> bool:
    """`None`, a whitespace-only string, or an empty list: a required field carrying no value."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return isinstance(value, list) and not value


def _check_generic_constraints(
    fm: BaseFrontmatter, data: Mapping[str, object], field_specs: tuple[FrontmatterFieldSpec, ...], schema: KnowledgeBaseSchema, page_type: str
) -> None:
    """Refuse a missing required field, a malformed id or prefix, a disallowed status or a value outside its choices."""
    for fs in field_specs:
        if fs.required and fs.name not in data:
            msg = f"required field {fs.name!r} missing from {page_type!r} frontmatter"
            raise FrontmatterError(msg)
        if fs.required and _is_blank(data[fs.name]):
            msg = f"required field {fs.name!r} is empty in {page_type!r} frontmatter, give it a value or leave the page unwritten"
            raise FrontmatterError(msg)

    if not _PAGE_ID_RE.fullmatch(fm.id):
        msg = (
            f"id {fm.id!r} is not a page id: one line of letters, digits and . _ - @ / + : with no spaces, quotes or brackets. "
            "An indented list line right under 'id:' folds into the id, move it under its own key"
        )
        raise FrontmatterError(msg)

    prefix = schema.prefix_by_page_type.get(page_type)
    if prefix and not fm.id.startswith(prefix):
        msg = f"id {fm.id!r} must start with {prefix!r} for type {page_type!r}, e.g. {prefix}short-slug-of-the-title"
        raise FrontmatterError(msg)

    pt = schema.page_type(page_type)
    allowed = frozenset(pt.status_values) if pt is not None and pt.status_values else DEFAULT_STATUSES
    if fm.status not in allowed:
        msg = f"status {fm.status!r} not allowed for type {page_type!r} (allowed: {sorted(allowed)})"
        raise FrontmatterError(msg)

    for fs in field_specs:
        if not fs.choices or fs.name not in data:
            continue
        val = data[fs.name]
        if val not in fs.choices:
            msg = (
                f"field {fs.name!r} value {val!r} not in choices {list(fs.choices)!r} for type {page_type!r}: edit "
                f"knowledge-base-schema.json page_types[page_type={page_type}].frontmatter_fields[name={fs.name}].choices"
            )
            raise FrontmatterError(msg)


def _parse_frontmatter_generic(data: Mapping[str, object], spec: KnowledgeBaseSchema, page_type: str) -> BaseFrontmatter:
    """Validate frontmatter for one page type the schema declares, its own fields kept as extras."""
    pt = spec.page_type(page_type)
    field_specs: tuple[FrontmatterFieldSpec, ...] = pt.frontmatter_fields if pt is not None else ()
    coerced = _coerce_generic_links(data, field_specs)
    fm = BaseFrontmatter.model_validate(coerced)
    _check_generic_constraints(fm, data, field_specs, spec, page_type)
    return fm


def parse_frontmatter(data: Mapping[str, object], spec: KnowledgeBaseSchema | None = None) -> BaseFrontmatter:
    """Validate raw frontmatter against the schema of the page type its `type` names.

    Link fields become `UnitLink` objects.
    Required fields, the id prefix, the status and each field's `choices` are checked.

    Args:
        data: Raw frontmatter mapping from the YAML or JSON parser.
        spec: The knowledge base schema.

    Returns:
        A `BaseFrontmatter` with the page type's own fields carried as extras.

    Raises:
        FrontmatterError: If `type` is missing or undeclared, or a field breaks the page type's schema.
        NoteGrammarError: If a link field holds a malformed unit link.
        KnowledgeBaseSchemaMissingError: If `spec` is None.
    """
    import engram.kb.domain.schema as _spec_mod  # noqa: PLC0415 (deferred for circular-import safety)

    resolved_spec = _spec_mod.resolve_schema(spec)
    page_type = data.get("type")

    if isinstance(page_type, str) and page_type in resolved_spec.page_type_values:
        return _parse_frontmatter_generic(data, resolved_spec, page_type)

    msg = f"unknown or missing page type: {page_type!r}"
    raise FrontmatterError(msg)


BUILT_IN_CLAIM_FIELDS: frozenset[str] = frozenset({"loc", "quote", "kind", "subject", "status", "disputed_by"})
"""The claim fields every claim carries whatever its schema declares, each a `ClaimBlock` attribute of its own.

A schema may list them in its claim field lists, and they parse the same either way.
"""


class ClaimBlock(BaseModel):
    """One claim with its source locator: a `^cN`-anchored bullet under the claims section.

    `loc` is the only required field. Validation, not this model, requires `quote` for evidence kinds.
    `fields` holds the claim's declared text fields and `links` its declared link fields, each keyed by field name.
    `status` and `disputed_by` are set by validation on a contradiction and are unset on a new claim.
    An unknown key is refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    anchor: str  # "^c3" as written, the indexer derives block_id "c3"
    text: str
    loc: str  # required locator into the source
    quote: str | None = None
    kind: str = ""
    subject: UnitLink | None = None
    edges: tuple[tuple[str, UnitLink], ...] = ()  # (relation, target), e.g. ("outperforms", [[graphrag]])
    status: str | None = None  # "disputed" when a contradiction is flagged, else unset (active)
    disputed_by: tuple[UnitLink, ...] = ()  # the claim(s) this one is disputed by, e.g. [[paper.x#^cN]]
    fields: Mapping[str, str] = Field(default_factory=dict)
    """The claim's declared text fields by name, each value as written."""
    links: Mapping[str, tuple[UnitLink, ...]] = Field(default_factory=dict)
    """The claim's declared link fields by name, each with its targets in written order, one target for a link scalar field."""


class Section(BaseModel):
    """One addressable note section: a heading body or synthetic preamble retrieval unit."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["heading", "preamble"] = "heading"
    heading: str
    level: Literal[0, 2, 3]
    path: str  # ltree-safe slug: "findings", "approach.graph_indexing"
    canon: str | None  # canonical-H2 slug when the heading matches the type's canon set, else None
    body: str
    ord: int


class KnowledgeBaseNote(BaseModel):
    """One parsed knowledge base note file: typed frontmatter, sections, and claims."""

    model_config = ConfigDict(frozen=True)

    frontmatter: BaseFrontmatter  # the per-type subclass in practice
    sections: tuple[Section, ...]
    claims: tuple[ClaimBlock, ...]
    source_path: str  # knowledge-base-relative path, for validation messages


class RelationSpec(BaseModel):
    """A registered relation's metadata."""

    model_config = ConfigDict(frozen=True)

    domain: str
    range: str
    description: str = ""
    symmetric: bool = False


class VocabularyIndex(BaseModel):
    """The indexed vocabulary from `_meta/vocabulary/*.md`: the validate stage's reference."""

    model_config = ConfigDict(frozen=True)

    entity_types: Mapping[str, str]  # value to definition
    relations: Mapping[str, RelationSpec]  # relation key to its metadata, domain and range included
    facets: Mapping[str, Mapping[str, str]]  # facet name to value to definition
    claim_types: Mapping[str, str]  # kind to definition
