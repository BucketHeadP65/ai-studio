"""Unit tests for gold-from-seed golden-QA generation.

The gold ids come from the structural `Seed`, never the model: the `PydanticAiDrafter` only writes
question/answer/quotes, and the `accept` gate keeps a draft iff every support quote is verbatim in
the seed's source bodies, so a kept case is quote-checked by construction. Each gate is tested directly
with hand-built drafts; the agent is driven by a network-free `TestModel` whose `custom_output_args`
force an exact batch of drafts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from engram.evals.generation.contracts import AuthoredCase, SeedCaseDraft, accept, mirror_hash
from engram.evals.generation.domain.drafting import SeedCaseDrafts
from engram.evals.generation.domain.mirror import passage_bodies
from engram.evals.generation.infrastructure.drafter import PydanticAiDrafter
from engram.evals.shared.domain import AnchorHash, Archetype, DifficultyThresholds, Seed

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

# --- Gold-from-seed generation ------------------------------------------------------------------

# Two source bodies the seed's quotes are verified against; the mirror is their newline join.
_BODY_A = "Graph index construction incurs substantial token cost during entity and relation extraction."
_BODY_B = "The map-reduce summarization step dominates query-time latency for global questions."


def _seed_draft(*, quotes: list[str], question: str = "Q", answer: str = "A") -> SeedCaseDraft:
    return SeedCaseDraft(question=question, answer=answer, support_quotes=quotes)


def _pinpoint_seed() -> Seed:
    """A single-source pinpoint seed (one gold id, no hop, one section -> easy)."""
    return Seed(
        archetype=Archetype.PINPOINT,
        gold_ids=("paper.x",),
        source_bodies=(_BODY_A,),
        seed_key="pinpoint:paper.x",
        anchors=(AnchorHash(unit_id="paper.x", body_hash="h-a"),),
    )


def _cross_section_seed() -> Seed:
    """A cross-section synthesis seed (two gold ids spanning three sections -> hard by default)."""
    return Seed(
        archetype=Archetype.SYNTHESIS,
        gold_ids=("paper.x", "paper.y"),
        source_bodies=(_BODY_A, _BODY_B),
        seed_key="synthesis:extends:paper.x",
        n_sections=3,
        anchors=(AnchorHash(unit_id="paper.x", body_hash="h-a"), AnchorHash(unit_id="paper.y", body_hash="h-b")),
    )


# One section of a page, and the page's card text, which a passage seed never carries.
_SECTION_BODY = "We trained with a batch size of 256 for 40 epochs on a single node with eight accelerators."
_CARD_TEXT = "A study of how graph index construction cost scales with corpus size."


def _passage_seed() -> Seed:
    """A passage seed: one section of one page (one gold id, no hop, one section -> easy)."""
    return Seed(
        archetype=Archetype.PASSAGE,
        gold_ids=("paper.x",),
        source_bodies=passage_bodies(_SECTION_BODY),
        seed_key="passage:paper.x#setup",
        anchors=(AnchorHash(unit_id="paper.x", body_hash="h-a"),),
    )


class TestAcceptFromSeed:
    def test_keeps_verbatim_draft_and_grounds_in_seed(self):
        seed = _pinpoint_seed()
        draft = _seed_draft(quotes=["substantial token cost"])

        case = accept(draft, seed=seed)

        assert isinstance(case, AuthoredCase)
        # The gold ids are the seed's, never the model's, and the meta carries the structural provenance.
        assert case.expected.expected_source_ids == seed.gold_ids
        assert case.meta.archetype is seed.archetype
        assert case.meta.anchors == seed.anchors
        assert case.meta.difficulty == seed.difficulty
        assert case.meta.anchor_page_id == seed.gold_ids[0]
        assert case.question.question == "Q"
        assert case.expected.answer == "A"

    def test_stamps_mirror_hash_matching_the_recipe(self):
        # accept stamps the mirror hash of the exact bodies it verified quotes against (the mirror era).
        seed = _cross_section_seed()
        draft = _seed_draft(quotes=["substantial token cost", "map-reduce summarization step"])

        case = accept(draft, seed=seed)

        assert case is not None
        assert case.meta.mirror_hash == mirror_hash(seed.source_bodies)

    def test_keeps_draft_under_whitespace_normalization(self):
        seed = _pinpoint_seed()
        draft = _seed_draft(quotes=["substantial   token\ncost"])

        assert accept(draft, seed=seed) is not None

    def test_rejects_empty_support_quotes(self):
        seed = _pinpoint_seed()
        draft = _seed_draft(quotes=[])

        assert accept(draft, seed=seed) is None

    def test_rejects_when_any_quote_is_fabricated(self):
        seed = _pinpoint_seed()
        # The first quote is verbatim; the second is invented, so the whole draft is dropped.
        draft = _seed_draft(quotes=["substantial token cost", "a phrase that is nowhere in any source body"])

        assert accept(draft, seed=seed) is None

    def test_multi_source_seed_uses_all_gold_ids_and_derives_hard(self):
        seed = _cross_section_seed()
        # One verbatim quote from each source body.
        draft = _seed_draft(quotes=["substantial token cost", "map-reduce summarization step"])

        case = accept(draft, seed=seed)

        assert case is not None
        assert case.expected.expected_source_ids == ("paper.x", "paper.y")
        # 2 sources across 3 sections crosses the default cross-section band -> hard.
        assert case.meta.difficulty == "hard"

    def test_custom_thresholds_flip_the_derived_difficulty(self):
        seed = _cross_section_seed()
        draft = _seed_draft(quotes=["substantial token cost", "map-reduce summarization step"])
        # Raise the cross-section section cutoff above the seed's 3 sections: the case is no longer
        # hard, proving difficulty is derived through the injected thresholds.
        lenient = DifficultyThresholds(cross_section_sections=4)

        case = accept(draft, seed=seed, thresholds=lenient)

        assert case is not None
        assert case.meta.difficulty == "medium"


class TestAcceptPassage:
    def test_gold_is_the_section_page_and_the_quotes_check_against_the_section_body(self):
        seed = _passage_seed()
        draft = _seed_draft(quotes=["a batch size of 256 for 40 epochs"])

        case = accept(draft, seed=seed)

        assert case is not None
        assert case.expected.expected_source_ids == ("paper.x",)
        assert case.meta.anchor_page_id == "paper.x"
        assert case.meta.archetype is Archetype.PASSAGE
        assert case.meta.difficulty == "easy"
        assert case.meta.mirror_hash == mirror_hash((_SECTION_BODY,))

    def test_a_quote_from_the_page_card_text_is_refused(self):
        draft = _seed_draft(quotes=["graph index construction cost"])

        assert accept(draft, seed=_passage_seed()) is None


class TestPydanticAiDrafter:
    async def test_draft_returns_seed_grounded_drafts_network_free(self):
        seed = _pinpoint_seed()
        drafts = SeedCaseDrafts(
            cases=[
                _seed_draft(quotes=["substantial token cost"], question="Q1", answer="A1"),
                _seed_draft(quotes=["entity and relation extraction"], question="Q2", answer="A2"),
            ]
        )
        model = TestModel(call_tools=[], custom_output_args=drafts.model_dump())
        drafter = PydanticAiDrafter(model)

        out = await drafter.draft(seed, instructions="write grounded cases", target=2)

        assert [d.question for d in out] == ["Q1", "Q2"]
        assert all(isinstance(d, SeedCaseDraft) for d in out)
        assert out[0].support_quotes == ["substantial token cost"]

    async def test_the_archetype_prompt_adds_to_the_grounding_rules_and_never_replaces_them(self):
        seen: list[str | None] = []
        drafts = SeedCaseDrafts(cases=[_seed_draft(quotes=["substantial token cost"], question="Q1", answer="A1")])

        def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            seen.append(info.instructions)
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, drafts.model_dump())])

        await PydanticAiDrafter(FunctionModel(respond)).draft(_pinpoint_seed(), instructions="Pin down one fact.", target=1)

        [instructions] = seen
        assert instructions is not None
        assert "copied VERBATIM" in instructions
        assert instructions.endswith("Pin down one fact.")
        assert "research" not in instructions
