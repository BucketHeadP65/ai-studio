r"""Unit specs for the pure coverage-gap top-up planner.

`plan_top_up` decides, without any IO, which parent cases survive a top-up and which retire, plus
which strata still need fresh generation. It re-applies the salted-hash selection rule under the new
inventory / allocation using the parent stamp's salt, so retirement is consistent-hashing-like: only
the cases the shrunken quota displaces churn, the surviving prefix is stable, and unrelated strata are
untouched. These specs pin that minimal-churn property, the `gold_gone` attribution, the gap listing,
and full determinism (no `random()` / wall-clock anywhere).

Retirement operates at `(archetype x difficulty)` band granularity, the granularity at which a
persisted case can be honestly attributed (its `QaCaseMeta` records no anchor kind), which is exactly
how `curate.stratify_cells` pools quotas. Gap generation stays at full `(archetype x difficulty x
anchor kind)` stratum granularity, read from the parent stamp's coverage map against the fresh
allocation (never a case recount).
"""

from __future__ import annotations

import hashlib

from typing import TYPE_CHECKING

from engram.evals.generation.contracts import (
    DatasetStamp,
    EvaluationCase,
    QuotaAllocation,
    Stratum,
    StratumCount,
    StratumInventory,
    TopUpPlan,
    merge_top_up,
    parent_case_refs,
    plan_top_up,
)
from engram.evals.generation.domain.allocation import CellQuota
from engram.evals.generation.domain.datasets import RetirementRecord
from engram.evals.generation.domain.topup import ParentCaseRef
from engram.evals.shared.domain import Archetype, QaCaseMeta, QaDifficulty, QaInput, RetrievalAnswer

if TYPE_CHECKING:
    from collections.abc import Sequence


def _ref(
    seed_key: str, *, archetype: Archetype = Archetype.PINPOINT, difficulty: QaDifficulty = "easy", gold: tuple[str, ...] = ()
) -> ParentCaseRef:
    """A parent-case reference named `{seed_key}#0`, gold defaulting to the seed key's page."""
    return ParentCaseRef(
        case_name=f"{seed_key}#0", seed_key=seed_key, archetype=archetype, difficulty=difficulty, gold_ids=gold or (seed_key,)
    )


def _inventory(*cells: tuple[Archetype, QaDifficulty, str, int]) -> StratumInventory:
    """A `StratumInventory` from `(archetype, difficulty, anchor_page_type, available)` tuples."""
    return StratumInventory(
        collection="research",
        counts=tuple(StratumCount(stratum=Stratum(archetype=a, difficulty=d, anchor_page_type=k), available=n) for a, d, k, n in cells),
    )


def _allocation(*cells: tuple[Archetype, QaDifficulty, str, int]) -> QuotaAllocation:
    """A `QuotaAllocation` from `(archetype, difficulty, anchor_page_type, quota)` tuples."""
    return QuotaAllocation(
        cells=tuple(CellQuota(stratum=Stratum(archetype=a, difficulty=d, anchor_page_type=k), quota=q) for a, d, k, q in cells)
    )


def _stamp(coverage: dict[str, int], *, salt: str = "s1") -> DatasetStamp:
    """A parent stamp carrying a coverage summary keyed `archetype/difficulty/kind` and a fixed salt."""
    return DatasetStamp(salt=salt, knowledge_base_fingerprint="fp:research", budget=10, coverage=coverage)


def _salted_order(seed_keys: Sequence[str], salt: str) -> list[str]:
    """The seed keys in ascending `md5(seed_key || salt)` order, mirroring the planner's rule."""
    return sorted(seed_keys, key=lambda key: hashlib.md5(f"{key}|{salt}".encode(), usedforsecurity=False).hexdigest())


class TestPlanTopUpRetirement:
    def test_unchanged_quota_produces_no_churn(self):
        # Parent's three pinpoint-easy cases exactly fill an unchanged quota: nothing retires, nothing generates.
        refs = [_ref(f"pinpoint:c{i}") for i in range(3)]
        plan = plan_top_up(
            refs,
            _stamp({"pinpoint/easy/paper": 3}),
            _inventory((Archetype.PINPOINT, "easy", "paper", 100)),
            _allocation((Archetype.PINPOINT, "easy", "paper", 3)),
            parent_version="research.v1.yaml",
        )

        assert plan.parent_version == "research.v1.yaml"
        assert plan.retire == ()
        assert plan.generate == ()

    def test_shrunk_quota_retires_exactly_the_hash_displaced_set(self):
        # The band quota shrinks 5 -> 2: the three lowest-ranked cases retire as `displaced`, the top two survive.
        seed_keys = [f"pinpoint:c{i}" for i in range(5)]
        refs = [_ref(key) for key in seed_keys]
        plan = plan_top_up(
            refs,
            _stamp({"pinpoint/easy/paper": 5}, salt="s1"),
            _inventory((Archetype.PINPOINT, "easy", "paper", 100)),
            _allocation((Archetype.PINPOINT, "easy", "paper", 2)),
            parent_version="research.v1.yaml",
        )

        ranked = _salted_order(seed_keys, "s1")
        assert {record.seed_key for record in plan.retire} == set(ranked[2:])  # exactly the displaced tail
        assert all(record.reason == "displaced" for record in plan.retire)
        assert plan.generate == ()  # a shrunk band never generates

    def test_surviving_prefix_is_consistent_hashing_stable(self):
        # Consistent hashing: a tighter quota only removes more from the bottom; the smaller survivor set
        # is a subset of the larger one (survivors never reshuffle).
        seed_keys = [f"pinpoint:c{i}" for i in range(6)]
        refs = [_ref(key) for key in seed_keys]
        inventory = _inventory((Archetype.PINPOINT, "easy", "paper", 100))
        stamp = _stamp({"pinpoint/easy/paper": 6})

        retired_q4 = {
            r.seed_key
            for r in plan_top_up(refs, stamp, inventory, _allocation((Archetype.PINPOINT, "easy", "paper", 4)), parent_version="v1").retire
        }
        retired_q2 = {
            r.seed_key
            for r in plan_top_up(refs, stamp, inventory, _allocation((Archetype.PINPOINT, "easy", "paper", 2)), parent_version="v1").retire
        }

        survivors_q4 = set(seed_keys) - retired_q4
        survivors_q2 = set(seed_keys) - retired_q2
        assert survivors_q2 <= survivors_q4  # tightening the quota only ever drops survivors, never adds

    def test_unrelated_strata_are_untouched(self):
        # The survey band shrinks; the pinpoint band keeps its quota, so no pinpoint case retires.
        refs = [_ref(f"pinpoint:c{i}") for i in range(2)] + [
            _ref(f"survey:builds_on:h{i}", archetype=Archetype.SURVEY, difficulty="hard") for i in range(4)
        ]
        plan = plan_top_up(
            refs,
            _stamp({"pinpoint/easy/paper": 2, "survey/hard/hub": 4}),
            _inventory((Archetype.PINPOINT, "easy", "paper", 100), (Archetype.SURVEY, "hard", "hub", 100)),
            _allocation((Archetype.PINPOINT, "easy", "paper", 2), (Archetype.SURVEY, "hard", "hub", 1)),
            parent_version="research.v1.yaml",
        )

        assert all(record.seed_key.startswith("survey:") for record in plan.retire)  # only the shrunk survey band churns
        assert len(plan.retire) == 3  # four survey cases, quota one, three displaced

    def test_dropped_band_retires_every_case(self):
        # The pinpoint archetype is gone from the allocation entirely: all its cases retire.
        refs = [_ref(f"pinpoint:c{i}") for i in range(3)]
        plan = plan_top_up(
            refs,
            _stamp({"pinpoint/easy/paper": 3}),
            _inventory((Archetype.PINPOINT, "easy", "paper", 100)),
            _allocation((Archetype.SURVEY, "hard", "hub", 5)),
            parent_version="research.v1.yaml",
        )

        assert {record.seed_key for record in plan.retire} == {f"pinpoint:c{i}" for i in range(3)}
        assert all(record.reason == "displaced" for record in plan.retire)


class TestPlanTopUpGoldGone:
    def test_missing_gold_retires_as_gold_gone(self):
        # Two cases keep their gold, one lost it: the survivor set is the two live cases (quota covers them),
        # the gold-less case retires as `gold_gone`, not `displaced`.
        refs = [_ref("pinpoint:c0", gold=("paper.a",)), _ref("pinpoint:c1", gold=("paper.b",)), _ref("pinpoint:c2", gold=("paper.gone",))]
        plan = plan_top_up(
            refs,
            _stamp({"pinpoint/easy/paper": 3}),
            _inventory((Archetype.PINPOINT, "easy", "paper", 100)),
            _allocation((Archetype.PINPOINT, "easy", "paper", 3)),
            parent_version="research.v1.yaml",
            existing_page_ids=frozenset({"paper.a", "paper.b"}),
        )

        gold_gone = [record for record in plan.retire if record.reason == "gold_gone"]
        assert [record.seed_key for record in gold_gone] == ["pinpoint:c2"]
        assert all(record.reason == "gold_gone" for record in plan.retire)  # the two live cases fit the quota

    def test_gold_gone_cases_do_not_consume_a_survivor_slot(self):
        # Quota one, three cases, one gold-gone: the gold-gone case retires as gold_gone and the remaining
        # two compete for the single slot, so exactly one of them is displaced (never the gold-gone one twice).
        refs = [_ref("pinpoint:c0", gold=("paper.a",)), _ref("pinpoint:c1", gold=("paper.b",)), _ref("pinpoint:c2", gold=("paper.gone",))]
        plan = plan_top_up(
            refs,
            _stamp({"pinpoint/easy/paper": 3}),
            _inventory((Archetype.PINPOINT, "easy", "paper", 100)),
            _allocation((Archetype.PINPOINT, "easy", "paper", 1)),
            parent_version="research.v1.yaml",
            existing_page_ids=frozenset({"paper.a", "paper.b"}),
        )

        reasons = {record.seed_key: record.reason for record in plan.retire}
        assert reasons["pinpoint:c2"] == "gold_gone"
        displaced = [key for key, reason in reasons.items() if reason == "displaced"]
        assert len(displaced) == 1  # one of the two live cases survives the single slot

    def test_gold_gone_not_evaluated_without_the_existing_pages_set(self):
        # With no `existing_page_ids`, gold existence is not checkable and never fires: only displacement.
        refs = [_ref("pinpoint:c0", gold=("paper.gone",))]
        plan = plan_top_up(
            refs,
            _stamp({"pinpoint/easy/paper": 1}),
            _inventory((Archetype.PINPOINT, "easy", "paper", 100)),
            _allocation((Archetype.PINPOINT, "easy", "paper", 1)),
            parent_version="research.v1.yaml",
        )

        assert plan.retire == ()  # the case fits the quota and gold_gone is not evaluated


class TestPlanTopUpGeneration:
    def test_generate_lists_only_under_covered_cells_with_the_gap_quota(self):
        # One cell is under its target (gap 3), one is met (no gap): only the under-covered cell generates.
        plan = plan_top_up(
            [],
            _stamp({"pinpoint/easy/paper": 2, "survey/hard/hub": 5}),
            _inventory((Archetype.PINPOINT, "easy", "paper", 100), (Archetype.SURVEY, "hard", "hub", 100)),
            _allocation((Archetype.PINPOINT, "easy", "paper", 5), (Archetype.SURVEY, "hard", "hub", 5)),
            parent_version="research.v1.yaml",
        )

        assert len(plan.generate) == 1
        cell = plan.generate[0]
        assert cell.stratum == Stratum(archetype=Archetype.PINPOINT, difficulty="easy", anchor_page_type="paper")
        assert cell.quota == 3  # target 5 minus the two already covered

    def test_a_brand_new_stratum_generates_its_full_quota(self):
        # A cell absent from the parent stamp's coverage map has zero covered, so the gap is its full quota.
        plan = plan_top_up(
            [],
            _stamp({}),
            _inventory((Archetype.TRACE, "hard", "concept", 50)),
            _allocation((Archetype.TRACE, "hard", "concept", 4)),
            parent_version="research.v1.yaml",
        )

        assert plan.generate == (CellQuota(stratum=Stratum(archetype=Archetype.TRACE, difficulty="hard", anchor_page_type="concept"), quota=4),)

    def test_gap_is_bounded_by_the_current_knowledge_base_pool(self):
        # The allocation asks for five but the knowledge base now offers only two seeds: the gap is capped at two.
        plan = plan_top_up(
            [],
            _stamp({}),
            _inventory((Archetype.PINPOINT, "easy", "paper", 2)),
            _allocation((Archetype.PINPOINT, "easy", "paper", 5)),
            parent_version="research.v1.yaml",
        )

        assert plan.generate == (CellQuota(stratum=Stratum(archetype=Archetype.PINPOINT, difficulty="easy", anchor_page_type="paper"), quota=2),)


class TestPlanTopUpDeterminism:
    def test_same_inputs_produce_an_identical_plan(self):
        refs = [_ref(f"pinpoint:c{i}") for i in range(5)]
        args = (
            refs,
            _stamp({"pinpoint/easy/paper": 5}),
            _inventory((Archetype.PINPOINT, "easy", "paper", 100)),
            _allocation((Archetype.PINPOINT, "easy", "paper", 2)),
        )

        first = plan_top_up(*args, parent_version="research.v1.yaml")
        second = plan_top_up(*args, parent_version="research.v1.yaml")

        assert first == second

    def test_an_explicit_salt_overrides_the_stamp_salt_for_ranking(self):
        # A different salt reorders the hash draw, so a different survivor prefix is chosen (churn changes).
        seed_keys = [f"pinpoint:c{i}" for i in range(5)]
        refs = [_ref(key) for key in seed_keys]
        common = (
            refs,
            _stamp({"pinpoint/easy/paper": 5}, salt="s1"),
            _inventory((Archetype.PINPOINT, "easy", "paper", 100)),
            _allocation((Archetype.PINPOINT, "easy", "paper", 2)),
        )

        with_stamp_salt = {r.seed_key for r in plan_top_up(*common, parent_version="v1").retire}
        with_override = {r.seed_key for r in plan_top_up(*common, parent_version="v1", salt="s2").retire}

        assert with_stamp_salt == set(_salted_order(seed_keys, "s1")[2:])
        assert with_override == set(_salted_order(seed_keys, "s2")[2:])


class TestPlanTopUpStale:
    def test_a_stale_case_leaves_the_survivor_pool_before_the_ranking(self):
        # Marking the top-ranked case stale removes it before the salted-hash draw, so the case that would
        # otherwise have been displaced now survives: stale is applied before ranking, not after it.
        seed_keys = [f"pinpoint:c{i}" for i in range(3)]
        refs = [_ref(key) for key in seed_keys]
        ranked = _salted_order(seed_keys, "s1")
        plan = plan_top_up(
            refs,
            _stamp({"pinpoint/easy/paper": 3}, salt="s1"),
            _inventory((Archetype.PINPOINT, "easy", "paper", 100)),
            _allocation((Archetype.PINPOINT, "easy", "paper", 2)),
            parent_version="research.v1.yaml",
            stale_case_names=frozenset({f"{ranked[0]}#0"}),
        )

        assert [(record.seed_key, record.reason) for record in plan.retire] == [(ranked[0], "stale")]  # only the stale case retires
        survivors = set(seed_keys) - {record.seed_key for record in plan.retire}
        assert ranked[2] in survivors  # the would-be-displaced case survives now the stale case left the pool
        assert plan.generate == ()  # losing one stale case meets the shrunk quota exactly, so no deficit

    def test_gold_gone_takes_precedence_over_stale(self):
        # A case that is both gold-gone and flagged stale records `gold_gone`: gold existence is checked first.
        refs = [_ref("pinpoint:c0", gold=("paper.gone",))]
        plan = plan_top_up(
            refs,
            _stamp({"pinpoint/easy/paper": 1}),
            _inventory((Archetype.PINPOINT, "easy", "paper", 100)),
            _allocation((Archetype.PINPOINT, "easy", "paper", 1)),
            parent_version="research.v1.yaml",
            existing_page_ids=frozenset(),
            stale_case_names=frozenset({"pinpoint:c0#0"}),
        )

        assert [(record.seed_key, record.reason) for record in plan.retire] == [("pinpoint:c0", "gold_gone")]


class TestPlanTopUpDeficit:
    def test_stale_shrinkage_reopens_bounded_drafting_over_cells_with_pool_headroom(self):
        # Band quota 10 over two cells; two of ten cases retire stale -> 8 survivors -> a 2-case deficit. The
        # first cell (figure) has no seed pool, so has no headroom, and the deficit lands entirely on the second (paper).
        refs = [_ref(f"pinpoint:c{i}") for i in range(10)]
        plan = plan_top_up(
            refs,
            _stamp({"pinpoint/easy/figure": 5, "pinpoint/easy/paper": 5}),
            _inventory((Archetype.PINPOINT, "easy", "paper", 100)),  # figure has no seed pool, so no headroom
            _allocation((Archetype.PINPOINT, "easy", "figure", 5), (Archetype.PINPOINT, "easy", "paper", 5)),
            parent_version="research.v1.yaml",
            stale_case_names=frozenset({"pinpoint:c0#0", "pinpoint:c1#0"}),
        )

        assert len(plan.retire) == 2
        assert all(record.reason == "stale" for record in plan.retire)
        assert plan.generate == (CellQuota(stratum=Stratum(archetype=Archetype.PINPOINT, difficulty="easy", anchor_page_type="paper"), quota=2),)

    def test_gold_gone_shrinkage_now_reopens_drafting(self):
        # The Phase-5 shrinkage bug healed: a gold_gone retirement drops a survivor below the band quota, so the
        # band now emits a deficit cell (Phase-5 read planned-vs-planned and never reopened the emptied slot).
        refs = [_ref("pinpoint:c0", gold=("paper.a",)), _ref("pinpoint:c1", gold=("paper.b",)), _ref("pinpoint:c2", gold=("paper.gone",))]
        plan = plan_top_up(
            refs,
            _stamp({"pinpoint/easy/paper": 3}),
            _inventory((Archetype.PINPOINT, "easy", "paper", 100)),
            _allocation((Archetype.PINPOINT, "easy", "paper", 3)),
            parent_version="research.v1.yaml",
            existing_page_ids=frozenset({"paper.a", "paper.b"}),
        )

        assert [record.reason for record in plan.retire] == ["gold_gone"]
        assert plan.generate == (CellQuota(stratum=Stratum(archetype=Archetype.PINPOINT, difficulty="easy", anchor_page_type="paper"), quota=1),)

    def test_deficit_never_exceeds_the_band_quota_when_retirement_outstrips_coverage(self):
        # The surviving allocation covers only `paper` (coverage 2); the `figure` kind dropped out entirely, so its
        # cases still retire via the band ledger but contribute no coverage. Eight of ten pinpoint-easy cases retire
        # (band quota 2), so survivors would be 2 - 8 = -6 unfloored: the floor caps the reopened deficit at the band
        # quota (2), never quota plus the negative overshoot (8), even with ample pool headroom (100 seeds).
        refs = [_ref(f"pinpoint:c{i}") for i in range(10)]
        plan = plan_top_up(
            refs,
            _stamp({"pinpoint/easy/paper": 2, "pinpoint/easy/figure": 8}),
            _inventory((Archetype.PINPOINT, "easy", "paper", 100)),
            _allocation((Archetype.PINPOINT, "easy", "paper", 2)),
            parent_version="research.v1.yaml",
        )

        assert len(plan.retire) == 8  # ten cases, band quota two, eight displaced
        assert plan.generate == (CellQuota(stratum=Stratum(archetype=Archetype.PINPOINT, difficulty="easy", anchor_page_type="paper"), quota=2),)

    def test_untouched_knowledge_base_with_empty_stale_matches_phase5(self):
        # Regression guard: with no gold_gone and no stale, passing an empty stale set is byte-identical to the
        # Phase-5 call, and displacement plus an under-covered stratum behave exactly as they did before.
        refs = [_ref(f"pinpoint:c{i}") for i in range(5)] + [_ref("survey:builds_on:h0", archetype=Archetype.SURVEY, difficulty="hard")]
        args = (
            refs,
            _stamp({"pinpoint/easy/paper": 5, "survey/hard/hub": 1}),
            _inventory((Archetype.PINPOINT, "easy", "paper", 100), (Archetype.SURVEY, "hard", "hub", 100)),
            _allocation((Archetype.PINPOINT, "easy", "paper", 2), (Archetype.SURVEY, "hard", "hub", 3)),
        )

        phase5 = plan_top_up(*args, parent_version="research.v1.yaml")
        with_empty_stale = plan_top_up(*args, parent_version="research.v1.yaml", stale_case_names=frozenset())

        assert with_empty_stale == phase5  # the additive param default preserves Phase-5 behavior exactly
        assert phase5.generate == (CellQuota(stratum=Stratum(archetype=Archetype.SURVEY, difficulty="hard", anchor_page_type="hub"), quota=2),)
        assert {record.reason for record in phase5.retire} == {"displaced"}
        assert len(phase5.retire) == 3  # five pinpoint cases, quota two, three displaced


def _case(name: str, *, archetype: Archetype = Archetype.PINPOINT, difficulty: QaDifficulty = "easy") -> EvaluationCase:
    """A labelled `EvaluationCase` carrying the `(archetype, difficulty)` band `merge_top_up` reads."""
    return EvaluationCase(
        name=name,
        inputs=QaInput(question="q"),
        expected_output=RetrievalAnswer(answer="a", cited_source_ids=["p.x"]),
        metadata=QaCaseMeta(anchor_page_id="p.x", difficulty=difficulty, archetype=archetype),
    )


def _plan(*retire_names: str) -> TopUpPlan:
    """A `TopUpPlan` retiring the named cases as `displaced` (only the retire ledger drives the merge)."""
    return TopUpPlan(
        parent_version="research.v1.yaml",
        generate=(),
        retire=tuple(RetirementRecord(case_name=name, seed_key=name.rsplit("#", 1)[0], reason="displaced") for name in retire_names),
    )


class TestMergeTopUp:
    def test_drops_retired_parent_cases_and_keeps_survivors_in_order(self):
        parents = [_case("pinpoint:c0#0"), _case("pinpoint:c1#0"), _case("pinpoint:c2#0")]
        allocation = _allocation((Archetype.PINPOINT, "easy", "paper", 3))

        merged = merge_top_up(parents, _plan("pinpoint:c1#0"), [], allocation)

        assert [case.name for case in merged] == ["pinpoint:c0#0", "pinpoint:c2#0"]  # retired dropped, survivors in parent order

    def test_fills_the_band_up_to_its_fresh_quota_with_newcomers(self):
        # One survivor already fills the band (filled = 1); the fresh quota is 3, so only two newcomers fit.
        parents = [_case("pinpoint:c0#0")]
        fresh = [_case("pinpoint:n0#0"), _case("pinpoint:n1#0"), _case("pinpoint:n2#0")]
        allocation = _allocation((Archetype.PINPOINT, "easy", "paper", 3))

        merged = merge_top_up(parents, _plan(), fresh, allocation)

        assert [case.name for case in merged] == ["pinpoint:c0#0", "pinpoint:n0#0", "pinpoint:n1#0"]  # the third newcomer exceeds the cap

    def test_skips_a_newcomer_that_collides_with_a_surviving_name(self):
        # The top-up re-draws with the parent salt, so a newcomer sharing a survivor's name is the same
        # re-drawn seed and is skipped, leaving only the genuinely new case to fill the gap.
        parents = [_case("pinpoint:c0#0")]
        fresh = [_case("pinpoint:c0#0"), _case("pinpoint:n1#0")]
        allocation = _allocation((Archetype.PINPOINT, "easy", "paper", 3))

        merged = merge_top_up(parents, _plan(), fresh, allocation)

        assert [case.name for case in merged] == ["pinpoint:c0#0", "pinpoint:n1#0"]  # the colliding re-draw is skipped


class TestParentCaseRefs:
    def test_parses_seed_key_from_the_stable_case_name(self):
        cases = [
            EvaluationCase(
                name="survey:builds_on:paper.x#2",
                inputs=QaInput(question="q"),
                expected_output=RetrievalAnswer(answer="a", cited_source_ids=["paper.x", "paper.y"]),
                metadata=QaCaseMeta(anchor_page_id="paper.x", difficulty="hard", archetype=Archetype.SURVEY),
            )
        ]

        refs = parent_case_refs(cases)

        assert len(refs) == 1
        assert refs[0].seed_key == "survey:builds_on:paper.x"  # the `#{ordinal}` suffix is stripped
        assert refs[0].archetype == Archetype.SURVEY
        assert refs[0].difficulty == "hard"
        assert refs[0].gold_ids == ("paper.x", "paper.y")

    def test_skips_unlabelled_cases(self):
        cases = [
            EvaluationCase(
                name="paper.x-0",
                inputs=QaInput(question="q"),
                expected_output=RetrievalAnswer(answer="a", cited_source_ids=["paper.x"]),
                metadata=QaCaseMeta(anchor_page_id="paper.x", difficulty="easy", archetype=None),
            )
        ]

        assert parent_case_refs(cases) == []  # no archetype means no stratum to attribute the case to


def test_a_human_judged_case_is_no_parent_case():
    cases = [
        EvaluationCase(
            name="q1",
            inputs=QaInput(question="q"),
            expected_output=RetrievalAnswer(answer="", cited_source_ids=["d1"]),
            metadata=QaCaseMeta(judgments={"d1": 1}, archetype=Archetype.PINPOINT),
        )
    ]

    assert parent_case_refs(cases) == []
