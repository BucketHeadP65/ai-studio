r"""Unit specs for the retrieval-tuning control loop (`engram.evals.tuning.service`).

`TuningLoop.run` is the orchestration heart of tuning: it splits the golden dataset into a
train and a validation hold-out, measures the baseline, then per round diagnoses the incumbent,
asks the proposer for one targeted revision, evaluates the candidate, accepts it iff it beats the
incumbent on the objective's train metric, and stops on the objective's convergence verdict. The
winner is the accepted incumbent (the baseline included) with the highest validation primary metric,
so a candidate that overfits the train split is accepted yet loses the run to a higher-val incumbent.

Every collaborator is faked network-free. `FakePolicyEvaluator` builds a structural `EvaluationRunReport`
from whichever dataset it is handed, assigning each case the `recall@5` scripted for the policy's
`instructions` (the loop's hashable policy key), so the train and val reports differ by their cases
exactly as a real run would. `FakeProposer` pops one scripted `ProposedPolicy` per round. The
reports reuse the same tiny `ReportCase` stand-ins as `test_diagnosis.py`, so `diagnose` (and thus
the loop) reads them through its Protocol port without a `cast` or `# type: ignore`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from engram.evals.generation.contracts import AuthoredCase, EvaluationCase, EvaluationDataset, build_dataset
from engram.evals.rca.contracts import BucketPrediction, DraftReflection, ReflectiveFinding
from engram.evals.rca.domain.reflection import ChangeManifest
from engram.evals.shared.domain import Archetype, ExpectedQa, QaCaseMeta, QaInput, RetrievalAnswer
from engram.evals.tuning.domain.objective import StopReason, TuningObjective
from engram.evals.tuning.domain.proposal import ProposedPolicy
from engram.evals.tuning.policy import RetrievalPolicy, baseline_policy
from engram.evals.tuning.service import TuningLoop, TuningResult, _core_case_scores, _stratified_split, carve_core

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# -- structural report stand-ins (the `diagnose` Protocol port, mirrors test_diagnosis.py) --


@dataclass(frozen=True)
class _Score:
    """A stand-in for one `EvaluationResult` - the loop's `diagnose` reads only `.value`."""

    value: float


@dataclass(frozen=True)
class _Case:
    """A stand-in for one `ReportCase` - carries the slice `diagnose` folds over plus the case name."""

    name: str
    inputs: QaInput
    metadata: QaCaseMeta | None
    expected_output: RetrievalAnswer | None
    output: RetrievalAnswer
    scores: dict[str, _Score]
    metrics: dict[str, float | int]
    total_duration: float


@dataclass(frozen=True)
class _Report:
    """A stand-in for an `EvaluationReport` - `diagnose` reads only `.cases`."""

    cases: list[_Case]


# -- the faked measurement port and proposer brain ---------------------------


class FakePolicyEvaluator:
    """A `PolicyEvaluator` that scores a dataset from a per-policy, per-case recall script.

    `recall` maps a policy key (the policy's `instructions`, the only hashable handle the loop
    keys on) to a per-`case.name` `recall@5`. `evaluate` builds a structural report from the
    dataset it is handed, so the train and validation reports differ by their cases exactly as a
    real run would. `evaluate_calls` records each `(instructions, case-names)` pair so a spec can
    assert which split a policy was measured on. `tokens` is the per-case total-token metric every
    case carries, so the objective's cost mapping is populated.
    """

    def __init__(self, recall: dict[str, dict[str, float]], *, tokens: float = 100.0) -> None:
        self.recall = recall
        self.tokens = tokens
        self.evaluate_calls: list[tuple[str, tuple[str, ...]]] = []

    async def evaluate(self, policy: RetrievalPolicy, *, dataset: EvaluationDataset) -> _Report:
        self.evaluate_calls.append((policy.instructions, tuple(case.name or "" for case in dataset.cases)))
        per_case = self.recall[policy.instructions]
        return _Report(cases=[self._case(case.name or "", per_case[case.name or ""]) for case in dataset.cases])

    def _case(self, name: str, recall: float) -> _Case:
        return _named_case(name, recall, tokens=self.tokens)


class ReversingPolicyEvaluator(FakePolicyEvaluator):
    """Same scores, report cases in reversed order - pairing must survive."""

    async def evaluate(self, policy: RetrievalPolicy, *, dataset: EvaluationDataset) -> _Report:
        report = await super().evaluate(policy, dataset=dataset)
        return _Report(cases=list(reversed(report.cases)))


@dataclass
class FakeProposer:
    """A `PolicyProposer` that pops one scripted `ProposedPolicy` per round (no model, no network).

    `proposals` is the scripted queue, consumed front-to-back, one per `propose` call. `proposed`
    records each emitted policy so a spec can assert the loop proposed against the live incumbent.
    """

    proposals: list[ProposedPolicy]
    proposed: list[RetrievalPolicy] = field(default_factory=list)
    findings_seen: list[tuple[ReflectiveFinding, ...]] = field(default_factory=list)

    async def propose(self, policy: RetrievalPolicy, diagnosis: object, *, findings: tuple[ReflectiveFinding, ...] = ()) -> ProposedPolicy:
        self.proposed.append(policy)
        self.findings_seen.append(findings)
        return self.proposals.pop(0)


# -- builders ----------------------------------------------------------------


def _policy(instructions: str, *, strategy: str = "full") -> RetrievalPolicy:
    """A `RetrievalPolicy` whose `instructions` double as its (hashable) evaluator key."""
    return RetrievalPolicy(instructions=instructions, strategy=strategy, tool_descriptions={})


def _proposal(instructions: str, *, strategy: str = "full") -> ProposedPolicy:
    """A scripted proposer output wrapping a candidate policy keyed by `instructions`."""
    return ProposedPolicy(policy=_policy(instructions, strategy=strategy), rationale=f"try {instructions}")


def _dataset(names_to_archetype: Mapping[str, Archetype]) -> EvaluationDataset:
    """A golden dataset of one case per name, each labelled with its archetype.

    The case name is `"{paper_id}-0"` (one case per paper), so the fake evaluator's per-case recall
    map keys on that name.
    """
    records = [
        AuthoredCase(
            question=QaInput(question=f"q-{paper}"),
            expected=ExpectedQa(answer="a", expected_source_ids=(paper,)),
            meta=QaCaseMeta(anchor_page_id=paper, difficulty="easy", archetype=archetype),
        )
        for paper, archetype in names_to_archetype.items()
    ]
    return build_dataset(records)


def _case_names(dataset: EvaluationDataset) -> list[str]:
    return [case.name or "" for case in dataset.cases]


def _uniform(names: Sequence[str], recall: float) -> dict[str, float]:
    """The same `recall@5` for every case name (a policy with flat train/val accuracy)."""
    return dict.fromkeys(names, recall)


def _named_case(name: str, recall: float, *, tokens: float = 100.0) -> _Case:
    """One structural report case carrying `name` and a `recall@5` score (the pairing join fields)."""
    return _Case(
        name=name,
        inputs=QaInput(question=f"q-{name}"),
        metadata=QaCaseMeta(anchor_page_id=name, difficulty="easy", archetype=Archetype.PINPOINT),
        expected_output=RetrievalAnswer(answer="ref", cited_source_ids=[name]),
        output=RetrievalAnswer(answer="got", cited_source_ids=[name]),
        scores={"recall@5": _Score(recall), "n_tool_calls": _Score(1.0)},
        metrics={"input_tokens": tokens},
        total_duration=0.1,
    )


def _report_from(recalls: Mapping[str, float]) -> _Report:
    """A structural report of one case per name, each carrying its scripted `recall@5` (the join fields)."""
    return _Report(cases=[_named_case(name, recall) for name, recall in recalls.items()])


# -- _stratified_split -------------------------------------------------------


class TestStratifiedSplit:
    def test_zero_split_disables_validation(self):
        dataset = _dataset({"p.a": Archetype.PINPOINT, "p.b": Archetype.SYNTHESIS})

        train, val = _stratified_split(dataset, val_split=0.0)

        assert _case_names(train) == _case_names(dataset)
        assert val.cases == []
        assert train.evaluators == dataset.evaluators

    def test_keeps_archetype_balance(self):
        # 4 pinpoint + 4 synthesis; a 0.5 split sends half of EACH archetype to validation.
        names = {f"pin.{i}": Archetype.PINPOINT for i in range(4)} | {f"con.{i}": Archetype.SYNTHESIS for i in range(4)}
        dataset = _dataset(names)

        train, val = _stratified_split(dataset, val_split=0.5)

        def archetypes(split: EvaluationDataset) -> list[Archetype | None]:
            return [case.metadata.archetype for case in split.cases if case.metadata is not None]

        assert archetypes(val).count(Archetype.PINPOINT) == 2
        assert archetypes(val).count(Archetype.SYNTHESIS) == 2
        assert archetypes(train).count(Archetype.PINPOINT) == 2
        assert archetypes(train).count(Archetype.SYNTHESIS) == 2
        # Every case lands in exactly one side (a partition).
        assert sorted(_case_names(train) + _case_names(val)) == sorted(_case_names(dataset))

    def test_is_deterministic(self):
        names = {f"pin.{i}": Archetype.PINPOINT for i in range(5)} | {f"con.{i}": Archetype.SYNTHESIS for i in range(3)}
        dataset = _dataset(names)

        first = _stratified_split(dataset, val_split=0.4)
        second = _stratified_split(dataset, val_split=0.4)

        assert _case_names(first[0]) == _case_names(second[0])
        assert _case_names(first[1]) == _case_names(second[1])

    def test_reconstructed_sides_carry_the_dataset_evaluators(self):
        dataset = _dataset({f"pin.{i}": Archetype.PINPOINT for i in range(4)})

        train, val = _stratified_split(dataset, val_split=0.5)

        assert train.evaluators == dataset.evaluators
        assert val.evaluators == dataset.evaluators


# -- TuningLoop.run -----------------------------------------------------


class TestFullConvergence:
    async def test_improving_candidates_are_accepted_until_max_rounds(self):
        # Strictly improving train recall every round, so patience never fires and the loop stops on
        # the round cap. The trajectory is seeded with the baseline, so max_rounds=3 runs two rounds.
        dataset = _dataset({"p.a": Archetype.PINPOINT, "p.b": Archetype.PINPOINT})
        names = _case_names(dataset)
        baseline = baseline_policy()
        recall = {baseline.instructions: _uniform(names, 0.40), "v1": _uniform(names, 0.60), "v2": _uniform(names, 0.80)}
        evaluator = FakePolicyEvaluator(recall)
        proposer = FakeProposer(proposals=[_proposal("v1"), _proposal("v2")])
        objective = TuningObjective(val_split=0.0, max_rounds=3, patience=10)

        result = await TuningLoop(evaluator, proposer, objective).run(dataset=dataset, baseline=baseline)

        assert isinstance(result, TuningResult)
        assert result.stop_reason == StopReason.MAX_ROUNDS
        assert len(result.rounds) == 2
        assert all(round_.accepted for round_ in result.rounds)
        assert result.winning_policy.instructions == "v2"  # the best (last accepted) policy
        assert result.baseline_policy == baseline

    async def test_proposes_against_the_live_incumbent(self):
        dataset = _dataset({"p.a": Archetype.PINPOINT})
        names = _case_names(dataset)
        baseline = baseline_policy()
        recall = {baseline.instructions: _uniform(names, 0.40), "v1": _uniform(names, 0.60), "v2": _uniform(names, 0.80)}
        proposer = FakeProposer(proposals=[_proposal("v1"), _proposal("v2")])
        objective = TuningObjective(val_split=0.0, max_rounds=3, patience=10)

        await TuningLoop(FakePolicyEvaluator(recall), proposer, objective).run(dataset=dataset, baseline=baseline)

        # Round 1 proposes against the baseline; round 2 against the accepted v1.
        assert [policy.instructions for policy in proposer.proposed] == [baseline.instructions, "v1"]


class TestNoImprovementStop:
    async def test_rejects_every_candidate_and_stops_on_patience_with_baseline_winner(self):
        dataset = _dataset({"p.a": Archetype.PINPOINT, "p.b": Archetype.PINPOINT})
        names = _case_names(dataset)
        baseline = baseline_policy()
        # Every candidate ties the baseline on train (no gain >= min_gain), so all are rejected and
        # the baseline stays the running best; patience=2 fires after two rounds.
        recall = {
            baseline.instructions: _uniform(names, 0.50),
            "v1": _uniform(names, 0.50),
            "v2": _uniform(names, 0.50),
            "v3": _uniform(names, 0.50),
        }
        proposer = FakeProposer(proposals=[_proposal("v1"), _proposal("v2"), _proposal("v3")])
        objective = TuningObjective(val_split=0.0, max_rounds=10, patience=2)

        result = await TuningLoop(FakePolicyEvaluator(recall), proposer, objective).run(dataset=dataset, baseline=baseline)

        assert result.stop_reason == StopReason.PATIENCE
        assert len(result.rounds) == 2
        assert not any(round_.accepted for round_ in result.rounds)
        assert result.winning_policy == baseline
        assert result.winner_train_metrics["recall@5"] == pytest.approx(0.50)


class TestOverfittingGuard:
    async def test_train_accepted_candidate_loses_to_higher_val_incumbent(self):
        # Eight cases, half to validation. The candidate lifts TRAIN recall but its VAL recall drops
        # below the baseline's, so it is accepted on train yet the winner falls back to the baseline.
        names = {f"pin.{i}": Archetype.PINPOINT for i in range(4)} | {f"con.{i}": Archetype.SYNTHESIS for i in range(4)}
        dataset = _dataset(names)
        train, val = _stratified_split(dataset, val_split=0.5)
        train_names, val_names = _case_names(train), _case_names(val)
        baseline = baseline_policy()
        recall = {
            baseline.instructions: _uniform(train_names, 0.50) | _uniform(val_names, 0.60),
            "v1": _uniform(train_names, 0.90) | _uniform(val_names, 0.40),  # train up, val down (overfit)
        }
        proposer = FakeProposer(proposals=[_proposal("v1")])
        objective = TuningObjective(val_split=0.5, max_rounds=2, patience=10)

        result = await TuningLoop(FakePolicyEvaluator(recall), proposer, objective).run(dataset=dataset, baseline=baseline)

        assert result.stop_reason == StopReason.MAX_ROUNDS
        assert len(result.rounds) == 1
        assert result.rounds[0].accepted  # accepted on the TRAIN objective
        assert result.rounds[0].train_metrics["recall@5"] == pytest.approx(0.90)
        assert result.rounds[0].val_metrics is not None
        assert result.rounds[0].val_metrics["recall@5"] == pytest.approx(0.40)
        # The headline: the winner is the higher-VAL incumbent (the baseline), not the accepted candidate.
        assert result.winning_policy == baseline
        assert result.winner_val_metrics is not None
        assert result.winner_val_metrics["recall@5"] == pytest.approx(0.60)

    async def test_winner_is_the_highest_val_accepted_candidate_when_val_improves(self):
        names = {f"pin.{i}": Archetype.PINPOINT for i in range(4)} | {f"con.{i}": Archetype.SYNTHESIS for i in range(4)}
        dataset = _dataset(names)
        train, val = _stratified_split(dataset, val_split=0.5)
        train_names, val_names = _case_names(train), _case_names(val)
        baseline = baseline_policy()
        recall = {
            baseline.instructions: _uniform(train_names, 0.50) | _uniform(val_names, 0.50),
            "v1": _uniform(train_names, 0.90) | _uniform(val_names, 0.80),  # train AND val improve
        }
        proposer = FakeProposer(proposals=[_proposal("v1")])
        objective = TuningObjective(val_split=0.5, max_rounds=2, patience=10)

        result = await TuningLoop(FakePolicyEvaluator(recall), proposer, objective).run(dataset=dataset, baseline=baseline)

        assert result.rounds[0].accepted
        assert result.winning_policy.instructions == "v1"
        assert result.winner_val_metrics is not None
        assert result.winner_val_metrics["recall@5"] == pytest.approx(0.80)


class TestValSplitDisabled:
    async def test_zero_split_picks_winner_by_train_with_no_val_metrics(self):
        dataset = _dataset({"p.a": Archetype.PINPOINT, "p.b": Archetype.PINPOINT})
        names = _case_names(dataset)
        baseline = baseline_policy()
        recall = {baseline.instructions: _uniform(names, 0.50), "v1": _uniform(names, 0.90)}
        evaluator = FakePolicyEvaluator(recall)
        proposer = FakeProposer(proposals=[_proposal("v1")])
        objective = TuningObjective(val_split=0.0, max_rounds=2, patience=10)

        result = await TuningLoop(evaluator, proposer, objective).run(dataset=dataset, baseline=baseline)

        assert result.winning_policy.instructions == "v1"  # the last accepted incumbent (train gain)
        assert result.winner_val_metrics is None
        assert result.baseline_val_metrics is None
        assert result.rounds[0].val_metrics is None
        # Every evaluation scored the full case set (the decision core spans the whole train split
        # here); the loop never carved out a validation split. Order is hash-driven, so compare sets.
        scored_name_sets = {frozenset(scored_names) for _instructions, scored_names in evaluator.evaluate_calls}
        assert scored_name_sets == {frozenset(names)}


class TestDecisionCore:
    async def test_accepts_are_decided_on_the_core_only(self):
        # A strict subset core (4 of 8) with a fresh salt. The candidate improves ONLY on the core
        # cases and is worse on the non-core (pool/tail) cases, yet it is accepted because acceptance
        # reads the core alone; the tail feeds diagnosis and the divergence, never the accept rule.
        dataset = _dataset({f"pin.{i}": Archetype.PINPOINT for i in range(8)})
        salt = "core-salt"
        train, _ = _stratified_split(dataset, val_split=0.0)
        core_tail = carve_core(train, core_size=4, min_per_stratum=1, salt=salt)
        core_names = {case.name for case in core_tail.core.cases}
        non_core = set(_case_names(dataset)) - core_names
        baseline = baseline_policy()
        recall = {
            baseline.instructions: _uniform(_case_names(dataset), 0.50),
            "v1": dict.fromkeys(core_names, 0.90) | dict.fromkeys(non_core, 0.10),
        }
        objective = TuningObjective(val_split=0.0, core_size=4, min_core_per_stratum=1, tail_size=4, max_rounds=2, patience=10)
        proposer = FakeProposer(proposals=[_proposal("v1")])

        result = await TuningLoop(FakePolicyEvaluator(recall), proposer, objective).run(
            dataset=dataset, baseline=baseline, core_salt=salt
        )

        assert result.rounds[0].accepted  # accepted on the core despite being worse off-core
        assert result.winning_policy.instructions == "v1"
        # core recall (0.90) far exceeds tail recall (0.10): a positive divergence flags the overfit.
        assert result.core_tail_divergence[0] == pytest.approx(0.80)
        assert result.core_salt == salt

    async def test_validation_is_scored_only_on_accept_events(self):
        # v1 ties the baseline on the core (rejected); v2 improves (accepted). Validation is scored
        # once for the baseline and only for the accepted v2, never for the rejected v1.
        dataset = _dataset({f"pin.{i}": Archetype.PINPOINT for i in range(8)})
        names = _case_names(dataset)
        _, val = _stratified_split(dataset, val_split=0.5)
        val_names = tuple(_case_names(val))
        baseline = baseline_policy()
        recall = {baseline.instructions: _uniform(names, 0.50), "v1": _uniform(names, 0.50), "v2": _uniform(names, 0.90)}
        objective = TuningObjective(val_split=0.5, core_size=100, min_core_per_stratum=1, max_rounds=3, patience=10)
        evaluator = FakePolicyEvaluator(recall)
        proposer = FakeProposer(proposals=[_proposal("v1"), _proposal("v2")])

        result = await TuningLoop(evaluator, proposer, objective).run(dataset=dataset, baseline=baseline)

        val_scorers = [instructions for instructions, scored in evaluator.evaluate_calls if scored == val_names]
        assert baseline.instructions in val_scorers  # baseline validation measured once at the start
        assert "v2" in val_scorers  # accepted candidate's validation measured on the accept event
        assert "v1" not in val_scorers  # rejected candidate's validation never measured
        assert result.winning_policy.instructions == "v2"  # winner still picked on the untouched validation split

    async def test_result_records_the_core_salt_size_and_divergence(self):
        dataset = _dataset({f"pin.{i}": Archetype.PINPOINT for i in range(8)})
        names = _case_names(dataset)
        baseline = baseline_policy()
        recall = {baseline.instructions: _uniform(names, 0.40), "v1": _uniform(names, 0.60), "v2": _uniform(names, 0.80)}
        objective = TuningObjective(val_split=0.0, core_size=4, min_core_per_stratum=1, tail_size=4, max_rounds=3, patience=10)
        proposer = FakeProposer(proposals=[_proposal("v1"), _proposal("v2")])

        result = await TuningLoop(FakePolicyEvaluator(recall), proposer, objective).run(
            dataset=dataset, baseline=baseline, core_salt="fixed-salt"
        )

        assert result.core_salt == "fixed-salt"
        assert result.core_size == 4
        assert result.tail_size == 4
        assert len(result.core_tail_divergence) == len(result.rounds)
        # Uniform recall makes the core and tail agree, so every round's divergence is zero.
        assert all(divergence == pytest.approx(0.0) for divergence in result.core_tail_divergence)

    async def test_core_salt_is_generated_and_recorded_when_not_supplied(self):
        dataset = _dataset({f"pin.{i}": Archetype.PINPOINT for i in range(4)})
        names = _case_names(dataset)
        baseline = baseline_policy()
        recall = {baseline.instructions: _uniform(names, 0.50), "v1": _uniform(names, 0.90)}
        objective = TuningObjective(val_split=0.0, core_size=100, min_core_per_stratum=1, max_rounds=2, patience=10)
        proposer = FakeProposer(proposals=[_proposal("v1")])

        result = await TuningLoop(FakePolicyEvaluator(recall), proposer, objective).run(dataset=dataset, baseline=baseline)

        assert len(result.core_salt) == 16  # secrets.token_hex(8) yields 16 hex characters


# -- frozen split -------------------------------------------------------------


class TestFrozenSplit:
    """The loop excludes frozen names everywhere and scores the yardstick exactly twice."""

    async def test_frozen_names_never_reach_a_working_evaluation(self):
        dataset = _dataset({f"pin.{i}": Archetype.PINPOINT for i in range(6)})
        names = _case_names(dataset)
        frozen = tuple(names[:2])
        baseline = baseline_policy()
        recall = {baseline.instructions: _uniform(names, 0.40), "v1": _uniform(names, 0.60)}
        evaluator = FakePolicyEvaluator(recall)
        objective = TuningObjective(val_split=0.5, max_rounds=2, patience=10)

        await TuningLoop(evaluator, FakeProposer(proposals=[_proposal("v1")]), objective).run(
            dataset=dataset, baseline=baseline, frozen_names=frozen
        )

        # Every evaluation except the two end-of-run frozen passes (train/core/tail AND validation)
        # is frozen-free: the yardstick never leaks into the search.
        working = evaluator.evaluate_calls[:-2]
        assert working
        assert all(not set(case_names) & set(frozen) for _, case_names in working)

    async def test_frozen_subset_scored_exactly_twice_baseline_then_winner_at_run_end(self):
        dataset = _dataset({f"pin.{i}": Archetype.PINPOINT for i in range(6)})
        names = _case_names(dataset)
        frozen = tuple(names[:2])
        baseline = baseline_policy()
        recall = {baseline.instructions: _uniform(names, 0.40), "v1": _uniform(names, 0.60)}
        evaluator = FakePolicyEvaluator(recall)
        objective = TuningObjective(val_split=0.0, max_rounds=2, patience=10)

        result = await TuningLoop(evaluator, FakeProposer(proposals=[_proposal("v1")]), objective).run(
            dataset=dataset, baseline=baseline, frozen_names=frozen
        )

        frozen_calls = [call for call in evaluator.evaluate_calls if set(call[1]) == set(frozen)]
        assert len(frozen_calls) == 2  # exactly twice: the baseline pass and the winner pass
        assert evaluator.evaluate_calls[-2:] == frozen_calls  # after the search ended
        assert [instructions for instructions, _ in frozen_calls] == [baseline.instructions, "v1"]
        assert result.frozen_baseline is not None
        assert result.frozen_baseline["recall@5"] == pytest.approx(0.40)
        assert result.frozen_winner is not None
        assert result.frozen_winner["recall@5"] == pytest.approx(0.60)

    async def test_without_frozen_names_the_frozen_fields_stay_none(self):
        dataset = _dataset({"p.a": Archetype.PINPOINT, "p.b": Archetype.PINPOINT})
        names = _case_names(dataset)
        baseline = baseline_policy()
        recall = {baseline.instructions: _uniform(names, 0.40), "v1": _uniform(names, 0.60)}
        objective = TuningObjective(val_split=0.0, max_rounds=2, patience=10)

        result = await TuningLoop(FakePolicyEvaluator(recall), FakeProposer(proposals=[_proposal("v1")]), objective).run(
            dataset=dataset, baseline=baseline
        )

        assert result.frozen_baseline is None
        assert result.frozen_winner is None
        assert result.frozen_beat is None
        assert result.applied_runtime is False
        assert result.apply_refusal is None


# -- core-case-score pairing ---------------------------------------------------


class TestCoreCaseScoresPairing:
    """`_core_case_scores` joins the report to the core by case name, loud on any name mismatch."""

    def test_reversed_report_yields_identical_per_case_map(self):
        # Three cases with DISTINCT recalls, so a positional mispair would visibly swap values.
        core = _dataset({"p.a": Archetype.PINPOINT, "p.b": Archetype.PINPOINT, "p.c": Archetype.PINPOINT})
        recalls = dict(zip(_case_names(core), (0.10, 0.50, 0.90), strict=True))
        in_order = _report_from(recalls)
        reversed_report = _Report(cases=list(reversed(in_order.cases)))

        assert _core_case_scores(reversed_report, core, "recall@5") == _core_case_scores(in_order, core, "recall@5")
        assert _core_case_scores(reversed_report, core, "recall@5") == recalls  # by name, not by position

    def test_report_missing_a_dataset_case_is_a_loud_error(self):
        core = _dataset({"p.a": Archetype.PINPOINT, "p.b": Archetype.PINPOINT, "p.c": Archetype.PINPOINT})
        names = _case_names(core)
        report = _report_from({names[0]: 0.10, names[1]: 0.50})  # drops the third dataset case

        with pytest.raises(ValueError, match="pair core scores by name"):
            _core_case_scores(report, core, "recall@5")

    def test_duplicate_report_names_are_a_loud_error(self):
        core = _dataset({"p.a": Archetype.PINPOINT, "p.b": Archetype.PINPOINT})
        first = _case_names(core)[0]
        report = _Report(cases=[_named_case(first, 0.10), _named_case(first, 0.90)])  # the report names collide

        with pytest.raises(ValueError, match="pair core scores by name"):
            _core_case_scores(report, core, "recall@5")

    def test_unnamed_dataset_case_is_a_loud_error(self):
        unnamed = EvaluationCase(
            name=None,
            inputs=QaInput(question="q"),
            expected_output=RetrievalAnswer(answer="a", cited_source_ids=["p"]),
            metadata=QaCaseMeta(anchor_page_id="p", difficulty="easy", archetype=Archetype.PINPOINT),
        )
        core = EvaluationDataset(name="core", cases=[unnamed])
        report = _report_from({"p-0": 0.50})

        with pytest.raises(ValueError, match="pair core scores by name"):
            _core_case_scores(report, core, "recall@5")


class TestFrozenBeatSurvivesShuffle:
    """The frozen verdict is invariant to report case order: pairing is by name."""

    async def test_frozen_verdict_identical_under_reversing_evaluator(self):
        dataset = _dataset({f"pin.{i}": Archetype.PINPOINT for i in range(6)})
        names = _case_names(dataset)
        frozen = tuple(names[:3])
        baseline = baseline_policy()
        # Distinct per-case recalls, so a positional mispairing of the frozen scores would move the
        # per-case deltas the frozen_beat guard reads (the maps are joined by name, not by position).
        recall = {
            baseline.instructions: dict(zip(names, (0.10, 0.30, 0.50, 0.20, 0.40, 0.60), strict=True)),
            "v1": dict(zip(names, (0.90, 0.70, 0.50, 0.80, 0.60, 0.40), strict=True)),
        }
        objective = TuningObjective(val_split=0.0, core_size=100, min_core_per_stratum=1, max_rounds=2, patience=10)

        in_order = await TuningLoop(FakePolicyEvaluator(recall), FakeProposer(proposals=[_proposal("v1")]), objective).run(
            dataset=dataset, baseline=baseline, core_salt="fixed-salt", frozen_names=frozen
        )
        shuffled = await TuningLoop(ReversingPolicyEvaluator(recall), FakeProposer(proposals=[_proposal("v1")]), objective).run(
            dataset=dataset, baseline=baseline, core_salt="fixed-salt", frozen_names=frozen
        )

        assert (shuffled.frozen_baseline, shuffled.frozen_winner, shuffled.frozen_beat) == (
            in_order.frozen_baseline,
            in_order.frozen_winner,
            in_order.frozen_beat,
        )
        assert [round_.accepted for round_ in shuffled.rounds] == [round_.accepted for round_ in in_order.rounds]


# -- reflection ---------------------------------------------------------------


@dataclass
class FakeReflector:
    """A `Reflector` that pops one scripted `DraftReflection` per round (no model, no network)."""

    drafts: list[DraftReflection]

    async def reflect(self, diagnosis: object) -> DraftReflection:
        del diagnosis
        return self.drafts.pop(0)


def _finding(*, metric: str = "recall@5", direction: str = "up", narrative: str = "n") -> ReflectiveFinding:
    """A gated-valid finding over the pinpoint/easy bucket every `_dataset` case lands in."""
    prediction = BucketPrediction(
        archetype=Archetype.PINPOINT, difficulty="easy", metric=metric, direction="up" if direction == "up" else "down"
    )
    return ReflectiveFinding(
        surface="S2", error_type="none", narrative=narrative, manifest=ChangeManifest(predictions=(prediction,), at_risk=())
    )


class TestReflection:
    """The loop reflects per round, passes gated findings to the proposer, and grades predictions next round."""

    async def test_rounds_carry_the_gated_findings_and_the_proposer_sees_them(self):
        dataset = _dataset({"p.a": Archetype.PINPOINT, "p.b": Archetype.PINPOINT})
        names = _case_names(dataset)
        baseline = baseline_policy()
        recall = {baseline.instructions: _uniform(names, 0.40), "v1": _uniform(names, 0.60), "v2": _uniform(names, 0.80)}
        finding_one, finding_two = _finding(narrative="round one"), _finding(narrative="round two")
        reflector = FakeReflector(drafts=[DraftReflection(findings=(finding_one,)), DraftReflection(findings=(finding_two,))])
        proposer = FakeProposer(proposals=[_proposal("v1"), _proposal("v2")])
        objective = TuningObjective(val_split=0.0, max_rounds=3, patience=10)

        result = await TuningLoop(FakePolicyEvaluator(recall), proposer, objective, reflector=reflector).run(
            dataset=dataset, baseline=baseline
        )

        assert [round_.findings for round_ in result.rounds] == [(finding_one,), (finding_two,)]
        assert proposer.findings_seen == [(finding_one,), (finding_two,)]

    async def test_predictions_grade_on_the_next_rounds_record(self):
        # Round 1 predicts recall@5 goes UP; round 2's candidate core moves 0.60 -> 0.80, so the
        # outcome (a hit) lands on ROUND 2's record, and round 1 carries no outcomes.
        dataset = _dataset({"p.a": Archetype.PINPOINT, "p.b": Archetype.PINPOINT})
        names = _case_names(dataset)
        baseline = baseline_policy()
        recall = {baseline.instructions: _uniform(names, 0.40), "v1": _uniform(names, 0.60), "v2": _uniform(names, 0.80)}
        reflector = FakeReflector(drafts=[DraftReflection(findings=(_finding(),)), DraftReflection(findings=())])
        objective = TuningObjective(val_split=0.0, max_rounds=3, patience=10)

        result = await TuningLoop(
            FakePolicyEvaluator(recall), FakeProposer(proposals=[_proposal("v1"), _proposal("v2")]), objective, reflector=reflector
        ).run(dataset=dataset, baseline=baseline)

        first, second = result.rounds
        assert first.prediction_outcomes == ()
        (outcome,) = second.prediction_outcomes
        assert outcome.measured_delta == pytest.approx(0.20)
        assert outcome.hit is True

    async def test_a_zero_delta_grades_as_a_miss(self):
        # v2 ties v1 on the core: the predicted "up" never materializes, a near-zero delta misses.
        dataset = _dataset({"p.a": Archetype.PINPOINT, "p.b": Archetype.PINPOINT})
        names = _case_names(dataset)
        baseline = baseline_policy()
        recall = {baseline.instructions: _uniform(names, 0.40), "v1": _uniform(names, 0.60), "v2": _uniform(names, 0.60)}
        reflector = FakeReflector(drafts=[DraftReflection(findings=(_finding(),)), DraftReflection(findings=())])
        objective = TuningObjective(val_split=0.0, max_rounds=3, patience=10)

        result = await TuningLoop(
            FakePolicyEvaluator(recall), FakeProposer(proposals=[_proposal("v1"), _proposal("v2")]), objective, reflector=reflector
        ).run(dataset=dataset, baseline=baseline)

        (outcome,) = result.rounds[1].prediction_outcomes
        assert outcome.measured_delta == pytest.approx(0.0)
        assert outcome.hit is False

    async def test_without_a_reflector_the_loop_is_unchanged(self):
        dataset = _dataset({"p.a": Archetype.PINPOINT, "p.b": Archetype.PINPOINT})
        names = _case_names(dataset)
        baseline = baseline_policy()
        recall = {baseline.instructions: _uniform(names, 0.40), "v1": _uniform(names, 0.60)}
        proposer = FakeProposer(proposals=[_proposal("v1")])
        objective = TuningObjective(val_split=0.0, max_rounds=2, patience=10)

        result = await TuningLoop(FakePolicyEvaluator(recall), proposer, objective).run(dataset=dataset, baseline=baseline)

        assert all(round_.findings == () for round_ in result.rounds)
        assert all(round_.prediction_outcomes == () for round_ in result.rounds)
        assert proposer.findings_seen == [()]  # the proposer saw the empty recommendation context
