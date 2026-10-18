"""Unit specs for the one diagnosis pipeline the recommend command and the `diagnose_structure` tool share.

`structural_diagnosis` reads the detector inputs through the knowledge base gateway with the knobs it is
handed, and `remap_diagnosis` renames every case from its question to its dataset case name.
"""

from __future__ import annotations

from dataclasses import dataclass

from engram.config import RecommendationSettings
from engram.evals.rca.contracts import CaseLike, Diagnosis, FailingCase
from engram.evals.recommendation.contracts import MinedLink, SectionStat, remap_diagnosis, structural_diagnosis
from engram.evals.shared.domain import Archetype
from engram.testing.fakes.recommendation import FakeKnowledgeBaseGateway


@dataclass(frozen=True)
class _NoCases:
    cases: tuple[CaseLike, ...] = ()


def _case(question: str, gold: tuple[str, ...], *, recall: float = 0.0, archetype: Archetype = Archetype.TRACE) -> FailingCase:
    return FailingCase(
        question=question, gold_ids=gold, cited_ids=(), recall=recall, n_tool_calls=1.0, archetype=archetype, difficulty="medium"
    )


def _diagnosis(*cases: FailingCase) -> Diagnosis:
    return Diagnosis(overall={}, buckets=(), worst_cases=cases, mean_duration_s=0.0, mean_total_tokens=None)


class TestStructuralDiagnosis:
    async def test_reads_the_failing_gold_pages_with_the_knobs_it_is_handed(self):
        base = _diagnosis(_case("q-trace", ("paper.a", "paper.b")), _case("q-solved", ("paper.z",), recall=1.0))
        gateway = FakeKnowledgeBaseGateway(
            sections_over_budget_result=(SectionStat(page_id="paper.a", canon="findings", tokens=900),),
            page_types_result={"paper.a": "paper", "paper.b": "paper"},
            reachable_pairs_result=(False, False),
            unlifted_links_result=(
                MinedLink(src_page_id="paper.a", src_type="paper", raw_target="paper.b", dst_page_id="paper.b", dst_type="paper", rel="extends"),
            ),
        )
        settings = RecommendationSettings(section_token_budget=250, max_hops=4)

        diagnosis = await structural_diagnosis(
            base, _NoCases(), gateway, evidence_grade="residual-of-tuned", collection="papers", settings=settings
        )

        assert diagnosis.evidence_grade == "residual-of-tuned"
        assert [call.page_ids for call in gateway.sections_over_budget_calls] == [("paper.a", "paper.b")]
        assert gateway.sections_over_budget_calls[0].token_budget == 250
        assert {call.max_hops for call in gateway.reachable_pairs_calls} == {4}
        assert diagnosis.section_splits[0].page_type == "paper"
        assert diagnosis.missing_edges[0].counterfactual is not None
        assert diagnosis.over_expansion is None


class TestRemapDiagnosis:
    async def test_names_every_case_by_its_dataset_name_and_keeps_unknown_questions(self):
        base = _diagnosis(_case("q-trace", ("paper.a", "paper.b")), _case("q-unnamed", ("paper.c", "paper.d")))
        gateway = FakeKnowledgeBaseGateway(
            sections_over_budget_result=(SectionStat(page_id="paper.a", canon="findings", tokens=900),),
            page_types_result={"paper.a": "paper"},
            reachable_pairs_result=(False, False, False, False),
        )
        diagnosis = await structural_diagnosis(
            base, _NoCases(), gateway, evidence_grade="raw-baseline", collection="papers", settings=RecommendationSettings()
        )

        remapped = remap_diagnosis(diagnosis, {"q-trace": "trace-0"})

        assert [case.question for case in remapped.base.worst_cases] == ["trace-0", "q-unnamed"]
        assert remapped.section_splits[0].case_names == ("trace-0",)
        assert remapped.missing_edges[0].case_names == ("trace-0", "q-unnamed")
