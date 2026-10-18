"""Case-custody governance for outcome-evaluation cycles: development vs independently withheld.

Every evaluation case is either `author_visible` (an ordinary development case, fair game for
author prompts, examples, tuning and development feedback) or `independently_withheld` (held out
from all of that, its label is usable only to grade a trial after the fact). A `CaseSeal` records
who authored and sealed a case, when, and against which implementation. `LabelExposure` records
every recorded instance of an actor seeing a sealed case's label. `audit_cycle` folds one cycle's
cases, seals and exposures into a `MethodologyAudit`: which cases stayed usable, and whether the
cycle's independently-withheld claims actually hold up.

Recorded custody (`authored_by`, `sealed_by`, an exposure's `actor`) is a good-faith record of who
touched what, not cryptographic proof of identity. `audit_cycle` verifies internal consistency,
timing against the frozen implementation, disjoint authorship, and the absence of a recorded
exposure, never who actually held the keyboard. Every actor name (`authored_by`, `sealed_by`,
`implementation_authors`, an exposure's `actor`) is folded, whitespace collapsed and casefolded,
before any of these comparisons, so a stray capital letter or trailing space cannot pass a
disjointness check that should fail.
"""

from __future__ import annotations

import unicodedata

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from core.ai.evaluation.contracts import EvaluationModel, Fingerprint, Nonempty, Split, require_unique
from pydantic import AwareDatetime, Field

from engram.evals.generation.contracts import CaseFreshness, CasePremise, is_audit_invalidating

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from core.ai.evaluation.contracts import TaskCase

CaseVisibility = Literal["author_visible", "independently_withheld"]
LabelExposureRole = Literal["author", "implementer", "custodian", "grader"]


class CaseSeal(EvaluationModel):
    """A case's held-out custody record: who authored and sealed it, and against what implementation.

    A held-out claim additionally requires `sealed_at` strictly after the implementation's freeze
    time and `implementation_fingerprint` equal to the exact frozen fingerprint (`audit_cycle`
    checks both). `authored_by` and `sealed_by` are self-reported custody, never verified identity.
    """

    case_id: Nonempty
    visibility: CaseVisibility
    authored_by: tuple[Nonempty, ...] = Field(min_length=1)
    sealed_by: Nonempty
    sealed_at: AwareDatetime
    implementation_fingerprint: Fingerprint
    label_fingerprint: Fingerprint
    source_snapshot_fingerprint: Fingerprint


class LabelExposure(EvaluationModel):
    """One recorded instance of an actor seeing a case's label.

    A `grader` or `custodian` exposure is expected and never a violation, including after freeze. An
    `author` or `implementer` exposure to an independently-withheld case's label breaks that case's
    independence claim outright, regardless of when it happened.
    """

    exposure_id: Nonempty
    case_id: Nonempty
    actor: Nonempty
    role: LabelExposureRole
    purpose: Nonempty
    at: AwareDatetime
    label_fingerprint: Fingerprint


class MethodologyAudit(EvaluationModel):
    """One cycle's case-custody verdict: which cases stayed usable, and whether holdout held.

    A case moves from `valid_case_ids` to `invalid_case_ids`, without being removed from the
    cycle's history, on any of: a test-split case carrying no seal, a test-split case sealed
    `author_visible` (a development case wearing the test split), a normalized-input collision with
    another case, a label-fingerprint collision with another seal, an era-invalidating freshness
    verdict (`gold_gone` or `mirror_stale` under any era, `page_drifted` only for a page-anchored
    case, see `freshness.is_audit_invalidating`), or a broken structural premise.
    `independent_holdout_verified` is true only when the cycle declares at least one
    held-out claim (an `independently_withheld` seal, or a seal-less test-split case counted as a
    failed claim) and every declared claim is both valid and custody-clean. `verified_case_ids` is
    the attributable subset of `independently_withheld`-sealed cases that passed every custody rule,
    so the boolean can be traced back to specific cases even when other cases in the same cycle failed.
    """

    independent_holdout_verified: bool
    valid_case_ids: tuple[Nonempty, ...]
    invalid_case_ids: tuple[Nonempty, ...]
    verified_case_ids: tuple[Nonempty, ...]
    reasons: tuple[Nonempty, ...]


def normalize_case_text(text: str) -> str:
    """Fold case text to its overlap-comparison identity.

    Unicode-normalizes to NFC (canonical composition, e.g. folding a combining accent onto its base
    letter) rather than NFKC. NFC leaves compatibility variants alone, full-width vs half-width
    forms, roman numerals, superscript or subscript digits, and similar typographic distinctions,
    where NFKC would additionally collapse them onto a shared form. Treating two inputs a person
    would read as meaningfully different as the same case is the worse failure for a custody audit.
    Runs of whitespace collapse to a single space, the ends are trimmed, and the result is
    casefolded (which already folds case-sensitive ligatures on its own), so formatting alone never
    masks or manufactures an overlap.

    Args:
        text: The case input to fold.

    Returns:
        The NFC-normalized, whitespace-folded, casefolded comparison identity.
    """
    return " ".join(unicodedata.normalize("NFC", text).split()).casefold()


def _normalize_actor(name: str) -> str:
    """Fold an actor name to its comparison identity: whitespace collapsed and casefolded."""
    return " ".join(name.split()).casefold()


def _exposure_violates(exposure: LabelExposure, *, implementation_authors: frozenset[str]) -> bool:
    """Whether one exposure breaks a held-out claim: an `author` or `implementer` role, or an implementation author under any role."""
    if exposure.role in {"author", "implementer"}:
        return True
    return _normalize_actor(exposure.actor) in implementation_authors


def _relevant_exposures(
    seal: CaseSeal, *, exposures_by_label: dict[str, list[LabelExposure]], exposures_by_case: dict[str, list[LabelExposure]]
) -> tuple[LabelExposure, ...]:
    """Every exposure tied to a seal's case, matched by label fingerprint or by case id."""
    return (*exposures_by_label.get(seal.label_fingerprint, []), *exposures_by_case.get(seal.case_id, []))


def _custody_failures(
    seal: CaseSeal,
    *,
    case_split: Split,
    implementation_fingerprint: str,
    implementation_frozen_at: datetime,
    implementation_authors: frozenset[str],
    exposures: tuple[LabelExposure, ...],
) -> tuple[str, ...]:
    """Every independence rule this seal's held-out claim fails, in check order, each checked on its own."""
    if seal.visibility != "independently_withheld":
        return ("visibility",) if case_split == "test" else ()
    failures: list[str] = []
    if seal.sealed_at <= implementation_frozen_at:
        failures.append("freeze timing")
    if seal.implementation_fingerprint != implementation_fingerprint:
        failures.append("fingerprint mismatch")
    if not implementation_authors:
        failures.append("no recorded implementation authors")
    else:
        custodians = {_normalize_actor(name) for name in (*seal.authored_by, seal.sealed_by)}
        if custodians & implementation_authors:
            failures.append("authorship not disjoint")
    if any(_exposure_violates(exposure, implementation_authors=implementation_authors) for exposure in exposures):
        failures.append("exposure")
    return tuple(failures)


def _collision_group(keyed_case_ids: Iterable[tuple[str, str]]) -> frozenset[str]:
    """Case ids that share a comparison key with at least one other case (a duplicate group)."""
    groups: dict[str, list[str]] = {}
    for key, case_id in keyed_case_ids:
        groups.setdefault(key, []).append(case_id)
    return frozenset(case_id for group in groups.values() if len(group) > 1 for case_id in group)


@dataclass(frozen=True)
class _Collisions:
    """This cycle's overlap-rejection sets: case ids sharing a normalized input or a label fingerprint."""

    inputs: frozenset[str]
    labels: frozenset[str]


def _structural_reasons(
    case: TaskCase, *, has_seal: bool, collisions: _Collisions, verdict: CaseFreshness | None, premise: CasePremise | None
) -> list[str]:
    """One case's invalidity reasons that hold regardless of any held-out claim."""
    reasons: list[str] = []
    if case.split == "test" and not has_seal:
        reasons.append(f"{case.case_id}: test-split case carries no custody seal")
    if case.case_id in collisions.inputs:
        reasons.append(f"{case.case_id}: normalized input collides with another case")
    if case.case_id in collisions.labels:
        reasons.append(f"{case.case_id}: label fingerprint collides with another case")
    if verdict is not None and is_audit_invalidating(verdict):
        reasons.append(f"{case.case_id}: freshness verdict {verdict.verdict}")
    if premise is not None and not premise.intact:
        reasons.append(f"{case.case_id}: broken structural premise ({'; '.join(premise.missing)})")
    return reasons


def audit_cycle(
    *,
    cases: tuple[TaskCase, ...],
    seals: tuple[CaseSeal, ...],
    exposures: tuple[LabelExposure, ...],
    implementation_fingerprint: Fingerprint,
    implementation_frozen_at: datetime,
    implementation_authors: tuple[str, ...],
    freshness: tuple[CaseFreshness, ...] = (),
    premises: tuple[CasePremise, ...] = (),
) -> MethodologyAudit:
    """Audit one cycle's cases for custody validity and independent-holdout verification.

    A case becomes invalid, without being removed from `cases`, when any of: a test-split case
    carries no seal, its normalized input collides with another case's, its seal's label fingerprint
    collides with another seal's, its freshness verdict is era-invalidating (`gold_gone` or
    `mirror_stale` under any era, `page_drifted` only for a page-anchored case, see
    `freshness.is_audit_invalidating`), or its structural premise is broken. A held-out claim fails
    on any of six independently checked rules: `visibility` (a test-split case sealed
    `author_visible`, a development case wearing the test split), `freeze timing` (`sealed_at` not
    strictly after `implementation_frozen_at`), `fingerprint mismatch` (against the exact frozen
    implementation fingerprint), `no recorded implementation authors` (an empty
    `implementation_authors` makes disjointness unfalsifiable, so it fails closed rather than
    vacuously passing), `authorship not disjoint` (`authored_by` or `sealed_by` overlaps
    `implementation_authors`), or `exposure` (a recorded `author`/`implementer`-role exposure, or any
    exposure whose actor is a recorded implementation author regardless of its role tag, matched by
    either the seal's label fingerprint or its case id). Every failing rule lands as its own entry in
    `reasons`, and every invalidity leaves the acceptance comparison for this cycle incomplete, an
    added `reasons` entry naming that too. A `grader` or `custodian` exposure whose actor is not an
    implementation author, at any time, is never a violation. Every actor name compared here is
    folded first (whitespace-collapsed, casefolded).

    `independent_holdout_verified` is true only when the cycle declares at least one held-out claim,
    an `independently_withheld` seal or a seal-less test-split case (counted as a failed claim), and
    every declared claim is both valid by the checks above and custody-clean. `verified_case_ids`
    names the specific cases that passed every custody rule, whether or not the cycle-level boolean
    holds.

    Args:
        cases: The cycle's task cases, unique by `case_id`.
        seals: The custody seal recorded for each case that carries one, unique by `case_id`.
        exposures: Every recorded instance of an actor seeing a sealed case's label.
        implementation_fingerprint: The implementation's fingerprint as of this cycle.
        implementation_frozen_at: When that implementation was frozen, timezone-aware.
        implementation_authors: Every actor with implementation knowledge, disjoint from a held-out
            case's own authorship and custody for its claim to hold.
        freshness: The cycle's per-case freshness verdicts, from a live knowledge base sweep.
        premises: The cycle's per-case structural-premise checks.

    Returns:
        The cycle's `MethodologyAudit`.

    Raises:
        ValueError: When `cases` or `seals` carries a duplicate `case_id`, or when
            `implementation_frozen_at` is timezone-naive.
    """
    if implementation_frozen_at.tzinfo is None:
        msg = "implementation_frozen_at must be timezone-aware"
        raise ValueError(msg)
    require_unique((case.case_id for case in cases), "case ids")
    require_unique((seal.case_id for seal in seals), "seal case ids")
    implementation_author_set = frozenset(_normalize_actor(name) for name in implementation_authors)
    seal_by_case = {seal.case_id: seal for seal in seals}

    exposures_by_label: dict[str, list[LabelExposure]] = {}
    exposures_by_case: dict[str, list[LabelExposure]] = {}
    for exposure in exposures:
        exposures_by_label.setdefault(exposure.label_fingerprint, []).append(exposure)
        exposures_by_case.setdefault(exposure.case_id, []).append(exposure)

    collisions = _Collisions(
        inputs=_collision_group((normalize_case_text(case.input), case.case_id) for case in cases),
        labels=_collision_group((seal.label_fingerprint, seal.case_id) for seal in seals),
    )
    freshness_by_case = {verdict.case_name: verdict for verdict in freshness}
    premise_by_case = {premise.case_name: premise for premise in premises}

    valid_ids: list[str] = []
    invalid_ids: list[str] = []
    verified_ids: list[str] = []
    reasons: list[str] = []
    holdout_declared = False
    holdout_failed = False

    for case in cases:
        seal = seal_by_case.get(case.case_id)
        case_reasons = _structural_reasons(
            case,
            has_seal=seal is not None,
            collisions=collisions,
            verdict=freshness_by_case.get(case.case_id),
            premise=premise_by_case.get(case.case_id),
        )

        custody_failures: tuple[str, ...] = ()
        if seal is not None:
            custody_failures = _custody_failures(
                seal,
                case_split=case.split,
                implementation_fingerprint=implementation_fingerprint,
                implementation_frozen_at=implementation_frozen_at,
                implementation_authors=implementation_author_set,
                exposures=_relevant_exposures(seal, exposures_by_label=exposures_by_label, exposures_by_case=exposures_by_case),
            )
        for failure in custody_failures:
            case_reasons.append(f"{case.case_id}: independence claim failed ({failure})")

        claims_holdout = (case.split == "test" and seal is None) or (seal is not None and seal.visibility == "independently_withheld")
        holdout_declared = holdout_declared or claims_holdout
        holdout_failed = holdout_failed or (claims_holdout and bool(case_reasons))
        if seal is not None and seal.visibility == "independently_withheld" and not case_reasons:
            verified_ids.append(case.case_id)

        if case_reasons:
            invalid_ids.append(case.case_id)
            reasons.extend(case_reasons)
        else:
            valid_ids.append(case.case_id)

    if invalid_ids:
        reasons.append("acceptance comparison incomplete: invalid cases excluded")

    return MethodologyAudit(
        independent_holdout_verified=holdout_declared and not holdout_failed,
        valid_case_ids=tuple(valid_ids),
        invalid_case_ids=tuple(invalid_ids),
        verified_case_ids=tuple(verified_ids),
        reasons=tuple(reasons),
    )
