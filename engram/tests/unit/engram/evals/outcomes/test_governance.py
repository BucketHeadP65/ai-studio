"""Unit specs for case-custody governance (`engram.evals.outcomes.domain.governance`).

Pins the independence rules `audit_cycle` enforces over a batch of task cases, custody seals and
label exposures: what makes a held-out claim verifiable, what invalidates a case without deleting
it, and the normalized-overlap checks that guard against silent contamination between development
and independently withheld cases.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.ai.evaluation.contracts import Provenance, TaskCase

from engram.evals.generation.contracts import CaseFreshness, CasePremise
from engram.evals.outcomes.domain.governance import CaseSeal, CaseVisibility, LabelExposure, audit_cycle, normalize_case_text
from engram.evals.shared.domain import Archetype

IMPLEMENTATION = "a" * 64
LABEL = "b" * 64
SNAPSHOT = "c" * 64
FROZEN_AT = datetime(2026, 9, 13, tzinfo=UTC)
AFTER_FREEZE = datetime(2026, 9, 14, tzinfo=UTC)
BEFORE_FREEZE = datetime(2026, 9, 12, tzinfo=UTC)

_PROVENANCE = Provenance(source="owner-case", kind="recorded", review="owner", reviewer="owner")

CASE = TaskCase(
    case_id="held-1",
    input="Design a weekly documentation workflow",
    success_criterion="hidden rubric",
    split="test",
    provenance=_PROVENANCE,
)


def _seal(
    *,
    case_id: str = "held-1",
    visibility: CaseVisibility = "independently_withheld",
    authored_by: tuple[str, ...] = ("owner",),
    sealed_by: str = "owner",
    sealed_at: datetime = AFTER_FREEZE,
    implementation_fingerprint: str = IMPLEMENTATION,
    label_fingerprint: str = LABEL,
    source_snapshot_fingerprint: str = SNAPSHOT,
) -> CaseSeal:
    return CaseSeal(
        case_id=case_id,
        visibility=visibility,
        authored_by=authored_by,
        sealed_by=sealed_by,
        sealed_at=sealed_at,
        implementation_fingerprint=implementation_fingerprint,
        label_fingerprint=label_fingerprint,
        source_snapshot_fingerprint=source_snapshot_fingerprint,
    )


@pytest.mark.parametrize(
    ("sealed_by", "exposed_actor", "expected"),
    [("owner", None, True), ("implementer", None, False), ("owner", "implementer", False)],
)
def test_independence_requires_external_custody(sealed_by: str, exposed_actor: str | None, expected: bool) -> None:
    seal = CaseSeal(
        case_id="held-1", visibility="independently_withheld",
        authored_by=("owner",), sealed_by=sealed_by,
        sealed_at=datetime(2026, 9, 14, tzinfo=UTC),
        implementation_fingerprint=IMPLEMENTATION,
        label_fingerprint=LABEL, source_snapshot_fingerprint=SNAPSHOT,
    )
    exposures = () if exposed_actor is None else (
        LabelExposure(
            exposure_id="x1", case_id="held-1", actor=exposed_actor,
            role="implementer", purpose="prompt tuning",
            at=datetime(2026, 9, 14, tzinfo=UTC), label_fingerprint=LABEL,
        ),
    )
    audit = audit_cycle(
        cases=(CASE,), seals=(seal,), exposures=exposures,
        implementation_fingerprint=IMPLEMENTATION,
        implementation_frozen_at=datetime(2026, 9, 13, tzinfo=UTC),
        implementation_authors=("implementer",),
    )
    assert audit.independent_holdout_verified is expected


def test_split_test_case_without_a_seal_is_not_independent() -> None:
    audit = audit_cycle(
        cases=(CASE,), seals=(), exposures=(),
        implementation_fingerprint=IMPLEMENTATION,
        implementation_frozen_at=FROZEN_AT,
        implementation_authors=("implementer",),
    )

    assert audit.independent_holdout_verified is False
    assert audit.invalid_case_ids == ("held-1",)
    assert any("custody seal" in reason for reason in audit.reasons)
    assert any("acceptance comparison incomplete" in reason for reason in audit.reasons)


def test_no_holdout_claim_reports_not_verified() -> None:
    # A plain development case (author-visible: no split=test claim, no seal) needs no custody proof.
    case = TaskCase(
        case_id="dev-1", input="A plain development question", success_criterion="c", split="validation", provenance=_PROVENANCE
    )

    audit = audit_cycle(
        cases=(case,), seals=(), exposures=(),
        implementation_fingerprint=IMPLEMENTATION,
        implementation_frozen_at=FROZEN_AT,
        implementation_authors=(),
    )

    assert audit.independent_holdout_verified is False
    assert audit.valid_case_ids == ("dev-1",)
    assert audit.invalid_case_ids == ()
    assert audit.reasons == ()


def test_normalized_duplicate_inputs_are_rejected() -> None:
    case_a = TaskCase(
        case_id="dev-1", input="Design a Weekly Documentation Workflow", success_criterion="c", split="train", provenance=_PROVENANCE
    )
    case_b = TaskCase(
        case_id="dev-2",
        input="  design a weekly   documentation workflow  ",
        success_criterion="c",
        split="train",
        provenance=_PROVENANCE,
    )

    audit = audit_cycle(
        cases=(case_a, case_b), seals=(), exposures=(),
        implementation_fingerprint=IMPLEMENTATION,
        implementation_frozen_at=FROZEN_AT,
        implementation_authors=(),
    )

    assert set(audit.invalid_case_ids) == {"dev-1", "dev-2"}
    assert any("normalized input collides" in reason for reason in audit.reasons)


def test_duplicate_label_fingerprints_are_rejected() -> None:
    case_b = TaskCase(
        case_id="held-2", input="A distinctly different question", success_criterion="c", split="test", provenance=_PROVENANCE
    )
    seal_a = _seal(case_id="held-1")
    seal_b = _seal(case_id="held-2")  # shares seal_a's label fingerprint

    audit = audit_cycle(
        cases=(CASE, case_b), seals=(seal_a, seal_b), exposures=(),
        implementation_fingerprint=IMPLEMENTATION,
        implementation_frozen_at=FROZEN_AT,
        implementation_authors=("implementer",),
    )

    assert set(audit.invalid_case_ids) == {"held-1", "held-2"}
    assert audit.independent_holdout_verified is False
    assert any("label fingerprint collides" in reason for reason in audit.reasons)


def test_seal_before_freeze_fails_independence() -> None:
    seal = _seal(sealed_at=BEFORE_FREEZE)

    audit = audit_cycle(
        cases=(CASE,), seals=(seal,), exposures=(),
        implementation_fingerprint=IMPLEMENTATION,
        implementation_frozen_at=FROZEN_AT,
        implementation_authors=("implementer",),
    )

    assert audit.independent_holdout_verified is False
    assert audit.invalid_case_ids == ("held-1",)


def test_gold_gone_case_invalidates_and_breaks_independence() -> None:
    seal = _seal()
    verdict = CaseFreshness(case_name="held-1", era="page_anchored", verdict="gold_gone", moved_page_ids=("p1",))

    audit = audit_cycle(
        cases=(CASE,), seals=(seal,), exposures=(),
        implementation_fingerprint=IMPLEMENTATION,
        implementation_frozen_at=FROZEN_AT,
        implementation_authors=("implementer",),
        freshness=(verdict,),
    )

    assert audit.invalid_case_ids == ("held-1",)
    assert audit.independent_holdout_verified is False
    assert any("gold_gone" in reason for reason in audit.reasons)


def test_mirror_stale_case_invalidates_and_breaks_independence() -> None:
    seal = _seal()
    verdict = CaseFreshness(case_name="held-1", era="mirror_anchored", verdict="mirror_stale", moved_page_ids=())

    audit = audit_cycle(
        cases=(CASE,), seals=(seal,), exposures=(),
        implementation_fingerprint=IMPLEMENTATION,
        implementation_frozen_at=FROZEN_AT,
        implementation_authors=("implementer",),
        freshness=(verdict,),
    )

    assert audit.invalid_case_ids == ("held-1",)
    assert audit.independent_holdout_verified is False
    assert any("mirror_stale" in reason for reason in audit.reasons)


def test_broken_required_premise_invalidates_and_breaks_independence() -> None:
    seal = _seal()
    premise = CasePremise(case_name="held-1", archetype=Archetype.TRACE, intact=False, missing=("edge a->b gone",))

    audit = audit_cycle(
        cases=(CASE,), seals=(seal,), exposures=(),
        implementation_fingerprint=IMPLEMENTATION,
        implementation_frozen_at=FROZEN_AT,
        implementation_authors=("implementer",),
        premises=(premise,),
    )

    assert audit.invalid_case_ids == ("held-1",)
    assert audit.independent_holdout_verified is False
    assert any("broken structural premise" in reason for reason in audit.reasons)


def test_grader_exposure_after_freeze_is_not_a_violation() -> None:
    seal = _seal()
    exposure = LabelExposure(
        exposure_id="x1", case_id="held-1", actor="judge-1", role="grader", purpose="grading the trial",
        at=datetime(2026, 9, 15, tzinfo=UTC), label_fingerprint=LABEL,
    )

    audit = audit_cycle(
        cases=(CASE,), seals=(seal,), exposures=(exposure,),
        implementation_fingerprint=IMPLEMENTATION,
        implementation_frozen_at=FROZEN_AT,
        implementation_authors=("implementer",),
    )

    assert audit.independent_holdout_verified is True
    assert audit.invalid_case_ids == ()


def test_duplicate_case_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate case ids"):
        audit_cycle(
            cases=(CASE, CASE), seals=(), exposures=(),
            implementation_fingerprint=IMPLEMENTATION,
            implementation_frozen_at=FROZEN_AT,
            implementation_authors=(),
        )


def test_normalize_case_text_folds_whitespace_and_case() -> None:
    assert normalize_case_text("  Design   a\tWeekly\ndocumentation WORKFLOW  ") == "design a weekly documentation workflow"


# -- actor names are compared after whitespace and case folding ---------------


def test_actor_folding_catches_mixed_case_sealed_by() -> None:
    seal = _seal(sealed_by="Implementer")

    audit = audit_cycle(
        cases=(CASE,), seals=(seal,), exposures=(),
        implementation_fingerprint=IMPLEMENTATION,
        implementation_frozen_at=FROZEN_AT,
        implementation_authors=("implementer",),
    )

    assert audit.independent_holdout_verified is False


def test_actor_folding_catches_trailing_whitespace_sealed_by() -> None:
    seal = _seal(sealed_by="implementer ")

    audit = audit_cycle(
        cases=(CASE,), seals=(seal,), exposures=(),
        implementation_fingerprint=IMPLEMENTATION,
        implementation_frozen_at=FROZEN_AT,
        implementation_authors=("implementer",),
    )

    assert audit.independent_holdout_verified is False


def test_actor_folding_catches_mixed_case_in_authored_by() -> None:
    seal = _seal(authored_by=("owner", "Implementer"))

    audit = audit_cycle(
        cases=(CASE,), seals=(seal,), exposures=(),
        implementation_fingerprint=IMPLEMENTATION,
        implementation_frozen_at=FROZEN_AT,
        implementation_authors=("implementer",),
    )

    assert audit.independent_holdout_verified is False


# -- a compromised seal cannot shadow, or be shadowed by, a clean one ---------


def test_duplicate_seal_case_ids_are_rejected_clean_then_compromised() -> None:
    clean = _seal()
    compromised = _seal(sealed_by="implementer")

    with pytest.raises(ValueError, match="duplicate seal case ids"):
        audit_cycle(
            cases=(CASE,), seals=(clean, compromised), exposures=(),
            implementation_fingerprint=IMPLEMENTATION,
            implementation_frozen_at=FROZEN_AT,
            implementation_authors=("implementer",),
        )


def test_duplicate_seal_case_ids_are_rejected_compromised_then_clean() -> None:
    clean = _seal()
    compromised = _seal(sealed_by="implementer")

    with pytest.raises(ValueError, match="duplicate seal case ids"):
        audit_cycle(
            cases=(CASE,), seals=(compromised, clean), exposures=(),
            implementation_fingerprint=IMPLEMENTATION,
            implementation_frozen_at=FROZEN_AT,
            implementation_authors=("implementer",),
        )


# -- an exposure's actor identity disqualifies regardless of its role tag -----


def test_implementation_author_exposure_fails_regardless_of_role() -> None:
    seal = _seal()
    exposure = LabelExposure(
        exposure_id="x3", case_id="held-1", actor="implementer", role="custodian", purpose="archiving",
        at=AFTER_FREEZE, label_fingerprint=LABEL,
    )

    audit = audit_cycle(
        cases=(CASE,), seals=(seal,), exposures=(exposure,),
        implementation_fingerprint=IMPLEMENTATION,
        implementation_frozen_at=FROZEN_AT,
        implementation_authors=("implementer",),
    )

    assert audit.independent_holdout_verified is False
    assert any("exposure" in reason for reason in audit.reasons)


# -- an exposure is matched by case id as well as by label fingerprint --------


def test_exposure_matched_by_case_id_with_different_label_fingerprint_fails_independence() -> None:
    seal = _seal()
    exposure = LabelExposure(
        exposure_id="x2", case_id="held-1", actor="implementer", role="implementer", purpose="debugging",
        at=AFTER_FREEZE, label_fingerprint="d" * 64,  # deliberately not seal.label_fingerprint
    )

    audit = audit_cycle(
        cases=(CASE,), seals=(seal,), exposures=(exposure,),
        implementation_fingerprint=IMPLEMENTATION,
        implementation_frozen_at=FROZEN_AT,
        implementation_authors=("implementer",),
    )

    assert audit.independent_holdout_verified is False
    assert any("exposure" in reason for reason in audit.reasons)


def test_empty_implementation_authors_fails_a_holdout_claim() -> None:
    seal = _seal()

    audit = audit_cycle(
        cases=(CASE,), seals=(seal,), exposures=(),
        implementation_fingerprint=IMPLEMENTATION,
        implementation_frozen_at=FROZEN_AT,
        implementation_authors=(),
    )

    assert audit.independent_holdout_verified is False
    assert any("no recorded implementation authors" in reason for reason in audit.reasons)


# -- every custody timestamp must be timezone-aware ---------------------------


def test_naive_sealed_at_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone info"):
        CaseSeal(
            case_id="held-1", visibility="independently_withheld", authored_by=("owner",), sealed_by="owner",
            sealed_at=datetime(2026, 9, 14),  # noqa: DTZ001 (pinning the naive-datetime refusal itself)
            implementation_fingerprint=IMPLEMENTATION, label_fingerprint=LABEL, source_snapshot_fingerprint=SNAPSHOT,
        )


def test_naive_exposure_at_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone info"):
        LabelExposure(
            exposure_id="x1", case_id="held-1", actor="judge-1", role="grader", purpose="grading",
            at=datetime(2026, 9, 15),  # noqa: DTZ001 (pinning the naive-datetime refusal itself)
            label_fingerprint=LABEL,
        )


def test_naive_implementation_frozen_at_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        audit_cycle(
            cases=(CASE,), seals=(), exposures=(),
            implementation_fingerprint=IMPLEMENTATION,
            implementation_frozen_at=datetime(2026, 9, 13),  # noqa: DTZ001 (pinning the naive-datetime refusal itself)
            implementation_authors=(),
        )


# -- each failing custody rule lands as its own named reason ------------------


def test_fingerprint_mismatch_fails_independence() -> None:
    seal = _seal(implementation_fingerprint="d" * 64)

    audit = audit_cycle(
        cases=(CASE,), seals=(seal,), exposures=(),
        implementation_fingerprint=IMPLEMENTATION,
        implementation_frozen_at=FROZEN_AT,
        implementation_authors=("implementer",),
    )

    assert audit.independent_holdout_verified is False
    assert any("fingerprint mismatch" in reason for reason in audit.reasons)


def test_author_in_implementation_authors_fails_independence() -> None:
    seal = _seal(authored_by=("owner", "implementer"))

    audit = audit_cycle(
        cases=(CASE,), seals=(seal,), exposures=(),
        implementation_fingerprint=IMPLEMENTATION,
        implementation_frozen_at=FROZEN_AT,
        implementation_authors=("implementer",),
    )

    assert audit.independent_holdout_verified is False
    assert any("authorship not disjoint" in reason for reason in audit.reasons)


def test_multiple_custody_failures_each_land_in_reasons() -> None:
    seal = _seal(sealed_at=BEFORE_FREEZE, implementation_fingerprint="d" * 64)

    audit = audit_cycle(
        cases=(CASE,), seals=(seal,), exposures=(),
        implementation_fingerprint=IMPLEMENTATION,
        implementation_frozen_at=FROZEN_AT,
        implementation_authors=("implementer",),
    )

    assert any("freeze timing" in reason for reason in audit.reasons)
    assert any("fingerprint mismatch" in reason for reason in audit.reasons)


# -- a test-split case sealed author-visible is flagged, not silently valid ---


def test_test_split_case_sealed_author_visible_is_invalid_and_unverified() -> None:
    seal = _seal(visibility="author_visible")

    audit = audit_cycle(
        cases=(CASE,), seals=(seal,), exposures=(),
        implementation_fingerprint=IMPLEMENTATION,
        implementation_frozen_at=FROZEN_AT,
        implementation_authors=("implementer",),
    )

    assert audit.invalid_case_ids == ("held-1",)
    assert audit.verified_case_ids == ()
    assert any("visibility" in reason for reason in audit.reasons)


def test_clean_holdout_case_is_verified() -> None:
    seal = _seal()

    audit = audit_cycle(
        cases=(CASE,), seals=(seal,), exposures=(),
        implementation_fingerprint=IMPLEMENTATION,
        implementation_frozen_at=FROZEN_AT,
        implementation_authors=("implementer",),
    )

    assert audit.independent_holdout_verified is True
    assert audit.verified_case_ids == ("held-1",)
