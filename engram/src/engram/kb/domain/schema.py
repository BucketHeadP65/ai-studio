"""The schema a knowledge base declares, which engram indexes the knowledge base against.

`KnowledgeBaseSchema` declares a knowledge base's page types, frontmatter fields, canonical sections,
typed relations, claim grammar, vocabulary, and facet mapping, and engram validates notes against it.
A knowledge base always carries its own `knowledge-base-schema.json`. A knowledge base with none is
refused, and the message names a template to start from.
"""

from __future__ import annotations

import json
import re

from collections.abc import Mapping
from functools import cached_property
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_serializer, model_validator

from engram.init.contracts import list_templates
from engram.kb.domain.note import BUILT_IN_CLAIM_FIELDS, DEFAULT_STATUSES
from engram.kb.exceptions import KnowledgeBaseSchemaMissingError

if TYPE_CHECKING:
    from pathlib import Path
    from typing import NoReturn

SCHEMA_FORMAT_VERSION = "0.1"

# The lowercase-hyphen shape of `KnowledgeBaseSchema.slug` and `PageTypeSpec.page_type`. Both become
# file path segments and appear verbatim in generated markdown, so neither may hold a path separator
# or markdown syntax.
_SLUG_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"

FieldType = Literal["scalar", "int", "scalar-list", "link", "link-list"]
"""A frontmatter field's value shape: scalar=str, int=int, scalar-list=tuple[str], link=one UnitLink,
link-list=tuple[UnitLink]."""

_LINK_TYPES: frozenset[FieldType] = frozenset({"link", "link-list"})
"""FieldType values that represent typed relations (UnitLink or tuple thereof)."""


class FrontmatterFieldSpec(BaseModel):
    """One per-type frontmatter field declared by the spec, beyond the universal BaseFrontmatter fields."""

    model_config = ConfigDict(frozen=True)

    name: str
    type: FieldType
    required: bool = False
    choices: tuple[str, ...] = ()  # closed value set for a scalar (e.g. ("open","answered")), empty = open
    default: str | int | None = None  # default for an optional scalar/int, lists default to empty


class CanonicalSection(BaseModel):
    """One canonical H2 slug a page type pre-seeds, with the heading it renders as and its authoring guidance.

    A bare slug string in `PageTypeSpec.canonical_sections` is this section's `slug` alone, with
    `heading` unset. The renderer then title-cases the slug (`tl_dr` becomes `Tl Dr`). A declared
    `heading` is the literal H2 text this knowledge base writes for that slug (e.g. `TL;DR`), and
    the renderer prefers it. A declared `guidance` says what this section should contain and is
    shown to whoever authors a page of this page type. "" means no such note.
    """

    model_config = ConfigDict(frozen=True)

    slug: str
    heading: str | None = None
    guidance: str = ""

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_slug(cls, data: object) -> object:
        """A plain string entry is this section's slug alone, with no declared heading."""
        if isinstance(data, str):
            return {"slug": data}
        return data

    @model_serializer
    def _serialize(self) -> str | dict[str, str]:
        """A bare slug string when this section declares neither heading nor guidance, else a dict of the fields set."""
        if self.heading is None and not self.guidance:
            return self.slug
        data = {"slug": self.slug}
        if self.heading is not None:
            data["heading"] = self.heading
        if self.guidance:
            data["guidance"] = self.guidance
        return data


class Outcome(BaseModel):
    """One status a decision may resolve to, with the link field an owner fills in when it does."""

    model_config = ConfigDict(frozen=True)

    status: str
    link: str | None = None  # an edge_field this outcome asks the owner to write, None = no link asked


class DecisionSpec(BaseModel):
    """A page type's decision block: the status it opens in, and the statuses an owner may resolve it to.

    `open` and every outcome `status` must sit in the page type's effective status set (its own
    `status_values`, or the grammar default set when it declares none), `open` itself must not also
    be an outcome, and every outcome `link` must name one of the page type's `link_list_edge_fields`.
    `KnowledgeBaseSchema` refuses a spec that breaks any of the three.
    """

    model_config = ConfigDict(frozen=True)

    open: str
    outcomes: tuple[Outcome, ...]


class PageTypeSpec(BaseModel):
    """One page type (`page_type`): id prefix, folder, fields, canonical sections, edges, and claim applicability."""

    model_config = ConfigDict(frozen=True)

    page_type: str = Field(pattern=_SLUG_PATTERN)
    id_prefix: str = ""  # "" means ids of this page type are free-form
    folder: str | None = None  # knowledge base subfolder under Memory/, informational, None = no fixed folder
    folders: tuple[str, ...] = ()  # additional knowledge-base-root-relative source folders (".." segments allowed)
    files: tuple[str, ...] = ()  # explicit knowledge-base-root-relative markdown files, for pages living outside any folder
    frontmatter_fields: tuple[FrontmatterFieldSpec, ...] = ()
    status_values: tuple[str, ...] = ()  # closed status set for this page type, empty = grammar defaults
    path_fields: tuple[str, ...] = ()  # scalar frontmatter_fields names whose values must resolve under the indexing path_root
    # Ordered H2 sections, rendered in tuple order. A bare string entry in the JSON becomes
    # `CanonicalSection(slug=that_string)`, so the tuple holds only `CanonicalSection` instances.
    canonical_sections: tuple[CanonicalSection, ...] = ()
    edge_fields: tuple[str, ...] = ()  # subset of frontmatter_fields names that are typed relation arrays (graph edges)
    claims_enabled: bool = False
    disclosed: bool = True  # whether units of this page type are listed as envelope children
    guidance: str = ""  # when and where a page of this page type is written, "" = no such note
    decision: DecisionSpec | None = None  # this page type's open status and outcomes, None = not a decision page type

    @property
    def canonical_section_slugs(self) -> tuple[str, ...]:
        """Ordered canonical H2 slugs alone, the identity half of `canonical_sections`."""
        return tuple(section.slug for section in self.canonical_sections)

    @property
    def decision_declared(self) -> bool:
        """Whether this page type declares a decision block (an open status and its outcomes)."""
        return self.decision is not None

    @property
    def effective_status_values(self) -> frozenset[str]:
        """This page type's closed status set: its own declared `status_values`, or the grammar's shared default set."""
        return frozenset(self.status_values) if self.status_values else DEFAULT_STATUSES

    @property
    def link_list_edge_fields(self) -> frozenset[str]:
        """This page type's `edge_fields` that are declared `link-list` frontmatter fields, not a singular `link`.

        These are the only edge fields a multi-valued write can target. An outcome's `link` must name
        one, and `engram.retrieval.decisions.decide_page` writes only these.
        """
        field_map = {field.name: field for field in self.frontmatter_fields}
        return frozenset(name for name in self.edge_fields if field_map[name].type == "link-list")

    @property
    def canonical_headings(self) -> Mapping[str, str]:
        """Slug -> declared display heading, for canonical sections that declare one."""
        return {section.slug: section.heading for section in self.canonical_sections if section.heading is not None}

    @property
    def disclosure_title(self) -> str:
        """The page type slug, this page type's identity in a disclosed listing."""
        return self.page_type

    @property
    def disclosure_summary(self) -> str:
        """One line a consumer reads before deciding to load the full type declaration."""
        sections = ", ".join(self.canonical_section_slugs) or "none"
        fields = ", ".join(f.name for f in self.frontmatter_fields) or "none"
        edges = ", ".join(self.edge_fields) or "none"
        prefix_clause = f"prefix {self.id_prefix}; " if self.id_prefix else ""
        return f"{prefix_clause}sections [{sections}]. Fields [{fields}]. Edges [{edges}]"


class RelationDecl(BaseModel):
    """A registered typed relation, as the knowledge base's vocabulary declares it."""

    model_config = ConfigDict(frozen=True)

    slug: str
    domain: str
    range: str
    symmetric: bool = False
    qualifiers: tuple[tuple[str, ...], ...] = ()
    """Groups of claim link field names. A claim asserting this relation, when the claim grammar lists it in
    `comparative_relations`, must carry at least one field of every group. A bare name in JSON is a group of one."""
    description: str = ""
    queue_when_missing: bool = False
    """Whether a link of this relation whose target is missing goes to the ingest queue rather than raising a dangling-link warning."""
    domain_section: str | None = Field(
        default=None,
        description=(
            "The slug of a canonical section of the domain page type. When set, this relation is derived from "
            "the wikilinks written in that section of a domain page's body, one edge per distinct link target, "
            "and never from frontmatter."
        ),
    )

    @field_validator("qualifiers", mode="before", json_schema_input_type=tuple[str | tuple[str, ...], ...])
    @classmethod
    def _bare_name_is_a_group_of_one(cls, value: object) -> object:
        """A bare field name among the qualifiers becomes a group holding that one name."""
        if isinstance(value, (list, tuple)):
            return tuple((item,) if isinstance(item, str) else item for item in value)
        return value


class VocabEntry(BaseModel):
    """A vocabulary value with its definition (entity types, claim types)."""

    model_config = ConfigDict(frozen=True)

    value: str
    definition: str = ""


class FacetSpec(BaseModel):
    """A domain facet: key, applicable scope, seed vocabulary, and whether the card prints it.

    The key is the sole address: `BaseFrontmatter.facet_values(key)` reads it from a page's frontmatter.
    An unknown key, such as `slot`, is refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    scope: tuple[str, ...] = ()  # page types it applies to, empty = all (informational)
    seed: tuple[str, ...] = ()  # seed vocabulary values for the facet
    card: bool = False
    """Whether a page's card prints this facet's values on its facet line, in the schema's facet order."""


class ClaimGrammarSpec(BaseModel):
    """Claim-bullet grammar field-sets and validation rules. Present iff some page type has claims_enabled."""

    model_config = ConfigDict(frozen=True)

    section: str
    """The slug of the canonical H2 section claims live under."""
    edge_relations: tuple[str, ...] = ()
    multilink_fields: tuple[str, ...] = ()
    link_scalar_fields: tuple[str, ...] = ()
    text_scalar_fields: tuple[str, ...] = ()
    plain_fields: tuple[str, ...] = ()
    quote_required_types: tuple[str, ...] = ()
    comparative_relations: tuple[str, ...] = ()

    @cached_property
    def link_fields(self) -> tuple[str, ...]:
        """The declared link field names, multilink fields then link scalar fields, each once.

        A built-in claim field or an edge relation is left out.
        """
        return _declared_names((*self.multilink_fields, *self.link_scalar_fields), excluded=frozenset(self.edge_relations))

    @cached_property
    def text_fields(self) -> tuple[str, ...]:
        """The declared text field names, text scalar fields then plain fields, each once.

        A built-in claim field, an edge relation or a link field is left out.
        """
        excluded = frozenset((*self.edge_relations, *self.link_fields))
        return _declared_names((*self.text_scalar_fields, *self.plain_fields), excluded=excluded)

    @cached_property
    def link_scalar_names(self) -> frozenset[str]:
        """The link fields that take exactly one target: the declared link scalar fields that are not multilink fields."""
        return (frozenset(self.link_fields) & frozenset(self.link_scalar_fields)) - frozenset(self.multilink_fields)

    def undeclared_field_refusal(self, key: str) -> str:
        """The refusal of claim key `key` when it is no built-in claim field, edge relation or declared field.

        Args:
            key: The refused key.

        Returns:
            The refusal, naming `key`, the built-in claim fields, the declared fields and the edge relations.
        """
        built_in = ", ".join(sorted(BUILT_IN_CLAIM_FIELDS))
        declared = ", ".join((*self.link_fields, *self.text_fields)) or "none"
        relations = ", ".join(self.edge_relations) or "none"
        return (
            f"field {key!r} is not declared by the knowledge base's claim grammar. "
            "Declare it in the claims block of knowledge-base-schema.json or remove it. "
            f"Built-in fields: {built_in}. Declared fields: {declared}. Edge relations: {relations}"
        )


def _declared_names(names: tuple[str, ...], *, excluded: frozenset[str]) -> tuple[str, ...]:
    """`names` in order, each once, without the built-in claim fields or `excluded`."""
    left_out = BUILT_IN_CLAIM_FIELDS | excluded
    return tuple(dict.fromkeys(name for name in names if name not in left_out))


SourceTypeName = Literal["video", "paper", "post", "article"]
"""The types a requested URL classifies into, the values `engram.sources.contracts.SourceType` carries."""

_FIELD_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
"""The shape a `SourcesSpec` mirror field name must have: a bare lowercase identifier, safe to
interpolate into a query as a frontmatter JSONB key or a bound value."""

LinkClass = Literal["profile", "encyclopedia", "homepage", "social-post", "repository"]
"""What a link is rather than what it says: the classes a policy refuses before any judgment is spent."""


class SourceQueuePolicy(BaseModel):
    """The hard rules of a knowledge base's ingestion policy: what a digest's references may become with nobody in the path.

    `source_types` are the source types a reference may have, checked against every reference when non-empty, empty
    means no source-type rule. `max_age_years` drops a reference whose year is older than now minus the value, an
    unknown year passes, no reader enforces it, it is a judgment rule stated in the guidance. `hop_depth` is how many
    queue hops from a source the owner added a reference may sit and still be fetched, past it the reference is
    recorded as seen. `per_source_cap` is how many rows one source may put on the queue in total, of any status.
    `per_pass_cap` is how many references one run of a digest workflow may add to the queue in total, across every
    source it digests, unset means no such ceiling. `never` names the link classes dropped on sight.
    `extra="forbid"` rejects an unknown or mistyped rule name passed to this model directly, a policy page's
    frontmatter is filtered to the known fields before validation, and the reader that filtered them reports what
    it dropped separately.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_types: tuple[SourceTypeName, ...] = ("video", "paper", "post", "article")
    max_age_years: int | None = Field(default=None, ge=1)
    hop_depth: int = Field(default=1, ge=0)
    per_source_cap: int = Field(default=5, ge=1)
    per_pass_cap: int | None = Field(default=None, ge=1)
    never: tuple[LinkClass, ...] = ("profile", "encyclopedia", "homepage", "social-post", "repository")


class SourcesSpec(BaseModel):
    """Whether this knowledge base reads outside sources, quote verification, which connectors read them, and its digest-split threshold."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    quote_verify: bool = False
    connectors: tuple[str, ...] = ()
    # The three mirror field names, none defaulted in code. A knowledge base enabling sources declares
    # all three. `key_field` is a digest's frontmatter field naming the mirror it digests, joined
    # against `sources_raw.provider_key`. `digest_link_field` is a digest's frontmatter field naming
    # the mirror it digests by `sources_raw.source_id`. `hash_field` is the frontmatter field a digest
    # copies its mirror's content hash into.
    key_field: str = ""
    digest_link_field: str = ""
    hash_field: str = ""
    # Heading-section threshold above which a digest should split into a lead page plus one page per
    # major section, instead of one page. Reading a source reports its heading sections against it as
    # `split_recommended`. None = no threshold, always one digest page.
    digest_sections_over: int | None = None
    digest_part_field: str = ""
    """The frontmatter field a section page of a split digest uses to name its lead page.

    A schema that sets `digest_sections_over` must set it, and every page type `digest_types` names must declare it."""
    # Whether a placement pass may move a newly landed mirror into a folder under Sources/ (and
    # create one) between sync and index. False (the default) leaves every mirror at the flat
    # top-level landing spot sync itself writes to.
    librarian: bool = False
    # Which page type digests a mirror of each item type (`video`, `paper`, `article`, `post`), none
    # defaulted in code. An item type left out here has no digest type, and a digest skeleton for it
    # is refused.
    digest_types: Mapping[str, str] = Field(default_factory=dict)
    digest_fields: Mapping[str, str] = Field(default_factory=dict)
    """Which digest frontmatter field each key of a mirror's metadata fills, from the metadata key to the field name.

    A digest skeleton copies the value of every mapped key the mirror's metadata holds. Every field named must be
    a frontmatter field of every page type `digest_types` names. A built-in field such as `title`, `summary` or
    `aliases` is refused, as it is for `digest_part_field`."""
    digest_claim_text_fields: bool = False
    """Whether a digest skeleton's claim stub also lists the claim grammar's text scalar fields. A link field is never listed."""
    digest_workflow: str = ""
    """The name of the workflow that digests this knowledge base's new mirrors, empty when it names none."""
    # The ingestion policy's hard rules when a knowledge base has no `_meta/ingestion-policy.md`, and the
    # values a page's frontmatter overrides field by field when it has one.
    source_queue: SourceQueuePolicy = Field(default_factory=SourceQueuePolicy)

    @model_validator(mode="before")
    @classmethod
    def _one_connector_spelling(cls, data: object) -> object:
        """`connector: "<name>"` is the one-connector spelling of `connectors`."""
        if isinstance(data, Mapping) and "connector" in data and "connectors" not in data:
            rest = {key: value for key, value in data.items() if key != "connector"}
            return {**rest, "connectors": (data["connector"],)}
        return data

    @field_validator("connectors")
    @classmethod
    def _no_repeats(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Refuse a repeated connector name."""
        if len(set(value)) != len(value):
            msg = f"connectors repeats a name: {value!r}"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _enabled_needs_a_connector(self) -> SourcesSpec:
        """Refuse `sources.enabled` with no connector named, since there is no default connector."""
        if self.enabled and not self.connectors:
            msg = "sources.enabled requires sources.connectors to name at least one connector"
            raise ValueError(msg)
        return self

    @field_validator("key_field", "digest_link_field", "hash_field", "digest_part_field")
    @classmethod
    def _safe_field_name(cls, value: str) -> str:
        """Refuse a declared field name that is not a bare lowercase identifier."""
        if value and not _FIELD_NAME_PATTERN.fullmatch(value):
            msg = f"must be a lowercase identifier ([a-z][a-z0-9_]*): {value!r}"
            raise ValueError(msg)
        return value

    @field_validator("digest_fields")
    @classmethod
    def _safe_digest_field_names(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        """Refuse a metadata key or a field name that is not a bare lowercase identifier."""
        bad = [name for pair in value.items() for name in pair if not _FIELD_NAME_PATTERN.fullmatch(name)]
        if bad:
            msg = f"sources.digest_fields names must be lowercase identifiers ([a-z][a-z0-9_]*): {', '.join(map(repr, bad))}"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _enabled_needs_field_names(self) -> SourcesSpec:
        """Refuse `sources.enabled` with any of the three mirror field names left empty."""
        missing = [name for name in ("key_field", "digest_link_field", "hash_field") if not getattr(self, name)]
        if self.enabled and missing:
            msg = f"sources.enabled requires sources.{' and sources.'.join(missing)} to be declared"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _enabled_needs_digest_types(self) -> SourcesSpec:
        """Refuse `sources.enabled` with an empty `digest_types`."""
        if self.enabled and not self.digest_types:
            msg = (
                "sources.enabled is true but sources.digest_types maps nothing: declare which digest page type "
                "each source type becomes (the research template maps video to talk, paper to paper, article to "
                "article, post to post), or set sources.enabled to false"
            )
            raise ValueError(msg)
        return self

    @field_validator("digest_sections_over")
    @classmethod
    def _positive_when_set(cls, value: int | None) -> int | None:
        """Refuse a threshold that is set but not positive."""
        if value is not None and value <= 0:
            msg = f"digest_sections_over must be positive when set: {value!r}"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _split_needs_a_part_field(self) -> SourcesSpec:
        """Refuse `digest_sections_over` set with `digest_part_field` left empty."""
        if self.digest_sections_over is not None and not self.digest_part_field:
            msg = (
                "sources.digest_sections_over is set but sources.digest_part_field is empty: name the frontmatter "
                "field a section page of a split digest uses to name its lead page"
            )
            raise ValueError(msg)
        return self

    @property
    def connector(self) -> str:
        """The first declared connector, or an empty string when none is declared (sources disabled)."""
        return self.connectors[0] if self.connectors else ""


class EntitiesSpec(BaseModel):
    """The page type a knowledge base keeps its entities in, one page per named thing.

    The page type must be declared and must hold a frontmatter field `entity_type`. `KnowledgeBaseSchema` refuses
    one that breaks either rule.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    page_type: str


class SourcesSelector(BaseModel):
    """A compounding input drawn from the source-mirror plane: mirrors with no digest yet."""

    model_config = ConfigDict(frozen=True)

    plane: Literal["sources"]
    selector: Literal["undigested"] = "undigested"


class PagesSelector(BaseModel):
    """A compounding input drawn from the pages plane: pages of the given page types.

    `within_hours` windows to pages written in the last N hours, None = no window. `status`
    narrows to one status value, None = any status except `skipped`.
    """

    model_config = ConfigDict(frozen=True)

    plane: Literal["pages"]
    page_types: tuple[str, ...] = Field(min_length=1)
    within_hours: int | None = Field(default=None, ge=1)
    status: str | None = None


class NotesSelector(BaseModel):
    """A compounding input drawn from the notes plane: markdown under one knowledge-base-root-relative folder.

    `folder` "" (the default) is the whole knowledge base.
    """

    model_config = ConfigDict(frozen=True)

    plane: Literal["notes"]
    folder: str = ""


Selector = Annotated[SourcesSelector | PagesSelector | NotesSelector, Field(discriminator="plane")]
"""One compounding entry's input: which plane it reads and how it narrows within it."""


class DerivationEntry(BaseModel):
    """One derivation rule: an input selector, the page types it may produce, and its run policy.

    `source` is the Python attribute for the JSON key `from`, the input side against `to`, the output side.
    `cap_per_run` bounds how many outputs one run of this entry may produce. `gate` is `"none"`
    (indexes straight in) or `"owner"` (lands for the owner to accept or dismiss before it
    indexes). `guidance` is free text a workflow step may surface to whatever produces the
    output, "" = no extra guidance beyond the output page type's own.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True, serialize_by_alias=True)

    name: str = Field(pattern=_SLUG_PATTERN)
    source: Selector = Field(alias="from")
    to: tuple[str, ...] = Field(min_length=1)
    cap_per_run: int = Field(default=20, ge=1)
    gate: Literal["none", "owner"] = "none"
    guidance: str = ""


class CompoundingSpec(BaseModel):
    """A knowledge base's derivation map: which existing material produces which page types, and under what policy.

    Absent from a spec, `entries` is empty and the knowledge base compounds nothing on its own.
    """

    model_config = ConfigDict(frozen=True)

    entries: tuple[DerivationEntry, ...] = ()

    @field_validator("entries")
    @classmethod
    def _unique_names(cls, value: tuple[DerivationEntry, ...]) -> tuple[DerivationEntry, ...]:
        """Refuse a repeated entry name."""
        names = [entry.name for entry in value]
        seen: set[str] = set()
        duplicates: list[str] = []
        for name in names:
            if name in seen:
                duplicates.append(name)
            seen.add(name)
        if duplicates:
            msg = f"duplicate entry names in compounding.entries: {duplicates!r}"
            raise ValueError(msg)
        return value

    def entry(self, name: str) -> DerivationEntry | None:
        """Return the entry named `name`, or None if unknown.

        Args:
            name: The entry's declared name.

        Returns:
            The matching `DerivationEntry`, or None.
        """
        for e in self.entries:
            if e.name == name:
                return e
        return None


class EmbedStrategiesSpec(BaseModel):
    """Which embedding-composition strategies index activates for this knowledge base, and their config.

    `active=None` (the default) activates the default set `KnowledgeBaseSchema.active_embed_strategies`
    derives: `page-card` and `section` always, `claim` when this spec declares a claim grammar, and
    `source-chunk` when `sources.enabled`. An explicit tuple replaces that set outright. `config` is an
    opaque settings blob per strategy name. The stock strategies ignore it, and a knowledge base's own
    strategy reads its own entry.
    """

    model_config = ConfigDict(frozen=True)

    active: tuple[str, ...] | None = None
    config: dict[str, dict[str, JsonValue]] = {}


class SectionSummarySpec(BaseModel):
    """Per-knowledge-base tuning for the indexed `kb.sections.summary` fallback (`engram.indexing.domain.section_summary`).

    `leading_sentences` bounds both the prose leg (leading sentence count) and the list leg
    (leading list-item count) of the derivation rule. `inline_gate_chars` is the body-length
    ceiling under which a section's summary is its own body verbatim.
    """

    model_config = ConfigDict(frozen=True)

    leading_sentences: int = 2
    inline_gate_chars: int = 1000


# How a collection presents over the disclosure surface. `strict` serves the root map and descent.
# `search_only` withholds the collection root map, and hits re-enter as disclosable refs.
DisclosureModeName = Literal["strict", "search_only"]


_RETRIEVAL_SWITCH_NAMES: tuple[str, ...] = ("embeddings", "sql", "graph")
"""The switch names `RetrievalSpec.floor` and `ceiling` may pin, the same as `engram.indexing.domain.switches.SWITCH_NAMES`."""


class RetrievalSpec(BaseModel):
    """Retrieval profile for the MCP server engram ships.

    `profile` is not validated here. The settings layer checks it against the strategy names.

    `collection` is the partition this knowledge base's MCP `search` tool scopes to. Left unset (the
    default), `KnowledgeBaseSchema` fills it in from the spec's own `slug`. Set it only when the
    knowledge base indexes into a partition named differently from its slug.

    `mode` picks between a fixed retrieval configuration and one `engram.indexing.domain.switches` decides
    from knowledge base signals. `floor` names switches pinned on regardless of signals. `ceiling`, when set,
    names the only switches allowed on and forces every other switch off. Both pins name switches from
    `embeddings`, `sql`, `graph`.
    """

    model_config = ConfigDict(frozen=True)

    profile: str = "full"
    collection: str = ""
    disclosure_mode: DisclosureModeName = "strict"  # search_only withholds the collection root map, hits re-enter by ref
    # The key the collection map's root level groups pages by, None = a flat listing. "app" groups by the
    # `frontmatter ->> 'app'` value (`api.envelope_collection_groups`, migration 0019). "page_type"
    # groups by the `p.page_type` column (`api.envelope_collection_page_types`, migration 0020).
    group_by: Literal["app", "page_type"] | None = None
    mode: Literal["fixed", "auto"] = "fixed"
    floor: tuple[str, ...] = ()
    ceiling: tuple[str, ...] | None = None
    doc_authoring: bool = False  # mount knowledge_base_profile, skeleton and write_note on the knowledge base MCP surface
    private: bool = False  # true keeps this knowledge base out of any host's library over several knowledge bases
    date_field: str = "created"
    """The frontmatter field each page's page date is read from, the day a search's date range reads.

    It must be `created` or a scalar frontmatter field a page type declares. A page whose field holds no date has no page date."""

    @model_validator(mode="after")
    def _check_switch_pins(self) -> RetrievalSpec:
        """Refuse a `floor` or `ceiling` naming an unknown switch, or a `floor` outside the `ceiling`."""
        valid = set(_RETRIEVAL_SWITCH_NAMES)
        unknown = (set(self.floor) | set(self.ceiling or ())) - valid
        if unknown:
            msg = f"unknown switch name(s) {sorted(unknown)!r}. Valid switch names are: {', '.join(_RETRIEVAL_SWITCH_NAMES)}"
            raise ValueError(msg)
        if self.ceiling is not None and not set(self.floor) <= set(self.ceiling):
            msg = f"floor {self.floor!r} must be a subset of ceiling {self.ceiling!r}"
            raise ValueError(msg)
        return self


# Module-level invariant helpers called by KnowledgeBaseSchema._check_invariants.


def _check_page_type_uniqueness(page_types: tuple[PageTypeSpec, ...]) -> None:
    """Refuse empty page_types, or a repeated page_type or non-empty id_prefix."""
    if not page_types:
        msg = "page_types must be non-empty"
        raise ValueError(msg)
    page_type_values = [pt.page_type for pt in page_types]
    if len(page_type_values) != len(set(page_type_values)):
        msg = f"duplicate page_type values in page_types: {page_type_values!r}"
        raise ValueError(msg)
    prefixes = [pt.id_prefix for pt in page_types if pt.id_prefix]
    if len(prefixes) != len(set(prefixes)):
        msg = f"duplicate id_prefix values in page_types: {prefixes!r}"
        raise ValueError(msg)


def _check_edge_fields(page_types: tuple[PageTypeSpec, ...]) -> None:
    """Refuse an edge_field that is not a link-typed frontmatter field of its page type."""
    for pt in page_types:
        field_map = {f.name: f for f in pt.frontmatter_fields}
        for edge_name in pt.edge_fields:
            if edge_name not in field_map:
                msg = f"edge_field {edge_name!r} on page_type {pt.page_type!r} is not in frontmatter_fields"
                raise ValueError(msg)
            if field_map[edge_name].type not in _LINK_TYPES:
                msg = f"edge_field {edge_name!r} on page_type {pt.page_type!r} has type {field_map[edge_name].type!r}. Must be 'link' or 'link-list'"
                raise ValueError(msg)


def _check_path_field_types(page_types: tuple[PageTypeSpec, ...]) -> None:
    """Refuse a path_fields entry that is not a declared frontmatter field of type 'scalar'."""
    for pt in page_types:
        field_map = {f.name: f for f in pt.frontmatter_fields}
        for field_name in pt.path_fields:
            if field_name not in field_map:
                msg = f"path_field {field_name!r} on page_type {pt.page_type!r} is not in frontmatter_fields"
                raise ValueError(msg)
            if field_map[field_name].type != "scalar":
                msg = f"path_field {field_name!r} on page_type {pt.page_type!r} has type {field_map[field_name].type!r}. Must be 'scalar'"
                raise ValueError(msg)


def _check_relation_slug_uniqueness(relations: tuple[RelationDecl, ...]) -> None:
    """Refuse a repeated relation slug."""
    slugs = [r.slug for r in relations]
    seen: set[str] = set()
    duplicates: list[str] = []
    for slug in slugs:
        if slug in seen:
            duplicates.append(slug)
        seen.add(slug)
    if duplicates:
        msg = f"duplicate relation slugs in relations: {duplicates!r}"
        raise ValueError(msg)


def _check_relation_domain_sections(page_types: tuple[PageTypeSpec, ...], relations: tuple[RelationDecl, ...]) -> None:
    """Refuse a relation `domain_section` that is not a canonical section of its declared domain page type."""
    by_page_type = {pt.page_type: pt for pt in page_types}
    for relation in relations:
        if relation.domain_section is None:
            continue
        pt = by_page_type.get(relation.domain)
        if pt is None:
            msg = f"relation {relation.slug!r} declares domain_section but its domain {relation.domain!r} is not a declared page type"
            raise ValueError(msg)
        if relation.domain_section not in pt.canonical_section_slugs:
            msg = (
                f"relation {relation.slug!r} domain_section {relation.domain_section!r} is not a canonical section "
                f"of page_type {relation.domain!r} (canonical sections: {list(pt.canonical_section_slugs)})"
            )
            raise ValueError(msg)


def _check_claims_consistency(page_types: tuple[PageTypeSpec, ...], claims: ClaimGrammarSpec | None) -> None:
    """Refuse a claim grammar that is present exactly when no page type has claims_enabled, or absent when one does."""
    has_claims_enabled = any(pt.claims_enabled for pt in page_types)
    if has_claims_enabled and claims is None:
        msg = "claims must be non-None when at least one page type has claims_enabled=True"
        raise ValueError(msg)
    if not has_claims_enabled and claims is not None:
        msg = "claims must be None when no page type has claims_enabled=True"
        raise ValueError(msg)


_BUILT_IN_CLAIM_NAMES: frozenset[str] = BUILT_IN_CLAIM_FIELDS | {"text", "anchor"}
"""Every name a claim holds of its own: the built-in claim fields, its text and its anchor."""


def _check_claim_relations(relations: tuple[RelationDecl, ...], claims: ClaimGrammarSpec | None) -> None:
    """Refuse an edge relation with a built-in claim name, or a comparative relation qualifier that is no declared claim link field."""
    if claims is None:
        return
    for name in claims.edge_relations:
        if name in _BUILT_IN_CLAIM_NAMES:
            msg = (
                f"edge relation {name!r} is a built-in claim name. Rename the relation. "
                f"Built-in claim names: {', '.join(sorted(_BUILT_IN_CLAIM_NAMES))}"
            )
            raise ValueError(msg)
    comparative = frozenset(claims.comparative_relations)
    for relation in relations:
        if relation.slug not in comparative:
            continue
        for name in (name for group in relation.qualifiers for name in group):
            if name not in claims.link_fields:
                msg = (
                    f"relation {relation.slug!r} qualifier {name!r} is not a declared claim link field. "
                    "Declare it in multilink_fields or link_scalar_fields of the claims block, or remove it from the relation's qualifiers. "
                    f"Declared link fields: {', '.join(claims.link_fields) or 'none'}"
                )
                raise ValueError(msg)


_PAGE_CARD_STRATEGY = "page-card"
"""The one embed-strategy name every knowledge base must keep active (see `_check_embed_strategies_active`)."""


def _check_embed_strategies_active(embed_strategies: EmbedStrategiesSpec) -> None:
    """Refuse an explicit `active` override that omits `page-card`, which every page's row depends on."""
    if embed_strategies.active is not None and _PAGE_CARD_STRATEGY not in embed_strategies.active:
        msg = f"embed_strategies.active must include {_PAGE_CARD_STRATEGY!r} when given explicitly: {embed_strategies.active!r}"
        raise ValueError(msg)


def _check_compounding_page_types(page_types: tuple[PageTypeSpec, ...], compounding: CompoundingSpec) -> None:
    """Refuse a compounding entry naming an undeclared page type in its `to` or its selector."""
    known = {pt.page_type for pt in page_types}
    for entry in compounding.entries:
        for page_type in entry.to:
            if page_type not in known:
                msg = f"compounding entry {entry.name!r} names undeclared output page type {page_type!r} in 'to'"
                raise ValueError(msg)
        if isinstance(entry.source, PagesSelector):
            for page_type in entry.source.page_types:
                if page_type not in known:
                    msg = f"compounding entry {entry.name!r} names undeclared page type {page_type!r} in 'from.page_types'"
                    raise ValueError(msg)


def _digest_types_lacking(page_types: tuple[PageTypeSpec, ...], sources: SourcesSpec, field: str) -> list[str]:
    """The page types `sources.digest_types` names that do not declare the frontmatter field `field`, sorted."""
    declared = {pt.page_type: {f.name for f in pt.frontmatter_fields} for pt in page_types}
    return sorted({page_type for page_type in sources.digest_types.values() if field not in declared.get(page_type, set())})


def _check_digest_part_field(page_types: tuple[PageTypeSpec, ...], sources: SourcesSpec) -> None:
    """Refuse a `sources.digest_part_field` that a page type `sources.digest_types` names does not declare."""
    field = sources.digest_part_field
    if not field:
        return
    missing = _digest_types_lacking(page_types, sources, field)
    if missing:
        msg = (
            f"sources.digest_part_field {field!r} is not a frontmatter field of every page type sources.digest_types "
            f"names: {', '.join(missing)} {'does' if len(missing) == 1 else 'do'} not declare it"
        )
        raise ValueError(msg)


def _check_digest_fields(page_types: tuple[PageTypeSpec, ...], sources: SourcesSpec) -> None:
    """Refuse a `sources.digest_fields` field that a page type `sources.digest_types` names does not declare."""
    for field in dict.fromkeys(sources.digest_fields.values()):
        missing = _digest_types_lacking(page_types, sources, field)
        if missing:
            msg = (
                f"sources.digest_fields maps to {field!r}, which is not a frontmatter field of every page type "
                f"sources.digest_types names: {', '.join(missing)} {'does' if len(missing) == 1 else 'do'} not declare it"
            )
            raise ValueError(msg)


_ENTITY_TYPE_FIELD = "entity_type"
"""The frontmatter field an entity page names its entity type in."""


def _check_entities(page_types: tuple[PageTypeSpec, ...], entities: EntitiesSpec | None) -> None:
    """Refuse an `entities.page_type` that is not declared or does not declare the `entity_type` field."""
    if entities is None:
        return
    by_page_type = {pt.page_type: pt for pt in page_types}
    pt = by_page_type.get(entities.page_type)
    if pt is None:
        msg = f"entities.page_type {entities.page_type!r} is not a declared page type (declared: {', '.join(by_page_type)})"
        raise ValueError(msg)
    if all(f.name != _ENTITY_TYPE_FIELD for f in pt.frontmatter_fields):
        msg = (
            f"entities.page_type {entities.page_type!r} does not declare the frontmatter field {_ENTITY_TYPE_FIELD!r}. "
            "Add it to that page type's frontmatter_fields"
        )
        raise ValueError(msg)


_WRITTEN_DATE_FIELD = "created"
"""The frontmatter field holding the day a page was written, which every knowledge base may date its pages by."""


def _check_date_field(page_types: tuple[PageTypeSpec, ...], retrieval: RetrievalSpec) -> None:
    """Refuse a `retrieval.date_field` that is neither `created` nor a scalar frontmatter field a page type declares."""
    scalar_fields = (f.name for pt in page_types for f in pt.frontmatter_fields if f.type == "scalar")
    allowed = tuple(dict.fromkeys((_WRITTEN_DATE_FIELD, *scalar_fields)))
    if retrieval.date_field not in allowed:
        msg = (
            f"retrieval.date_field {retrieval.date_field!r} is not a field a page date can be read from. Name `created` or a "
            f"scalar frontmatter field a page type declares: {', '.join(allowed)}"
        )
        raise ValueError(msg)


def _check_decision_spec(page_types: tuple[PageTypeSpec, ...]) -> None:
    """Refuse a `decision` block that breaks its page type's status set or link-list edge fields."""
    for pt in page_types:
        decision = pt.decision
        if decision is None:
            continue
        allowed = pt.effective_status_values
        if decision.open not in allowed:
            msg = f"decision.open {decision.open!r} on page_type {pt.page_type!r} is not in its status set: {sorted(allowed)}"
            raise ValueError(msg)
        outcome_statuses = [outcome.status for outcome in decision.outcomes]
        if decision.open in outcome_statuses:
            msg = f"decision.open {decision.open!r} on page_type {pt.page_type!r} must not also be an outcome status"
            raise ValueError(msg)
        link_fields = pt.link_list_edge_fields
        for outcome in decision.outcomes:
            if outcome.status not in allowed:
                msg = f"decision outcome status {outcome.status!r} on page_type {pt.page_type!r} is not in its status set: {sorted(allowed)}"
                raise ValueError(msg)
            if outcome.link is not None and outcome.link not in link_fields:
                msg = f"decision outcome link {outcome.link!r} on page_type {pt.page_type!r} is not a link-list edge field (allowed: {sorted(link_fields)})"
                raise ValueError(msg)


class KnowledgeBaseSchema(BaseModel):
    """The complete per-knowledge-base ontology, loaded from `<knowledge_base>/knowledge-base-schema.json`."""

    model_config = ConfigDict(frozen=True)

    version: str = SCHEMA_FORMAT_VERSION
    slug: str = Field(pattern=_SLUG_PATTERN)
    purpose: str = ""
    slug_conventions: str = ""
    page_types: tuple[PageTypeSpec, ...]
    relations: tuple[RelationDecl, ...] = ()
    entity_types: tuple[VocabEntry, ...] = ()
    claim_types: tuple[VocabEntry, ...] = ()
    facets: tuple[FacetSpec, ...] = ()
    claims: ClaimGrammarSpec | None = None
    sources: SourcesSpec = SourcesSpec()
    entities: EntitiesSpec | None = None
    """The page type this knowledge base keeps its entities in, None when it keeps none."""
    compounding: CompoundingSpec = CompoundingSpec()
    retrieval: RetrievalSpec = RetrievalSpec()
    section_summaries: SectionSummarySpec = SectionSummarySpec()
    embed_strategies: EmbedStrategiesSpec = EmbedStrategiesSpec()
    # The filesystem base a `path_fields` value resolves against. "parent" (the default) is the
    # knowledge base root's parent, for a knowledge base inside a larger tree whose paths point outside
    # it. "knowledge_base" is the knowledge base root itself, for a standalone knowledge base whose
    # paths are relative to it.
    path_root: Literal["parent", "knowledge_base"] = "parent"
    run_indexing: Literal["each_write", "once_per_run"] = "each_write"
    """When this knowledge base's writes are indexed. `each_write` indexes after every write.

    `once_per_run` holds back the writes a workflow run makes and indexes them once when the run ends."""

    @property
    def disclosure_title(self) -> str:
        """The knowledge base slug, this spec's identity in a disclosed listing."""
        return self.slug

    @property
    def disclosure_summary(self) -> str:
        """One line a consumer reads before deciding to load the full ontology."""
        page_type_values = ", ".join(pt.page_type for pt in self.page_types)
        return (
            f"{len(self.page_types)} page types ({page_type_values}). {len(self.relations)} relations, "
            f"collection '{self.retrieval.collection}'. {self.purpose}"
        ).strip()

    @property
    def claims_declared(self) -> bool:
        """Whether this knowledge base records claims at all, which is what the claim-bearing api views depend on."""
        return self.claims is not None

    # Structural validators.
    @model_validator(mode="before")
    @classmethod
    def _default_collection_to_slug(cls, data: object) -> object:
        """Fill an unset `retrieval.collection` with this spec's own `slug`."""
        if not isinstance(data, dict):
            return data
        slug = data.get("slug")
        if not isinstance(slug, str) or not slug:
            return data
        retrieval = data.get("retrieval")
        if retrieval is None:
            data = {**data, "retrieval": RetrievalSpec(collection=slug)}
        elif isinstance(retrieval, dict) and not retrieval.get("collection"):
            data = {**data, "retrieval": {**retrieval, "collection": slug}}
        elif isinstance(retrieval, RetrievalSpec) and not retrieval.collection:
            data = {**data, "retrieval": retrieval.model_copy(update={"collection": slug})}
        return data

    @model_validator(mode="after")
    def _check_invariants(self) -> KnowledgeBaseSchema:
        """Refuse a spec that breaks any structural rule across its parts."""
        _check_page_type_uniqueness(self.page_types)
        _check_relation_slug_uniqueness(self.relations)
        _check_edge_fields(self.page_types)
        _check_path_field_types(self.page_types)
        _check_relation_domain_sections(self.page_types, self.relations)
        _check_claims_consistency(self.page_types, self.claims)
        _check_claim_relations(self.relations, self.claims)
        _check_embed_strategies_active(self.embed_strategies)
        _check_compounding_page_types(self.page_types, self.compounding)
        _check_digest_part_field(self.page_types, self.sources)
        _check_digest_fields(self.page_types, self.sources)
        _check_entities(self.page_types, self.entities)
        _check_decision_spec(self.page_types)
        _check_date_field(self.page_types, self.retrieval)
        return self

    # Derived lookups.

    def disclosure_excluded_page_types(self) -> frozenset[str]:
        """Return the page types whose units never appear as envelope children.

        Returns:
            The page type slugs declared with `disclosed` false.
        """
        return frozenset(pt.page_type for pt in self.page_types if not pt.disclosed)

    @cached_property
    def page_type_values(self) -> frozenset[str]:
        """All page_type values declared in this spec."""
        return frozenset(pt.page_type for pt in self.page_types)

    @cached_property
    def prefix_by_page_type(self) -> Mapping[str, str]:
        """Mapping from page_type to id_prefix."""
        return {pt.page_type: pt.id_prefix for pt in self.page_types}

    @cached_property
    def decision_page_types(self) -> tuple[PageTypeSpec, ...]:
        """The page types carrying a `decision` block, in declaration order."""
        return tuple(pt for pt in self.page_types if pt.decision is not None)

    @cached_property
    def note_folders(self) -> tuple[str, ...]:
        """Distinct knowledge-base-root-relative folders declared by the page types, for index discovery.

        The index reader globs these and no other folder. Order follows first declaration, merging each page
        type's `folder` and `folders`. A page type with neither contributes nothing.
        """
        seen: dict[str, None] = {}
        for pt in self.page_types:
            for raw in (pt.folder or "", *pt.folders):
                folder = raw.strip("/")
                if folder:
                    seen.setdefault(folder, None)
        return tuple(seen)

    @cached_property
    def note_files(self) -> tuple[str, ...]:
        """Distinct knowledge-base-root-relative file paths declared by the page types, for index discovery."""
        seen: dict[str, None] = {}
        for pt in self.page_types:
            for f in pt.files:
                if f:
                    seen.setdefault(f, None)
        return tuple(seen)

    def page_type(self, page_type: str) -> PageTypeSpec | None:
        """Return the PageTypeSpec for `page_type`, or None if unknown.

        Args:
            page_type: The page type string.

        Returns:
            The matching `PageTypeSpec`, or None.
        """
        for pt in self.page_types:
            if pt.page_type == page_type:
                return pt
        return None

    def sections_for(self, page_type: str) -> tuple[str, ...]:
        """Return ordered canonical section slugs for `page_type`, or an empty tuple for an unknown page type.

        Args:
            page_type: The page type string.

        Returns:
            Ordered tuple of canonical H2 slugs, or `()` if the page type is unknown.
        """
        pt = self.page_type(page_type)
        return pt.canonical_section_slugs if pt is not None else ()

    def canon_set_for(self, page_type: str) -> frozenset[str]:
        """Return canonical section slugs for `page_type` as an unordered frozenset.

        Args:
            page_type: The page type string.

        Returns:
            Frozenset of section slugs, or an empty frozenset for an unknown page type.
        """
        return frozenset(self.sections_for(page_type))

    def orientation(self) -> str:
        """Return the knowledge base's structure as one screen, for a reader deciding what to open next.

        It names the level grammar, every page type with its id prefix and canonical sections, and how
        claims are addressed. It is derived from this spec alone. Its opening sentence follows
        `retrieval.group_by`, so a grouped map says it lists groups and names the grouping key.

        Returns:
            The orientation text: the opening lines, one line per page type, then how a section is addressed.
        """
        structures: dict[Literal["app", "page_type"] | None, str] = {
            None: "Structure: this map lists pages. A page lists its sections. A section carries the text.",
            "page_type": (
                "Structure: this map lists page types. Open one to list its pages. A page lists its "
                "sections. A section carries the text."
            ),
            "app": (
                "Structure: this map lists the apps documented here. Open one to list its pages. A "
                "page lists its sections. A section carries the text."
            ),
        }
        lines = [structures[self.retrieval.group_by], "Page types:"]
        for pt in self.page_types:
            listed = ", ".join(pt.canonical_section_slugs) if pt.canonical_section_slugs else ""
            sections = f"sections [{listed}]" if listed else "free-form sections (open the page to list them)"
            notes = []
            if pt.claims_enabled and self.claims is not None:
                notes.append(f"its claims live under '{self.claims.section}' and are addressed `<page-id>#c1`")
            if not pt.disclosed:
                notes.append("never listed on maps, reached through search hits")
            suffix = f" ({'; '.join(notes)})" if notes else ""
            id_clause = f" (ids `{pt.id_prefix}*`)" if pt.id_prefix else ""
            lines.append(f"- {pt.page_type}{id_clause}: {sections}{suffix}")
        lines.append("A section is addressed `<page-id>#<section>`, so a page of a known page type opens straight at the section needed.")
        return "\n".join(lines)

    def edge_fields_for(self, page_type: str) -> frozenset[str]:
        """Return edge-field names declared for `page_type`.

        Args:
            page_type: The page type string.

        Returns:
            Frozenset of edge field names, or an empty frozenset for an unknown page type.
        """
        pt = self.page_type(page_type)
        return frozenset(pt.edge_fields) if pt is not None else frozenset()

    def link_fields_for(self, page_type: str) -> frozenset[str]:
        """Return all frontmatter field names of type `link` or `link-list` for `page_type`.

        Args:
            page_type: The page type string.

        Returns:
            Frozenset of field names with a link type, or an empty frozenset for an unknown page type.
        """
        pt = self.page_type(page_type)
        if pt is None:
            return frozenset()
        return frozenset(f.name for f in pt.frontmatter_fields if f.type in _LINK_TYPES)

    def required_fields_for(self, page_type: str) -> frozenset[str]:
        """Return names of required frontmatter fields for `page_type`.

        Args:
            page_type: The page type string.

        Returns:
            Frozenset of required field names, or an empty frozenset for an unknown page type.
        """
        pt = self.page_type(page_type)
        if pt is None:
            return frozenset()
        return frozenset(f.name for f in pt.frontmatter_fields if f.required)

    def field_spec(self, page_type: str, name: str) -> FrontmatterFieldSpec | None:
        """Return the `FrontmatterFieldSpec` for `(page_type, name)`, or None if not found.

        Args:
            page_type: The page type string.
            name: The field name.

        Returns:
            The `FrontmatterFieldSpec`, or None.
        """
        pt = self.page_type(page_type)
        if pt is None:
            return None
        for f in pt.frontmatter_fields:
            if f.name == name:
                return f
        return None

    def claims_enabled_for(self, page_type: str) -> bool:
        """Return whether claims are enabled for `page_type`.

        Args:
            page_type: The page type string.

        Returns:
            True if the page type has claims_enabled=True, False otherwise.
        """
        pt = self.page_type(page_type)
        return pt.claims_enabled if pt is not None else False

    def active_embed_strategies(self) -> tuple[str, ...]:
        """Return the embed-strategy names this knowledge base activates, in the order index runs them.

        An explicit `embed_strategies.active` overrides the default outright. Absent one, this
        derives the default set: `page-card` and `section` always (every knowledge base embeds
        a page card and its sections), `claim` when this spec declares a claim grammar, and
        `source-chunk` when `sources.enabled`.

        Returns:
            The active strategy names, in declaration order.
        """
        if self.embed_strategies.active is not None:
            return self.embed_strategies.active
        names = ["page-card", "section"]
        if self.claims is not None:
            names.append("claim")
        if self.sources.enabled:
            names.append("source-chunk")
        return tuple(names)

    # Loading helpers.

    @classmethod
    def from_json(cls, text: str) -> KnowledgeBaseSchema:
        """Parse a KnowledgeBaseSchema from a JSON string.

        Args:
            text: JSON text to parse.

        Returns:
            A validated KnowledgeBaseSchema.
        """
        return cls.model_validate(json.loads(text))

    @classmethod
    def from_path(cls, path: Path) -> KnowledgeBaseSchema:
        """Load a KnowledgeBaseSchema from a JSON file on disk, with the guidance pages beside it merged in.

        The file's directory is taken as the knowledge base root, so its `_meta/documentation-policy.md`
        and `_meta/compounding-policy.md` prose wins over the JSON's own `guidance` strings
        wherever a page names a page type, section or entry (`engram.kb.infrastructure.guidance_pages`). A directory
        with no such pages leaves the JSON as loaded.

        Args:
            path: Path to a JSON file.

        Returns:
            A validated KnowledgeBaseSchema.
        """
        from engram.kb.infrastructure.guidance_pages import apply_guidance  # noqa: PLC0415 (guidance_pages imports this module)

        return apply_guidance(cls.from_json(path.read_text(encoding="utf-8")), path.parent)


def _refuse_missing_spec(knowledge_base_path: Path | None = None) -> NoReturn:
    """Raise `KnowledgeBaseSchemaMissingError`, naming the templates to start the knowledge base from."""
    slugs = ", ".join(template.slug for template in list_templates()) or "none"
    where = f" at {knowledge_base_path}" if knowledge_base_path is not None else ""
    msg = f"no knowledge-base-schema.json{where}, run studio init --template <slug> to start one (known templates: {slugs})"
    raise KnowledgeBaseSchemaMissingError(msg)


def resolve_schema(spec: KnowledgeBaseSchema | None) -> KnowledgeBaseSchema:
    """Return `spec`, refusing when the caller has none to give.

    You must pass the knowledge base's schema. An absent one is refused, never guessed at.

    Args:
        spec: The knowledge base's schema.

    Returns:
        `spec`, unchanged.

    Raises:
        KnowledgeBaseSchemaMissingError: `spec` is None.
    """
    if spec is None:
        _refuse_missing_spec()
    return spec
