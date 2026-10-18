"""The authoring payload: the JSON document a writer emits in place of markdown.

`engram.kb.domain.renderer` turns a payload into canonical markdown.
The frontmatter stays a raw dict but is validated on construction, so a bad page is refused before it is rendered.
Claim anchors are optional and `assign_anchors` fills them, and link fields accept raw `"[[…]]"` strings.
Claims are listed apart from the sections, and the renderer writes them as bullets under the claims section.
A claim keeps its `status` and `disputed_by` keys, so a payload written from a flagged note keeps the flag.
A claim names each declared field as a key of its own, and the payload moves it into the claim's `fields` or `links`.
A claim that nests `fields` or `links` itself is refused, and every refused claim key is reported at the key the writer sent.
`payload_json_schema` exports the JSON Schema of a payload for one page type.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Annotated, Any

from core.markdown import heading_for_slug, heading_slug
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, JsonValue, ValidationError, model_validator
from pydantic_core import InitErrorDetails, PydanticCustomError

from engram.kb.domain.note import BaseFrontmatter, ClaimBlock, UnitLink, parse_frontmatter
from engram.kb.domain.schema import ClaimGrammarSpec, KnowledgeBaseSchema, PageTypeSpec, VocabEntry
from engram.kb.exceptions import FrontmatterError, NoteGrammarError

# An authoring claim's link field accepts a `UnitLink` or a raw `"[[…]]"` string.
# Its `BeforeValidator` turns a string into a `UnitLink`, so the stored value is always a `UnitLink`.
LinkInput = UnitLink | str
EdgeInput = tuple[str, LinkInput]


def _coerce_link(value: object) -> object:
    """Turn a raw `"[[…]]"` string into a `UnitLink` and pass anything else through."""
    if isinstance(value, str):
        return UnitLink.parse(value)
    return value


def _coerce_edges(value: object) -> object:
    """Turn the raw string target of each `(relation, target)` pair into a `UnitLink`."""
    if not isinstance(value, Iterable) or isinstance(value, str):
        return value
    coerced: list[tuple[str, UnitLink]] = []
    for pair in value:
        if isinstance(pair, (tuple, list)) and len(pair) == _EDGE_PAIR_LEN:
            relation, target = pair
            link = UnitLink.parse(target) if isinstance(target, str) else target
            coerced.append((relation, link))
        else:
            return value  # malformed, let pydantic raise the precise error
    return tuple(coerced)


# An edge is a `(relation, target)` pair.
_EDGE_PAIR_LEN = 2


class AuthoringClaim(BaseModel):
    """One claim as a writer emits it: a `ClaimBlock` with an optional anchor.

    `assign_anchors` fills missing anchors in payload order.
    Link fields accept raw `"[[…]]"` strings. Every other field means what it means on `ClaimBlock`.
    An unknown key is refused.
    `AuthoringPayload` moves a claim's declared field keys into `fields` and `links` before this model reads them,
    and refuses a raw claim that carries `fields` or `links` itself.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    loc: str
    anchor: str | None = None
    quote: str | None = None
    kind: str = ""
    subject: Annotated[LinkInput, BeforeValidator(_coerce_link)] | None = None
    edges: Annotated[tuple[EdgeInput, ...], BeforeValidator(_coerce_edges)] = ()
    status: str | None = None
    disputed_by: tuple[Annotated[LinkInput, BeforeValidator(_coerce_link)], ...] = ()
    fields: Mapping[str, str] = Field(default_factory=dict)
    links: Mapping[str, tuple[Annotated[LinkInput, BeforeValidator(_coerce_link)], ...]] = Field(default_factory=dict)

    def to_block(self) -> ClaimBlock:
        """Convert to a `ClaimBlock`. You must set the anchor first, through `assign_anchors`.

        Returns:
            The claim as a `ClaimBlock`.

        Raises:
            ValueError: If the anchor is unset.
        """
        if self.anchor is None:
            msg = "claim anchor must be assigned (call assign_anchors) before converting to a ClaimBlock"
            raise ValueError(msg)
        return ClaimBlock.model_validate({**self.model_dump(), "anchor": self.anchor})


class AuthoringSection(BaseModel):
    """One body section the writer provides: a heading, a body and a level that defaults to H2.

    `heading` may be left out when `canon`, a canonical section slug such as `tl_dr`, is given in its place.
    The heading is then derived from the slug. A heading whose slug disagrees with `canon` is refused.
    Claims do not go in a body. The renderer writes them from `AuthoringPayload.claims`.
    `guidance` is carried through unread, so a skeleton validates as a payload unchanged.
    """

    model_config = ConfigDict(frozen=True)

    heading: str
    body: str
    level: int = 2
    guidance: str = ""

    @model_validator(mode="before")
    @classmethod
    def _accept_canon_alias(cls, data: object) -> object:
        """Derive `heading` from the `canon` slug when it is absent, and refuse a pair that disagrees."""
        if not isinstance(data, Mapping):
            return data
        canon = data.get("canon")
        if not isinstance(canon, str):
            return data
        heading = data.get("heading")
        if heading is None:
            return {**data, "heading": heading_for_slug(canon)}
        if isinstance(heading, str) and heading_slug(heading) != canon:
            msg = f"section heading {heading!r} does not match canon {canon!r}. Drop one or make them agree"
            raise ValueError(msg)
        return data


class AuthoringPayload(BaseModel):
    """What a writer emits in place of raw markdown, which the renderer turns into a `.md` file.

    The frontmatter is validated against the schema on construction.
    The raw dict is kept, so keys the models do not type render verbatim.
    """

    model_config = ConfigDict(frozen=True)

    frontmatter: Mapping[str, JsonValue]
    sections: tuple[AuthoringSection, ...] = ()
    claims: tuple[AuthoringClaim, ...] = ()

    knowledge_base_schema: KnowledgeBaseSchema | None = None
    """The knowledge base schema. A payload without one is refused when its frontmatter is validated."""

    @model_validator(mode="before")
    @classmethod
    def _fold_declared_claim_fields(cls, data: object) -> object:
        """Move each raw claim's declared field keys into its `fields` and `links`, refusing every key it cannot hold at that key."""
        if not isinstance(data, Mapping):
            return data
        schema = data.get("knowledge_base_schema")
        claims = data.get("claims")
        if not isinstance(schema, KnowledgeBaseSchema) or schema.claims is None or not isinstance(claims, (list, tuple)):
            return data
        refusals: list[InitErrorDetails] = []
        folded = [
            _folded_claim(claim, schema.claims, index, refusals) if isinstance(claim, Mapping) else claim
            for index, claim in enumerate(claims)
        ]
        if refusals:
            raise ValidationError.from_exception_data(cls.__name__, refusals)
        return {**data, "claims": folded}

    @model_validator(mode="after")
    def _validate_frontmatter(self) -> AuthoringPayload:
        """Validate the frontmatter, raising a refusal as `ValueError` so it joins the `ValidationError`."""
        try:
            parse_frontmatter(self.frontmatter, self.knowledge_base_schema)
        except FrontmatterError as exc:
            raise ValueError(str(exc)) from exc
        return self

    @property
    def page_type(self) -> str:
        """The validated page type (`frontmatter['type']`)."""
        return self.validated_frontmatter().type

    def validated_frontmatter(self) -> BaseFrontmatter:
        """Return the typed frontmatter.

        Returns:
            The `BaseFrontmatter`, with the page type's own fields carried as extras.
        """
        return parse_frontmatter(self.frontmatter, self.knowledge_base_schema)


_AUTHORING_CLAIM_KEYS = frozenset(AuthoringClaim.model_fields)

# The claim attributes a raw claim never carries itself: its declared fields are keys of their own.
_NESTED_CLAIM_KEYS = frozenset({"fields", "links"})


def _folded_claim(
    claim: Mapping[str, object], grammar: ClaimGrammarSpec, index: int, refusals: list[InitErrorDetails]
) -> dict[str, object]:
    """`claim` with each declared field key moved into `fields` or `links`, an unset one left out, each refusal added to `refusals`."""
    folded: dict[str, object] = {}
    fields: dict[str, str] = {}
    links: dict[str, tuple[UnitLink, ...]] = {}
    for key, value in claim.items():
        at = ("claims", index, key)
        if key in _AUTHORING_CLAIM_KEYS and key not in _NESTED_CLAIM_KEYS:
            folded[key] = value
        elif key not in grammar.link_fields and key not in grammar.text_fields:
            refusals.append(_refusal(at, value, _refused_key_reason(key, grammar)))
        elif _is_unset(value):
            continue
        elif key in grammar.link_fields:
            links[key] = _declared_links(key, value, at, scalar=key in grammar.link_scalar_names, refusals=refusals)
        elif isinstance(value, str):
            fields[key] = value
        else:
            refusals.append(InitErrorDetails(type="string_type", loc=at, input=value))
    if fields:
        folded["fields"] = fields
    if links:
        folded["links"] = links
    return folded


def _refused_key_reason(key: str, grammar: ClaimGrammarSpec) -> str:
    """Why a raw claim may not carry `key`: it nests the declared fields, it is an edge relation, or nothing declares it."""
    if key in _NESTED_CLAIM_KEYS:
        declared = ", ".join((*grammar.link_fields, *grammar.text_fields)) or "none"
        return f"write each declared field as a key of its own on the claim, not inside {key!r}. Declared fields: {declared}"
    if key in grammar.edge_relations:
        return f'field {key!r} is an edge relation. Write it in edges as ["{key}", "[[target]]"]'
    return grammar.undeclared_field_refusal(key)


def _declared_links(
    key: str, value: object, at: tuple[str | int, ...], *, scalar: bool, refusals: list[InitErrorDetails]
) -> tuple[UnitLink, ...]:
    """The links of one declared link field's raw value, each refusal added to `refusals` at the key or list position it is about."""
    if not isinstance(value, (list, tuple)):
        if not scalar:
            refusals.append(_refusal(at, value, f'field {key!r} takes a list of [[page]] links, such as ["[[page]]"]'))
            return ()
        items: list[tuple[tuple[str | int, ...], object]] = [(at, value)]
    elif scalar and len(value) > 1:
        refusals.append(_refusal(at, value, f"field {key!r} takes one link, got {len(value)}"))
        return ()
    else:
        items = [((*at, position), item) for position, item in enumerate(value)]
    links: list[UnitLink] = []
    for item_at, item in items:
        link = _link_or_none(item)
        if link is None:
            refusals.append(_refusal(item_at, item, f"field {key!r} takes [[page]] links, got {item!r}"))
        else:
            links.append(link)
    return tuple(links)


def _link_or_none(item: object) -> UnitLink | None:
    """`item` as a `UnitLink`, or None when it is not a link or a `"[[…]]"` string."""
    if isinstance(item, UnitLink):
        return item
    if not isinstance(item, str):
        return None
    try:
        return UnitLink.parse(item)
    except NoteGrammarError:
        return None


def _refusal(at: tuple[str | int, ...], value: object, reason: str) -> InitErrorDetails:
    """One refused claim key, reported at `at` with `reason` as its message."""
    return InitErrorDetails(type=PydanticCustomError("claim_field", "{reason}", {"reason": reason}), loc=at, input=value)


def _is_unset(value: object) -> bool:
    """Whether a raw claim field value carries nothing: null, an empty string or an empty list."""
    return value is None or value == "" or (isinstance(value, (list, tuple)) and not value)


def assign_anchors(claims: Iterable[AuthoringClaim]) -> tuple[AuthoringClaim, ...]:
    """Return `claims` with every anchor in `^cN` form.

    An explicit anchor, `^c7` or bare `c7`, is kept in caret form.
    A claim without one gets the next free `cN` in payload order, skipping numbers explicit anchors use.

    Args:
        claims: The claims in payload order.

    Returns:
        The claims in the same order, every one anchored.
    """
    pending = tuple(claims)  # the iterable is walked twice
    explicit = [_normalise_anchor(c.anchor) for c in pending]
    counter = _AnchorCounter(used={a for a in explicit if a is not None})
    return tuple(
        claim.model_copy(update={"anchor": anchor if anchor is not None else counter.next_free()})
        for claim, anchor in zip(pending, explicit, strict=True)
    )


class _AnchorCounter:
    """Dispenses the next free `^cN` anchor, skipping numbers that explicit anchors already claimed."""

    def __init__(self, used: set[str]) -> None:
        self._used = used
        self._n = 1

    def next_free(self) -> str:
        while f"^c{self._n}" in self._used:
            self._n += 1
        anchor = f"^c{self._n}"
        self._used.add(anchor)
        self._n += 1
        return anchor


def _normalise_anchor(anchor: str | None) -> str | None:
    """Normalize an explicit anchor to `^cN`, None stays None (auto-assign later)."""
    if anchor is None:
        return None
    return anchor if anchor.startswith("^") else f"^{anchor}"


def payload_json_schema(page_type: str, spec: KnowledgeBaseSchema) -> dict[str, Any]:
    """Export the JSON schema for an `AuthoringPayload` of one page type.

    Args:
        page_type: The page type, such as `"note"` or `"recipe"`.
        spec: The knowledge base schema.

    Returns:
        A JSON Schema dict describing the authoring payload for this page type.

    Raises:
        FrontmatterError: If the schema declares no such page type.
    """
    pt = spec.page_type(page_type)
    if pt is None:
        msg = f"unknown page type {page_type!r}"
        raise FrontmatterError(msg)

    return _generic_payload_json_schema(page_type, pt, spec)


_FIELD_TYPE_TO_JSON_SCHEMA: dict[str, dict[str, Any]] = {
    "scalar": {"type": "string"},
    "int": {"type": "integer"},
    "scalar-list": {"type": "array", "items": {"type": "string"}},
    "link": {"type": "string", "description": "Unit link string, e.g. [[target]]"},
    "link-list": {"type": "array", "items": {"type": "string"}, "description": "List of unit link strings"},
}
"""JSON Schema fragment for each `FieldType` value."""


def _generic_payload_json_schema(page_type: str, page_type_schema: PageTypeSpec, spec: KnowledgeBaseSchema) -> dict[str, Any]:
    """Build the JSON Schema of a whole authoring payload for one page type."""
    # Universal BaseFrontmatter properties.
    properties: dict[str, Any] = {
        "id": {"type": "string"},
        "type": {"type": "string", "const": page_type},
        "title": {"type": "string"},
        "summary": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
        "status": {"type": "string", "enum": list(page_type_schema.status_values or ("stub", "draft", "synthesized", "verified"))},
        "superseded_by": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
        "aliases": {"type": "array", "items": {"type": "string"}, "default": []},
    }
    # Each facet the knowledge base declares for this page type, an empty scope meaning every page type.
    properties.update(
        {
            facet.key: {"type": "array", "items": {"type": "string"}, "default": []}
            for facet in spec.facets
            if not facet.scope or page_type in facet.scope
        }
    )
    required_fields = ["id", "type", "title", "status"]

    for fs in page_type_schema.frontmatter_fields:
        prop = dict(_FIELD_TYPE_TO_JSON_SCHEMA.get(fs.type, {"type": "string"}))
        if fs.choices:
            prop["enum"] = list(fs.choices)
        if fs.default is not None:
            prop["default"] = fs.default
        properties[fs.name] = prop
        if fs.required:
            required_fields.append(fs.name)

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"AuthoringPayload[{page_type}]",
        "type": "object",
        "properties": {
            "frontmatter": {
                "type": "object",
                "title": f"{page_type.capitalize()}Frontmatter",
                "properties": properties,
                "required": required_fields,
            },
            "sections": _sections_schema(page_type_schema),
            "claims": _claims_schema(page_type_schema, spec),
        },
        "required": ["frontmatter"],
    }


def _nullable_string_schema(*, description: str | None = None) -> dict[str, Any]:
    """JSON Schema fragment for an optional `str | None = None` field."""
    prop: dict[str, Any] = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
    if description is not None:
        prop["description"] = description
    return prop


def _sections_schema(pt: PageTypeSpec) -> dict[str, Any]:
    """Build the `sections` array schema, needing `heading` or `canon` and naming the canonical slugs."""
    heading_schema: dict[str, Any] = {"type": "string"}
    canon_schema: dict[str, Any] = {
        "type": "string",
        "description": "Canonical section slug (e.g. 'tl_dr'), an alternative to heading: derives the heading via "
        "slug-to-title. `heading` wins when both are given and agree. A disagreement is an error.",
    }
    if pt.canonical_sections:
        canon_slugs = ", ".join(pt.canonical_section_slugs)
        heading_schema["description"] = (
            f"Canonical section slugs for this page_type, in order: {canon_slugs}. A heading is canonical when its own "
            "slug (the same slug-to-title mapping canon uses, in reverse) equals one of these. Non-canonical "
            "headings are also legal."
        )
        canon_schema["description"] += f" Canonical section slugs for this page_type, in order: {canon_slugs}."
    item = {
        "type": "object",
        "title": "AuthoringSection",
        "properties": {
            "heading": heading_schema,
            "canon": canon_schema,
            "body": {"type": "string"},
            "level": {"type": "integer", "default": 2},
        },
        "required": ["body"],
        "anyOf": [{"required": ["heading"]}, {"required": ["canon"]}],
    }
    return {"type": "array", "items": item, "default": []}


def _claims_schema(pt: PageTypeSpec, spec: KnowledgeBaseSchema) -> dict[str, Any]:
    """Build the `claims` array schema, an empty-only array when the page type takes no claims."""
    if pt.claims_enabled and spec.claims is not None:
        return {"type": "array", "items": _claim_item_schema(spec.claims, spec.claim_types), "default": []}
    return {
        "type": "array",
        "items": {"type": "object"},
        "maxItems": 0,
        "default": [],
        "description": "Claims are not applicable to this page type.",
    }


def _claim_item_schema(claims: ClaimGrammarSpec, claim_types: tuple[VocabEntry, ...]) -> dict[str, Any]:
    """Build the schema of one claim from a knowledge base's claim grammar and claim kinds."""
    link_schema = _FIELD_TYPE_TO_JSON_SCHEMA["link"]
    link_list_schema = _FIELD_TYPE_TO_JSON_SCHEMA["link-list"]

    properties: dict[str, Any] = {
        "text": {"type": "string"},
        "loc": {"type": "string"},
        "anchor": _nullable_string_schema(description="Explicit ^cN anchor. Omit to auto-assign in payload order."),
    }

    if claim_types:
        properties["kind"] = {
            "type": "string",
            "enum": [entry.value for entry in claim_types],
            "default": "",
            "description": "\n".join(f"{entry.value}: {entry.definition}" for entry in claim_types),
        }
    else:
        properties["kind"] = {
            "type": "string",
            "default": "",
            "description": "The knowledge base defines the claim kind. This knowledge base declares no closed claim-kind vocabulary.",
        }

    quote_description = None
    if claims.quote_required_types:
        quote_description = f"Required when kind is one of: {', '.join(claims.quote_required_types)}."
    properties["quote"] = _nullable_string_schema(description=quote_description)

    properties["subject"] = {"anyOf": [dict(link_schema), {"type": "null"}], "default": None}

    relation_schema: dict[str, Any] = {"type": "string"}
    if claims.edge_relations:
        relation_schema["enum"] = list(claims.edge_relations)
    properties["edges"] = {
        "type": "array",
        "items": {"type": "array", "prefixItems": [relation_schema, dict(link_schema)], "minItems": 2, "maxItems": 2},
        "default": [],
        "description": "List of (relation, target) pairs.",
    }

    for name in claims.link_fields:
        if name in claims.link_scalar_names:
            properties[name] = {"anyOf": [dict(link_schema), {"type": "null"}], "default": None}
        else:
            properties[name] = {**link_list_schema, "default": []}

    for name in claims.text_fields:
        properties[name] = _nullable_string_schema()

    properties["status"] = _nullable_string_schema()
    properties["disputed_by"] = {**link_list_schema, "default": []}

    return {
        "type": "object",
        "title": "AuthoringClaim",
        "properties": properties,
        "required": ["text", "loc"],
    }
