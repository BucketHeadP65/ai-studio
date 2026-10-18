"""Unit smoke for the pure parts of the synthetic knowledge base builder.

Covers only what needs no database: `SyntheticKnowledgeBaseSpec` parameter validation, the Zipf weight maths, the
skewed count distribution, the deterministic facet slice, and `plan_knowledge_base`'s determinism and
ground-truth arithmetic. FK integrity and the reader-vs-ground-truth census live in the integration
tier (`tests/integration/evals/test_coverage_scale.py`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pydantic import ValidationError

from engram.evals.generation.contracts import MIN_PASSAGE_CHARS
from engram.evals.shared.domain import DEFAULT_DIFFICULTY_THRESHOLDS, Archetype
from engram.testing.infrastructure.synthetic_knowledge_base import (
    SyntheticKnowledgeBaseSpec,
    _all_edge_rows,
    _all_page_rows,
    _all_section_rows,
    _budget,
    _facets_for,
    _skewed_counts,
    plan_knowledge_base,
    synthetic_schema,
    zipf_weights,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# A small, fast spec for the arithmetic assertions (still exercises every archetype family).
_SMALL = SyntheticKnowledgeBaseSpec(n_pages=800, seed=7)


# --- SyntheticKnowledgeBaseSpec parameter validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        lambda: SyntheticKnowledgeBaseSpec(n_pages=10),  # below the floor
        lambda: SyntheticKnowledgeBaseSpec(facets_per_page=-1),  # negative facet count
        lambda: SyntheticKnowledgeBaseSpec(zipf_exponent=0.0),  # non-positive exponent
        lambda: SyntheticKnowledgeBaseSpec(survey_max_fanin=2),  # cap below the structural floor
        lambda: SyntheticKnowledgeBaseSpec(synthesis_fanout=1),  # below the synthesis floor
        lambda: SyntheticKnowledgeBaseSpec(trace_chain_length=2),  # too short for a 2-hop path
    ],
)
def test_invalid_specs_are_rejected(build: Callable[[], SyntheticKnowledgeBaseSpec]):
    with pytest.raises(ValidationError):
        build()


def test_max_cap_below_average_is_rejected():
    with pytest.raises(ValidationError):
        SyntheticKnowledgeBaseSpec(survey_fanin=10, survey_max_fanin=5)


def test_all_zero_shares_rejected():
    with pytest.raises(ValidationError):
        SyntheticKnowledgeBaseSpec(pinpoint_share=0.0, survey_share=0.0, trace_share=0.0, synthesis_share=0.0, passage_share=0.0)


def test_empty_anchor_kinds_rejected():
    with pytest.raises(ValidationError):
        SyntheticKnowledgeBaseSpec(anchor_page_types=())


def test_defaults_are_valid_and_frozen():
    spec = SyntheticKnowledgeBaseSpec()
    assert spec.n_pages == 5000
    with pytest.raises(ValidationError):
        spec.n_pages = 1  # frozen


# --- Zipf weights -------------------------------------------------------------------------------


def test_zipf_weights_sum_to_one_and_descend():
    weights = zipf_weights(20, 1.15)
    assert weights == tuple(sorted(weights, reverse=True))  # rank 1 is heaviest
    assert sum(weights) == pytest.approx(1.0)


def test_zipf_weights_empty_for_non_positive_n():
    assert zipf_weights(0, 1.2) == ()
    assert zipf_weights(-3, 1.2) == ()


def test_larger_exponent_concentrates_the_head():
    flat = zipf_weights(50, 0.5)
    steep = zipf_weights(50, 2.0)
    # A steeper exponent puts more mass on the top rank.
    assert steep[0] > flat[0]


# --- skewed counts ------------------------------------------------------------------------------


def test_skewed_counts_are_clamped_and_head_heavy():
    counts = _skewed_counts(n_groups=40, average=4, floor=3, cap=16, exponent=1.15)
    assert len(counts) == 40
    assert all(3 <= count <= 16 for count in counts)
    assert counts[0] == 16  # the head saturates at the cap
    assert counts[0] > counts[-1]  # skewed: the head attracts most members
    assert min(counts) == 3  # the tail falls back to the floor


def test_skewed_counts_are_deterministic():
    first = _skewed_counts(n_groups=25, average=5, floor=3, cap=12, exponent=1.3)
    second = _skewed_counts(n_groups=25, average=5, floor=3, cap=12, exponent=1.3)
    assert first == second


def test_skewed_counts_empty_for_no_groups():
    assert _skewed_counts(n_groups=0, average=4, floor=3, cap=16, exponent=1.15) == ()


# --- facet slice --------------------------------------------------------------------------------


def test_facets_are_deterministic_and_bounded():
    spec = SyntheticKnowledgeBaseSpec(facet_vocab=("a", "b", "c", "d"), facets_per_page=2)
    assert _facets_for("pp.000001", spec) == _facets_for("pp.000001", spec)
    assert len(_facets_for("pp.000001", spec)) == 2
    assert all(facet in spec.facet_vocab for facet in _facets_for("pp.000001", spec))


def test_facets_empty_when_disabled():
    assert _facets_for("pp.000001", SyntheticKnowledgeBaseSpec(facets_per_page=0)) == ()


def test_facets_capped_at_vocab_size():
    spec = SyntheticKnowledgeBaseSpec(facet_vocab=("only",), facets_per_page=5)
    assert _facets_for("sv.hub.00001", spec) == ("only",)


# --- plan determinism ---------------------------------------------------------------------------


def test_plan_is_deterministic_for_the_same_spec():
    first = plan_knowledge_base(_SMALL)
    second = plan_knowledge_base(_SMALL)
    assert first.ground_truth == second.ground_truth
    assert first.pinpoints == second.pinpoints
    assert first.surveys == second.surveys
    assert first.total_pages == second.total_pages
    assert first.directed_edges == second.directed_edges


def test_a_different_seed_reshuffles_anchor_page_types():
    one = plan_knowledge_base(SyntheticKnowledgeBaseSpec(n_pages=800, seed=1))
    two = plan_knowledge_base(SyntheticKnowledgeBaseSpec(n_pages=800, seed=2))
    # The kind assignment is seed-driven, so the pinpoint anchor-kind sequence differs.
    assert [pinpoint.page_type for pinpoint in one.pinpoints] != [pinpoint.page_type for pinpoint in two.pinpoints]


# --- ground-truth arithmetic --------------------------------------------------------------------


def test_budget_splits_sum_to_about_n_pages():
    budget = _budget(_SMALL)
    assert set(budget) == set(Archetype)
    assert sum(budget.values()) == pytest.approx(_SMALL.n_pages, abs=3)


def test_pinpoint_ground_truth_counts_claims_by_page_type():
    plan = plan_knowledge_base(_SMALL)
    expected: dict[str, int] = {}
    for pinpoint in plan.pinpoints:
        expected[pinpoint.page_type] = expected.get(pinpoint.page_type, 0) + 1
    actual = {count.stratum.anchor_page_type: count.available for count in plan.ground_truth if count.stratum.archetype is Archetype.PINPOINT}
    assert actual == expected
    assert all(count.stratum.difficulty == "easy" for count in plan.ground_truth if count.stratum.archetype is Archetype.PINPOINT)


def test_survey_strata_are_always_hard():
    plan = plan_knowledge_base(_SMALL)
    survey = [count for count in plan.ground_truth if count.stratum.archetype is Archetype.SURVEY]
    assert survey, "the knowledge base must plan survey hubs"
    assert all(count.stratum.difficulty == "hard" for count in survey)
    assert sum(count.available for count in survey) == len(plan.surveys)


def test_trace_ground_truth_equals_two_hop_path_count():
    plan = plan_knowledge_base(_SMALL)
    trace = [count for count in plan.ground_truth if count.stratum.archetype is Archetype.TRACE]
    expected_paths = sum(max(len(chain.node_ids) - 2, 0) for chain in plan.traces)
    assert sum(count.available for count in trace) == expected_paths == plan.total_trace_paths
    assert all(count.stratum.difficulty == "hard" for count in trace)


def test_synthesis_ground_truth_matches_derive_difficulty():
    from engram.evals.shared.domain import derive_difficulty

    plan = plan_knowledge_base(_SMALL)
    expected: dict[tuple[str, str], int] = {}
    for synthesis in plan.syntheses:
        band = derive_difficulty(len(synthesis.target_ids), 0, max(synthesis.n_sections, 1), DEFAULT_DIFFICULTY_THRESHOLDS)
        key = (band, synthesis.source_kind)
        expected[key] = expected.get(key, 0) + 1
    actual: dict[tuple[str, str], int] = {}
    for count in plan.ground_truth:
        if count.stratum.archetype is Archetype.SYNTHESIS:
            actual[(count.stratum.difficulty, count.stratum.anchor_page_type)] = count.available
    assert actual == expected


def test_passage_ground_truth_counts_one_long_section_per_passage_page():
    plan = plan_knowledge_base(_SMALL)
    assert plan.passages, "the knowledge base must plan passage pages"
    expected: dict[str, int] = {}
    for passage in plan.passages:
        expected[passage.page_type] = expected.get(passage.page_type, 0) + 1
    passage_counts = [count for count in plan.ground_truth if count.stratum.archetype is Archetype.PASSAGE]
    assert {count.stratum.anchor_page_type: count.available for count in passage_counts} == expected
    assert all(count.stratum.difficulty == "easy" for count in passage_counts)


def test_only_passage_pages_carry_a_section_long_enough_to_draw():
    plan = plan_knowledge_base(_SMALL)
    rows = _all_section_rows(plan, "c")

    long_pages = [row["page_id"] for row in rows if len(row["body"]) >= MIN_PASSAGE_CHARS]
    assert long_pages == [passage.page_id for passage in plan.passages]
    assert plan.total_sections == len(rows)


def test_metrics_are_internally_consistent():
    plan = plan_knowledge_base(_SMALL)
    assert plan.total_claims == len(plan.pinpoints)
    assert plan.total_edges == len(plan.directed_edges)
    assert plan.total_pages > 0


# --- neutral defaults ------------------------------------------------------------------------------------------


def test_the_default_plan_names_no_research_vocabulary():
    plan = plan_knowledge_base(_SMALL)
    pages = _all_page_rows(plan, "c")

    assert {row["page_type"] for row in pages} <= {"note", "topic", "guide"}
    assert {row["rel"] for row in _all_edge_rows(plan, "c")} <= {"links_to", "summarizes"}
    assert set(_SMALL.facet_vocab) == {"alpha", "beta", "gamma", "delta", "epsilon", "zeta"}


def test_planned_facets_are_written_to_the_facets_column():
    plan = plan_knowledge_base(_SMALL)

    for row in _all_page_rows(plan, "c"):
        assert set(row) >= {"facets"}
        assert not set(row) & {"tasks", "domains"}
        assert row["facets"] == {"tags": list(_facets_for(row["id"], _SMALL))}


def test_the_synthetic_schema_declares_the_spec_s_own_anchor_page_types():
    schema = synthetic_schema(SyntheticKnowledgeBaseSpec(n_pages=200, anchor_page_types=("recipe",)))

    assert {page_type.page_type for page_type in schema.page_types} == {"recipe", "note"}
