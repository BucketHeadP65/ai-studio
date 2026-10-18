"""Hill-climb a retrieval policy against a dataset, deciding every accept on one fixed decision core.

`TuningLoop.run` splits the dataset into train and validation cases, carves the decision core out of the train
cases and measures the baseline on it once. Each round diagnoses the incumbent, asks the proposer for one
revision, measures the candidate on the core and accepts it only when the objective does. Each round also scores
a rotating tail of train cases outside the core, which feeds the diagnosis and never decides an accept. The
validation cases are scored for the baseline and for each accepted candidate, and they pick the winner among the
accepted incumbents. The loop does no I/O of its own and measures only through the injected `PolicyEvaluator`.
"""

from __future__ import annotations

import secrets

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from engram.evals.generation.contracts import EvaluationCase, EvaluationDataset, carve_strata, case_stratum
from engram.evals.harness.contracts import RetrievalMetrics
from engram.evals.rca.contracts import PredictionOutcome, ReflectiveFinding, diagnose, gate_reflection, grade_predictions
from engram.evals.shared.contracts import Archetype, RatedDifficulty
from engram.evals.tuning.domain.objective import StopReason
from engram.evals.tuning.policy import RetrievalPolicy

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from engram.evals.rca.contracts import CaseLike, Diagnosis, Reflector, ReportLike
    from engram.evals.tuning.domain.objective import TuningObjective
    from engram.evals.tuning.domain.proposal import ProposedPolicy
    from engram.evals.tuning.internal_contracts import PolicyEvaluator, PolicyProposer


# A stratum key: the case's archetype (`None` when unlabelled) and its difficulty band. The core,
# the tail, and the train/validation split are all stratified over this pair.
type _Stratum = tuple[Archetype | None, RatedDifficulty]

# The number of random bytes the run-boundary salt is generated from when the caller supplies none.
_SALT_BYTES = 8

# The name of the dataset the rotating tail is built as. It is never written to disk.
_TAIL_DATASET_NAME = "engram-retrieval-tail"


# -- the per-round and whole-run results -------------------------------------


class TuningRound(BaseModel):
    """One tuning round, the candidate proposed and how it fared on the core, tail, and hold-out.

    Attributes:
        index: The round's one-based position in the search.
        policy: The candidate policy proposed and evaluated this round.
        rationale: The proposer's one-line reason for the change.
        train_metrics: The candidate's per-metric score mapping on the decision core, a subset of the
            train split. The field keeps its name for the run-summary contract.
        tail_metrics: The candidate's per-metric score mapping on this round's rotating tail, or an
            empty mapping when the pool held no non-core cases to draw one.
        val_metrics: The candidate's per-metric score mapping on the validation split, populated only
            when the candidate was accepted (validation is scored on accept events), and `None` on a
            rejected round or when the hold-out is disabled (`val_split == 0`).
        accepted: Whether the candidate beat the incumbent on the core with the paired-bootstrap
            guard (and so became the new incumbent).
        findings: The round's gated reflective findings, the recommendation context the proposer saw this
            round, empty when no reflector is bound.
        prediction_outcomes: The PREVIOUS round's predictions graded against this round's measured
            decision-core bucket deltas (outcomes land on the next round's record), empty on round
            one or when the previous round drafted no predictions.
    """

    model_config = ConfigDict(frozen=True)

    index: int
    policy: RetrievalPolicy
    rationale: str
    train_metrics: Mapping[str, float]
    tail_metrics: Mapping[str, float]
    val_metrics: Mapping[str, float] | None
    accepted: bool
    findings: tuple[ReflectiveFinding, ...] = ()
    prediction_outcomes: tuple[PredictionOutcome, ...] = ()


class TuningResult(BaseModel):
    """The whole tuning run, the winner, the baseline, every round, the core identity, and why it stopped.

    Attributes:
        winning_policy: The policy the run promotes (the highest-validation accepted incumbent).
        baseline_policy: The incumbent the run started from.
        rounds: Every round run, in order.
        baseline_train_metrics: The baseline's per-metric score mapping on the decision core.
        baseline_val_metrics: The baseline's validation score mapping, or `None` when disabled.
        winner_train_metrics: The winner's core score mapping.
        winner_val_metrics: The winner's validation score mapping, or `None` when disabled.
        stop_reason: The convergence trigger that ended the loop, or `None` when no round ran.
        core_salt: The salt the decision core was carved with, recorded so the run reproduces. The
            caller may supply it, otherwise the loop generates one at the run boundary.
        core_size: The number of cases actually carved into the decision core (capped by the train
            split when the configured `core_size` exceeds it).
        tail_size: The configured rotating-tail size each round draws toward.
        core_tail_divergence: The per-round core-minus-tail primary-metric mean difference (aligned
            with `rounds`), a positive value flagging a candidate that does better on the core than
            on fresh tail cases (an overfit signal), `0.0` when the round drew no tail.
        frozen_baseline: The baseline's score mapping on the frozen split, or `None` when the
            run had no frozen split to score.
        frozen_winner: The winner's score mapping on the frozen split, or `None` when the
            run had no frozen split to score.
        frozen_beat: Whether the winner beat the incumbent on the frozen split under the paired
            guard (the machine-rung gate), `None` when the run had no frozen split to score.
        applied_runtime: Whether this run applied its winner to the knowledge base's runtime policy
            (`tune --apply-runtime` with all four guardrails green).
        apply_refusal: The guardrail that refused the runtime apply, or `None` when no apply was
            requested or the apply went through.
    """

    model_config = ConfigDict(frozen=True)

    winning_policy: RetrievalPolicy
    baseline_policy: RetrievalPolicy
    rounds: tuple[TuningRound, ...]
    baseline_train_metrics: Mapping[str, float]
    baseline_val_metrics: Mapping[str, float] | None
    winner_train_metrics: Mapping[str, float]
    winner_val_metrics: Mapping[str, float] | None
    stop_reason: StopReason | None
    core_salt: str
    core_size: int
    tail_size: int
    core_tail_divergence: tuple[float, ...]
    frozen_baseline: Mapping[str, float] | None = None
    frozen_winner: Mapping[str, float] | None = None
    frozen_beat: bool | None = None
    applied_runtime: bool = False
    apply_refusal: str | None = None


# -- the decision core and rotating tail -------------------------------------


class FoldedStratum(BaseModel):
    """A decision-core stratum too thin to trust on its own, folded into its parent difficulty band.

    Recorded so the fold is visible in the run rather than silent: a stratum whose train-split pool
    is below `min_per_stratum` joins the difficulty band instead of being trusted as its own.

    Attributes:
        archetype: The folded stratum's archetype, or `None` for an unlabelled stratum.
        difficulty: The folded stratum's difficulty band (the band it folded into).
    """

    model_config = ConfigDict(frozen=True)

    archetype: Archetype | None
    difficulty: RatedDifficulty


class CoreTail(BaseModel):
    """The carved decision core plus the pool the rotating tails are drawn from.

    Attributes:
        core: The fixed decision core, an `EvaluationDataset` carrying the source dataset's evaluators so
            it scores identically, every accept decision in the run is measured on it.
        pool: The train-split cases outside the core, the reservoir `draw_tail` samples each round.
        folded: The strata that folded into their parent difficulty band under the min-per-stratum
            floor, empty when no stratum was thin.
    """

    model_config = ConfigDict(frozen=True)

    core: EvaluationDataset
    pool: tuple[EvaluationCase, ...]
    folded: tuple[FoldedStratum, ...] = ()


def carve_core(train: EvaluationDataset, *, core_size: int, min_per_stratum: int, salt: str) -> CoreTail:
    """Carve one stratified decision core out of the train split by the salted-hash rule.

    The train cases are grouped by their `(archetype, difficulty)` stratum. A stratum whose pool is
    below `min_per_stratum` folds into its parent difficulty band (an archetype-agnostic bucket for
    that band) and is recorded in `folded`, so a thin stratum is never silently trusted as its own.
    `core_size` cases (capped by the train split) are then allocated across the resulting bands
    proportionally to their pool sizes by largest remainder, and within each band the leading cases in
    salted-hash order are taken. The draw is pure and deterministic given `salt`, so the same salt
    always yields the same core and a different salt draws a different one.

    Args:
        train: The train split to carve the core out of.
        core_size: The target core size, capped at the number of train cases.
        min_per_stratum: The floor below which a stratum folds into its parent difficulty band.
        salt: The salt the salted-hash draw is keyed on (recorded by the caller for reproduction).

    Returns:
        A `CoreTail` carrying the carved core, the remaining pool, and the folded strata.
    """
    cases = list(train.cases)
    core_cases, folded = carve_strata(cases, total=min(core_size, len(cases)), min_per_stratum=min_per_stratum, salt=salt)

    core_names = {case.name for case in core_cases}
    pool = tuple(case for case in cases if case.name not in core_names)
    return CoreTail(
        core=_rebuild(train, core_cases),
        pool=pool,
        folded=tuple(FoldedStratum(archetype=archetype, difficulty=difficulty) for archetype, difficulty in folded),
    )


def draw_tail(pool: Sequence[EvaluationCase], *, tail_size: int, salt: str, round_index: int) -> EvaluationDataset:
    """Draw one round's rotating tail, a fresh stratified sample of the non-core pool.

    The pool is grouped by stratum, `tail_size` cases (capped by the pool) are allocated across the
    strata by largest remainder, and within each stratum the leading cases in salted-hash order are
    taken, keyed on `"{name}|{salt}|{round_index}"` so each round draws a fresh sample from the same
    reservoir. Because the pool excludes the core, the tail is disjoint from the core by construction.
    An empty pool yields an empty tail.

    Args:
        pool: The non-core train cases the tail is sampled from (the `CoreTail.pool`).
        tail_size: The target tail size, capped at the pool size.
        salt: The run's salt, combined with the round index into the per-round draw key.
        round_index: The one-based round the tail is drawn for (rotates the sample across rounds).

    Returns:
        An `EvaluationDataset` of the sampled cases, carrying the retrieval evaluator so the loop can score it.
    """
    selected, _folded = carve_strata(pool, total=min(tail_size, len(pool)), min_per_stratum=0, salt=f"{salt}|{round_index}")
    return EvaluationDataset(name=_TAIL_DATASET_NAME, cases=selected, evaluators=[RetrievalMetrics()])


# -- the train / validation split --------------------------------------------


def _stratified_split(dataset: EvaluationDataset, *, val_split: float) -> tuple[EvaluationDataset, EvaluationDataset]:
    """Split a dataset into a train and a validation hold-out, stratified by archetype and difficulty."""
    cases = list(dataset.cases)
    if val_split == 0.0:
        return _rebuild(dataset, cases), _rebuild(dataset, [])

    groups: dict[_Stratum, list[EvaluationCase]] = {}
    for case in cases:
        groups.setdefault(case_stratum(case), []).append(case)

    val_names: set[str] = set()
    for group in groups.values():
        ordered = sorted(group, key=_name)
        n_val = round(val_split * len(ordered))
        val_names.update(_name(case) for case in ordered[:n_val])

    val = [case for case in cases if _name(case) in val_names]
    train = [case for case in cases if _name(case) not in val_names]
    return _rebuild(dataset, train), _rebuild(dataset, val)


def _name(case: EvaluationCase) -> str:
    """The case's name as a sortable, hashable string (`""` when the dataset left it unset)."""
    return case.name or ""


def _rebuild(dataset: EvaluationDataset, cases: Sequence[EvaluationCase]) -> EvaluationDataset:
    """Rebuild a dataset over `cases`, carrying the source dataset's name and evaluators."""
    return EvaluationDataset(name=dataset.name, cases=list(cases), evaluators=list(dataset.evaluators))


# -- the core-plus-tail diagnosis view ---------------------------------------


@dataclass(frozen=True)
class _CombinedReport:
    """A `ReportLike` view concatenating several reports' cases, feeding the core-plus-tail diagnosis."""

    cases: Sequence[CaseLike]


def _combine(core_report: ReportLike, tail_report: ReportLike) -> ReportLike:
    """Concatenate a core report and a tail report into one `ReportLike` for a core-plus-tail diagnosis."""
    return _CombinedReport(cases=[*core_report.cases, *tail_report.cases])


@dataclass(frozen=True, slots=True)
class _CoreMeasurement:
    """One policy's measurement on the decision core: the report, its metric means, per-case primary scores."""

    report: ReportLike
    means: dict[str, float]
    per_case_primary: dict[str, float]


# -- the loop ----------------------------------------------------------------


class TuningLoop:
    """Hill-climbs a retrieval policy against a dataset, deciding on a fixed decision core."""

    def __init__(
        self, evaluator: PolicyEvaluator, proposer: PolicyProposer, objective: TuningObjective, *, reflector: Reflector | None = None
    ) -> None:
        """Bind the measurement port, the proposer brain, and the accept / convergence objective.

        Args:
            evaluator: The `PolicyEvaluator` that scores a policy on a dataset into a report.
            proposer: The `PolicyProposer` that emits one targeted revision per round.
            objective: The `TuningObjective` owning the accept rule, the convergence rule, the
                split fraction, the core / tail sizes, and the primary / cost metric names.
            reflector: The optional reflection brain: when bound, each round's diagnosis is
                reflected into gated recommendation findings for the proposer and the previous round's
                predictions are graded. `None` (the default) runs the loop without reflection.
        """
        self._evaluator = evaluator
        self._proposer = proposer
        self._objective = objective
        self._reflector = reflector

    async def run(
        self, *, dataset: EvaluationDataset, baseline: RetrievalPolicy, core_salt: str | None = None, frozen_names: Collection[str] = ()
    ) -> TuningResult:
        """Run the tuning loop from `baseline` over `dataset`, returning the run's result.

        The incumbent is measured on the core once and reused, so every accept comparison is
        apples-to-apples. The winner is the highest-validation accepted incumbent (the baseline
        included, ties keeping the earliest), or the last accepted incumbent on the core when the
        validation hold-out is disabled.

        Args:
            dataset: The dataset to tune against.
            baseline: The incumbent policy the search starts from.
            core_salt: The salt to carve the decision core with, for reproducing a prior run. When
                `None` the loop generates one at the run boundary. Either way it is recorded on the
                result.
            frozen_names: The frozen-split case names, excluded from the dataset before the
                split and core/tail carving, so the loop never trains on, proposes against, accepts
                on, or validates with them. The baseline and the winner are each scored once on the
                frozen subset at run end. Empty (the default) leaves `frozen_baseline` /
                `frozen_winner` `None`.

        Returns:
            The `TuningResult` carrying the winner, the baseline, every round, the core identity,
            and the stop reason.

        Raises:
            ValueError: When the dataset or a report cannot be paired by case name, because a dataset case carries
                no name, a report repeats a case name, or the dataset and a report disagree on the names.
        """
        frozen, dataset = _split_frozen(dataset, frozen_names)
        train, val = _stratified_split(dataset, val_split=self._objective.val_split)
        has_val = bool(val.cases)
        salt = core_salt or secrets.token_hex(_SALT_BYTES)
        core_tail = carve_core(train, core_size=self._objective.core_size, min_per_stratum=self._objective.min_core_per_stratum, salt=salt)
        core = core_tail.core
        primary, cost = self._objective.primary_metric, self._objective.cost_metric

        baseline_measurement = await self._measure_core(baseline, core)
        baseline_core = baseline_measurement.means
        baseline_val = self._score(await self._evaluator.evaluate(baseline, dataset=val)) if has_val else None

        incumbent = baseline
        incumbent_core_report = baseline_measurement.report
        incumbent_core = baseline_core
        incumbent_core_cases = baseline_measurement.per_case_primary
        # Baseline first, each kept with its core and its validation score so the winner pick can
        # take the highest-validation one.
        accepted: list[_Accepted] = [_Accepted(policy=baseline, core=baseline_core, val=baseline_val)]
        # Seed the convergence trajectory with the baseline so `max_rounds=3` runs two proposal rounds
        # and a baseline-tying stream stops on patience with the baseline as the running best.
        trajectory: list[Mapping[str, float]] = [baseline_core]

        rounds: list[TuningRound] = []
        divergences: list[float] = []
        tokens_spent = 0.0
        stop_reason: StopReason | None = None
        state = _ReflectionState()

        while True:
            round_index = len(rounds) + 1
            tail = draw_tail(core_tail.pool, tail_size=self._objective.tail_size, salt=salt, round_index=round_index)
            proposed, findings = await self._propose_next(incumbent, incumbent_core_report, tail)
            candidate = proposed.policy

            candidate_measurement = await self._measure_core(candidate, core)
            candidate_core = candidate_measurement.means
            candidate_tail = self._score(await self._evaluator.evaluate(candidate, dataset=tail)) if tail.cases else {}
            divergence = candidate_core.get(primary, 0.0) - candidate_tail.get(primary, 0.0) if tail.cases else 0.0

            decision = self._objective.accepts(
                candidate_measurement.per_case_primary, incumbent_core_cases, candidate_core.get(cost), incumbent_core.get(cost)
            )
            candidate_val = self._score(await self._evaluator.evaluate(candidate, dataset=val)) if (decision.accepted and has_val) else None

            rounds.append(
                TuningRound(
                    index=round_index,
                    policy=candidate,
                    rationale=proposed.rationale,
                    train_metrics=candidate_core,
                    tail_metrics=candidate_tail,
                    val_metrics=candidate_val,
                    accepted=decision.accepted,
                    findings=findings,
                    prediction_outcomes=state.grade(findings, candidate_measurement.report),
                )
            )
            divergences.append(divergence)
            if decision.accepted:
                incumbent = candidate
                incumbent_core_report = candidate_measurement.report
                incumbent_core = candidate_core
                incumbent_core_cases = candidate_measurement.per_case_primary
                accepted.append(_Accepted(policy=candidate, core=candidate_core, val=candidate_val))

            trajectory.append(candidate_core)
            tokens_spent += candidate_core.get(cost, 0.0)
            stop = self._objective.should_stop(trajectory, tokens_spent=tokens_spent)
            if stop.should_stop:
                stop_reason = stop.reason
                break

        winner = self._pick_winner(accepted, has_val=has_val)
        frozen_baseline, frozen_winner, frozen_beat = await self._score_frozen(frozen, baseline=baseline, winner=winner.policy)
        return TuningResult(
            winning_policy=winner.policy,
            baseline_policy=baseline,
            rounds=tuple(rounds),
            baseline_train_metrics=baseline_core,
            baseline_val_metrics=baseline_val,
            winner_train_metrics=winner.core,
            winner_val_metrics=winner.val,
            stop_reason=stop_reason,
            core_salt=salt,
            core_size=len(core.cases),
            tail_size=self._objective.tail_size,
            core_tail_divergence=tuple(divergences),
            frozen_baseline=frozen_baseline,
            frozen_winner=frozen_winner,
            frozen_beat=frozen_beat,
        )

    async def _score_frozen(
        self, frozen: EvaluationDataset | None, *, baseline: RetrievalPolicy, winner: RetrievalPolicy
    ) -> tuple[dict[str, float] | None, dict[str, float] | None, bool | None]:
        """Score the baseline then the winner once each on the frozen split, all `None` without one."""
        if frozen is None or not frozen.cases:
            return None, None, None
        baseline_report = await self._evaluator.evaluate(baseline, dataset=frozen)
        winner_report = await self._evaluator.evaluate(winner, dataset=frozen)
        primary = self._objective.primary_metric
        beat = self._objective.frozen_beat(
            _core_case_scores(winner_report, frozen, primary), _core_case_scores(baseline_report, frozen, primary)
        )
        return self._score(baseline_report), self._score(winner_report), beat

    async def _measure_core(self, policy: RetrievalPolicy, core: EvaluationDataset) -> _CoreMeasurement:
        """Score `policy` on the decision core, returning its report, metric means, and per-case primary scores."""
        report = await self._evaluator.evaluate(policy, dataset=core)
        primary = _core_case_scores(report, core, self._objective.primary_metric)
        return _CoreMeasurement(report=report, means=self._score(report), per_case_primary=primary)

    async def _propose_next(
        self, incumbent: RetrievalPolicy, incumbent_core_report: ReportLike, tail: EvaluationDataset
    ) -> tuple[ProposedPolicy, tuple[ReflectiveFinding, ...]]:
        """Diagnose the incumbent on its core plus this round's tail and ask the proposer for one revision."""
        tail_report = await self._evaluator.evaluate(incumbent, dataset=tail) if tail.cases else None
        signal_report = _combine(incumbent_core_report, tail_report) if tail_report is not None else incumbent_core_report
        diagnosis = diagnose(signal_report)
        findings = gate_reflection(await self._reflector.reflect(diagnosis), diagnosis) if self._reflector is not None else ()
        return await self._proposer.propose(incumbent, diagnosis, findings=findings), findings

    def _score(self, report: ReportLike) -> dict[str, float]:
        """Fold a report into the per-metric score mapping the objective compares, with its cost and duration."""
        signal = diagnose(report)
        return {**signal.overall, self._objective.cost_metric: signal.mean_total_tokens or 0.0, "duration_s": signal.mean_duration_s}

    def _pick_winner(self, accepted: Sequence[_Accepted], *, has_val: bool) -> _Accepted:
        """The highest-validation accepted incumbent, ties keeping the earliest, or the last one without validation."""
        if not has_val:
            return accepted[-1]
        primary = self._objective.primary_metric
        return max(accepted, key=lambda candidate: candidate.val[primary] if candidate.val is not None else 0.0)


class _ReflectionState:
    """Round-to-round reflection state: the last round's findings and decision-core diagnosis."""

    def __init__(self) -> None:
        self._findings: tuple[ReflectiveFinding, ...] = ()
        self._diagnosis: Diagnosis | None = None

    def grade(self, findings: tuple[ReflectiveFinding, ...], candidate_core_report: ReportLike) -> tuple[PredictionOutcome, ...]:
        """Grade the previous round's predictions against this round's core, then roll the state forward."""
        diagnosis = diagnose(candidate_core_report)
        outcomes = grade_predictions(self._findings, before=self._diagnosis, after=diagnosis) if self._diagnosis is not None else ()
        self._findings, self._diagnosis = findings, diagnosis
        return outcomes


def _split_frozen(dataset: EvaluationDataset, frozen_names: Collection[str]) -> tuple[EvaluationDataset | None, EvaluationDataset]:
    """Partition `dataset` into the frozen subset and the remainder, the subset `None` when no names are given."""
    selected = frozenset(frozen_names)
    if not selected:
        return None, dataset
    frozen = _rebuild(dataset, [case for case in dataset.cases if (case.name or "") in selected])
    remainder = _rebuild(dataset, [case for case in dataset.cases if (case.name or "") not in selected])
    return frozen, remainder


def _core_case_scores(report: ReportLike, core: EvaluationDataset, primary_metric: str) -> dict[str, float]:
    """Each core case's primary-metric score, joined to the report by case name, raising `ValueError` on a mismatch."""
    by_name: dict[str, CaseLike] = {}
    for report_case in report.cases:
        if report_case.name in by_name:
            msg = f"cannot pair core scores by name: report duplicates case name {report_case.name!r}"
            raise ValueError(msg)
        by_name[report_case.name] = report_case

    names: list[str] = []
    for case in core.cases:
        if not case.name:
            msg = "cannot pair core scores by name: a dataset case carries no name"
            raise ValueError(msg)
        names.append(case.name)

    if mismatch := set(names).symmetric_difference(by_name):
        msg = f"cannot pair core scores by name: dataset and report disagree on {sorted(mismatch)!r}"
        raise ValueError(msg)

    scores: dict[str, float] = {}
    for name in names:
        score = by_name[name].scores.get(primary_metric)
        if score is not None:
            scores[name] = float(score.value)
    return scores


class _Accepted(BaseModel):
    """An incumbent kept for the winner pick, the policy and its core and validation score mappings."""

    model_config = ConfigDict(frozen=True)

    policy: RetrievalPolicy
    core: Mapping[str, float]
    val: Mapping[str, float] | None
