"""Unit tests for the note-grammar model layer (`src/engram/kb/domain/note.py`).

These pin the note grammar at the *parse* level only: id-prefix discipline per
page type, frozen immutability, verbatim preservation of long-tail frontmatter
keys (`extra="allow"`), the unit link mini-grammar, type discrimination through
`parse_frontmatter`, and the claim-block required-field (`loc::`). Facets and
relation vocabulary are validated against the `VocabularyIndex` at the index
*validate* stage, not here, and validation-stage concerns (e.g. a >400-char summary)
parse fine.

All frontmatter routes through the single generic path (:func:`parse_frontmatter`
→ :func:`_parse_frontmatter_generic`), so tests construct through
`parse_frontmatter(data, spec)` against the research spec — the only supported
construction path.
"""

from __future__ import annotations

import pydantic
import pytest

from engram.kb.contracts import (
    BaseFrontmatter,
    ClaimBlock,
    KnowledgeBaseNote,
    NoteGrammarError,
    RelationSpec,
    Section,
    UnitLink,
    VocabularyIndex,
    parse_frontmatter,
)
from engram.kb.exceptions import FrontmatterError
from engram.testing.specs import research_spec

# ---------------------------------------------------------------------------
# Helpers — minimal valid data for each page_type
# ---------------------------------------------------------------------------


def _paper(id_: str = "paper.sample-2025", **kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": id_,
        "type": "paper",
        "title": "T",
        "status": "draft",
        "paper_type": "method",
        "year": 2025,
        "zotero_key": "K",
        "source_hash": "h",
    }
    base.update(kw)
    return base


def _entity(id_: str = "ent.sample", **kw: object) -> dict[str, object]:
    base: dict[str, object] = {"id": id_, "type": "entity", "title": "T", "status": "draft", "entity_type": "technique"}
    base.update(kw)
    return base


def _parse(data: dict[str, object]) -> BaseFrontmatter:
    """Parse via the single generic path using the research spec."""
    return parse_frontmatter(data, research_spec())


class TestPageTypePrefixes:
    """Every page type binds to exactly one id prefix (schema.md §Page types)."""

    @pytest.mark.parametrize(
        ("valid_id", "extra"),
        [
            ("paper.lightrag-2025", {"type": "paper", "paper_type": "method", "year": 2025, "zotero_key": "K", "source_hash": "h"}),
            ("ent.lightrag", {"type": "entity", "entity_type": "technique"}),
            ("con.knowledge-compounding", {"type": "concept"}),
            ("cmp.graph-rag-architectures", {"type": "comparison"}),
            ("pb.when-to-graph", {"type": "playbook"}),
            ("q.llm-judged-eval-validity", {"type": "question"}),
            ("moc.graph-rag", {"type": "moc"}),
            ("meta.schema", {"type": "meta"}),
        ],
    )
    def test_valid_prefix_accepted(self, valid_id: str, extra: dict[str, object]) -> None:
        data: dict[str, object] = {"id": valid_id, "title": "T", "status": "draft", **extra}
        fm = _parse(data)

        assert fm.id == valid_id
        assert fm.type == extra["type"]

    @pytest.mark.parametrize(
        ("wrong_id", "page_type", "extra"),
        [
            ("ent.lightrag", "paper", {"paper_type": "method", "year": 2025, "zotero_key": "K", "source_hash": "h"}),
            ("paper.lightrag-2025", "entity", {"entity_type": "technique"}),
            ("cmp.x", "concept", {}),
            ("con.x", "comparison", {}),
            ("q.x", "playbook", {}),
            ("pb.x", "question", {}),
            ("meta.x", "moc", {}),
            ("moc.x", "meta", {}),
        ],
    )
    def test_prefix_mismatch_rejected(self, wrong_id: str, page_type: str, extra: dict[str, object]) -> None:
        data: dict[str, object] = {"id": wrong_id, "type": page_type, "title": "T", "status": "draft", **extra}
        with pytest.raises(FrontmatterError):
            _parse(data)

    def test_missing_prefix_rejected(self) -> None:
        with pytest.raises(FrontmatterError):
            _parse(_paper(id_="lightrag-2025"))


class TestFrozenImmutability:
    def test_frontmatter_is_frozen(self) -> None:
        fm = _parse(_entity())

        with pytest.raises(pydantic.ValidationError):
            fm.title = "mutated"  # type: ignore[misc]

    def test_unit_link_is_frozen(self) -> None:
        link = UnitLink(target="lightrag")

        with pytest.raises(pydantic.ValidationError):
            link.target = "mutated"  # type: ignore[misc]

    def test_claim_block_is_frozen(self) -> None:
        claim = ClaimBlock(anchor="^c1", text="t", loc="§1", kind="observation")

        with pytest.raises(pydantic.ValidationError):
            claim.text = "mutated"  # type: ignore[misc]

    def test_section_is_frozen(self) -> None:
        section = Section(heading="Findings", level=2, path="findings", canon="findings", body="b", ord=0)

        with pytest.raises(pydantic.ValidationError):
            section.body = "mutated"  # type: ignore[misc]


class TestExtraKeysPreserved:
    """Long-tail frontmatter (citekey, venue, source_note, artifacts…) is kept verbatim.

    schema.md lists many type-specific keys (`citekey`, `venue`,
    `source_note`, `artifacts`) the plan does not promote to typed fields;
    `extra="allow"` carries them through to `pages.frontmatter` jsonb intact.
    """

    def test_unmodelled_keys_round_trip_via_model_dump(self) -> None:
        raw: dict[str, object] = {
            "id": "paper.lightrag-2025",
            "type": "paper",
            "title": "LightRAG",
            "status": "synthesized",
            "paper_type": "method",
            "year": 2025,
            "zotero_key": "8QXFXYMI",
            "source_hash": "bd72ee",
            "citekey": "guo2025lightrag",
            "venue": "arXiv",
            "source_note": "[[LightRAG - Simple and Fast Retrieval-Augmented Generation]]",
            "artifacts": {"code": "https://github.com/HKUDS/LightRAG"},
        }

        fm = _parse(raw)
        dumped = fm.model_dump()

        assert dumped["citekey"] == "guo2025lightrag"
        assert dumped["venue"] == "arXiv"
        assert dumped["artifacts"] == {"code": "https://github.com/HKUDS/LightRAG"}
        # The extra keys live on the model itself, reachable via `model_extra`
        # (pydantic's typed accessor for `extra="allow"` overflow), not just the dump.
        assert fm.model_extra is not None
        assert fm.model_extra["citekey"] == "guo2025lightrag"
        assert fm.model_extra["source_note"] == "[[LightRAG - Simple and Fast Retrieval-Augmented Generation]]"


class TestFacetValues:
    """`BaseFrontmatter.facet_values` resolves a facet key whether declared or long-tail."""

    def test_declared_tuple_field_returns_its_values(self) -> None:
        fm = _parse(_paper(tasks=["long-doc-qa", "multi-hop-qa"]))

        assert fm.facet_values("tasks") == ("long-doc-qa", "multi-hop-qa")

    def test_long_tail_scalar_extra_returns_a_one_tuple(self) -> None:
        fm = _parse(_paper(paper_type="survey"))

        assert fm.facet_values("paper_type") == ("survey",)

    def test_absent_declared_field_returns_empty_tuple(self) -> None:
        fm = _parse(_paper())

        assert fm.facet_values("domains") == ()

    def test_unknown_key_returns_empty_tuple(self) -> None:
        fm = _parse(_paper())

        assert fm.facet_values("not-a-facet") == ()


class TestUnitLinkParse:
    """The unit link mini-grammar (schema.md §Identity, Obsidian dialect)."""

    def test_bare_target(self) -> None:
        link = UnitLink.parse("[[lightrag]]")

        assert link == UnitLink(target="lightrag")

    def test_alias(self) -> None:
        link = UnitLink.parse("[[lightrag|LightRAG]]")

        assert link.target == "lightrag"
        assert link.alias == "LightRAG"
        assert link.block is None

    def test_heading(self) -> None:
        link = UnitLink.parse("[[lightrag-2025#Findings]]")

        assert link.target == "lightrag-2025"
        assert link.heading == "Findings"
        assert link.block is None

    def test_block(self) -> None:
        link = UnitLink.parse("[[lightrag-2025#^c3]]")

        assert link.target == "lightrag-2025"
        assert link.block == "c3"
        assert link.heading is None

    def test_alias_with_block(self) -> None:
        link = UnitLink.parse("[[lightrag-2025#^c3|claim 3]]")

        assert link.target == "lightrag-2025"
        assert link.block == "c3"
        assert link.alias == "claim 3"

    def test_whitespace_tolerated(self) -> None:
        link = UnitLink.parse("[[ lightrag | LightRAG ]]")

        assert link.target == "lightrag"
        assert link.alias == "LightRAG"

    def test_unbracketed_string_rejected(self) -> None:
        with pytest.raises(NoteGrammarError):
            UnitLink.parse("lightrag")

    def test_empty_target_rejected(self) -> None:
        with pytest.raises(NoteGrammarError):
            UnitLink.parse("[[]]")

    def test_two_adjacent_links_are_rejected_not_merged(self) -> None:
        # Regression: a value carrying two links must never parse as one link whose body
        # swallows the `]], [[` in between (silent target corruption).
        with pytest.raises(NoteGrammarError):
            UnitLink.parse("[[f1-score]], [[exact-match]]")

    def test_three_adjacent_links_are_rejected_not_merged(self) -> None:
        with pytest.raises(NoteGrammarError):
            UnitLink.parse("[[a]], [[b]], [[c]]")


class TestFrontmatterDiscrimination:
    """`parse_frontmatter` dispatches to the single generic path for all page_types."""

    def test_parse_frontmatter_research_kind_via_generic(self) -> None:
        """parse_frontmatter routes all research page_types through the generic path."""
        fm = parse_frontmatter(_paper(), research_spec())

        assert isinstance(fm, BaseFrontmatter)
        assert fm.type == "paper"
        assert fm.model_extra is not None
        assert fm.model_extra["year"] == 2025

    def test_parse_frontmatter_returns_paper_fields(self) -> None:
        fm = parse_frontmatter(_paper(id_="paper.lightrag-2025", title="LightRAG", year=2024, zotero_key="K", source_hash="h"), spec=research_spec())

        assert fm.type == "paper"
        assert fm.model_extra is not None
        assert fm.model_extra["year"] == 2024

    def test_parse_frontmatter_returns_question_fields(self) -> None:
        fm = parse_frontmatter({"id": "q.x", "type": "question", "title": "Q?", "status": "draft", "question_status": "open"}, spec=research_spec())

        assert fm.type == "question"
        assert fm.model_extra is not None
        assert fm.model_extra["question_status"] == "open"

    def test_parse_frontmatter_unknown_type_raises(self) -> None:
        with pytest.raises(FrontmatterError):
            parse_frontmatter({"id": "x.y", "type": "spaceship", "title": "T", "status": "draft"}, spec=research_spec())

    def test_parse_frontmatter_missing_type_raises(self) -> None:
        with pytest.raises(FrontmatterError):
            parse_frontmatter({"id": "paper.x", "title": "T", "status": "draft"}, spec=research_spec())

    def test_parse_frontmatter_no_spec_defaults_to_research(self) -> None:
        """parse_frontmatter with no spec defaults to the bundled research spec."""
        fm = parse_frontmatter({"id": "ent.lightrag", "type": "entity", "title": "LightRAG", "status": "draft", "entity_type": "technique"}, spec=research_spec())

        assert fm.type == "entity"
        assert isinstance(fm, BaseFrontmatter)


class TestPaperRelationCoercion:
    """Page-level edges arrive as `"[[target]]"` strings and parse to `UnitLink`."""

    def test_relation_strings_become_unit_links(self) -> None:
        fm = parse_frontmatter(
            _paper(
                id_="paper.lightrag-2025",
                title="LightRAG",
                proposes=["[[lightrag]]", "[[dual-level-retrieval]]"],
                builds_on=["[[graphrag-2024]]"],
            ),
            spec=research_spec(),
        )

        assert fm.type == "paper"
        assert fm.model_extra is not None
        assert fm.model_extra["proposes"] == (UnitLink(target="lightrag"), UnitLink(target="dual-level-retrieval"))
        assert fm.model_extra["builds_on"] == (UnitLink(target="graphrag-2024"),)

    def test_facet_tuples_default_empty(self) -> None:
        fm = _parse(_entity())

        assert fm.facet_values("tasks") == ()
        assert fm.aliases == ()

    def test_a_facet_written_as_one_string_reads_as_one_value(self) -> None:
        fm = _parse(_entity(tasks="retrieval"))

        assert fm.facet_values("tasks") == ("retrieval",)

    def test_the_frontmatter_model_declares_no_knowledge_base_facets(self) -> None:
        assert not {"tasks", "domains", "stages", "modalities", "scale_regimes"} & set(BaseFrontmatter.model_fields)


class TestClaimBlock:
    """Claim grammar: `loc::` is required; quote/qualifiers/edges are optional."""

    def test_loc_is_required(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            ClaimBlock(anchor="^c1", text="t", kind="benchmark-result")  # type: ignore[call-arg]

    def test_minimal_claim(self) -> None:
        claim = ClaimBlock(anchor="^c1", text="LightRAG wins", loc="§4.2", kind="benchmark-result")

        assert claim.loc == "§4.2"
        assert claim.quote is None
        assert claim.edges == ()
        assert claim.links == {}

    def test_edges_and_qualifiers(self) -> None:
        claim = ClaimBlock(
            anchor="^c1",
            text="LightRAG beats GraphRAG",
            loc="§4.2, Table 1",
            quote="LightRAG consistently outperforms GraphRAG",
            kind="benchmark-result",
            subject=UnitLink(target="lightrag"),
            edges=(("outperforms", UnitLink(target="graphrag")),),
            fields={"condition": "hybrid mode"},
            links={"on_dataset": (UnitLink(target="ultradomain"),), "metric": (UnitLink(target="llm-judged-win-rate"),)},
        )

        assert claim.edges == (("outperforms", UnitLink(target="graphrag")),)
        assert claim.links["on_dataset"] == (UnitLink(target="ultradomain"),)
        assert claim.links["metric"] == (UnitLink(target="llm-judged-win-rate"),)

    def test_derived_table_flag(self) -> None:
        claim = ClaimBlock(anchor="^c2", text="tie", loc="§4.2, Table 1", kind="negative-result", fields={"derived": "table"})

        assert claim.fields["derived"] == "table"

    def test_declared_fields_and_links_default_to_empty(self) -> None:
        claim = ClaimBlock(anchor="^c1", text="t", loc="§1")

        assert claim.fields == {}
        assert claim.links == {}

    def test_declared_fields_and_links_are_kept_by_name(self) -> None:
        claim = ClaimBlock(
            anchor="^c1",
            text="t",
            loc="§1",
            fields={"jurisdiction": "EU"},
            links={"applies_to": (UnitLink(target="a"), UnitLink(target="b"))},
        )

        assert claim.fields == {"jurisdiction": "EU"}
        assert claim.links == {"applies_to": (UnitLink(target="a"), UnitLink(target="b"))}


class TestSection:
    def test_level_must_be_two_or_three(self) -> None:
        # H1/H4+ are not retrieval units; validated via model_validate (the parser path)
        # so the out-of-range level is data, not a typed literal pyright would reject.
        with pytest.raises(pydantic.ValidationError):
            Section.model_validate({"heading": "X", "level": 1, "path": "x", "canon": None, "body": "b", "ord": 0})

    def test_canon_optional(self) -> None:
        section = Section(heading="Custom H3", level=3, path="approach.custom_h3", canon=None, body="b", ord=2)

        assert section.canon is None
        assert section.level == 3


class TestMemoryNote:
    def test_assembles_typed_parts(self) -> None:
        fm = parse_frontmatter(_paper(id_="paper.lightrag-2025", title="LightRAG"), spec=research_spec())
        note = KnowledgeBaseNote(
            frontmatter=fm,
            sections=(Section(heading="TL;DR", level=2, path="tl_dr", canon="tl_dr", body="b", ord=0),),
            claims=(ClaimBlock(anchor="^c1", text="t", loc="§1", kind="observation"),),
            source_path="Memory/Papers/lightrag-2025.md",
        )

        assert note.frontmatter.id == "paper.lightrag-2025"
        assert len(note.sections) == 1
        assert len(note.claims) == 1

    def test_is_frozen(self) -> None:
        fm = _parse(_entity())
        note = KnowledgeBaseNote(frontmatter=fm, sections=(), claims=(), source_path="Memory/Entities/x.md")

        with pytest.raises(pydantic.ValidationError):
            note.source_path = "elsewhere.md"  # type: ignore[misc]


class TestVocabularyIndex:
    def test_holds_vocabulary_maps(self) -> None:
        index = VocabularyIndex(
            entity_types={"technique": "A method"},
            relations={"outperforms": RelationSpec(domain="entity", range="entity", description="Comparative win")},
            facets={"tasks": {"long-doc-qa": "Long document QA"}},
            claim_types={"benchmark-result": "Quantitative result"},
        )

        assert index.entity_types["technique"] == "A method"
        assert index.relations["outperforms"].domain == "entity"
        assert index.facets["tasks"]["long-doc-qa"] == "Long document QA"

    def test_is_frozen(self) -> None:
        index = VocabularyIndex(entity_types={}, relations={}, facets={}, claim_types={})

        with pytest.raises(pydantic.ValidationError):
            index.entity_types = {"x": "y"}  # type: ignore[misc]


class TestLintConcernsNotEnforcedAtParse:
    """Validation-stage rules (summary length, vocabulary membership) do not fire at parse.

    The frontmatter contract caps `summary` at 400 chars and the validate stage
    checks facet/relation vocabulary — but the *model* layer must accept these so a
    not-yet-clean note can still be parsed and reported on.
    """

    def test_oversized_summary_parses(self) -> None:
        long_summary = "x" * 400

        fm = _parse(_entity(summary=long_summary))

        assert fm.summary is not None
        assert len(fm.summary) == 400

    def test_unregistered_facet_value_parses(self) -> None:
        # "not-a-real-task" is not in _meta/vocabulary/facets.md — but parse accepts it;
        # the validate stage flags it, the model does not.
        fm = _parse(_entity(tasks=["not-a-real-task"]))

        assert fm.facet_values("tasks") == ("not-a-real-task",)


class TestFactoriesBuildValidInstances:
    """The polyfactory fixtures must respect the id-prefix validator, not trip it.

    A naive `ModelFactory` would generate a random `id`/`type` pair and fail
    the prefix check — this guards that the factories pin a coherent pair so other
    slices can `.build()` `KnowledgeBaseNote` fixtures freely.
    """

    def test_paper_frontmatter_factory(self) -> None:
        from engram.testing.factories.note import PaperFrontmatterFactory

        fm = PaperFrontmatterFactory.build()

        assert isinstance(fm, BaseFrontmatter)
        assert fm.id.startswith("paper.")

    def test_unit_link_factory_builds_bare_link(self) -> None:
        from engram.testing.factories.note import UnitLinkFactory

        link = UnitLinkFactory.build()

        assert isinstance(link, UnitLink)
        assert link.alias is None
        assert link.block is None

    def test_claim_block_factory_has_required_loc(self) -> None:
        from engram.testing.factories.note import ClaimBlockFactory

        claim = ClaimBlockFactory.build()

        assert isinstance(claim, ClaimBlock)
        assert claim.loc

    def test_memory_note_factory_assembles(self) -> None:
        from engram.testing.factories.note import KnowledgeBaseNoteFactory

        note = KnowledgeBaseNoteFactory.build()

        assert isinstance(note, KnowledgeBaseNote)
        assert isinstance(note.frontmatter, BaseFrontmatter)
        assert note.frontmatter.id.startswith("paper.")
