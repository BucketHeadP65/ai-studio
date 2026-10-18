"""Unit tests for `RowBuilder`'s row construction (`src/engram/indexing/service/_row_builder.py`), no knowledge base or database.

Exercises the write-path shapes this slice introduces directly against `RowBuilder`:

- `_add_claim` emits one `kb.claim_qualifiers` row per multilink target, `ord` preserving
  authored order (the regression pinning the fix of the standing element-zero truncation).
- `_page_row` splits a note's declared frontmatter into `pages.facets` (declared facet keys)
  and the `pages.frontmatter` payload (everything else), never both.
- `add_source` emits the vendor-neutral `source_id` / `provider` / `provider_key` fields.
- `_orientation_page` carries none of the research-profile keys the maturation dropped.
- `_page_row` reads `page_date` from the field the schema names and keeps `created` as written.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from engram.indexing.contracts import MirrorDoc, ResolvedNote, resolve
from engram.indexing.service.row_builder import RowBuilder
from engram.kb.contracts import (
    ClaimBlock,
    ClaimGrammarSpec,
    FrontmatterFieldSpec,
    KnowledgeBaseNote,
    KnowledgeBaseSchema,
    PageTypeSpec,
    SectionSummarySpec,
    SourcesSpec,
    UnitLink,
    parse_frontmatter,
)
from engram.testing.specs import research_spec

_RESEARCH_SPEC = research_spec()

# A knowledge base of its own whose mirror key field is a knowledge base path, not the research bundle's field name.
_PATH_KEYED_SPEC = KnowledgeBaseSchema(
    slug="handbooks",
    page_types=(
        PageTypeSpec(
            page_type="document",
            id_prefix="doc.",
            frontmatter_fields=(
                FrontmatterFieldSpec(name="source_path", type="scalar"),
                FrontmatterFieldSpec(name="content_hash", type="scalar"),
            ),
        ),
    ),
    sources=SourcesSpec(
        enabled=True,
        connectors=("library",),
        key_field="source_path",
        digest_link_field="derived_from",
        hash_field="content_hash",
        digest_types={"article": "document"},
    ),
)


def _document_note(**fields: object) -> KnowledgeBaseNote:
    """A `document` note of `_PATH_KEYED_SPEC`'s own page_type, carrying whatever frontmatter `fields` names."""
    data: dict[str, object] = {"id": "doc.handbook", "type": "document", "title": "Handbook", "status": "draft", **fields}
    frontmatter = parse_frontmatter(data, _PATH_KEYED_SPEC)
    return KnowledgeBaseNote(frontmatter=frontmatter, sections=(), claims=(), source_path="Wiki/handbook.md")


def _paper_note(*, claims: tuple[ClaimBlock, ...] = ()) -> KnowledgeBaseNote:
    data: dict[str, object] = {
        "id": "paper.lightrag-2025",
        "type": "paper",
        "title": "LightRAG",
        "summary": "Graph-indexed RAG.",
        "status": "draft",
        "paper_type": "method",
        "year": 2025,
        "zotero_key": "K",
        "source_hash": "h",
        "tasks": ["long-doc-qa"],
    }
    frontmatter = parse_frontmatter(data, _RESEARCH_SPEC)
    return KnowledgeBaseNote(frontmatter=frontmatter, sections=(), claims=claims, source_path="Memory/Papers/lightrag-2025.md")


def _entity_note(entity_id: str, title: str) -> KnowledgeBaseNote:
    data = {"id": entity_id, "type": "entity", "title": title, "status": "draft", "entity_type": "dataset"}
    frontmatter = parse_frontmatter(data, _RESEARCH_SPEC)
    return KnowledgeBaseNote(frontmatter=frontmatter, sections=(), claims=(), source_path=f"Memory/Entities/{title.lower()}.md")


def _builder() -> RowBuilder:
    return RowBuilder(
        "research",
        section_summary_spec=SectionSummarySpec(),
        facets=_RESEARCH_SPEC.facets,
        source_key_field=_RESEARCH_SPEC.sources.key_field,
        source_hash_field=_RESEARCH_SPEC.sources.hash_field,
        date_field="created",
    )


def _as_mapping(value: object) -> Mapping[str, object]:
    """Narrow a `Row` value (typed `object`) to a mapping, for asserting on a nested jsonb column."""
    assert isinstance(value, Mapping)
    return value


class TestChildRowsCarryTheirCollection:
    """Every child row `RowBuilder` emits gains `collection` (and claims gain `subject_collection`)."""

    def test_section_and_claim_rows_carry_the_builder_collection(self):
        claim = ClaimBlock(anchor="^c1", text="LightRAG wins.", loc="§4.2", kind="benchmark-result")
        paper = _paper_note(claims=(claim,))
        resolved_paper = resolve((paper,)).notes[0]
        builder = _builder()

        builder.add_note(resolved_paper)

        assert builder.sections or builder.claims  # the fixture note has at least one child row
        assert all(row["collection"] == "research" for row in builder.sections)
        assert all(row["collection"] == "research" for row in builder.claims)

    def test_claim_subject_collection_is_backfilled_when_subject_resolves(self):
        claim = ClaimBlock(anchor="^c1", text="LightRAG wins.", loc="§4.2", kind="benchmark-result", subject=UnitLink(target="paper.lightrag-2025"))
        paper = _paper_note(claims=(claim,))
        resolved_paper = resolve((paper,)).notes[0]
        builder = _builder()

        builder.add_note(resolved_paper)

        (row,) = builder.claims
        assert row["subject_id"] == "paper.lightrag-2025"
        assert row["subject_collection"] == "research"

    def test_claim_subject_collection_is_none_when_subject_is_absent(self):
        claim = ClaimBlock(anchor="^c1", text="LightRAG wins.", loc="§4.2", kind="benchmark-result")
        paper = _paper_note(claims=(claim,))
        resolved_paper = resolve((paper,)).notes[0]
        builder = _builder()

        builder.add_note(resolved_paper)

        (row,) = builder.claims
        assert row["subject_id"] is None
        assert row["subject_collection"] is None

    def test_add_source_emits_collection_on_the_source_and_its_chunk_rows(self):
        mirror = MirrorDoc(
            source_key="K",
            frontmatter={"title": "T"},
            body="## 1. Intro\n\nSome text here.\n",
            text="## 1. Intro\n\nSome text here.\n",
            collection="research",
            path="Sources/k.md",
        )
        builder = _builder()

        builder.add_source(mirror)

        (source_row,) = builder.sources
        assert source_row["collection"] == "research"
        assert builder.chunks, "the mirror body must chunk into at least one raw_chunks row"
        assert all(row["collection"] == "research" for row in builder.chunks)


class TestMultilinkClaimQualifiers:
    """A claim naming two datasets stores two `claim_qualifiers` rows, not just the first."""

    def test_two_dataset_targets_yield_two_qualifier_rows_ordered(self):
        claim = ClaimBlock(
            anchor="^c1",
            text="LightRAG beats GraphRAG on both benchmarks.",
            loc="§4.2",
            kind="benchmark-result",
            links={"on_dataset": (UnitLink(target="ent.ultradomain"), UnitLink(target="ent.hotpotqa"))},
        )
        paper = _paper_note(claims=(claim,))
        batch = resolve((paper, _entity_note("ent.ultradomain", "UltraDomain"), _entity_note("ent.hotpotqa", "HotpotQA")))
        resolved_paper = next(rn for rn in batch.notes if rn.note.frontmatter.id == "paper.lightrag-2025")
        builder = _builder()

        builder.add_note(resolved_paper)

        dataset_rows = [row for row in builder.claim_qualifiers if row["qualifier"] == "on_dataset"]
        assert [(row["ord"], row["target_id"]) for row in dataset_rows] == [(0, "ent.ultradomain"), (1, "ent.hotpotqa")]
        assert all(row["claim_id"] == "paper.lightrag-2025#c1" for row in dataset_rows)
        assert all(row["collection"] == "research" and row["target_collection"] == "research" for row in dataset_rows)

    def test_single_link_qualifiers_write_one_row_at_ord_zero(self):
        claim = ClaimBlock(
            anchor="^c1",
            text="LightRAG wins.",
            loc="§4.2",
            kind="benchmark-result",
            links={"metric": (UnitLink(target="ent.win-rate"),)},
        )
        paper = _paper_note(claims=(claim,))
        batch = resolve((paper, _entity_note("ent.win-rate", "Win rate")))
        resolved_paper = next(rn for rn in batch.notes if rn.note.frontmatter.id == "paper.lightrag-2025")
        builder = _builder()

        builder.add_note(resolved_paper)

        metric_rows = [row for row in builder.claim_qualifiers if row["qualifier"] == "metric"]
        assert [(row["ord"], row["target_id"]) for row in metric_rows] == [(0, "ent.win-rate")]

    def test_a_dangling_dataset_target_writes_no_row(self):
        claim = ClaimBlock(
            anchor="^c1",
            text="LightRAG wins.",
            loc="§4.2",
            kind="benchmark-result",
            links={"on_dataset": (UnitLink(target="ent.nowhere"),)},
        )
        paper = _paper_note(claims=(claim,))
        resolved_paper = resolve((paper,)).notes[0]
        builder = _builder()

        builder.add_note(resolved_paper)

        assert builder.claim_qualifiers == []


# A claim grammar of its own: two link fields and two text fields, none of them the research bundle's.
_DECLARED_GRAMMAR = ClaimGrammarSpec(
    section="findings",
    multilink_fields=("applies_to",),
    link_scalar_fields=("governed_by",),
    text_scalar_fields=("jurisdiction",),
    plain_fields=("scope",),
)


class TestDeclaredClaimFieldRows:
    """A claim's declared fields land generically: link targets as qualifier rows, text fields in `fields`."""

    def _resolved_paper(self, claim: ClaimBlock) -> ResolvedNote:
        """The paper carrying `claim`, resolved against the three pages its links name."""
        notes = (
            _paper_note(claims=(claim,)),
            _entity_note("ent.team-a", "Team A"),
            _entity_note("ent.team-b", "Team B"),
            _entity_note("ent.policy", "Policy"),
        )
        return next(rn for rn in resolve(notes).notes if rn.note.frontmatter.id == "paper.lightrag-2025")

    def test_each_declared_link_target_writes_one_qualifier_row_named_by_its_field(self):
        claim = ClaimBlock(
            anchor="^c1",
            text="The policy binds both teams.",
            loc="§2",
            links={
                "governed_by": (UnitLink(target="ent.policy"),),
                "applies_to": (UnitLink(target="ent.team-a"), UnitLink(target="ent.team-b")),
            },
        )
        builder = _builder()

        builder.add_note(self._resolved_paper(claim))

        assert sorted((row["qualifier"], row["ord"], row["target_id"]) for row in builder.claim_qualifiers) == [
            ("applies_to", 0, "ent.team-a"),
            ("applies_to", 1, "ent.team-b"),
            ("governed_by", 0, "ent.policy"),
        ]
        assert all(row["claim_id"] == "paper.lightrag-2025#c1" for row in builder.claim_qualifiers)

    def test_the_claim_row_stores_every_declared_text_field_by_name(self):
        claim = ClaimBlock(anchor="^c1", text="The policy binds both teams.", loc="§2", fields={"jurisdiction": "EU", "scope": "tenants"})
        builder = _builder()

        builder.add_note(self._resolved_paper(claim))

        (row,) = builder.claims
        assert row["fields"] == {"jurisdiction": "EU", "scope": "tenants"}

    def test_an_empty_declared_text_field_is_not_stored(self):
        claim = ClaimBlock(anchor="^c1", text="The policy binds both teams.", loc="§2", fields={"jurisdiction": "EU", "scope": ""})
        builder = _builder()

        builder.add_note(self._resolved_paper(claim))

        (row,) = builder.claims
        assert row["fields"] == {"jurisdiction": "EU"}

    def test_the_claim_embed_text_appends_the_declared_fields_in_grammar_order(self):
        claim = ClaimBlock(
            anchor="^c1",
            text="The policy binds both teams.",
            loc="§2",
            fields={"scope": "tenants", "jurisdiction": "EU"},
            links={"governed_by": (UnitLink(target="ent.policy"),), "applies_to": (UnitLink(target="ent.team-a"),)},
        )
        builder = RowBuilder(
            "research",
            section_summary_spec=SectionSummarySpec(),
            claim_grammar=_DECLARED_GRAMMAR,
            source_key_field=_RESEARCH_SPEC.sources.key_field,
            source_hash_field=_RESEARCH_SPEC.sources.hash_field,
            date_field="created",
        )

        builder.add_note(self._resolved_paper(claim))

        assert (
            builder.embed_inputs["paper.lightrag-2025#c1"] == "LightRAG: The policy binds both teams. [ent.team-a; ent.policy; EU; tenants]"
        )


class TestPageRowFacetsAndFrontmatterSplit:
    """`_page_row` routes a declared facet to `facets`, everything else to `frontmatter`."""

    def test_declared_facet_lands_in_the_facets_dict_not_frontmatter(self):
        resolved_paper = resolve((_paper_note(),)).notes[0]
        builder = _builder()

        builder.add_note(resolved_paper)

        (page,) = [row for row in builder.pages if row["id"] == "paper.lightrag-2025"]
        assert _as_mapping(page["facets"])["tasks"] == ["long-doc-qa"]
        assert "tasks" not in _as_mapping(page["frontmatter"])

    def test_a_facet_written_as_one_string_lands_as_a_one_value_list(self):
        data: dict[str, object] = {
            "id": "paper.lightrag-2025",
            "type": "paper",
            "title": "LightRAG",
            "status": "draft",
            "paper_type": "method",
            "year": 2025,
            "zotero_key": "K",
            "source_hash": "h",
            "tasks": "retrieval",
        }
        note = KnowledgeBaseNote(frontmatter=parse_frontmatter(data, _RESEARCH_SPEC), sections=(), claims=(), source_path="Memory/Papers/x.md")
        builder = _builder()

        builder.add_note(resolve((note,)).notes[0])

        (page,) = builder.pages
        assert _as_mapping(page["facets"])["tasks"] == ["retrieval"]

    def test_a_non_facet_declared_field_lands_in_frontmatter_not_facets(self):
        # `year` is a declared frontmatter field on `paper` but not one of research's declared
        # facets (`tasks`/`domains`/`stages`/`modalities`/`scale_regimes`/`paper_type`/`maturity`/
        # `eval_setting`) — it must land only in the jsonb payload.
        resolved_paper = resolve((_paper_note(),)).notes[0]
        builder = _builder()

        builder.add_note(resolved_paper)

        (page,) = [row for row in builder.pages if row["id"] == "paper.lightrag-2025"]
        assert _as_mapping(page["frontmatter"])["year"] == 2025
        assert "year" not in _as_mapping(page["facets"])

    def test_source_id_and_source_hash_stay_dedicated_columns(self):
        resolved_paper = resolve((_paper_note(),)).notes[0]
        builder = _builder()

        builder.add_note(resolved_paper)

        (page,) = [row for row in builder.pages if row["id"] == "paper.lightrag-2025"]
        assert page["source_id"] == "K"
        assert page["source_hash"] == "h"


class TestOrientationPageCarriesNoResearchKeys:
    def test_orientation_page_row_has_no_research_columns(self):
        builder = _builder()

        builder.add_about({"query": "Read the schema first."})

        (page,) = builder.pages
        assert page["page_type"] == "meta"
        assert set(page.keys()) == {
            "id",
            "collection",
            "page_type",
            "slug",
            "title",
            "aliases",
            "summary",
            "status",
            "superseded_by",
            "source_id",
            "source_hash",
            "created",
            "page_date",
            "facets",
            "frontmatter",
            "card_text",
            "body_hash",
        }
        assert page["facets"] == {}
        assert page["frontmatter"] == {}
        assert page["page_date"] is None

    def test_orientation_page_row_carries_the_same_columns_as_a_note_page_row(self):
        builder = _builder()

        builder.add_note(resolve((_paper_note(),)).notes[0])
        builder.add_about({"query": "Read the schema first."})

        note_page, orientation_page = builder.pages
        assert set(orientation_page) == set(note_page)


class TestAddSourceEmitsVendorNeutralFields:
    def test_add_source_emits_provider_and_provider_key_equal_to_source_id(self):
        mirror = MirrorDoc(
            source_key="ABCD1234",
            frontmatter={"title": "LightRAG", "provider": "zotero", "provider_key": "ABCD1234", "source_id": "ABCD1234"},
            body="body",
            text="body",
            collection="research",
            path="Sources/ABCD1234.md",
        )
        builder = _builder()

        builder.add_source(mirror)

        (row,) = builder.sources
        assert row["provider"] == "zotero"
        assert row["provider_key"] == "ABCD1234"
        assert row["source_id"] == "ABCD1234"
        assert row["provider_key"] == row["source_id"]

    def test_add_source_without_neutral_frontmatter_falls_back_to_the_source_key(self):
        # A mirror whose frontmatter names no source_id/provider_key: its own source key answers for
        # both, so the row still carries an identity. It declares a provider, since a mirror naming
        # none never reaches a load at all.
        mirror = MirrorDoc(
            source_key="LEGACY01",
            frontmatter={"title": "Legacy", "provider": "example-library"},
            body="body",
            text="body",
            collection="research",
            path="s.md",
        )
        builder = _builder()

        builder.add_source(mirror)

        (row,) = builder.sources
        assert row["provider"] == "example-library"
        assert row["provider_key"] == "LEGACY01"
        assert row["source_id"] == "LEGACY01"

    def test_doi_arxiv_id_venue_year_fold_into_meta(self):
        mirror = MirrorDoc(
            source_key="K",
            frontmatter={"title": "T", "doi": "10.1/x", "arxiv_id": "2401.00001", "venue": "arXiv", "year": 2024},
            body="body",
            text="body",
            collection="research",
            path="s.md",
        )
        builder = _builder()

        builder.add_source(mirror)

        (row,) = builder.sources
        assert row["meta"] == {"doi": "10.1/x", "arxiv_id": "2401.00001", "venue": "arXiv", "year": 2024}
        assert "doi" not in row
        assert "arxiv_id" not in row
        assert "venue" not in row
        assert "year" not in row

    def test_a_mirror_naming_only_its_url_carries_the_arxiv_id_and_doi_in_meta(self):
        """A request is matched on url, arXiv id or DOI: a mirror with just `url` still exposes the id in it."""
        arxiv = MirrorDoc(
            source_key="K", frontmatter={"title": "T", "url": "https://arxiv.org/abs/2505.10978"}, body="body", text="body", collection="research", path="s.md"
        )
        doi = MirrorDoc(
            source_key="D", frontmatter={"title": "T", "url": "https://doi.org/10.1000/xyz"}, body="body", text="body", collection="research", path="d.md"
        )
        builder = _builder()

        builder.add_source(arxiv)
        builder.add_source(doi)

        first, second = builder.sources
        assert first["meta"] == {"url": "https://arxiv.org/abs/2505.10978", "arxiv_id": "2505.10978"}
        assert second["meta"] == {"url": "https://doi.org/10.1000/xyz", "doi": "10.1000/xyz"}

    def test_item_type_folds_into_meta(self):
        mirror = MirrorDoc(
            source_key="K", frontmatter={"title": "T", "item_type": "video"}, body="body", text="body", collection="research", path="s.md"
        )
        builder = _builder()

        builder.add_source(mirror)

        (row,) = builder.sources
        assert row["meta"] == {"item_type": "video"}
        assert "item_type" not in row

    def test_every_key_of_the_mirror_metadata_lands_in_meta(self):
        mirror = MirrorDoc(
            source_key="K",
            frontmatter={"title": "T", "meta": {"region": "north", "issued_on": "2024-01-02"}},
            body="body",
            text="body",
            collection="research",
            path="s.md",
        )
        builder = _builder()

        builder.add_source(mirror)

        (row,) = builder.sources
        assert row["meta"] == {"region": "north", "issued_on": "2024-01-02"}

    def test_a_mirror_metadata_key_never_overwrites_a_value_the_row_builder_derives(self):
        mirror = MirrorDoc(
            source_key="K",
            frontmatter={
                "title": "T",
                "doi": "10.1/x",
                "venue": "arXiv",
                "item_type": "paper",
                "url": "https://arxiv.org/abs/2505.10978",
                "year": 2024,
                "meta": {
                    "doi": "10.9/other",
                    "arxiv_id": "9999.99999",
                    "venue": "elsewhere",
                    "item_type": "video",
                    "url": "https://example.org/other",
                    "year": "1999",
                    "region": "north",
                },
            },
            body="body",
            text="body",
            collection="research",
            path="s.md",
        )
        builder = _builder()

        builder.add_source(mirror)

        (row,) = builder.sources
        assert row["meta"] == {
            "doi": "10.1/x",
            "venue": "arXiv",
            "item_type": "paper",
            "url": "https://arxiv.org/abs/2505.10978",
            "arxiv_id": "2505.10978",
            "year": 2024,
            "region": "north",
        }

    def test_item_type_is_absent_from_meta_when_the_mirror_has_none(self):
        mirror = MirrorDoc(source_key="K", frontmatter={"title": "T"}, body="body", text="body", collection="research", path="s.md")
        builder = _builder()

        builder.add_source(mirror)

        (row,) = builder.sources
        assert "item_type" not in _as_mapping(row["meta"])


class TestPageSourceLinkGate:
    """`_page_row` keeps `source_id` only for a key in `known_source_keys` (None keeps every key)."""

    def test_source_id_kept_when_the_key_is_known(self):
        builder = RowBuilder(
            "research",
            section_summary_spec=SectionSummarySpec(),
            facets=_RESEARCH_SPEC.facets,
            known_source_keys=frozenset({"K"}),
            source_key_field=_RESEARCH_SPEC.sources.key_field,
            source_hash_field=_RESEARCH_SPEC.sources.hash_field,
            date_field="created",
        )

        builder.add_note(resolve((_paper_note(),)).notes[0])

        (row,) = builder.pages
        assert row["source_id"] == "K"

    def test_source_id_nulled_when_the_key_is_unknown(self):
        builder = RowBuilder(
            "research",
            section_summary_spec=SectionSummarySpec(),
            facets=_RESEARCH_SPEC.facets,
            known_source_keys=frozenset(),
            source_key_field=_RESEARCH_SPEC.sources.key_field,
            source_hash_field=_RESEARCH_SPEC.sources.hash_field,
            date_field="created",
        )

        builder.add_note(resolve((_paper_note(),)).notes[0])

        (row,) = builder.pages
        assert row["source_id"] is None

    def test_no_universe_keeps_every_key(self):
        builder = _builder()

        builder.add_note(resolve((_paper_note(),)).notes[0])

        (row,) = builder.pages
        assert row["source_id"] == "K"


class TestThePageSourceIdComesFromTheSpecsKeyField:
    """`pages.source_id` is the page's value under the knowledge base's declared `sources.key_field`."""

    def test_the_declared_field_becomes_the_page_source_id(self):
        builder = RowBuilder(
            "handbooks", section_summary_spec=SectionSummarySpec(), source_key_field="source_path", source_hash_field="", date_field="created"
        )

        builder.add_note(resolve((_document_note(source_path="Sources/handbook.md"),)).notes[0])

        (row,) = builder.pages
        assert row["source_id"] == "Sources/handbook.md"

    def test_another_knowledge_base_own_key_field_leaves_the_page_source_id_null(self):
        builder = RowBuilder(
            "handbooks", section_summary_spec=SectionSummarySpec(), source_key_field="source_path", source_hash_field="", date_field="created"
        )

        builder.add_note(resolve((_document_note(zotero_key="K"),)).notes[0])

        (row,) = builder.pages
        assert row["source_id"] is None

    def test_a_knowledge_base_declaring_no_key_field_leaves_every_page_source_id_null(self):
        builder = RowBuilder("handbooks", section_summary_spec=SectionSummarySpec(), source_key_field="", source_hash_field="", date_field="created")

        builder.add_note(resolve((_document_note(source_path="Sources/handbook.md"),)).notes[0])

        (row,) = builder.pages
        assert row["source_id"] is None

    def test_the_research_bundles_own_key_field_still_lands_in_source_id(self):
        builder = RowBuilder(
            "research",
            section_summary_spec=SectionSummarySpec(),
            facets=_RESEARCH_SPEC.facets,
            source_key_field="zotero_key",
            source_hash_field="",
            date_field="created",
        )

        builder.add_note(resolve((_paper_note(),)).notes[0])

        (row,) = builder.pages
        assert row["source_id"] == "K"

    def test_a_key_outside_the_known_source_universe_is_gated_to_null(self):
        builder = RowBuilder(
            "handbooks",
            section_summary_spec=SectionSummarySpec(),
            known_source_keys=frozenset({"Sources/other.md"}),
            source_key_field="source_path",
            source_hash_field="",
            date_field="created",
        )

        builder.add_note(resolve((_document_note(source_path="Sources/handbook.md"),)).notes[0])

        (row,) = builder.pages
        assert row["source_id"] is None


class TestThePageSourceHashComesFromTheSpecsHashField:
    """`pages.source_hash` is the page's value under the knowledge base's declared `sources.hash_field`."""

    def _builder_for(self, hash_field: str) -> RowBuilder:
        return RowBuilder(
            "handbooks", section_summary_spec=SectionSummarySpec(), source_key_field="source_path", source_hash_field=hash_field, date_field="created"
        )

    def test_the_declared_field_becomes_the_page_source_hash(self):
        builder = self._builder_for("content_hash")

        builder.add_note(resolve((_document_note(source_path="Sources/handbook.md", content_hash="abc123"),)).notes[0])

        (row,) = builder.pages
        assert row["source_hash"] == "abc123"

    def test_another_knowledge_base_own_hash_field_leaves_the_page_source_hash_null(self):
        builder = self._builder_for("some_other_hash")

        builder.add_note(resolve((_document_note(source_path="Sources/handbook.md", content_hash="abc123"),)).notes[0])

        (row,) = builder.pages
        assert row["source_hash"] is None

    def test_a_knowledge_base_declaring_no_hash_field_leaves_every_page_source_hash_null(self):
        builder = RowBuilder("handbooks", section_summary_spec=SectionSummarySpec(), source_key_field="source_path", source_hash_field="", date_field="created")

        builder.add_note(resolve((_document_note(source_path="Sources/handbook.md", content_hash="abc123"),)).notes[0])

        (row,) = builder.pages
        assert row["source_hash"] is None

    def test_the_research_bundles_own_hash_field_still_lands_in_source_hash(self):
        builder = RowBuilder(
            "research",
            section_summary_spec=SectionSummarySpec(),
            facets=_RESEARCH_SPEC.facets,
            source_key_field=_RESEARCH_SPEC.sources.key_field,
            source_hash_field=_RESEARCH_SPEC.sources.hash_field,
            date_field="created",
        )

        builder.add_note(resolve((_paper_note(),)).notes[0])

        (row,) = builder.pages
        assert row["source_hash"] == "h"


class TestThePageDateComesFromTheSchemasDateField:
    """`pages.page_date` is the page's date under the schema's `retrieval.date_field`, and `pages.created` keeps its own day."""

    def _builder_for(self, date_field: str) -> RowBuilder:
        return RowBuilder(
            "handbooks", section_summary_spec=SectionSummarySpec(), source_key_field="source_path", source_hash_field="", date_field=date_field
        )

    def _page_row(self, date_field: str, **fields: object) -> Mapping[str, object]:
        builder = self._builder_for(date_field)

        builder.add_note(resolve((_document_note(**fields),)).notes[0])

        (row,) = builder.pages
        return row

    def test_the_named_field_becomes_the_page_date_and_created_keeps_the_day_it_names(self):
        row = self._page_row("reviewed_on", created="2026-05-04", reviewed_on="2019-03-14")

        assert row["page_date"] == date(2019, 3, 14)
        assert row["created"] == date(2026, 5, 4)

    def test_a_date_the_yaml_reader_already_parsed_is_kept_as_that_day(self):
        row = self._page_row("reviewed_on", reviewed_on=date(2019, 3, 14))

        assert row["page_date"] == date(2019, 3, 14)

    def test_created_named_as_the_date_field_makes_the_page_date_the_created_day(self):
        row = self._page_row("created", created="2026-05-04", reviewed_on="2019-03-14")

        assert row["page_date"] == date(2026, 5, 4)
        assert row["created"] == date(2026, 5, 4)

    def test_a_value_that_is_no_date_leaves_the_page_date_empty(self):
        row = self._page_row("reviewed_on", created="2026-05-04", reviewed_on="sometime in spring")

        assert row["page_date"] is None
        assert row["created"] == date(2026, 5, 4)

    def test_a_page_without_the_named_field_has_no_page_date_whatever_its_created_day(self):
        row = self._page_row("reviewed_on", created="2026-05-04")

        assert row["page_date"] is None
        assert row["created"] == date(2026, 5, 4)


class TestAddSourceCarriesTheDeclaredProvider:
    """`sources_raw.provider` is the name the mirror declares; the row builder never guesses one."""

    def test_the_row_carries_the_mirrors_own_provider(self):
        mirror = MirrorDoc.from_text("Sources/a.md", "---\nsource_key: A\nprovider: example-library\ntitle: T\n---\n\nbody\n", "research")
        builder = _builder()

        builder.add_source(mirror)

        (row,) = builder.sources
        assert row["provider"] == "example-library"

    def test_a_mirror_naming_no_provider_carries_no_provider_name(self):
        # `loadable_mirrors` keeps such a mirror out of every load, so this row is never built by a
        # indexing path; the row builder still refuses to invent a name for a direct caller.
        mirror = MirrorDoc.from_text("Sources/a.md", "---\nsource_key: A\ntitle: T\n---\n\nbody\n", "research")
        builder = _builder()

        builder.add_source(mirror)

        (row,) = builder.sources
        assert row["provider"] == ""
