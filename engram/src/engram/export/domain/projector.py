"""Project resolved notes into an `OkfBundle`, a pure function with no IO.

Every knowledge-base-specific choice comes from the `KnowledgeBaseSchema` the caller passes: the page
types, their folders, their id prefixes, the facet keys and the source field names.

The mapping:

* Pages of the reserved `meta` page type never become concepts.
* A concept's folder is its page type's declared folder basename, lower-cased, or the page type name.
  Its file name is the page slug, the page id minus its page type's declared prefix. The concept id is
  `<dir>/<slug>` and the file path is `<dir>/<slug>.md`.
* The frontmatter holds a non-empty `type` (the page type verbatim), `title`, `description` (from
  `summary`, when present) and `tags` (from the free-form `tags` list).
* `status` folds the QA status into `draft`, `stable` or `deprecated`, and supersession wins. `generated`
  is `{by: <actor>, at: <updated>}`. `verified` holds one machine confirmation for a QA `verified` page.
  `sources` gives a page naming a source mirror under the schema's `sources.key_field` one entry, whose
  `resource` is the deep link that mirror carries.
* `okf_x_relations` maps each page-level typed relation to the exported concept ids it points at.
  `okf_x_engram` carries the page id, status, collection, non-empty facets, aliases, supersession, the
  source key and hash the schema names, and every other frontmatter key under `fields`.
* The body is the canonical sections in authored order. Outside fenced code, each Obsidian unit link or
  embed becomes a relative Markdown link when its target is exported, or else its alias or target as
  plain text. Inside a fence it is left as written. A trailing `## Relations` section lists the same
  exported edges as Markdown links.
* The root `index.md` lists every concept grouped by folder. Its frontmatter is `okf_version: "0.2"` alone.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import JsonValue, TypeAdapter
from pydantic_core import to_jsonable_python

from engram.export.domain.models import OkfBundle, OkfConcept

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from engram.indexing.contracts import ResolvedEdge, ResolvedNote
    from engram.kb.contracts import BaseFrontmatter, KnowledgeBaseNote, KnowledgeBaseSchema

__all__ = ["ProjectionResult", "project_bundle"]

# The QA statuses that fold to OKF's `draft`. Every other status folds to `stable`, and supersession overrides both.
_OKF_DRAFT_STATUSES: frozenset[str] = frozenset({"stub", "draft"})

# The reserved page type whose pages never become concepts.
_META_PAGE_TYPE = "meta"

_DEV_ACTOR = "engram/dev"
"""The `generated.by` actor used when the caller passes none."""

# The long-tail frontmatter key the overlay always emits under its own name.
_OVERLAY_COLLECTION_KEY = "collection"

_JSON_VALUE = TypeAdapter(JsonValue)
"""Validates one authored frontmatter value into the JSON shape a concept's frontmatter carries."""


@dataclass(frozen=True, slots=True)
class _Projection:
    """The inputs every concept's projection reads in one export."""

    collection: str
    schema: KnowledgeBaseSchema
    generated_by: str = _DEV_ACTOR
    source_links: Mapping[str, str] = field(default_factory=dict)

    @property
    def source_fields(self) -> tuple[str, str] | None:
        """The schema's `(key_field, hash_field)`, or `None` when it declares no source layer."""
        sources = self.schema.sources
        if not (sources.enabled and sources.key_field):
            return None
        return sources.key_field, sources.hash_field


# A fenced code block, opened and closed by a line of three or more backticks or tildes, captured whole.
_FENCE_RE = re.compile(r"(?P<fence>^[ \t]*(?:`{3,}|~{3,}).*?^[ \t]*(?:`{3,}|~{3,})[ \t]*$)", re.DOTALL | re.MULTILINE)

# One Obsidian unit link or embed: optional `!` (embed), `[[target]]` with optional
# `#fragment` and optional `|alias`. Groups: embed marker, target, fragment, alias.
_UNIT_LINK_RE = re.compile(r"(?P<embed>!?)\[\[(?P<target>[^\]|#]+?)(?:#(?P<frag>[^\]|]+?))?(?:\|(?P<alias>[^\]]+?))?\]\]")


class _ExportIndex:
    """Maps a lower-cased unit link target to the concept id of the exported page it names."""

    def __init__(self, by_key: Mapping[str, str], titles: Mapping[str, str]) -> None:
        self._by_key = dict(by_key)
        self._titles = dict(titles)

    def resolve(self, target: str) -> str | None:
        """The exported concept id for a unit link `target`, or `None` if not exported."""
        return self._by_key.get(target.strip().lower())

    def title_of(self, concept_id: str) -> str:
        """The exported page's title for a concept id (its slug as a fallback)."""
        return self._titles.get(concept_id) or concept_id.rsplit("/", 1)[-1]


class ProjectionResult:
    """The projected `OkfBundle` with the counts the report and the events carry."""

    def __init__(self, bundle: OkfBundle, *, skipped_meta: int, relations: int, dangling: int) -> None:
        """Hold one projection's output.

        Args:
            bundle: The projected concepts and root index.
            skipped_meta: How many notes were skipped as `meta` pages.
            relations: How many page-level typed edges landed in some concept's `okf_x_relations`.
            dangling: How many body unit links were flattened to plain text because their target was not exported.
        """
        self.bundle = bundle
        self.skipped_meta = skipped_meta
        self.relations = relations
        self.dangling = dangling


def project_bundle(
    notes: Iterable[ResolvedNote],
    *,
    collection: str,
    schema: KnowledgeBaseSchema,
    source_links: Mapping[str, str] | None = None,
    generated_by: str = _DEV_ACTOR,
) -> OkfBundle:
    """Project resolved notes into an `OkfBundle`.

    Args:
        notes: The resolved notes to project.
        collection: The export's collection, written into `okf_x_engram` for a page that declares none.
        schema: The knowledge base's schema. It decides which page types are exported, how ids are
            prefixed and which frontmatter fields name a page's source.
        source_links: Each source key with the deep link its mirror carries, the `resource` of a
            page's `sources` entry. `None` leaves every entry without one.
        generated_by: The actor for `generated.by` and `verified[].by`, in the form `<producer>/<version>`.

    Returns:
        The projected concepts and root index.
    """
    return project_with_counts(
        notes, collection=collection, schema=schema, source_links=source_links, generated_by=generated_by
    ).bundle


def project_with_counts(
    notes: Iterable[ResolvedNote],
    *,
    collection: str,
    schema: KnowledgeBaseSchema,
    source_links: Mapping[str, str] | None = None,
    generated_by: str = _DEV_ACTOR,
) -> ProjectionResult:
    """Project resolved notes into an `OkfBundle` and count what the projection skipped and linked.

    A link resolves to its target's concept id whatever order the notes arrive in.

    Args:
        notes: The resolved notes to project.
        collection: The export's collection, written into `okf_x_engram` for a page that declares none.
        schema: The knowledge base's schema. It decides which page types are exported, how ids are
            prefixed and which frontmatter fields name a page's source.
        source_links: Each source key with the deep link its mirror carries, the `resource` of a
            page's `sources` entry. `None` leaves every entry without one.
        generated_by: The actor for `generated.by` and `verified[].by`, in the form `<producer>/<version>`.

    Returns:
        The projected `OkfBundle` with its skipped, relation and dangling counts.
    """
    projection = _Projection(collection=collection, schema=schema, generated_by=generated_by, source_links=source_links or {})
    resolved_notes = tuple(notes)
    exportable = [resolved for resolved in resolved_notes if _is_exportable(resolved.note, schema)]
    skipped_meta = len(resolved_notes) - len(exportable)

    index = _build_export_index((resolved.note for resolved in exportable), schema=schema)

    concepts: list[OkfConcept] = []
    relations = 0
    dangling = 0
    for resolved in exportable:
        concept, edge_count, dangling_count = _project_concept(resolved, index, projection)
        concepts.append(concept)
        relations += edge_count
        dangling += dangling_count

    ordered = tuple(sorted(concepts, key=lambda concept: concept.path))
    bundle = OkfBundle(concepts=ordered, index=_build_index_concept(ordered))
    return ProjectionResult(bundle, skipped_meta=skipped_meta, relations=relations, dangling=dangling)


# -- index over exported pages -------------------------------------------------


def _is_exportable(note: KnowledgeBaseNote, schema: KnowledgeBaseSchema) -> bool:
    """Whether a note's page type is declared in the schema and is not `meta`."""
    return note.frontmatter.type in schema.page_type_values and note.frontmatter.type != _META_PAGE_TYPE


def _subdir_for_page_type(page_type: str, schema: KnowledgeBaseSchema) -> str:
    """The export folder for `page_type`: its declared folder's basename lower-cased, or else its name."""
    page_type_schema = schema.page_type(page_type)
    if page_type_schema is not None and page_type_schema.folder is not None:
        return page_type_schema.folder.rsplit("/", 1)[-1].lower()
    return page_type


def _concept_id(note: KnowledgeBaseNote, schema: KnowledgeBaseSchema) -> str:
    """The OKF concept id `<dir>/<slug>` for an exportable note."""
    page_type = note.frontmatter.type
    return f"{_subdir_for_page_type(page_type, schema)}/{_slug(note.frontmatter, schema)}"


def _slug(frontmatter: BaseFrontmatter, schema: KnowledgeBaseSchema) -> str:
    """The page id with its page type's declared prefix removed, if it declares one."""
    prefix = schema.prefix_by_page_type.get(frontmatter.type, "")
    return frontmatter.id.removeprefix(prefix)


def _build_export_index(notes: Iterable[KnowledgeBaseNote], *, schema: KnowledgeBaseSchema) -> _ExportIndex:
    """Map every exported page's file stem, id, title and aliases, lower-cased, to its concept id."""
    by_key: dict[str, str] = {}
    titles: dict[str, str] = {}
    for note in notes:
        concept_id = _concept_id(note, schema)
        titles[concept_id] = note.frontmatter.title
        keys = [_filename_stem(note.source_path), note.frontmatter.id, note.frontmatter.title, *note.frontmatter.aliases]
        for key in keys:
            normalized = key.strip().lower()
            if normalized:
                by_key[normalized] = concept_id
    return _ExportIndex(by_key, titles)


def _filename_stem(source_path: str) -> str:
    """The note's basename without directory or `.md` (its Obsidian link name)."""
    return source_path.rsplit("/", 1)[-1].removesuffix(".md")


# -- per-concept projection ----------------------------------------------------


def _project_concept(resolved: ResolvedNote, index: _ExportIndex, projection: _Projection) -> tuple[OkfConcept, int, int]:
    """Project one resolved note into an `OkfConcept` with its relation and dangling counts."""
    note = resolved.note
    relations = _exported_relations(resolved.edges, index)
    body, dangling = _project_body(note, relations, index)
    concept = OkfConcept(
        path=f"{_concept_id(note, projection.schema)}.md", frontmatter=_frontmatter(note, relations, projection), body=body
    )
    relation_count = sum(len(targets) for targets in relations.values())
    return concept, relation_count, dangling


def _frontmatter(note: KnowledgeBaseNote, relations: Mapping[str, list[str]], projection: _Projection) -> dict[str, JsonValue]:
    """Build the OKF frontmatter: the recommended fields, then status, generated, verified and sources, then the overlay."""
    front = note.frontmatter
    carried: set[str] = set()
    okf: dict[str, JsonValue] = {"type": front.type}
    okf["title"] = front.title
    if front.summary is not None:
        okf["description"] = front.summary
    tags = _extra_str_list(front, "tags")
    if tags:
        okf["tags"] = tags
        carried.add("tags")
    okf["status"] = _okf_status(front)
    generated = _generated(front, projection.generated_by)
    okf["generated"] = generated
    if "at" in generated:
        carried.add("updated")
    verified = _verified(front, projection.generated_by)
    if verified:
        okf["verified"] = verified
    sources = _sources(front, projection)
    if sources:
        okf["sources"] = sources
    if relations:
        okf["okf_x_relations"] = {rel: list(targets) for rel, targets in relations.items()}
    okf["okf_x_engram"] = _engram_overlay(front, projection, carried=frozenset(carried))
    return okf


def _okf_status(front: BaseFrontmatter) -> str:
    """The OKF `status`: `deprecated` when superseded, `draft` for a stub or draft, else `stable`."""
    if front.superseded_by is not None:
        return "deprecated"
    return "draft" if front.status in _OKF_DRAFT_STATUSES else "stable"


def _generated(front: BaseFrontmatter, generated_by: str) -> dict[str, JsonValue]:
    """The OKF `generated` entry: the actor, and the page's `updated` date when it has one."""
    generated: dict[str, JsonValue] = {"by": generated_by}
    updated = _extra_str(front, "updated")
    if updated is not None:
        generated["at"] = updated
    return generated


def _verified(front: BaseFrontmatter, generated_by: str) -> list[JsonValue]:
    """One OKF `verified` event for a QA `verified` page with an `updated` date, else none."""
    if front.status != "verified":
        return []
    updated = _extra_str(front, "updated")
    if updated is None:
        return []
    return [{"by": generated_by, "at": updated}]


def _sources(front: BaseFrontmatter, projection: _Projection) -> list[JsonValue]:
    """The one OKF `sources` entry for a page naming a source mirror, with the mirror's deep link when known."""
    source_fields = projection.source_fields
    if source_fields is None:
        return []
    key_field, _ = source_fields
    source_id = _extra_str(front, key_field)
    if source_id is None:
        return []
    entry: dict[str, JsonValue] = {"id": source_id, "title": front.title}
    link = projection.source_links.get(source_id, "").strip()
    if link:
        entry["resource"] = link
    return [entry]


def _engram_overlay(front: BaseFrontmatter, projection: _Projection, *, carried: frozenset[str]) -> dict[str, JsonValue]:
    """The lossless `okf_x_engram` overlay, which keeps every authored key the rest of the concept did not."""
    engram: dict[str, JsonValue] = {
        "id": front.id,
        "status": front.status,
        _OVERLAY_COLLECTION_KEY: _collection_of(front, projection.collection),
    }
    emitted = set(carried) | {_OVERLAY_COLLECTION_KEY}
    for facet in projection.schema.facets:
        values = front.facet_values(facet.key)
        if values:
            engram[facet.key] = list(values)
            emitted.add(facet.key)
    if front.aliases:
        engram["aliases"] = list(front.aliases)
    if front.superseded_by is not None:
        engram["superseded_by"] = front.superseded_by.target
    source_fields = projection.source_fields
    if source_fields is not None:
        key_field, hash_field = source_fields
        source_id = _extra_str(front, key_field)
        if source_id is not None:
            engram["source_id"] = source_id
            emitted.add(key_field)
        source_hash = _extra_str(front, hash_field) if hash_field else None
        if source_hash is not None:
            engram["source_hash"] = source_hash
            emitted.add(hash_field)
    fields = _long_tail(front, emitted)
    if fields:
        engram["fields"] = fields
    return engram


def _long_tail(front: BaseFrontmatter, emitted: set[str]) -> dict[str, JsonValue]:
    """Every extra frontmatter key not in `emitted`, with its value coerced to JSON."""
    extras = front.model_extra or {}
    return {key: _as_json(value) for key, value in extras.items() if key not in emitted}


def _as_json(value: object) -> JsonValue:
    """Coerce one authored frontmatter value to JSON, falling back to its string form."""
    return _JSON_VALUE.validate_python(to_jsonable_python(value, fallback=str))


def _collection_of(front: BaseFrontmatter, default: str) -> str:
    """The page's declared `collection` frontmatter, or `default` (the export's collection)."""
    return _extra_str(front, "collection") or default


def _extra_str(front: BaseFrontmatter, key: str) -> str | None:
    """An extra frontmatter scalar as stripped text, or `None` when absent or blank."""
    value = getattr(front, key, None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _extra_str_list(front: BaseFrontmatter, key: str) -> list[JsonValue]:
    """An extra frontmatter list, or a scalar wrapped as one, as stripped non-blank strings."""
    value = getattr(front, key, None)
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    return [str(item).strip() for item in items if str(item).strip()]


def _exported_relations(edges: Sequence[ResolvedEdge], index: _ExportIndex) -> dict[str, list[str]]:
    """Group typed edges by relation as exported concept ids, in authored order without repeats."""
    relations: dict[str, list[str]] = {}
    for edge in edges:
        concept_id = index.resolve(edge.dst_target)
        if concept_id is None:
            continue
        targets = relations.setdefault(edge.rel, [])
        if concept_id not in targets:
            targets.append(concept_id)
    return relations


# -- body rendering and unit link rewriting outside fences ----------------------


def _project_body(note: KnowledgeBaseNote, relations: Mapping[str, list[str]], index: _ExportIndex) -> tuple[str, int]:
    """Render the canonical sections and the `## Relations` section, with the count of flattened links."""
    rendered_sections: list[str] = []
    dangling = 0
    for section in sorted(note.sections, key=lambda s: s.ord):
        rewritten, miss = _rewrite_links(section.body, index)
        dangling += miss
        rendered_sections.append(f"## {section.heading}\n\n{rewritten}".rstrip())

    body = "\n\n".join(rendered_sections)
    relations_section = _relations_section(relations, index)
    if relations_section:
        body = f"{body}\n\n{relations_section}" if body else relations_section
    return body, dangling


def _rewrite_links(text: str, index: _ExportIndex) -> tuple[str, int]:
    """Rewrite the unit links in `text` outside fenced code, with the count of flattened links."""
    parts: list[str] = []
    dangling = 0
    last = 0
    for fence in _FENCE_RE.finditer(text):
        outside_rewritten, miss = _rewrite_outside_fence(text[last : fence.start()], index)
        dangling += miss
        parts.append(outside_rewritten)
        parts.append(fence.group("fence"))  # the fenced block, verbatim
        last = fence.end()
    tail_rewritten, miss = _rewrite_outside_fence(text[last:], index)
    dangling += miss
    parts.append(tail_rewritten)
    return "".join(parts), dangling


def _rewrite_outside_fence(text: str, index: _ExportIndex) -> tuple[str, int]:
    """Rewrite every unit link in a span with no fence, with the count of flattened links."""
    dangling = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal dangling
        target = match.group("target").strip()
        alias = match.group("alias")
        display = alias.strip() if alias else target
        concept_id = index.resolve(target)
        if concept_id is None:
            dangling += 1
            return display  # not exported, so plain text
        return f"[{display}](/{concept_id}.md)"

    return _UNIT_LINK_RE.sub(replace, text), dangling


def _relations_section(relations: Mapping[str, list[str]], index: _ExportIndex) -> str:
    """The `## Relations` section, one bullet per exported edge, or empty when there is none."""
    if not relations:
        return ""
    bullets = [
        f"- **{rel}** → [{index.title_of(concept_id)}](/{concept_id}.md)" for rel, targets in relations.items() for concept_id in targets
    ]
    return "\n".join(["## Relations", "", *bullets])


# -- root index.md -------------------------------------------------------------


def _build_index_concept(concepts: Sequence[OkfConcept]) -> OkfConcept:
    """Build the root `index.md`, whose frontmatter is `okf_version: "0.2"` alone."""
    frontmatter: dict[str, JsonValue] = {"okf_version": "0.2"}
    body = _index_body(concepts)
    return OkfConcept(path="index.md", frontmatter=frontmatter, body=body)


def _index_body(concepts: Sequence[OkfConcept]) -> str:
    """The listing of every concept under a `### <subdir>/` heading per folder."""
    lines = ["# Knowledge bundle", "", "An Open Knowledge Format v0.2 bundle exported from the engram memory.", ""]
    for subdir in sorted({concept.path.split("/", 1)[0] for concept in concepts}):
        lines.append(f"### {subdir}/")
        lines.append("")
        for concept in concepts:
            if concept.path.split("/", 1)[0] != subdir:
                continue
            concept_id = concept.path.removesuffix(".md")
            description = concept.frontmatter.get("description")
            suffix = f" — {description}" if isinstance(description, str) and description else ""
            lines.append(f"- [{concept_id}](/{concept.path}){suffix}")
        lines.append("")
    return "\n".join(lines).rstrip()
