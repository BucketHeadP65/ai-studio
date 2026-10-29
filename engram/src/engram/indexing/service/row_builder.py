"""The row assembly the full, incremental and ingest paths share, so each path writes byte-identical rows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

from engram.indexing.domain import card
from engram.indexing.domain.embed_strategy import build_note_embed_strategies, build_source_embed_strategies
from engram.indexing.domain.mirror_chunks import DEFAULT_CHUNK_SIZES, chunk_mirror
from engram.indexing.domain.models import EdgeRow, IndexedPage, ValidationIssue, cited_mirror_key, frontmatter_value
from engram.indexing.domain.rows import ClaimQualifierRow, ClaimRow, LinkRefRow, SectionRow, ValidationRow
from engram.indexing.domain.section_summary import derive_section_summary
from engram.indexing.service.orientation import ORIENTATION_PAGES, orientation_page_id
from engram.indexing.service.vocab import vocab_rows
from engram.kb.contracts import UnitLink
from engram.sources.contracts import arxiv_id_of, doi_of

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.ai.retrieval.contracts import Row
    from pydantic import JsonValue

    from engram.indexing.domain.embed_strategy import EmbedStrategyUnit, NoteEmbedStrategies, SourceEmbedStrategies
    from engram.indexing.domain.manifest import ManifestEntry
    from engram.indexing.domain.mirror_chunks import ChunkSizes
    from engram.indexing.domain.models import MirrorDoc, ResolvedClaim, ResolvedEdge, ResolvedLink, ResolvedNote
    from engram.indexing.domain.resolver import ResolveResult
    from engram.kb.contracts import (
        BaseFrontmatter,
        ClaimGrammarSpec,
        FacetSpec,
        KnowledgeBaseNote,
        KnowledgeBaseSchema,
        Section,
        SectionSummarySpec,
        VocabularyIndex,
    )


def duplicate_id_errors(notes: Sequence[ResolvedNote]) -> tuple[ValidationIssue, ...]:
    """Return one `duplicate-id` error per note whose `id` an earlier note already used, which the load then skips.

    Args:
        notes: The resolved notes, in load order.

    Returns:
        The errors, in note order.
    """
    first_seen: dict[str, str] = {}
    issues: list[ValidationIssue] = []
    for resolved in notes:
        page_id = resolved.note.frontmatter.id
        prior = first_seen.get(page_id)
        if prior is None:
            first_seen[page_id] = resolved.note.source_path
        else:
            issues.append(
                ValidationIssue(
                    severity="error",
                    rule="duplicate-id",
                    page_id=page_id,
                    message=f"id {page_id!r} already used by {prior!r}. Ids must be unique: {resolved.note.source_path!r} is skipped",
                )
            )
    return tuple(issues)


def page_type_contract_rows(spec: KnowledgeBaseSchema, collection: str) -> list[Row]:
    """One `page_type_contracts` row per `spec.page_types` entry, the spec node serialized whole.

    Args:
        spec: The knowledge base schema.
        collection: The collection the rows belong to.

    Returns:
        The rows, in the order the schema declares its page types.
    """
    return [{"collection": collection, "page_type": pt.page_type, "contract": pt.model_dump()} for pt in spec.page_types]


def manifest_rows(entries: Sequence[ManifestEntry], collection: str) -> list[Row]:
    """One `kb.index_manifest` row per manifest entry.

    Args:
        entries: The entries to store.
        collection: The collection the rows belong to.

    Returns:
        The rows, in entry order.
    """
    return [
        {
            "collection": collection,
            "path": entry.path,
            "kind": entry.kind.value,
            "content_hash": entry.content_hash,
            "identity_hash": entry.identity_hash,
            "page_id": entry.page_id,
            "source_key": entry.source_key,
        }
        for entry in entries
    ]


def row_ids(rows: list[Row]) -> set[str]:
    """The `id` column of every row as a `set[str]` (the prune keep-set).

    Args:
        rows: The rows to read.

    Returns:
        The ids.
    """
    return {id_str(row["id"]) for row in rows}


def digest_page_ids(rows: list[Row], digest_types: frozenset[str]) -> tuple[str, ...]:
    """The ids of every digest page among `rows`, in load order.

    Args:
        rows: The page rows to read.
        digest_types: The page types the schema digests sources into, the values of `sources.digest_types`.

    Returns:
        The ids of the rows whose `page_type` is one of `digest_types`.
    """
    return tuple(id_str(row["id"]) for row in rows if row.get("page_type") in digest_types)


def id_str(value: object) -> str:
    """Narrow a NOT NULL key column's row value to `str` for a key set, never given `None`.

    Args:
        value: The row value.

    Returns:
        `value` when it is a string, else its string form.
    """
    return value if isinstance(value, str) else str(value)


def link_ref_rows(resolved: ResolveResult, collection: str) -> list[Row]:
    """Return every outbound link target the notes declare, resolved and dangling, as `kb.link_refs` rows.

    Args:
        resolved: The resolved notes.
        collection: The collection the rows belong to.

    Returns:
        One row for each edge of each note and each claim, and one for each target of each claim's subject, declared
        link fields and `disputed_by`.
    """
    rows: list[Row] = []
    for resolved_note in resolved.notes:
        rows.extend(link_ref_row(edge, collection) for edge in resolved_note.edges)
        for resolved_claim in resolved_note.claims:
            rows.extend(link_ref_row(edge, collection) for edge in resolved_claim.edges)
            rows.extend(link_ref_row(link, collection) for link in resolved_claim.field_links)
    return rows


def link_ref_row(link: ResolvedLink, collection: str) -> Row:
    """Build one `kb.link_refs` row from a link, its raw target lower-cased and `dst_collection` set only when it resolved.

    Args:
        link: The link to record.
        collection: The collection the row belongs to.

    Returns:
        The row.
    """
    return LinkRefRow(
        collection=collection,
        src_page_id=_page_id_of(link.src_id),
        src_kind=link.src_type,
        rel=link.rel,
        raw_target=link.dst_target.strip().lower(),
        dst_id=link.dst_id,
        dst_collection=collection if link.dst_id is not None else None,
        provenance_claim=link.provenance_claim,
    ).model_dump()


def validation_row(issue: ValidationIssue, collection: str) -> Row:
    """One `kb.validation_store` row from a validation issue.

    Args:
        issue: The issue to store.
        collection: The collection the row belongs to.

    Returns:
        The row.
    """
    return ValidationRow(
        collection=collection, page_id=issue.page_id, severity=issue.severity, rule=issue.rule, message=issue.message, anchor=issue.anchor
    ).model_dump()


def _page_id_of(src_id: str) -> str:
    """The owning page id of an edge source (a claim source id is `<page_id>#<block_id>`)."""
    return src_id.split("#", 1)[0]


def _frontmatter_date(frontmatter: BaseFrontmatter, name: str) -> date | None:
    """Return the `name` field of `frontmatter` as a date, read as ISO when it is text, or `None` when it is no date."""
    value = getattr(frontmatter, name, None)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


@dataclass(frozen=True, slots=True)
class LoadSet:
    """Every row to load for one rebuild, and the embed input for each row id."""

    pages: list[Row]
    sections: list[Row]
    claims: list[Row]
    claim_qualifiers: list[Row]
    edges: list[Row]
    sources: list[Row]
    chunks: list[Row]
    vocab: list[Row]
    embed_inputs: dict[str, str]


def _jsonable_frontmatter_value(value: object) -> JsonValue | None:
    """Convert one extra frontmatter value to JSON, or `None` to drop it (links and unknown objects are dropped)."""
    if isinstance(value, UnitLink):
        return None
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, list | tuple):
        # The element type is stated because list is invariant and the filter yields a narrower type.
        items: list[JsonValue] = [v for v in (_jsonable_frontmatter_value(item) for item in value) if v is not None]
        return items or None
    if isinstance(value, Mapping):
        # A nested mapping is kept as a JSON object.
        nested: dict[str, JsonValue] = {
            str(key): v for key, v in ((key, _jsonable_frontmatter_value(item)) for key, item in value.items()) if v is not None
        }
        return nested or None
    return None


def _frontmatter_payload(fm: object, facet_keys: frozenset[str]) -> dict[str, JsonValue]:
    """Return the note's extra frontmatter fields, facets left out, as the `pages.frontmatter` JSON."""
    extra = getattr(fm, "model_extra", None) or {}
    out: dict[str, JsonValue] = {}
    for name, value in extra.items():
        if name in facet_keys:
            continue
        projected = _jsonable_frontmatter_value(value)
        if projected is not None:
            out[name] = projected
    return out


def _facets_dict(fm: BaseFrontmatter, facets: tuple[FacetSpec, ...]) -> dict[str, list[str]]:
    """Return the page's declared facet values keyed by `FacetSpec.key`, non-empty lists only."""
    out: dict[str, list[str]] = {}
    for facet in facets:
        values = fm.facet_values(facet.key)
        if values:
            out[facet.key] = list(values)
    return out


_ALL_STOCK_STRATEGIES: frozenset[str] = frozenset({"page-card", "section", "claim", "source-chunk"})
"""The default `active` set: every stock embed strategy."""


class RowBuilder:
    """Turns resolved notes and source mirrors into the load rows and the embed input map."""

    def __init__(  # noqa: PLR0913 (the twelve arguments configure one builder, a wrapper object would just rename them)
        self,
        collection: str,
        *,
        section_summary_spec: SectionSummarySpec,
        note_strategies: NoteEmbedStrategies | None = None,
        source_strategies: SourceEmbedStrategies | None = None,
        active: frozenset[str] | None = None,
        facets: tuple[FacetSpec, ...] = (),
        claim_grammar: ClaimGrammarSpec | None = None,
        known_source_keys: frozenset[str] | None = None,
        source_key_field: str,
        digest_link_field: str,
        source_hash_field: str,
        date_field: str,
        chunk_sizes: ChunkSizes = DEFAULT_CHUNK_SIZES,
    ) -> None:
        """Create the builder for one collection.

        Args:
            collection: The collection every row belongs to.
            section_summary_spec: How a section's summary is derived.
            note_strategies: The note embed strategies. `None` builds the stock ones.
            source_strategies: The source mirror embed strategies. `None` builds the stock ones, cutting by `chunk_sizes`.
            active: The strategy names that produce rows. `None` activates every stock strategy.
            facets: The facets the schema declares, read into each page's `facets` column.
            claim_grammar: The schema's claim grammar, whose declared field order the claim embed text follows. `None` keeps
                each claim's own order.
            known_source_keys: The source keys a page may cite. A page citing another key gets no `source_id`. `None` skips the check.
            source_key_field: The frontmatter field holding a page's source key. A page with a value there takes it as its `source_id`.
            digest_link_field: The frontmatter field a digest names its source by. A page with no key field value takes its value
                as its `source_id`.
            source_hash_field: The frontmatter field holding the hash of the cited source.
            date_field: The frontmatter field a page's page date is read from. A value that is no date leaves it empty.
            chunk_sizes: How many tokens a chunk of a mirror aims for and may hold. Custom `source_strategies` must cut by the same sizes.
        """
        self._collection = collection
        self._chunk_sizes = chunk_sizes
        self._section_summary_spec = section_summary_spec
        card_facets = tuple(facet.key for facet in facets if facet.card)
        claim_fields = (*claim_grammar.link_fields, *claim_grammar.text_fields) if claim_grammar is not None else ()
        self._note_strategies = (
            note_strategies
            if note_strategies is not None
            else build_note_embed_strategies(card_facets=card_facets, claim_fields=claim_fields)
        )
        self._source_strategies = source_strategies if source_strategies is not None else build_source_embed_strategies(chunk_sizes=chunk_sizes)
        self._active = active if active is not None else _ALL_STOCK_STRATEGIES
        self._facets = facets
        self._known_source_keys = known_source_keys
        self._source_key_field = source_key_field
        self._digest_link_field = digest_link_field
        self._source_hash_field = source_hash_field
        self._date_field = date_field
        self._facet_keys = frozenset(facet.key for facet in facets)
        self.pages: list[Row] = []
        self.sections: list[Row] = []
        self.claims: list[Row] = []
        self.claim_qualifiers: list[Row] = []
        self.edges: list[Row] = []
        self.sources: list[Row] = []
        self.chunks: list[Row] = []
        self.embed_inputs: dict[str, str] = {}

    def add_note(self, resolved: ResolvedNote) -> None:
        """Add a note's page row, its section and claim rows when active, and every resolved edge.

        Args:
            resolved: The resolved note to add.
        """
        note = resolved.note
        (page_unit,) = tuple(self._note_strategies.select("page-card").units(resolved))
        self.pages.append(self._page_row(resolved, page_unit))
        self.embed_inputs[page_unit.id] = page_unit.text

        if "section" in self._active:
            section_units = self._note_strategies.select("section").units(resolved)
            for section, unit in zip(note.sections, section_units, strict=True):
                self._add_section(note, section, unit)

        if "claim" in self._active:
            claim_units = self._note_strategies.select("claim").units(resolved)
            for resolved_claim, unit in zip(resolved.claims, claim_units, strict=True):
                self._add_claim(note, resolved_claim, unit)

        self.edges.extend(_edge_row(edge, self._collection) for edge in resolved.edges if edge.dst_id is not None)
        if "claim" in self._active:
            for resolved_claim in resolved.claims:
                self.edges.extend(_edge_row(edge, self._collection) for edge in resolved_claim.edges if edge.dst_id is not None)

    def _page_row(self, resolved: ResolvedNote, unit: EmbedStrategyUnit) -> Row:
        """Build a `kb.pages` row from a resolved note, its `body_hash` the hash of the page card text."""
        note = resolved.note
        model = IndexedPage(
            frontmatter=_frontmatter_payload(note.frontmatter, self._facet_keys),
            id=note.frontmatter.id,
            collection=self._collection,
            page_type=note.frontmatter.type,
            slug=note.source_path.rsplit("/", 1)[-1].removesuffix(".md"),
            title=note.frontmatter.title,
            aliases=note.frontmatter.aliases,
            summary=note.frontmatter.summary,
            status=note.frontmatter.status,
            superseded_by=resolved.superseded_by_id,
            # A blank key is no source at all: the pages.source_id foreign key takes NULL, never "".
            source_id=self._gated_source_id(
                cited_mirror_key(note.frontmatter, key_field=self._source_key_field, digest_link_field=self._digest_link_field) or None
            ),
            source_hash=frontmatter_value(note.frontmatter, self._source_hash_field) or None,
            created=_frontmatter_date(note.frontmatter, "created"),
            page_date=_frontmatter_date(note.frontmatter, self._date_field),
            facets=_facets_dict(note.frontmatter, self._facets),
            card_text=unit.text,
            body_hash=card.content_hash(unit.text),
        )
        return _orm_row(model)

    def _gated_source_id(self, key: str | None) -> str | None:
        """Return `key` when it is in `known_source_keys` or no such set was given, else `None`."""
        if key is None or self._known_source_keys is None or key in self._known_source_keys:
            return key
        return None

    def _add_section(self, note: KnowledgeBaseNote, section: Section, unit: EmbedStrategyUnit) -> None:
        """Add a `kb.sections` row and its embed input."""
        self.sections.append(
            SectionRow(
                id=unit.id,
                collection=self._collection,
                page_id=note.frontmatter.id,
                canon=section.canon,
                heading=section.heading,
                path=section.path,
                ord=section.ord,
                body=section.body,
                tokens=None,
                body_hash=card.content_hash(unit.text),
                summary=derive_section_summary(section, note.sections, self._section_summary_spec),
            ).model_dump()
        )
        self.embed_inputs[unit.id] = unit.text

    def _add_claim(self, note: KnowledgeBaseNote, resolved_claim: ResolvedClaim, unit: EmbedStrategyUnit) -> None:
        """Add a `kb.claims` row, its qualifier rows and its embed input."""
        block = resolved_claim.block
        self.claims.append(
            ClaimRow(
                id=unit.id,
                collection=self._collection,
                page_id=note.frontmatter.id,
                block_id=resolved_claim.block_id,
                text=block.text,
                quote=block.quote,
                loc=resolved_claim.loc,
                loc_path=resolved_claim.loc_path,
                kind=block.kind,
                subject_id=resolved_claim.subject_id,
                subject_collection=self._collection if resolved_claim.subject_id is not None else None,
                fields={name: value for name, value in block.fields.items() if value},
                status=block.status or "active",
            ).model_dump()
        )
        self.claim_qualifiers.extend(_claim_qualifier_rows(unit.id, resolved_claim, self._collection))
        self.embed_inputs[unit.id] = unit.text

    def add_source(self, source: MirrorDoc) -> None:
        """Add a `kb.sources_raw` row, and its raw chunk rows when `source-chunk` is active.

        Every key of the mirror's own `meta` lands in the row's `meta`, except a key the row
        already derives from the mirror's other fields, whose derived value is kept. The chunks follow
        the mirror's cut plans.

        Args:
            source: The source mirror to add.
        """
        front = source.frontmatter
        title = str(front.get("title") or source.source_key)
        provider = source.provider
        provider_key = _as_str(front.get("provider_key")) or source.source_key
        source_id = _as_str(front.get("source_id")) or provider_key
        meta: dict[str, JsonValue] = {}
        for key in ("doi", "arxiv_id", "venue", "item_type", "url"):
            value = _as_str(front.get(key))
            if value is not None:
                meta[key] = value
        # The identities a request is matched on: a mirror that names only its URL still carries the ids derivable from it.
        url = _as_str(front.get("url"))
        if url is not None:
            if "arxiv_id" not in meta and (arxiv_id := arxiv_id_of(url)) is not None:
                meta["arxiv_id"] = arxiv_id
            if "doi" not in meta and (doi := doi_of(url)) is not None:
                meta["doi"] = doi
        year = _as_int(front.get("year"))
        if year is not None:
            meta["year"] = year
        # A value derived above wins over a connector's key of the same name.
        meta = {**_connector_meta(front.get("meta")), **meta}
        self.sources.append(
            {
                "source_id": source_id,
                "provider": provider,
                "provider_key": provider_key,
                "collection": source.collection,
                "title": title,
                "source_hash": str(front.get("source_hash") or ""),
                "pdf_path": _as_str(front.get("pdf_path")),
                "meta": meta,
            }
        )
        if "source-chunk" not in self._active:
            return
        chunk_units = self._source_strategies.select("source-chunk").units(source)
        for chunk, unit in zip(chunk_mirror(source.text, self._chunk_sizes, source.cut_plans), chunk_units, strict=True):
            self.chunks.append(
                {
                    "id": unit.id,
                    "collection": source.collection,
                    "source_id": source_id,
                    "section_path": chunk.section_path,
                    "heading": chunk.heading,
                    "seq": chunk.ord,
                    "body": chunk.body,
                    "tokens": None,
                }
            )
            self.embed_inputs[unit.id] = unit.text

    def add_about(self, orientation: Mapping[str, str]) -> None:
        """Add one synthetic orientation page per audience that has text.

        Args:
            orientation: The orientation text of each audience.
        """
        for audience, (base_id, title) in ORIENTATION_PAGES.items():
            text = orientation.get(audience, "")
            if not text.strip():
                continue
            page_id = orientation_page_id(base_id, self._collection)
            self.pages.append(self._orientation_page(page_id, title, text))
            self.embed_inputs[page_id] = text

    def _orientation_page(self, page_id: str, title: str, text: str) -> Row:
        """One synthetic page row. Every column a note would carry is empty: it owns no file."""
        return {
            "id": page_id,
            "collection": self._collection,
            "page_type": "meta",
            "slug": page_id.split(".", 1)[-1],
            "title": title,
            "aliases": [],
            "summary": None,
            "status": "verified",
            "superseded_by": None,
            "source_id": None,
            "source_hash": None,
            "created": None,
            "page_date": None,
            "facets": {},
            "frontmatter": {},
            "card_text": text,
            "body_hash": card.content_hash(text),
        }

    def build(self, vocabulary: VocabularyIndex) -> LoadSet:
        """Finalise the row lists, indexing the vocabulary into vocab rows.

        Args:
            vocabulary: The vocabulary index to write as `kb.vocab` rows.

        Returns:
            Every row to load and the embed input of each row id.
        """
        return LoadSet(
            pages=self.pages,
            sections=self.sections,
            claims=self.claims,
            claim_qualifiers=self.claim_qualifiers,
            edges=self.edges,
            sources=self.sources,
            chunks=self.chunks,
            vocab=list(vocab_rows(vocabulary, self._collection)),
            embed_inputs=self.embed_inputs,
        )


def _edge_row(edge: ResolvedEdge, collection: str) -> Row:
    """Build a `kb.edges` row from a resolved edge, both ends in `collection`."""
    return EdgeRow(
        src_id=edge.src_id,
        src_collection=collection,
        src_kind=edge.src_type,
        rel=edge.rel,
        dst_id=str(edge.dst_id),
        dst_collection=collection,
        dst_kind="page",
        qualifiers=edge.qualifiers,
        provenance_claim=edge.provenance_claim,
    ).model_dump()


def _claim_qualifier_rows(claim_id: str, resolved_claim: ResolvedClaim, collection: str) -> list[Row]:
    """Build one `kb.claim_qualifiers` row per resolved link field target, named by its field, `ord` keeping written order."""
    return [
        ClaimQualifierRow(
            collection=collection, claim_id=claim_id, qualifier=name, ord=ord_, target_collection=collection, target_id=target_id
        ).model_dump()
        for name, target_ids in resolved_claim.link_ids.items()
        for ord_, target_id in enumerate(target_ids)
    ]


def _orm_row(model: IndexedPage) -> Row:
    """Dump a page model, turning tuple columns into lists for the array columns."""
    data = model.model_dump()
    return {key: (list(value) if isinstance(value, tuple) else value) for key, value in data.items()}


def _as_int(value: object) -> int | None:
    """Coerce a frontmatter scalar to `int` when it is one, else `None`."""
    return value if isinstance(value, int) else None


def _as_str(value: object) -> str | None:
    """Coerce a frontmatter scalar to `str` when present, else `None`."""
    return str(value) if value is not None else None


def _connector_meta(value: object) -> dict[str, str]:
    """A mirror's own `meta` mapping with every present value as text, empty when it holds no mapping."""
    if not isinstance(value, Mapping):
        return {}
    return {key: text for key, item in value.items() if isinstance(key, str) and (text := _as_str(item)) is not None}
