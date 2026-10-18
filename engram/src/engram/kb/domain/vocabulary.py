"""Index `_meta/vocabulary/*.md` into a `VocabularyIndex`.

The knowledge base's controlled vocabulary lives as three governed markdown files
(`_meta/vocabulary/{entity-types,relation-types,facets}.md`). The index validate
stage checks every note's facet values, relation keys, and entity types against it.

The three files use three different markdown shapes:

* `entity-types.md`: a single `| entity_type | Definition | Examples |` pipe
  table of backtick-wrapped slugs, then a `## Boundary rules` prose section
  (ignored). Produces `entity_types` (value to definition).
* `relation-types.md`: several `| Relation | Domain → Range | Meaning |` tables
  grouped under H2s. The arrow in column 2 carries domain and range (`↔` marks a
  symmetric relation). The Lifecycle table is `| Relation | Meaning |` with no
  arrow column (domain/range left blank). The `## Claim qualifier fields` table
  holds scoping fields, not relations, and is excluded. Produces `relations`
  (key to `RelationSpec`).
* `facets.md`: each facet is a ``## `name`, on …`` section whose values come
  in one of three shapes: a ``Seed: `a`, `b`, ...`` prose paragraph, a closed
  `| Value | Definition |` table, or a dot-separated inline-code list.
  The `kind` facet's values are the claim kinds and route to `claim_types`.
  Everything else routes to `facets` (name to value to definition, prose and
  dot-list values carry an empty definition).

Fault tolerance: a malformed row is never fatal. It is skipped and recorded as a
`VocabularyWarning` on the returned `VocabularyParseResult`. Files the caller does
not supply simply leave their section of the index empty.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.markdown import split_frontmatter

from engram.kb.domain.note import RelationSpec, VocabularyIndex

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence

    from engram.kb.domain.schema import KnowledgeBaseSchema

# ``## `name`, on note, topic`` matches the bare facet name, tolerates an em-dash
# or hyphen scope suffix and surrounding backticks/quotes.
_FACET_HEADING = re.compile(r"^#{2,3}\s+`(?P<name>[^`]+)`")
# A backtick-wrapped lowercase-hyphen slug, e.g. `technique` or `multi-hop-qa`.
_BACKTICK_SLUG = re.compile(r"`(?P<slug>[^`]+)`")
# The `Domain → Range` cell, e.g. `technique → problem` or `entity ↔ entity`. The arrow
# separates the two endpoints, and `↔` also marks a symmetric relation.
_DOMAIN_RANGE = re.compile(r"^(?P<domain>.+?)\s*(?P<arrow>→|↔|->|<->)\s*(?P<range>.+)$")

# H2 section titles in relation-types.md that are not relation tables.
_NON_RELATION_SECTIONS = frozenset({"claim qualifier fields", "adding a relation"})
# The facet whose controlled values are claim kinds, not a frontmatter facet.
_CLAIM_KIND_FACET = "kind"

# A usable vocabulary row needs at least a slug cell and a definition cell.
_MIN_DEFINED_CELLS = 2
# A relation row carrying `Domain → Range` needs a slug cell, an arrow cell and a meaning cell.
_MIN_RELATION_CELLS = 3
# Placeholder substituted for `\|` before splitting on unescaped `|`, then
# restored to a literal `|` in the final cell value.
_ESCAPED_PIPE_SENTINEL = "\x00"


@dataclass(frozen=True, slots=True)
class VocabularyWarning:
    """A row or section the parser could not interpret: skipped and surfaced, not raised."""

    source: str  # the vocabulary file name, e.g. "entity-types.md"
    message: str


@dataclass(frozen=True, slots=True)
class VocabularyParseResult:
    """The indexed `VocabularyIndex` plus any malformed-row warnings."""

    index: VocabularyIndex
    warnings: tuple[VocabularyWarning, ...] = ()


@dataclass
class _Accumulator:
    """Mutable scratch the per-file parsers fill, frozen into a result at the end."""

    entity_types: dict[str, str] = field(default_factory=dict)
    relations: dict[str, RelationSpec] = field(default_factory=dict)
    facets: dict[str, dict[str, str]] = field(default_factory=dict)
    claim_types: dict[str, str] = field(default_factory=dict)
    warnings: list[VocabularyWarning] = field(default_factory=list)


def parse_vocabulary(documents: Iterable[tuple[str, str]]) -> VocabularyParseResult:
    """Index `(file_name, markdown_text)` vocabulary documents into an index.

    Documents are routed by file-name stem (`entity-types`, `relation-types`,
    `facets`). An unrecognized name is recorded as a warning and skipped.

    Args:
        documents: The `(file_name, markdown_text)` pairs to index.

    Returns:
        The index, with a warning for every row or file that could not be read.
    """
    acc = _Accumulator()
    for name, text in documents:
        _, body = split_frontmatter(text)
        stem = name.removesuffix(".md")
        if stem == "entity-types":
            _parse_entity_types(name, body, acc)
        elif stem == "relation-types":
            _parse_relation_types(name, body, acc)
        elif stem == "facets":
            _parse_facets(name, body, acc)
        else:
            acc.warnings.append(VocabularyWarning(source=name, message=f"unrecognized vocabulary file: {name!r}"))

    index = VocabularyIndex(entity_types=acc.entity_types, relations=acc.relations, facets=acc.facets, claim_types=acc.claim_types)
    return VocabularyParseResult(index=index, warnings=tuple(acc.warnings))


def _iter_sections(body: str) -> Iterator[tuple[str, str]]:
    """Yield `(heading_text, section_body)` per H2 block, the content before any H2 first under `""`."""
    heading = ""
    lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            yield heading, "\n".join(lines)
            heading = line[3:].strip()
            lines = []
        else:
            lines.append(line)
    yield heading, "\n".join(lines)


def _iter_table_rows(section: str) -> Iterator[list[str]]:
    """Yield the data rows of a markdown pipe table as lists of trimmed cells, skipping header and prose."""
    seen_separator = False
    for raw in section.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        protected = line.replace("\\|", _ESCAPED_PIPE_SENTINEL).strip("|")
        cells = [c.replace(_ESCAPED_PIPE_SENTINEL, "|").strip() for c in protected.split("|")]
        if all(set(c) <= {"-", ":"} and c for c in cells):
            seen_separator = True
            continue
        if not seen_separator:
            # The header row precedes the separator, skip it before we have seen one.
            continue
        yield cells


def _first_slug(cell: str) -> str | None:
    """Return the first backtick-wrapped slug in a cell, or None if there is none."""
    match = _BACKTICK_SLUG.search(cell)
    return match.group("slug").strip() if match else None


def _parse_entity_types(name: str, body: str, acc: _Accumulator) -> None:
    """Parse `| entity_type | Definition | ... |` rows into `entity_types`, skip prose sections."""
    for heading, section in _iter_sections(body):
        if heading.lower().startswith("boundary"):
            continue  # "## Boundary rules" is prose, not vocabulary
        for cells in _iter_table_rows(section):
            slug = _first_slug(cells[0]) if cells else None
            if slug is None or len(cells) < _MIN_DEFINED_CELLS:
                acc.warnings.append(VocabularyWarning(source=name, message=f"unparseable entity-type row: {cells!r}"))
                continue
            acc.entity_types[slug] = cells[1]


def _parse_relation_types(name: str, body: str, acc: _Accumulator) -> None:
    """Parse relation tables into `relations` (with domain/range/symmetry), skip qualifier table."""
    for heading, section in _iter_sections(body):
        if heading.lower() in _NON_RELATION_SECTIONS:
            continue  # qualifier fields and prose sections are not relations
        for cells in _iter_table_rows(section):
            spec = _relation_from_row(name, cells, acc)
            if spec is not None:
                slug, relation = spec
                acc.relations[slug] = relation


def _relation_from_row(name: str, cells: Sequence[str], acc: _Accumulator) -> tuple[str, RelationSpec] | None:
    """Build a `(slug, RelationSpec)` from a three- or two-column relation row, or warn and skip."""
    slug = _first_slug(cells[0]) if cells else None
    if slug is None:
        acc.warnings.append(VocabularyWarning(source=name, message=f"unparseable relation row: {list(cells)!r}"))
        return None

    domain = ""
    range_ = ""
    symmetric = False
    description = cells[-1] if len(cells) >= _MIN_DEFINED_CELLS else ""
    if len(cells) >= _MIN_RELATION_CELLS:
        match = _DOMAIN_RANGE.match(cells[1])
        if match is not None:
            domain = match.group("domain").strip()
            range_ = match.group("range").strip()
            symmetric = match.group("arrow") in {"↔", "<->"}
            description = cells[2]
    return slug, RelationSpec(domain=domain, range=range_, description=description, symmetric=symmetric)


def _parse_facets(name: str, body: str, acc: _Accumulator) -> None:
    """Parse facet sections into `facets` (or `claim_types` for the `kind` facet)."""
    for heading, section in _iter_sections(body):
        match = _FACET_HEADING.match("## " + heading)
        if match is None:
            continue  # an H2 without a backtick facet name is prose, not a facet
        facet_name = match.group("name").strip()
        values = _facet_values(section)
        if not values:
            acc.warnings.append(VocabularyWarning(source=name, message=f"facet {facet_name!r} has no parseable values"))
            continue
        if facet_name == _CLAIM_KIND_FACET:
            acc.claim_types.update(values)
        else:
            acc.facets[facet_name] = values


def _facet_values(section: str) -> dict[str, str]:
    """Extract a facet's value-to-definition map from a table, or its slugs with empty definitions from prose."""
    values: dict[str, str] = {}
    for cells in _iter_table_rows(section):
        slug = _first_slug(cells[0]) if cells else None
        if slug is not None:
            values[slug] = cells[1] if len(cells) >= _MIN_DEFINED_CELLS else ""
    if values:
        return values
    # No table found: harvest every backtick slug from the prose/dot-list body.
    for match in _BACKTICK_SLUG.finditer(section):
        values.setdefault(match.group("slug").strip(), "")
    return values


def schema_vocabulary_drift(vocabulary: VocabularyIndex, spec: KnowledgeBaseSchema) -> tuple[str, ...]:
    """Report vocabulary the `spec` seed declares that the parsed `vocabulary` lacks.

    The comparison follows how `engram.init.contracts.scaffold` seeds the vocabulary from the spec:

    * `spec.entity_types[*].value` -> `vocabulary.entity_types` keys
    * `spec.relations[*].slug` -> `vocabulary.relations` keys
    * each `spec.facets[*].key` -> a `vocabulary.facets` section, and that facet's
      `seed` values -> the keys of that section
    * `spec.claim_types[*].value` -> `vocabulary.claim_types` keys

    Only vocabulary the spec declares and the parsed vocabulary lacks is reported. A vocabulary that extends
    the seed is never reported.

    Args:
        vocabulary: The indexed `VocabularyIndex` parsed from the vocabulary files.
        spec: The knowledge base's schema, whose seed the vocabulary is compared against.

    Returns:
        A tuple of human-readable drift descriptions, each naming both the `knowledge-base-schema.json` field
        path and the `_meta/vocabulary/*.md` file it should be reflected in (empty when the vocabulary
        covers every vocabulary item the spec seeds), one per missing item, in a stable order
        (category, then declaration order within the spec).
    """
    drift: list[str] = []
    drift.extend(
        _missing_vocab(
            vocabulary.entity_types,
            (e.value for e in spec.entity_types),
            "entity_type",
            "entity_types[value={value}].value",
            "_meta/vocabulary/entity-types.md",
        )
    )
    drift.extend(
        _missing_vocab(
            vocabulary.relations,
            (r.slug for r in spec.relations),
            "relation",
            "relations[slug={value}].slug",
            "_meta/vocabulary/relation-types.md",
        )
    )
    drift.extend(
        _missing_vocab(
            vocabulary.claim_types,
            (c.value for c in spec.claim_types),
            "claim kind",
            "claim_types[value={value}].value",
            "_meta/vocabulary/facets.md (facet 'kind')",
        )
    )

    for facet in spec.facets:
        registered = vocabulary.facets.get(facet.key)
        if registered is None:
            if facet.seed:
                drift.append(
                    f"facet {facet.key!r} (with {len(facet.seed)} seed value(s)) is declared by knowledge-base-schema.json "
                    f"facets[key={facet.key}] but absent from _meta/vocabulary/facets.md"
                )
            continue
        drift.extend(
            f"facet {facet.key!r} value {value!r} is seeded by knowledge-base-schema.json facets[key={facet.key}].seed but "
            f"missing from _meta/vocabulary/facets.md"
            for value in facet.seed
            if value not in registered
        )
    return tuple(drift)


def facet_choices_drift(spec: KnowledgeBaseSchema) -> tuple[str, ...]:
    """Report a facet's `seed` and a same-keyed frontmatter field's `choices` disagreeing.

    A field is compared when a `FacetSpec.key` also names a `FrontmatterFieldSpec` (on any page type)
    that declares non-empty `choices`. Both directions are reported: a seed value the choices lack, and
    a choice the seed lacks. A field with empty `choices`, or a facet key with no same-keyed field, is
    never reported.

    Args:
        spec: The knowledge base's schema, whose facets and frontmatter fields are compared.

    Returns:
        A tuple of human-readable mismatch descriptions naming both `knowledge-base-schema.json` locations
        (empty when every facet-mirroring field agrees with its facet's seed), in page-type
        declaration order, then field declaration order.
    """
    facets_by_key = {facet.key: facet for facet in spec.facets}
    drift: list[str] = []
    for pt in spec.page_types:
        for fs in pt.frontmatter_fields:
            facet = facets_by_key.get(fs.name)
            if facet is None or not fs.choices:
                continue
            drift.extend(_facet_choices_mismatch(facet.key, facet.seed, pt.page_type, fs.name, fs.choices))
    return tuple(drift)


def _facet_choices_mismatch(facet_key: str, seed: Sequence[str], page_type: str, field_name: str, choices: Sequence[str]) -> Iterator[str]:
    """Yield one mismatch line per seed value missing from `choices`, and vice versa."""
    seed_path = f"facets[key={facet_key}].seed"
    choices_path = f"page_types[page_type={page_type}].frontmatter_fields[name={field_name}].choices"
    choice_set = frozenset(choices)
    seed_set = frozenset(seed)
    for value in seed:
        if value not in choice_set:
            yield f"facet {facet_key!r} value {value!r} is seeded by knowledge-base-schema.json {seed_path} but missing from {choices_path}"
    for value in choices:
        if value not in seed_set:
            yield f"facet {facet_key!r} value {value!r} is a choice in knowledge-base-schema.json {choices_path} but missing from {seed_path}"


def _missing_vocab(
    registered: Mapping[str, object], declared: Iterable[str], label: str, spec_path: str, vocabulary_file: str
) -> Iterator[str]:
    """Yield a drift line for each declared vocab value absent from the registered keys, naming both files."""
    for value in declared:
        if value not in registered:
            yield f"{label} {value!r} is declared by knowledge-base-schema.json {spec_path.format(value=value)} but missing from {vocabulary_file}"
