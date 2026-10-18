"""Unit specs for the pure freshness verdict core (`engram.evals.generation.domain.freshness`).

The judge-free staleness core maps a golden case onto an era-authoritative `CaseFreshness` verdict
from a knowledge base snapshot, with no session, IO, or LLM. These specs pin era classification, the verdict
precedence, the recompute-failure rule, `moved_page_ids` attribution, and the era-authority of
`staleness_fraction` and `retiring_names` (an intact-mirror drift is report-only, never retired).
"""

from __future__ import annotations

import pytest

from engram.evals.generation.contracts import (
    CaseFreshness,
    CaseFreshnessRef,
    CasePremise,
    EvaluationCase,
    build_report,
    case_freshness_refs,
    evaluate_freshness,
    evaluate_premises,
    is_audit_invalidating,
    parent_case_refs,
    premise_reads,
    ranked_stale_pages,
    render_freshness_markdown,
    retiring_names,
    staleness_fraction,
)
from engram.evals.generation.domain import freshness
from engram.evals.generation.domain.datasets import case_name
from engram.evals.generation.domain.freshness import AnchorEra, FreshnessVerdict, case_era, evaluate_case
from engram.evals.generation.infrastructure import files
from engram.evals.generation.infrastructure.files import load_freshness_report
from engram.evals.shared.domain import AnchorHash, Archetype, QaCaseMeta, QaInput, RetrievalAnswer

_FRESH = "hash-fresh"
_MOVED = "hash-moved"
_MIRROR = "mirror-hash-v1"


def _page_anchored_ref(
    *,
    case_name: str = "pinpoint:c1#0",
    gold_ids: tuple[str, ...] = ("p1",),
    anchors: tuple[AnchorHash, ...] = (AnchorHash(unit_id="p1", body_hash=_FRESH),),
) -> CaseFreshnessRef:
    return CaseFreshnessRef(
        case_name=case_name, seed_key="pinpoint:c1", archetype=Archetype.PINPOINT, gold_ids=gold_ids, anchors=anchors, mirror_hash=None
    )


def _mirror_anchored_ref(
    *,
    case_name: str = "pinpoint:c1#0",
    gold_ids: tuple[str, ...] = ("p1",),
    anchors: tuple[AnchorHash, ...] = (AnchorHash(unit_id="p1", body_hash=_FRESH),),
    mirror_hash: str = _MIRROR,
) -> CaseFreshnessRef:
    return CaseFreshnessRef(
        case_name=case_name,
        seed_key="pinpoint:c1",
        archetype=Archetype.PINPOINT,
        gold_ids=gold_ids,
        anchors=anchors,
        mirror_hash=mirror_hash,
    )


def _legacy_ref(*, case_name: str = "paper.x-0", gold_ids: tuple[str, ...] = ("p1",)) -> CaseFreshnessRef:
    return CaseFreshnessRef(case_name=case_name, seed_key=None, archetype=None, gold_ids=gold_ids, anchors=(), mirror_hash=None)


def _verdict(
    *, name: str = "c", era: AnchorEra = "page_anchored", verdict: FreshnessVerdict = "fresh", moved: tuple[str, ...] = ()
) -> CaseFreshness:
    return CaseFreshness(case_name=name, era=era, verdict=verdict, moved_page_ids=moved)


class TestCaseEra:
    def test_mirror_hash_present_is_mirror_anchored(self):
        assert case_era(_mirror_anchored_ref()) == "mirror_anchored"

    def test_mirror_hash_wins_even_with_a_legacy_positional_name(self):
        ref = CaseFreshnessRef(case_name="paper.x-0", seed_key=None, archetype=None, gold_ids=("p1",), anchors=(), mirror_hash=_MIRROR)

        assert case_era(ref) == "mirror_anchored"

    def test_anchors_without_a_mirror_hash_are_page_anchored(self):
        assert case_era(_page_anchored_ref()) == "page_anchored"

    def test_a_parseable_seed_key_without_anchors_is_page_anchored(self):
        ref = CaseFreshnessRef(
            case_name="survey:r:d#0", seed_key="survey:r:d", archetype=Archetype.SURVEY, gold_ids=("p1",), anchors=(), mirror_hash=None
        )

        assert case_era(ref) == "page_anchored"

    def test_no_anchors_and_no_parseable_seed_key_is_legacy(self):
        assert case_era(_legacy_ref()) == "legacy"


class TestEvaluateCase:
    def test_page_anchored_all_intact_is_fresh(self):
        result = evaluate_case(
            _page_anchored_ref(), current_ids=frozenset({"p1"}), current_hashes={"p1": _FRESH}, recomputed_mirror_hash=None
        )

        assert result.verdict == "fresh"
        assert result.era == "page_anchored"
        assert result.moved_page_ids == ()

    def test_page_anchored_drift_flags_page_drifted_with_attribution(self):
        result = evaluate_case(
            _page_anchored_ref(), current_ids=frozenset({"p1"}), current_hashes={"p1": _MOVED}, recomputed_mirror_hash=None
        )

        assert result.verdict == "page_drifted"
        assert result.moved_page_ids == ("p1",)

    def test_missing_page_from_current_hashes_is_drift(self):
        result = evaluate_case(_page_anchored_ref(), current_ids=frozenset({"p1"}), current_hashes={}, recomputed_mirror_hash=None)

        assert result.verdict == "page_drifted"
        assert result.moved_page_ids == ("p1",)

    def test_gold_id_absent_flags_gold_gone_with_missing_ids(self):
        ref = _page_anchored_ref(gold_ids=("p1", "p2"))
        result = evaluate_case(ref, current_ids=frozenset({"p1"}), current_hashes={"p1": _FRESH}, recomputed_mirror_hash=None)

        assert result.verdict == "gold_gone"
        assert result.moved_page_ids == ("p2",)

    def test_legacy_case_is_unanchored_even_when_gold_is_gone(self):
        result = evaluate_case(_legacy_ref(), current_ids=frozenset(), current_hashes={}, recomputed_mirror_hash=None)

        assert result.verdict == "unanchored"
        assert result.era == "legacy"
        assert result.moved_page_ids == ()

    def test_mirror_era_recompute_mismatch_flags_mirror_stale(self):
        result = evaluate_case(
            _mirror_anchored_ref(), current_ids=frozenset({"p1"}), current_hashes={"p1": _FRESH}, recomputed_mirror_hash="mirror-hash-v2"
        )

        assert result.verdict == "mirror_stale"
        assert result.era == "mirror_anchored"

    def test_mirror_era_recompute_match_is_fresh(self):
        result = evaluate_case(
            _mirror_anchored_ref(), current_ids=frozenset({"p1"}), current_hashes={"p1": _FRESH}, recomputed_mirror_hash=_MIRROR
        )

        assert result.verdict == "fresh"

    def test_d10_recompute_failure_on_intact_pages_is_mirror_stale(self):
        result = evaluate_case(
            _mirror_anchored_ref(), current_ids=frozenset({"p1"}), current_hashes={"p1": _FRESH}, recomputed_mirror_hash=None
        )

        assert result.verdict == "mirror_stale"


class TestVerdictPrecedence:
    def test_gold_gone_beats_mirror_stale(self):
        ref = _mirror_anchored_ref(gold_ids=("p1", "gone"))
        result = evaluate_case(ref, current_ids=frozenset({"p1"}), current_hashes={"p1": _MOVED}, recomputed_mirror_hash=None)

        assert result.verdict == "gold_gone"

    def test_mirror_stale_beats_page_drifted(self):
        result = evaluate_case(
            _mirror_anchored_ref(), current_ids=frozenset({"p1"}), current_hashes={"p1": _MOVED}, recomputed_mirror_hash=None
        )

        assert result.verdict == "mirror_stale"
        assert result.moved_page_ids == ("p1",)  # the drifted page still attributed on a mirror-stale verdict

    def test_gold_gone_beats_page_drifted(self):
        ref = _page_anchored_ref(gold_ids=("gone",), anchors=(AnchorHash(unit_id="p1", body_hash=_FRESH),))
        result = evaluate_case(ref, current_ids=frozenset(), current_hashes={"p1": _MOVED}, recomputed_mirror_hash=None)

        assert result.verdict == "gold_gone"
        assert result.moved_page_ids == ("gone",)


class TestEvaluateFreshness:
    def test_scores_each_ref_in_order_reading_recomputed_by_name(self):
        page = _page_anchored_ref(case_name="pinpoint:c1#0")
        mirror = _mirror_anchored_ref(case_name="pinpoint:c2#0")
        results = evaluate_freshness(
            (page, mirror), current_ids=frozenset({"p1"}), current_hashes={"p1": _FRESH}, recomputed={"pinpoint:c2#0": "mirror-hash-v2"}
        )

        assert [r.case_name for r in results] == ["pinpoint:c1#0", "pinpoint:c2#0"]
        assert results[0].verdict == "fresh"
        assert results[1].verdict == "mirror_stale"  # recomputed entry read by case name

    def test_non_mirror_era_case_needs_no_recomputed_entry(self):
        results = evaluate_freshness((_page_anchored_ref(),), current_ids=frozenset({"p1"}), current_hashes={"p1": _FRESH}, recomputed={})

        assert results[0].verdict == "fresh"


class TestStalenessFraction:
    def test_excludes_unanchored_from_the_denominator(self):
        verdicts = [
            _verdict(verdict="gold_gone"),
            _verdict(verdict="mirror_stale", era="mirror_anchored"),
            _verdict(verdict="fresh"),
            _verdict(verdict="unanchored", era="legacy"),
        ]

        assert staleness_fraction(verdicts) == 2 / 3  # two stale over three verdict-able (unanchored excluded)

    def test_report_only_mirror_drift_is_not_counted_as_stale(self):
        verdicts = [_verdict(verdict="page_drifted", era="mirror_anchored"), _verdict(verdict="fresh")]

        assert staleness_fraction(verdicts) == 0.0  # intact-mirror drift stays out of the numerator

    def test_all_unanchored_is_zero(self):
        assert staleness_fraction([_verdict(verdict="unanchored", era="legacy")]) == 0.0

    def test_empty_is_zero(self):
        assert staleness_fraction([]) == 0.0

    def test_all_fresh_is_zero(self):
        assert staleness_fraction([_verdict(verdict="fresh"), _verdict(verdict="fresh")]) == 0.0


class TestRetiringNames:
    def test_gold_gone_and_mirror_stale_and_page_era_drift_retire(self):
        verdicts = [
            _verdict(name="a", verdict="gold_gone"),
            _verdict(name="b", verdict="mirror_stale", era="mirror_anchored"),
            _verdict(name="c", verdict="page_drifted", era="page_anchored"),
        ]

        assert retiring_names(verdicts) == frozenset({"a", "b", "c"})

    def test_mirror_era_page_drift_is_report_only(self):
        verdicts = [_verdict(name="d", verdict="page_drifted", era="mirror_anchored")]

        assert retiring_names(verdicts) == frozenset()

    def test_fresh_and_unanchored_never_retire(self):
        verdicts = [_verdict(name="e", verdict="fresh"), _verdict(name="f", verdict="unanchored", era="legacy")]

        assert retiring_names(verdicts) == frozenset()


class TestCaseFreshnessRefs:
    def test_parses_seed_key_from_a_stable_case_name(self):
        cases = [
            EvaluationCase(
                name="pinpoint:c1#2",
                inputs=QaInput(question="q"),
                expected_output=RetrievalAnswer(answer="a", cited_source_ids=["p1", "p2"]),
                metadata=QaCaseMeta(
                    anchor_page_id="p1",
                    difficulty="easy",
                    archetype=Archetype.PINPOINT,
                    anchors=(AnchorHash(unit_id="p1", body_hash=_FRESH),),
                    mirror_hash=_MIRROR,
                ),
            )
        ]

        refs = case_freshness_refs(cases)

        assert len(refs) == 1
        assert refs[0].seed_key == "pinpoint:c1"
        assert refs[0].gold_ids == ("p1", "p2")
        assert refs[0].anchors == (AnchorHash(unit_id="p1", body_hash=_FRESH),)
        assert refs[0].mirror_hash == _MIRROR

    def test_a_passage_seed_key_holding_a_real_section_id_round_trips(self):
        # A section id is `<page-id>#<section-path>`, so the case name carries two `#`.
        seed_key = "passage:paper.alpha#approach.graph_indexing"
        cases = [
            EvaluationCase(
                name=case_name(seed_key, 3),
                inputs=QaInput(question="q"),
                expected_output=RetrievalAnswer(answer="a", cited_source_ids=["paper.alpha"]),
                metadata=QaCaseMeta(anchor_page_id="paper.alpha", difficulty="easy", archetype=Archetype.PASSAGE),
            )
        ]

        assert cases[0].name == "passage:paper.alpha#approach.graph_indexing#3"
        assert [ref.seed_key for ref in case_freshness_refs(cases)] == [seed_key]
        assert [ref.seed_key for ref in parent_case_refs(cases)] == [seed_key]

    def test_keeps_legacy_cases_with_a_none_seed_key(self):
        cases = [
            EvaluationCase(
                name="paper.x-0",
                inputs=QaInput(question="q"),
                expected_output=RetrievalAnswer(answer="a", cited_source_ids=["p1"]),
                metadata=QaCaseMeta(anchor_page_id="paper.x", difficulty="easy", archetype=None),
            )
        ]

        refs = case_freshness_refs(cases)

        assert len(refs) == 1  # unlike parent_case_refs, legacy cases are kept (reported as unanchored)
        assert refs[0].seed_key is None
        assert refs[0].anchors == ()
        assert refs[0].mirror_hash is None
        assert case_era(refs[0]) == "legacy"


class TestBuildReport:
    def test_assembles_counts_fraction_and_page_tally(self):
        verdicts = (
            _verdict(name="a", verdict="gold_gone", moved=("p9",)),
            _verdict(name="b", verdict="page_drifted", era="page_anchored", moved=("p3",)),
            _verdict(name="c", verdict="fresh"),
        )

        report = build_report("research", "research.v2.yaml", verdicts)

        assert report.collection == "research"
        assert report.dataset_version == "research.v2.yaml"
        assert report.verdicts == verdicts
        assert report.counts["gold_gone"] == 1
        assert report.counts["fresh"] == 1
        assert report.counts["mirror_stale"] == 0  # every verdict kind present in the tally
        assert report.staleness_fraction == 2 / 3
        assert report.stale_by_page == {"p9": 1, "p3": 1}


class TestRankedStalePages:
    def test_orders_by_count_desc_then_page_id_asc(self):
        # "b" and "a" tie on count 1, so the ascending-id tie-break puts "a" before "b" below "hot".
        assert ranked_stale_pages({"b": 1, "hot": 3, "a": 1}) == (("hot", 3), ("a", 1), ("b", 1))

    def test_empty_tally_is_empty(self):
        assert ranked_stale_pages({}) == ()


class TestLoadFreshnessReport:
    def test_oversized_file_is_rejected_before_parsing(self, tmp_path, monkeypatch):
        # A tiny cap override + a padded file exercises the guard without allocating a real 10MB report.
        monkeypatch.setattr(files, "_MAX_REPORT_BYTES", 16)
        report_path = tmp_path / "over.freshness.yaml"
        report_path.write_bytes(b"x" * 64)

        with pytest.raises(ValueError, match="exceeding"):
            load_freshness_report(report_path)


# -- structural-integrity premises (report-only, D12) ---------------------------


def _seeded_ref(seed_key: str, archetype: Archetype, *, case_name: str | None = None) -> CaseFreshnessRef:
    """A minimal seed-keyed ref for the premise matrix (anchors/gold are irrelevant to premises)."""
    return CaseFreshnessRef(
        case_name=case_name or f"{seed_key}#0", seed_key=seed_key, archetype=archetype, gold_ids=(), anchors=(), mirror_hash=None
    )


def _premises(
    refs: list[CaseFreshnessRef],
    *,
    present_pairs: frozenset[tuple[str, str]] = frozenset(),
    inbound_fans: dict[tuple[str, str], int] | None = None,
    outbound_fans: dict[tuple[str, str], int] | None = None,
) -> tuple[CasePremise, ...]:
    """Run `evaluate_premises` with empty defaults for any read not scripted by the spec."""
    return evaluate_premises(
        refs,
        present_pairs=present_pairs,
        inbound_fans=inbound_fans or {},
        outbound_fans=outbound_fans or {},
    )


class TestEvaluatePremises:
    def test_trace_with_every_hop_present_is_intact(self):
        refs = [_seeded_ref("trace:a>b>c", Archetype.TRACE)]

        (premise,) = _premises(refs, present_pairs=frozenset({("a", "b"), ("b", "c")}))

        assert premise.intact is True
        assert premise.missing == ()

    def test_trace_with_a_gone_edge_is_broken_and_names_the_hop(self):
        refs = [_seeded_ref("trace:a>b>c", Archetype.TRACE)]

        (premise,) = _premises(refs, present_pairs=frozenset({("a", "b")}))

        assert premise.intact is False
        assert premise.missing == ("edge b->c gone",)

    def test_survey_below_the_seeder_threshold_is_broken(self):
        refs = [_seeded_ref("survey:cites:hub", Archetype.SURVEY)]

        (premise,) = _premises(refs, inbound_fans={("cites", "hub"): 2})

        assert premise.intact is False
        assert premise.missing == ("fan-in 2 < threshold 3",)

    def test_survey_at_the_threshold_is_intact(self):
        refs = [_seeded_ref("survey:cites:hub", Archetype.SURVEY)]

        (premise,) = _premises(refs, inbound_fans={("cites", "hub"): 3})

        assert premise.intact is True

    def test_synthesis_below_the_seeder_threshold_is_broken(self):
        refs = [_seeded_ref("synthesis:extends:src", Archetype.SYNTHESIS)]

        (premise,) = _premises(refs, outbound_fans={("extends", "src"): 1})

        assert premise.intact is False
        assert premise.missing == ("fan-out 1 < threshold 2",)

    def test_pinpoint_is_always_intact(self):
        refs = [_seeded_ref("pinpoint:c1", Archetype.PINPOINT)]

        (premise,) = _premises(refs)

        assert premise.intact is True
        assert premise.missing == ()

    def test_passage_has_no_premise_and_is_always_intact(self):
        refs = [_seeded_ref("passage:paper.alpha#setup", Archetype.PASSAGE)]

        (premise,) = _premises(refs)

        assert premise.archetype is Archetype.PASSAGE
        assert premise.intact is True
        assert premise.missing == ()

    def test_legacy_and_unlabelled_names_are_skipped(self):
        legacy = _legacy_ref()
        unlabelled = CaseFreshnessRef(
            case_name="trace:a>b#0", seed_key="trace:a>b", archetype=None, gold_ids=(), anchors=(), mirror_hash=None
        )

        assert _premises([legacy, unlabelled]) == ()

    def test_premise_outcomes_never_move_a_verdict(self):
        # The verdict core and the premise core are independent: a broken premise coexists with a
        # fresh verdict (D12 — report-only, nothing retires).
        ref = _page_anchored_ref(case_name="trace:a>b#0")
        verdict = evaluate_case(ref, current_ids=frozenset({"p1"}), current_hashes={"p1": _FRESH}, recomputed_mirror_hash=None)
        (premise,) = _premises([_seeded_ref("trace:a>b", Archetype.TRACE)], present_pairs=frozenset())

        assert verdict.verdict == "fresh"
        assert premise.intact is False


class TestPremiseReads:
    def test_collects_every_lookup_key_once(self):
        refs = [
            _seeded_ref("trace:a>b>c", Archetype.TRACE),
            _seeded_ref("survey:cites:hub", Archetype.SURVEY),
            _seeded_ref("synthesis:extends:src", Archetype.SYNTHESIS),
            _seeded_ref("pinpoint:c1", Archetype.PINPOINT),
            _seeded_ref("passage:paper.alpha#setup", Archetype.PASSAGE),
            _legacy_ref(),
        ]

        reads = premise_reads(refs)

        assert reads.edge_pairs == frozenset({("a", "b"), ("b", "c")})
        assert reads.inbound_keys == frozenset({("cites", "hub")})
        assert reads.outbound_keys == frozenset({("extends", "src")})


class TestStructuralReport:
    def test_build_report_carries_the_premises_and_the_broken_count(self):
        verdict = _verdict(name="trace:a>b#0", era="page_anchored", verdict="fresh")
        broken = CasePremise(case_name="trace:a>b#0", archetype=Archetype.TRACE, intact=False, missing=("edge a->b gone",))
        intact = CasePremise(case_name="pinpoint:c1#0", archetype=Archetype.PINPOINT, intact=True, missing=())

        report = build_report("research", "v1.yaml", [verdict], premises=[broken, intact])

        assert report.structural == (broken, intact)
        assert report.n_premise_broken == 1
        assert report.staleness_fraction == 0.0  # premises never move the fraction

    def test_markdown_carries_the_structural_section(self):
        verdict = _verdict(name="trace:a>b#0", era="page_anchored", verdict="fresh")
        broken = CasePremise(case_name="trace:a>b#0", archetype=Archetype.TRACE, intact=False, missing=("edge a->b gone",))

        markdown = render_freshness_markdown(build_report("research", "v1.yaml", [verdict], premises=[broken]))

        assert "## Structural integrity (report-only)" in markdown
        assert "checked: 1, broken: 1" in markdown
        assert "edge a->b gone" in markdown

    def test_a_pre_phase7_artifact_still_loads(self):
        report = build_report("research", "v1.yaml", [])
        payload = report.model_dump(mode="json")
        del payload["structural"]
        del payload["n_premise_broken"]

        loaded = freshness.FreshnessReport.model_validate(payload)

        assert loaded.structural == ()
        assert loaded.n_premise_broken == 0


class TestAuditInvalidation:
    @pytest.mark.parametrize(
        ("verdict", "era", "expected"),
        [
            ("gold_gone", "page_anchored", True),
            ("gold_gone", "mirror_anchored", True),
            ("mirror_stale", "mirror_anchored", True),
            ("page_drifted", "page_anchored", True),
            ("page_drifted", "mirror_anchored", False),
            ("fresh", "page_anchored", False),
            ("unanchored", "legacy", False),
        ],
    )
    def test_is_audit_invalidating_is_era_aware(self, verdict: FreshnessVerdict, era: AnchorEra, expected: bool):
        # A page-anchored page_drifted case invalidates (its label was verified against a page that
        # moved), but a mirror-anchored one does not (its intact mirror hash already covers the
        # exact drafting basis, so a page move alone never touched the fingerprinted label).
        assert is_audit_invalidating(_verdict(verdict=verdict, era=era)) is expected
