from __future__ import annotations

import pydantic
import pytest

from polyfactory.factories.pydantic_factory import ModelFactory

from engram.config import ARCHETYPES
from engram.evals.shared.domain import (
    DEFAULT_DIFFICULTY_THRESHOLDS,
    AnchorHash,
    Archetype,
    DifficultyThresholds,
    ExpectedQa,
    QaCaseMeta,
    QaInput,
    RetrievalAnswer,
    Seed,
    derive_difficulty,
)


class QaCaseMetaFactory(ModelFactory[QaCaseMeta]):
    """A deterministic `QaCaseMeta` with an in-set `difficulty`; override per test."""

    anchor_page_id = "attention.is.all.you.need"
    difficulty = "easy"


class TestQaInput:
    def test_constructs_with_question_only(self):
        qa = QaInput(question="What is multi-head attention?")

        assert qa.question == "What is multi-head attention?"

    def test_task_defaults_to_none(self):
        assert QaInput(question="q").task is None

    def test_accepts_task_hint(self):
        assert QaInput(question="q", task="compare").task == "compare"

    def test_is_frozen(self):
        qa = QaInput(question="q")

        with pytest.raises(pydantic.ValidationError):
            qa.question = "mutated"


class TestExpectedQa:
    def test_constructs_with_required_fields(self):
        expected = ExpectedQa(answer="a", expected_source_ids=("p1", "p2"))

        assert expected.answer == "a"
        assert expected.expected_source_ids == ("p1", "p2")

    def test_source_ids_coerce_to_tuple(self):
        # The list arrives as raw payload data (the dataset-loading path), so validate it.
        expected = ExpectedQa.model_validate({"answer": "a", "expected_source_ids": ["p1", "p2"]})

        assert expected.expected_source_ids == ("p1", "p2")

    def test_is_frozen(self):
        expected = ExpectedQa(answer="a", expected_source_ids=("p1",))

        with pytest.raises(pydantic.ValidationError):
            expected.answer = "mutated"


class TestQaCaseMeta:
    def test_factory_produces_valid_instance(self):
        meta = QaCaseMetaFactory.build()

        assert meta.difficulty == "easy"

    @pytest.mark.parametrize("difficulty", ["easy", "medium", "hard"])
    def test_accepts_each_difficulty(self, difficulty: str):
        assert QaCaseMetaFactory.build(difficulty=difficulty).difficulty == difficulty

    def test_rejects_out_of_set_difficulty(self):
        # An out-of-set label can only arrive as raw payload data, so validate it.
        with pytest.raises(pydantic.ValidationError):
            QaCaseMeta.model_validate({"paper_id": "p", "difficulty": "trivial"})

    def test_is_frozen(self):
        meta = QaCaseMetaFactory.build()

        with pytest.raises(pydantic.ValidationError):
            meta.anchor_page_id = "mutated"


class TestRetrievalAnswer:
    def test_constructs_with_ordered_source_ids(self):
        answer = RetrievalAnswer(answer="a", cited_source_ids=["p2", "p1"])

        assert answer.answer == "a"
        assert answer.cited_source_ids == ["p2", "p1"]

    def test_is_frozen(self):
        answer = RetrievalAnswer(answer="a", cited_source_ids=["p1"])

        with pytest.raises(pydantic.ValidationError):
            answer.answer = "mutated"


class TestArchetype:
    def test_the_archetypes_are_exactly_the_five_a_knowledge_base_can_seed(self):
        assert [member.value for member in Archetype] == ["pinpoint", "survey", "trace", "synthesis", "passage"]

    def test_the_settings_know_exactly_the_question_types_the_enum_has(self):
        assert tuple(member.value for member in Archetype) == ARCHETYPES

    @pytest.mark.parametrize("member", list(Archetype))
    def test_member_value_equals_its_lowercase_name(self, member: Archetype):
        assert member.value == member.name.lower()
        assert member == member.name.lower()


class TestDifficultyThresholds:
    def test_defaults_match_the_standard_research_bands(self):
        thresholds = DifficultyThresholds()

        assert thresholds.single_source == 1
        assert thresholds.deep_path_hops == 2
        assert thresholds.wide_fanout_sources == 4
        assert thresholds.cross_section_sources == 2
        assert thresholds.cross_section_sections == 3

    def test_is_frozen(self):
        thresholds = DifficultyThresholds()

        with pytest.raises(pydantic.ValidationError):
            thresholds.wide_fanout_sources = 2

    def test_default_singleton_equals_a_fresh_default_instance(self):
        assert DifficultyThresholds() == DEFAULT_DIFFICULTY_THRESHOLDS


class TestDeriveDifficulty:
    @pytest.mark.parametrize(
        ("n_sources", "n_hops", "n_sections", "expected"),
        [
            (1, 0, 1, "easy"),  # single source, no hop
            (2, 0, 1, "medium"),  # multi-source, not cross-section, not wide, no deep path
            (3, 0, 2, "medium"),  # still under cross-section's 3-section bar
            (2, 0, 3, "hard"),  # cross-section: >=2 sources AND >=3 sections
            (4, 0, 1, "hard"),  # wide fan-out: >=4 sources
            (1, 2, 1, "hard"),  # deep path: >=2 hops (also proves the easy-guard requires not n_hops)
        ],
    )
    def test_difficulty_band(self, n_sources: int, n_hops: int, n_sections: int, expected: str):
        assert derive_difficulty(n_sources, n_hops, n_sections) == expected

    def test_omitting_thresholds_uses_the_standard_defaults(self):
        # No thresholds arg must behave identically to passing the default singleton.
        assert derive_difficulty(2, 0, 3) == derive_difficulty(2, 0, 3, DEFAULT_DIFFICULTY_THRESHOLDS)

    def test_lowered_wide_fanout_bar_makes_two_sources_hard(self):
        assert derive_difficulty(2, 0, 1, DifficultyThresholds(wide_fanout_sources=2)) == "hard"

    def test_lowered_deep_path_bar_makes_one_hop_hard(self):
        # At h=1 the easy-guard's `not n_hops` is False, so the lowered deep-path bar catches it.
        assert derive_difficulty(1, 1, 1, DifficultyThresholds(deep_path_hops=1)) == "hard"


class TestAnchorHash:
    def test_constructs_from_unit_id_and_body_hash(self):
        anchor = AnchorHash(unit_id="paper.x#^c1", body_hash="deadbeef")

        assert anchor.unit_id == "paper.x#^c1"
        assert anchor.body_hash == "deadbeef"

    def test_is_frozen(self):
        anchor = AnchorHash(unit_id="paper.x", body_hash="h")

        with pytest.raises(pydantic.ValidationError):
            anchor.body_hash = "mutated"


class TestSeed:
    def test_n_sources_counts_gold_ids(self):
        seed = Seed(archetype=Archetype.SURVEY, gold_ids=("a", "b", "c"), source_bodies=("x",), seed_key="k")

        assert seed.n_sources == 3

    def test_difficulty_matches_derive_difficulty(self):
        seed = Seed(archetype=Archetype.SURVEY, gold_ids=("a", "b"), source_bodies=("x",), seed_key="k", n_hops=1, n_sections=2)

        assert seed.difficulty == derive_difficulty(seed.n_sources, seed.n_hops, seed.n_sections)

    def test_pinpoint_seed_is_easy(self):
        seed = Seed(archetype=Archetype.PINPOINT, gold_ids=("a",), source_bodies=("x",), seed_key="k")

        assert seed.difficulty == "easy"

    def test_deep_path_seed_is_hard(self):
        seed = Seed(archetype=Archetype.TRACE, gold_ids=("a",), source_bodies=("x",), seed_key="k", n_hops=2)

        assert seed.difficulty == "hard"

    def test_defaults_for_optional_fields(self):
        seed = Seed(archetype=Archetype.PINPOINT, gold_ids=("a",), source_bodies=("x",), seed_key="k")

        assert seed.n_hops == 0
        assert seed.n_sections == 1
        assert seed.anchors == ()

    def test_is_frozen(self):
        seed = Seed(archetype=Archetype.PINPOINT, gold_ids=("a",), source_bodies=("x",), seed_key="k")

        with pytest.raises(pydantic.ValidationError):
            seed.seed_key = "mutated"


class TestQaCaseMetaArchetypeFields:
    def test_a_case_is_saved_under_anchor_page_id_and_an_old_file_still_loads(self):
        saved = QaCaseMeta(anchor_page_id="x", difficulty="easy").model_dump()
        old = QaCaseMeta.model_validate({"paper_id": "x", "difficulty": "easy"})

        assert saved["anchor_page_id"] == "x"
        assert "paper_id" not in saved
        assert old.anchor_page_id == "x"

    def test_back_compat_load_defaults_new_fields(self):
        meta = QaCaseMeta.model_validate({"paper_id": "p", "difficulty": "easy"})

        assert meta.archetype is None
        assert meta.anchors == ()

    def test_round_trips_archetype_and_anchors(self):
        meta = QaCaseMeta(
            anchor_page_id="p", difficulty="easy", archetype=Archetype.PINPOINT, anchors=(AnchorHash(unit_id="p#^c1", body_hash="abc"),)
        )

        assert meta.archetype is Archetype.PINPOINT
        assert meta.anchors[0].unit_id == "p#^c1"
        assert meta.anchors[0].body_hash == "abc"

    def test_legacy_mirrorless_load_defaults_mirror_hash_to_none(self):
        # An old (pre-mirror) YAML case carries no mirror_hash; it must still load, defaulting to None.
        meta = QaCaseMeta.model_validate({"paper_id": "p", "difficulty": "easy"})

        assert meta.mirror_hash is None

    def test_round_trips_mirror_hash(self):
        meta = QaCaseMeta(anchor_page_id="p", difficulty="easy", mirror_hash="deadbeef")

        assert meta.mirror_hash == "deadbeef"


class TestHumanJudgedCaseMeta:
    def test_a_human_judged_case_needs_no_difficulty_and_no_anchor(self):
        meta = QaCaseMeta(judgments={"4983": 2, "5836": 1})

        assert meta.difficulty is None
        assert meta.anchor_page_id is None
        assert meta.model_dump()["judgments"] == {"4983": 2, "5836": 1}

    def test_a_generated_case_carries_no_judgments(self):
        assert QaCaseMeta(anchor_page_id="p", difficulty="easy").judgments == {}
