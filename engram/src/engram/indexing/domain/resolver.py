"""The Resolve stage, a pure service turning unit links into ids and `loc` into ltree.

Two jobs, no IO:

1. **Link resolution.** Build a `LinkIndex` over every parsed note: its filename
   stem, every alias, its title and its frontmatter `id` all map to that `id`, the way
   Obsidian resolves unit links. Matching is **case-insensitive**, so the index folds
   every key to lower case and resolves a link by its lower-cased target. Then resolve
   the unit links in each note's frontmatter relations and claim fields (`subject`,
   `edges`, `disputed_by` and every declared link field) to ids. For a relation
   declaring `RelationDecl.domain_section`, also resolve every unit link in that
   section's body (and any subsection nested under it) of a note whose page type is the
   relation's domain, one edge per distinct target, in document order. An **unresolved**
   target is a `dangling-link` warning, except a dangling target of a relation the schema
   declares `queue_when_missing`, which goes to the **ingest queue** as a wanted page not
   yet ingested.

2. **Locator paths.** Index a claim's `loc::` `§`-path to an `ltree` label so
   it can be matched against `raw_chunks.section_path`: `§4.1` becomes `s4.s1` and
   `§4.1.3` becomes `s4.s1.s3` (each numeric segment gets an `s` prefix, since bare
   integers are not valid ltree labels). `Table N` / `Fig N` / `p.N` parts are
   locator **qualifiers**, never path segments (`'§5.1, Table 2'` becomes `s5.s1`).
   The raw `loc` text is kept alongside the derived path.

Vocabulary, quote-presence and qualifier rules belong to the validator. This module
decides only the split between dangling-link warnings and the ingest queue.
"""

from __future__ import annotations

import re

from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

from engram.indexing.domain.models import ResolvedClaim, ResolvedEdge, ResolvedLink, ResolvedNote, ValidationIssue
from engram.kb.contracts import UnitLink, unit_links_in

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from engram.kb.contracts import BaseFrontmatter, ClaimBlock, KnowledgeBaseNote, RelationDecl


class PageKeyRow(Protocol):
    """The read-only slice of a `kb.pages` row needed to build a `LinkIndex` key set.

    `build_link_index_from_pages` reads only these four resolution keys, the same
    `{filename_stem, id, title, aliases}` set `build_link_index` reads off a note
    (`slug` stands in for the note's filename stem). A row whose `aliases` is a
    `list[str]` or a `tuple[str, ...]` satisfies it.
    """

    @property
    def id(self) -> str:
        """The page id this row resolves its keys to."""
        ...

    @property
    def slug(self) -> str:
        """The filename-stem analog (clean-DB counterpart of a note's basename)."""
        ...

    @property
    def title(self) -> str:
        """The page title (a resolution key)."""
        ...

    @property
    def aliases(self) -> Sequence[str]:
        """The page's aliases (each an additional resolution key)."""
        ...


# A `§`-locator's leading section path: the run of dot-separated integers right after
# the `§` sign. `Table`/`Fig`/`p.` qualifiers that follow are intentionally
# outside the capture, so they never contribute path segments.
_SECTION_LOC_RE = re.compile(r"§\s*(?P<path>\d+(?:\.\d+)*)")


class _DanglingLink(BaseModel):
    """A unit link whose target did not resolve, with where it was written (for messages)."""

    model_config = ConfigDict(frozen=True)

    page_id: str
    target: str
    relation: str
    anchor: str | None = None


class ResolveResult(BaseModel):
    """The Resolve stage's output: resolved notes + dangling-link warnings + ingest queue."""

    model_config = ConfigDict(frozen=True)

    notes: tuple[ResolvedNote, ...]
    warnings: tuple[ValidationIssue, ...] = ()
    ingest_queue: tuple[str, ...] = ()


class LinkIndex(BaseModel):
    """A lower-cased `unit link target → page id` map (filename stem + aliases + title + id).

    Built by `build_link_index` or `build_link_index_from_pages`. `resolve` folds a
    link's target to lower case before lookup.
    """

    model_config = ConfigDict(frozen=True)

    by_key: dict[str, str] = {}

    def resolve(self, link: UnitLink) -> str | None:
        """Look up the page id for `link`'s target.

        Args:
            link: The unit link to resolve.

        Returns:
            The page id, or `None` if the target does not resolve.
        """
        return self.by_key.get(link.target.strip().lower())

    def merge(self, other: LinkIndex) -> LinkIndex:
        """Overlay `other` on this index, `other`'s keys winning on conflict.

        Neither operand is changed.

        Args:
            other: The index whose keys win where both carry the same key.

        Returns:
            A new index holding the keys of both.
        """
        return LinkIndex(by_key={**self.by_key, **other.by_key})


def _filename_stem(source_path: str) -> str:
    """The note's basename without directory or `.md` extension (its Obsidian link name)."""
    return source_path.rsplit("/", 1)[-1].removesuffix(".md")


def _add_page_keys(by_key: dict[str, str], page_id: str, keys: Iterable[str]) -> None:
    """Fold `keys` (lower-cased, empties skipped) into `by_key`, each mapping to `page_id`, later keys winning."""
    for key in keys:
        normalized = key.strip().lower()
        if normalized:
            by_key[normalized] = page_id


def build_link_index(notes: Iterable[KnowledgeBaseNote]) -> LinkIndex:
    """Build the lower-cased link index from every note's stem, aliases, title, and id.

    Later keys overwrite earlier ones on a collision.

    Args:
        notes: The parsed notes.

    Returns:
        The link index over every note's keys.
    """
    by_key: dict[str, str] = {}
    for note in notes:
        page_id = note.frontmatter.id
        _add_page_keys(by_key, page_id, [_filename_stem(note.source_path), page_id, note.frontmatter.title, *note.frontmatter.aliases])
    return LinkIndex(by_key=by_key)


def build_link_index_from_pages(rows: Iterable[PageKeyRow]) -> LinkIndex:
    """Build the link index from clean `kb.pages` key rows.

    Each row's `slug`, `id`, `title`, and `aliases` map to its `id` by the same rule
    `build_link_index` uses for notes, so an equivalent key set yields a byte-identical index.

    Args:
        rows: The stored page rows.

    Returns:
        The link index over every row's keys.
    """
    by_key: dict[str, str] = {}
    for row in rows:
        _add_page_keys(by_key, row.id, [row.slug, row.id, row.title, *row.aliases])
    return LinkIndex(by_key=by_key)


def loc_to_ltree(loc: str) -> str | None:
    """Index a `loc::` locator's `§`-section path to an ltree label.

    `§4.1, Table 2` becomes `s4.s1`, `§5` becomes `s5`, and `Table 2` (no `§`) becomes `None`.
    Each numeric segment is prefixed with `s`.

    Args:
        loc: The raw locator text.

    Returns:
        The ltree label, or `None` when the locator names no `§` section.
    """
    match = _SECTION_LOC_RE.search(loc)
    if match is None:
        return None
    return ".".join(f"s{segment}" for segment in match.group("path").split("."))


def section_loc(section_path: str | None) -> str | None:
    """The `loc::` form of a `raw_chunks.section_path` label, the inverse of `loc_to_ltree`.

    `s4.s1` becomes `§4.1` and `s5` becomes `§5`.

    Args:
        section_path: The ltree label, or `None` for a chunk under no numbered heading.

    Returns:
        The locator, or `None` when `section_path` is empty or `None`.
    """
    if not section_path:
        return None
    return "§" + ".".join(label.removeprefix("s") for label in section_path.split("."))


def _frontmatter_relations(frontmatter: BaseFrontmatter) -> list[tuple[str, UnitLink]]:
    """Every `(relation, target)` pair from a note's `UnitLink` frontmatter fields, declared fields first, then extras."""
    pairs: list[tuple[str, UnitLink]] = []

    # 1. Declared model fields (covers all research subclass edge fields)
    for name in type(frontmatter).model_fields:
        value = getattr(frontmatter, name)
        if isinstance(value, UnitLink):
            pairs.append((name, value))
        elif isinstance(value, tuple) and value and all(isinstance(item, UnitLink) for item in value):
            pairs.extend((name, link) for link in value)

    # 2. Extra fields (covers generic-page_type link fields coerced by _parse_frontmatter_generic)
    extras = frontmatter.__pydantic_extra__
    if extras:
        for name, value in extras.items():
            if isinstance(value, UnitLink):
                pairs.append((name, value))
            elif isinstance(value, tuple) and value and all(isinstance(item, UnitLink) for item in value):
                pairs.extend((name, link) for link in value)

    return pairs


def _resolve_frontmatter_edges(frontmatter: BaseFrontmatter, index: LinkIndex, dangling: list[_DanglingLink]) -> tuple[ResolvedEdge, ...]:
    r"""Resolve a note's frontmatter relations to page-level `ResolvedEdge`\s."""
    page_id = frontmatter.id
    edges: list[ResolvedEdge] = []
    for relation, link in _frontmatter_relations(frontmatter):
        dst_id = index.resolve(link)
        if dst_id is None:
            dangling.append(_DanglingLink(page_id=page_id, target=link.target, relation=relation))
        edges.append(ResolvedEdge(src_id=page_id, src_type="page", rel=relation, dst_target=link.target, dst_id=dst_id))
    return tuple(edges)


def _domain_section_body(note: KnowledgeBaseNote, section_slug: str) -> str:
    """The joined body text of one canonical H2 section and every subsection nested under it."""
    return "\n".join(
        section.body for section in note.sections if section.path == section_slug or section.path.startswith(f"{section_slug}.")
    )


def _resolve_section_edges(
    note: KnowledgeBaseNote, relations: Sequence[RelationDecl], index: LinkIndex, dangling: list[_DanglingLink]
) -> tuple[ResolvedEdge, ...]:
    """Resolve a note's section-derived relations to page-level edges, one per distinct target in first-seen order."""
    page_id = note.frontmatter.id
    page_type = note.frontmatter.type
    edges: list[ResolvedEdge] = []
    for relation in relations:
        if relation.domain_section is None or relation.domain != page_type:
            continue
        seen: set[str] = set()
        for link in unit_links_in(_domain_section_body(note, relation.domain_section)):
            if link.target in seen:
                continue
            seen.add(link.target)
            dst_id = index.resolve(link)
            if dst_id is None:
                dangling.append(_DanglingLink(page_id=page_id, target=link.target, relation=relation.slug))
            edges.append(ResolvedEdge(src_id=page_id, src_type="page", rel=relation.slug, dst_target=link.target, dst_id=dst_id))
    return tuple(edges)


def _claim_qualifiers(claim: ClaimBlock) -> dict[str, JsonValue]:
    """The claim's non-empty declared text fields, copied verbatim onto each of its edges."""
    return {name: value for name, value in claim.fields.items() if value}


def _resolve_claim(claim: ClaimBlock, page_id: str, index: LinkIndex, dangling: list[_DanglingLink]) -> ResolvedClaim:
    """Resolve one claim's link fields to ids and its `loc` to an ltree path."""
    block_id = claim.anchor.removeprefix("^")
    claim_id = f"{page_id}#{block_id}"
    anchor = claim.anchor

    field_links: list[ResolvedLink] = []

    def resolve_one(link: UnitLink | None, relation: str) -> str | None:
        if link is None:
            return None
        dst_id = index.resolve(link)
        if dst_id is None:
            dangling.append(_DanglingLink(page_id=page_id, target=link.target, relation=relation, anchor=anchor))
        return dst_id

    def resolve_field(link: UnitLink | None, field: str) -> str | None:
        if link is None:
            return None
        dst_id = resolve_one(link, field)
        field_links.append(
            ResolvedLink(src_id=claim_id, src_type="claim", rel=field, dst_target=link.target, dst_id=dst_id, provenance_claim=claim_id)
        )
        return dst_id

    def resolve_fields(links: Sequence[UnitLink], field: str) -> tuple[str, ...]:
        resolved = [resolve_field(link, field) for link in links]
        return tuple(dst for dst in resolved if dst is not None)

    qualifiers = _claim_qualifiers(claim)
    edges = tuple(
        ResolvedEdge(
            src_id=claim_id,
            src_type="claim",
            rel=relation,
            dst_target=link.target,
            dst_id=resolve_one(link, relation),
            qualifiers=qualifiers,
            provenance_claim=claim_id,
        )
        for relation, link in claim.edges
    )
    subject_id = resolve_field(claim.subject, "subject")
    link_ids = {name: resolve_fields(links, name) for name, links in claim.links.items()}
    disputed_by_ids = resolve_fields(claim.disputed_by, "disputed_by")

    return ResolvedClaim(
        block=claim,
        block_id=block_id,
        loc=claim.loc,
        loc_path=loc_to_ltree(claim.loc),
        subject_id=subject_id,
        link_ids=link_ids,
        disputed_by_ids=disputed_by_ids,
        edges=edges,
        field_links=tuple(field_links),
    )


def resolve_with_index(notes: Iterable[KnowledgeBaseNote], index: LinkIndex, *, relations: tuple[RelationDecl, ...] = ()) -> ResolveResult:
    """Resolve `notes` against a pre-built `index`, splitting dangling links into warnings and the ingest queue.

    A dangling target of a relation declared `queue_when_missing` goes to `ingest_queue` once,
    in first-seen order. Every other dangling target becomes a `dangling-link` warning.

    Args:
        notes: The notes to resolve.
        index: The link index to resolve against, which may cover more pages than `notes`.
        relations: The schema's declared relations. Only entries carrying a `domain_section` are read.

    Returns:
        The resolved notes, the dangling-link warnings and the ingest queue.
    """
    all_notes = tuple(notes)
    dangling: list[_DanglingLink] = []

    resolved_notes = tuple(
        ResolvedNote(
            note=note,
            edges=_resolve_frontmatter_edges(note.frontmatter, index, dangling) + _resolve_section_edges(note, relations, index, dangling),
            claims=tuple(_resolve_claim(claim, note.frontmatter.id, index, dangling) for claim in note.claims),
            superseded_by_id=index.resolve(note.frontmatter.superseded_by) if note.frontmatter.superseded_by is not None else None,
        )
        for note in all_notes
    )

    queued = frozenset(relation.slug for relation in relations if relation.queue_when_missing)
    warnings: list[ValidationIssue] = []
    ingest_queue: list[str] = []
    for miss in dangling:
        if miss.relation in queued:
            if miss.target not in ingest_queue:
                ingest_queue.append(miss.target)
        else:
            warnings.append(
                ValidationIssue(
                    severity="warning",
                    rule="dangling-link",
                    page_id=miss.page_id,
                    anchor=miss.anchor,
                    message=f"unresolved {miss.relation} link {miss.target!r}",
                )
            )

    return ResolveResult(notes=resolved_notes, warnings=tuple(warnings), ingest_queue=tuple(ingest_queue))


def resolve(notes: Iterable[KnowledgeBaseNote], *, relations: tuple[RelationDecl, ...] = ()) -> ResolveResult:
    """Resolve every note's links to ids against an index built over `notes` alone.

    Dangling links split the same way as in `resolve_with_index`.

    Args:
        notes: The notes to resolve, which are also every page a link can resolve to.
        relations: The schema's declared relations. Only entries carrying a `domain_section` are read.

    Returns:
        The resolved notes, the dangling-link warnings and the ingest queue.
    """
    all_notes = tuple(notes)
    return resolve_with_index(all_notes, build_link_index(all_notes), relations=relations)
