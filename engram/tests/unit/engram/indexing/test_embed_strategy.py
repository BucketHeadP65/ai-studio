r"""Unit specs for the embedding-composition seam (`engram.indexing.domain.embed_strategy`).

Pure plumbing, no database and no network: `EmbedStrategy` implementations wrap the `card`
text builders verbatim, and the two vocabularies (`NoteEmbedStrategies`, `SourceEmbedStrategies`)
are the same enum-keyed-dispatch pattern `SearchDialects` / `GraphBackends` use, keyed by name
instead of an enum member because the strategy set is knowledge_base-extensible, not closed.

A hand-written `FakeNoteStrategy` / `FakeSourceStrategy` structurally implements `EmbedStrategy`
so the vocabularies are exercised without a database. The stock-strategy specs pin that each one
reproduces its matching `card` builder's output exactly (id scheme + text), the acceptance bar
for a knowledge base that declares nothing.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from engram.indexing.contracts import ChunkSizes, MirrorDoc, content_hash, resolve
from engram.indexing.domain import card
from engram.indexing.domain.embed_strategy import (
    ClaimStrategy,
    EmbedStrategyUnit,
    NoteEmbedStrategies,
    PageCardStrategy,
    SectionStrategy,
    SourceChunkStrategy,
    SourceEmbedStrategies,
    active_levels,
    build_note_embed_strategies,
    build_source_embed_strategies,
)
from engram.indexing.domain.mirror_chunks import chunk_mirror
from engram.indexing.service.row_builder import RowBuilder
from engram.kb.contracts import ClaimBlock, KnowledgeBaseNote, KnowledgeBaseSchema, Section, SectionSummarySpec, UnitLink, parse_frontmatter
from engram.kb.domain.schema import EmbedStrategiesSpec
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from collections.abc import Iterable

    from engram.indexing.contracts import ResolvedNote

_RESEARCH_SPEC = research_spec()

_SECTION = Section(heading="Findings", level=2, path="findings", canon="findings", body="LightRAG wins.", ord=0)

_CLAIM = ClaimBlock(
    anchor="^c1",
    text="LightRAG beats GraphRAG.",
    loc="§4.2",
    kind="benchmark-result",
    fields={"condition": "hybrid mode"},
    links={"on_dataset": (UnitLink(target="ultradomain"),), "metric": (UnitLink(target="win-rate"),)},
)


def _note(*, sections: tuple[Section, ...] = (), claims: tuple[ClaimBlock, ...] = ()) -> KnowledgeBaseNote:
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
    return KnowledgeBaseNote(frontmatter=frontmatter, sections=sections, claims=claims, source_path="Memory/Papers/lightrag-2025.md")


def _resolved(note: KnowledgeBaseNote) -> ResolvedNote:
    """Resolve a single-note batch and return its one `ResolvedNote` (the strategy's input shape)."""
    return resolve((note,)).notes[0]


def _mirror(*, source_key: str = "K", title: str = "LightRAG", body: str = "## 1 Intro\n\nShort body.") -> MirrorDoc:
    return MirrorDoc(source_key=source_key, frontmatter={"title": title}, body=body, text=body, collection="research", path="s.md")


class FakeNoteStrategy:
    """Structural stand-in for a note-level `EmbedStrategy`, records every note it is called with."""

    def __init__(self, level: str = "custom") -> None:
        self.level = level
        self.calls: list[ResolvedNote] = []

    def units(self, item: ResolvedNote) -> Iterable[EmbedStrategyUnit]:
        self.calls.append(item)
        yield EmbedStrategyUnit(id=item.note.frontmatter.id, level=self.level, text="custom text")


class FakeSourceStrategy:
    """Structural stand-in for a source-mirror `EmbedStrategy`."""

    def __init__(self, level: str = "custom-chunk") -> None:
        self.level = level

    def units(self, item: MirrorDoc) -> Iterable[EmbedStrategyUnit]:
        yield EmbedStrategyUnit(id=f"{item.source_key}:custom", level=self.level, text="custom chunk text")


class TestEmbedStrategyUnit:
    def test_holds_id_level_and_text(self):
        unit = EmbedStrategyUnit(id="paper.x", level="page", text="hello")

        assert unit.id == "paper.x"
        assert unit.level == "page"
        assert unit.text == "hello"


class TestPageCardStrategy:
    """One page-level unit per note, wrapping `card.card_text` verbatim."""

    def test_level_is_page(self):
        assert PageCardStrategy().level == "page"

    def test_yields_exactly_one_unit_keyed_by_the_page_id(self):
        resolved = _resolved(_note())

        (unit,) = tuple(PageCardStrategy().units(resolved))

        assert unit.id == "paper.lightrag-2025"
        assert unit.level == "page"

    def test_text_matches_card_card_text_exactly(self):
        note = _note()
        resolved = _resolved(note)

        (unit,) = tuple(PageCardStrategy().units(resolved))

        assert unit.text == card.card_text(note, card_facets=())


class TestSectionStrategy:
    """One unit per section, wrapping `card.section_embed_input` verbatim."""

    def test_level_is_section(self):
        assert SectionStrategy().level == "section"

    def test_a_claimless_sectionless_note_yields_no_units(self):
        resolved = _resolved(_note())

        assert list(SectionStrategy().units(resolved)) == []

    def test_yields_one_unit_per_section_keyed_page_id_hash_path(self):
        resolved = _resolved(_note(sections=(_SECTION,)))

        (unit,) = tuple(SectionStrategy().units(resolved))

        assert unit.id == "paper.lightrag-2025#findings"
        assert unit.level == "section"

    def test_text_matches_card_section_embed_input_exactly(self):
        note = _note(sections=(_SECTION,))
        resolved = _resolved(note)

        (unit,) = tuple(SectionStrategy().units(resolved))

        assert unit.text == card.section_embed_input(note.frontmatter.title, _SECTION)

    def test_yields_units_in_the_notes_section_order(self):
        second = Section(heading="Approach", level=2, path="approach", canon="approach", body="How it works.", ord=1)
        resolved = _resolved(_note(sections=(_SECTION, second)))

        units = list(SectionStrategy().units(resolved))

        assert [unit.id for unit in units] == ["paper.lightrag-2025#findings", "paper.lightrag-2025#approach"]


class TestClaimStrategy:
    """One unit per resolved claim, wrapping `card.claim_embed_input` verbatim."""

    def test_level_is_claim(self):
        assert ClaimStrategy().level == "claim"

    def test_a_claimless_note_yields_no_units(self):
        resolved = _resolved(_note())

        assert list(ClaimStrategy().units(resolved)) == []

    def test_yields_one_unit_per_claim_keyed_page_id_hash_block_id(self):
        resolved = _resolved(_note(claims=(_CLAIM,)))

        (unit,) = tuple(ClaimStrategy().units(resolved))

        assert unit.id == "paper.lightrag-2025#c1"
        assert unit.level == "claim"

    def test_text_matches_card_claim_embed_input_exactly(self):
        note = _note(claims=(_CLAIM,))
        resolved = _resolved(note)

        (unit,) = tuple(ClaimStrategy().units(resolved))

        assert unit.text == card.claim_embed_input(note.frontmatter.title, resolved.claims[0], claim_fields=())
        assert "[ultradomain; win-rate; hybrid mode]" in unit.text

    def test_the_declared_field_order_reaches_the_embed_text(self):
        resolved = _resolved(_note(claims=(_CLAIM,)))

        (unit,) = tuple(ClaimStrategy(claim_fields=("condition", "metric", "on_dataset")).units(resolved))

        assert unit.text.endswith("[win-rate; ultradomain; hybrid mode]")


class TestSourceChunkStrategy:
    """One unit per numbered-heading chunk, wrapping `card.raw_chunk_embed_input` verbatim."""

    def test_level_is_chunk(self):
        assert SourceChunkStrategy().level == "chunk"

    def test_yields_one_unit_per_chunk_keyed_source_key_colon_ord(self):
        mirror = _mirror()

        (unit,) = tuple(SourceChunkStrategy().units(mirror))

        assert unit.id == "K:0"
        assert unit.level == "chunk"

    def test_text_matches_card_raw_chunk_embed_input_exactly(self):
        mirror = _mirror()

        (unit,) = tuple(SourceChunkStrategy().units(mirror))

        assert unit.text == card.raw_chunk_embed_input("LightRAG", section_path="s1", heading="1 Intro", body="Short body.")

    def test_falls_back_to_the_source_key_when_frontmatter_has_no_title(self):
        mirror = MirrorDoc(source_key="Z1", frontmatter={}, body="preamble text", text="preamble text", collection="research", path="s.md")

        (unit,) = tuple(SourceChunkStrategy().units(mirror))

        assert unit.text.startswith("Z1 >")

    def test_the_tail_of_a_section_over_the_token_budget_reaches_an_embed_input(self):
        mirror = _mirror(body="## 1 Intro\n\n" + " ".join(f"w{index}" for index in range(20_000)))

        units = tuple(SourceChunkStrategy().units(mirror))

        assert "w19999" in units[-1].text

    def test_yields_one_unit_per_chunk_under_the_chunk_sizes_it_holds(self):
        sizes = ChunkSizes(target_tokens=40, max_tokens=80)
        mirror = _mirror(body="\n\n".join(f"Paragraph {index} records the tide at the harbour wall today." for index in range(20)))

        units = tuple(SourceChunkStrategy(sizes).units(mirror))

        assert len(units) > 1
        assert [unit.id for unit in units] == [f"K:{chunk.ord}" for chunk in chunk_mirror(mirror.text, sizes)]

    def test_yields_one_unit_per_chunk_the_mirrors_cut_plans_cut(self):
        sizes = ChunkSizes(target_tokens=40, max_tokens=80)
        paragraphs = [f"Paragraph {index} records the tide at the harbour wall today." for index in range(20)]
        body = "\n\n".join(paragraphs)
        mirror = replace(_mirror(body=body), cut_plans={content_hash(body): (6, 11, 16)})

        units = tuple(SourceChunkStrategy(sizes).units(mirror))

        assert [unit.text for unit in units] == [
            card.raw_chunk_embed_input("LightRAG", section_path=None, heading=None, body="\n\n".join(paragraphs[first:end]))
            for first, end in ((0, 5), (5, 10), (10, 15), (15, 20))
        ]


class TestNoteEmbedStrategies:
    def test_select_returns_the_registered_strategy_by_name(self):
        fake = FakeNoteStrategy()
        vocabulary = NoteEmbedStrategies(by_key={"custom": fake})

        assert vocabulary.select("custom") is fake

    def test_accepts_a_structural_fake_without_cast(self):
        resolved = _resolved(_note())
        fake = FakeNoteStrategy()
        vocabulary = NoteEmbedStrategies(by_key={"custom": fake})

        (unit,) = tuple(vocabulary.select("custom").units(resolved))

        assert unit.text == "custom text"
        assert fake.calls == [resolved]

    def test_select_raises_keyerror_for_an_unregistered_name(self):
        vocabulary = NoteEmbedStrategies(by_key={})

        with pytest.raises(KeyError):
            vocabulary.select("page-card")


class TestSourceEmbedStrategies:
    def test_select_returns_the_registered_strategy_by_name(self):
        fake = FakeSourceStrategy()
        vocabulary = SourceEmbedStrategies(by_key={"custom": fake})

        assert vocabulary.select("custom") is fake

    def test_select_raises_keyerror_for_an_unregistered_name(self):
        vocabulary = SourceEmbedStrategies(by_key={})

        with pytest.raises(KeyError):
            vocabulary.select("source-chunk")


class TestBuildNoteEmbedStrategies:
    def test_default_registers_exactly_the_three_stock_note_strategies(self):
        vocabulary = build_note_embed_strategies()

        assert set(vocabulary.by_key) == {"page-card", "section", "claim"}
        assert isinstance(vocabulary.select("page-card"), PageCardStrategy)
        assert isinstance(vocabulary.select("section"), SectionStrategy)
        assert isinstance(vocabulary.select("claim"), ClaimStrategy)

    def test_overrides_add_a_new_name_alongside_the_stock_set(self):
        fake = FakeNoteStrategy()
        vocabulary = build_note_embed_strategies({"custom": fake})

        assert vocabulary.select("custom") is fake
        assert isinstance(vocabulary.select("page-card"), PageCardStrategy)

    def test_overrides_replace_a_stock_name(self):
        fake = FakeNoteStrategy()
        vocabulary = build_note_embed_strategies({"page-card": fake})

        assert vocabulary.select("page-card") is fake


class TestBuildSourceEmbedStrategies:
    def test_default_registers_exactly_the_stock_source_strategy(self):
        vocabulary = build_source_embed_strategies()

        assert set(vocabulary.by_key) == {"source-chunk"}
        assert isinstance(vocabulary.select("source-chunk"), SourceChunkStrategy)

    def test_overrides_replace_the_stock_name(self):
        fake = FakeSourceStrategy()
        vocabulary = build_source_embed_strategies({"source-chunk": fake})

        assert vocabulary.select("source-chunk") is fake

    def test_the_stock_source_strategy_cuts_by_the_chunk_sizes_given(self):
        sizes = ChunkSizes(target_tokens=40, max_tokens=80)
        mirror = _mirror(body="\n\n".join(f"Paragraph {index} records the tide at the harbour wall today." for index in range(20)))

        units = tuple(build_source_embed_strategies(chunk_sizes=sizes).select("source-chunk").units(mirror))

        assert len(units) == len(list(chunk_mirror(mirror.text, sizes)))
        assert len(units) > 1


class TestActiveLevels:
    """`active_levels` derives the level vocabulary from a spec's active strategy names."""

    def test_research_spec_yields_page_section_claim_chunk(self):
        # The research bundle's spec declares both a claim grammar and `sources.enabled=True`.
        spec = research_spec()

        assert active_levels(spec) == ("page", "section", "claim", "chunk")

    def test_no_claim_grammar_and_no_sources_yields_page_and_section_only(self):
        base = research_spec()
        spec = KnowledgeBaseSchema.model_validate(
            {
                **base.model_dump(),
                "claims": None,
                "page_types": _no_claims_page_types(),
                "sources": base.sources.model_copy(update={"enabled": False}).model_dump(),
            }
        )

        assert active_levels(spec) == ("page", "section")

    def test_sources_enabled_adds_chunk_even_without_a_claim_grammar(self):
        base = research_spec()
        spec = KnowledgeBaseSchema.model_validate(
            {**base.model_dump(), "claims": None, "page_types": _no_claims_page_types(), "sources": base.sources.model_dump()}
        )

        assert active_levels(spec) == ("page", "section", "chunk")

    def test_an_explicit_active_override_is_honored_verbatim(self):
        base = research_spec()
        spec = base.model_copy(update={"embed_strategies": EmbedStrategiesSpec(active=("page-card",))})

        assert active_levels(spec) == ("page",)

    def test_an_active_name_absent_from_both_registries_is_skipped_not_raised(self):
        base = research_spec()
        spec = base.model_copy(update={"embed_strategies": EmbedStrategiesSpec(active=("page-card", "not-a-real-strategy"))})

        assert active_levels(spec) == ("page",)

    def test_custom_registries_are_consulted_over_the_stock_defaults(self):
        spec = research_spec().model_copy(update={"embed_strategies": EmbedStrategiesSpec(active=("custom",))})
        notes = build_note_embed_strategies({"custom": FakeNoteStrategy(level="weird")})

        assert active_levels(spec, note_strategies=notes) == ("weird",)


class TestRowBuilderPluggableStrategies:
    """`RowBuilder` (`engram.indexing.service.row_builder`) actually uses an injected vocabulary override.

    The seam that lets a knowledge base with unusual structure plug in its own strategy without
    editing the indexing pipeline: `RowBuilder` picks up whatever `NoteEmbedStrategies` /
    `SourceEmbedStrategies` it is constructed with, defaulting to the stock vocabularies when
    none is given (the default path every other spec in this file exercises).
    """

    def test_a_custom_page_card_strategy_changes_the_pages_embed_input(self):
        resolved = _resolved(_note())
        fake = FakeNoteStrategy(level="page")
        builder = RowBuilder(
            "research",
            section_summary_spec=SectionSummarySpec(),
            note_strategies=build_note_embed_strategies({"page-card": fake}),
            source_key_field="", digest_link_field="",
            source_hash_field="",
            date_field="created",
        )

        builder.add_note(resolved)

        assert builder.embed_inputs["paper.lightrag-2025"] == "custom text"
        assert fake.calls == [resolved]

    def test_a_custom_source_chunk_strategy_changes_the_chunk_embed_input(self):
        mirror = _mirror()
        fake = FakeSourceStrategy()
        builder = RowBuilder(
            "research",
            section_summary_spec=SectionSummarySpec(),
            source_strategies=build_source_embed_strategies({"source-chunk": fake}),
            source_key_field="", digest_link_field="",
            source_hash_field="",
            date_field="created",
        )

        builder.add_source(mirror)

        assert builder.embed_inputs["K:custom"] == "custom chunk text"

    def test_the_default_registries_reproduce_the_stock_page_card_text(self):
        note = _note()
        resolved = _resolved(note)
        builder = RowBuilder("research", section_summary_spec=SectionSummarySpec(), source_key_field="", digest_link_field="", source_hash_field="", date_field="created")

        builder.add_note(resolved)

        assert builder.embed_inputs["paper.lightrag-2025"] == card.card_text(note, card_facets=())


    def test_a_card_prints_the_facets_the_schema_marks_for_it(self):
        note = _note()
        builder = RowBuilder(
            "research", section_summary_spec=SectionSummarySpec(), source_key_field="", digest_link_field="", source_hash_field="", date_field="created", facets=research_spec().facets
        )

        builder.add_note(_resolved(note))

        assert builder.embed_inputs["paper.lightrag-2025"].splitlines()[1] == "long-doc-qa"

    def test_a_card_prints_no_facet_line_when_no_facet_is_marked(self):
        note = _note()
        unmarked = tuple(facet.model_copy(update={"card": False}) for facet in research_spec().facets)
        builder = RowBuilder("research", section_summary_spec=SectionSummarySpec(), source_key_field="", digest_link_field="", source_hash_field="", date_field="created", facets=unmarked)

        builder.add_note(_resolved(note))

        assert builder.embed_inputs["paper.lightrag-2025"] == card.card_text(note, card_facets=())


def _no_claims_page_types() -> list[dict[str, object]]:
    """The bundled research spec's page types with `claims_enabled` forced off everywhere."""
    types = research_spec().model_dump()["page_types"]
    return [{**pt, "claims_enabled": False} for pt in types]
