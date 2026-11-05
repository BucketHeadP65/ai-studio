"""A reusable step's own cases: run once per content address, through the registry's own executor, and kept.

Each test names the change that breaks it: reading no kept report first, running a step holding a tool outside the
case-run table, paying for a step with no cases, or performing a program instead of running its code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import AgentStep, Briefing, Program, Shape, StepRegistry, step_content_address
from core.ai.workflows.domain.vocabulary import SameAs
from pydantic import BaseModel, JsonValue

from harness.adapters.contracts import StandInCall
from harness.domain.contracts import CaseReport, CaseResult
from harness.features.evals.contracts import CaseRunRefusedError, perform_step_cases
from harness.features.evals.infrastructure.case_reports import FileCaseReports

if TYPE_CHECKING:
    from pathlib import Path

    from harness.adapters.contracts import StandInCalls

VERDICT = Shape(
    json_schema={
        "type": "object",
        "title": "Verdict",
        "properties": {"outcome": {"enum": ["approve", "reject", "cannot_judge"]}, "feedback": {"type": "string"}},
        "required": ["outcome", "feedback"],
    }
)


def _briefing(*, tools: tuple[str, ...] = ("search", "write_note"), output: Shape | SameAs = VERDICT) -> Briefing:
    return Briefing(
        name="check_drafts",
        instructions="Read every draft and answer approve, reject or cannot_judge for the owner, with feedback.",
        inputs={"drafts": Shape(json_schema={"type": "array", "items": {"type": "object"}})},
        output=output,
        tools=tools,
    )


TWO_CASES: dict[str, JsonValue] = {
    "cases": [
        {"name": "a sound draft", "inputs": {"drafts": [{"slug": "one"}]}, "expected_output": {"outcome": "approve"}},
        {"name": "an empty draft", "inputs": {"drafts": [{"slug": "two"}]}, "expected_output": {"outcome": "reject"}},
    ],
    "evaluators": ["OutcomeMatches"],
}


class ScriptedPerform:
    """A perform answering `outcome` for every case, recording one stand-in call per case as a write would.

    A case whose first draft's slug is in `raising` raises instead, as a run whose model connection broke does.
    """

    def __init__(self, calls: StandInCalls, outcome: str = "approve", raising: frozenset[str] = frozenset()) -> None:
        self.stand_ins = calls
        self.outcome = outcome
        self.raising = raising
        self.performed: list[dict[str, object]] = []
        self.output_types: list[type] = []

    async def __call__(self, task: Briefing, output_type: type, /, **inputs: object) -> object:
        self.performed.append(dict(inputs))
        self.output_types.append(output_type)
        drafts = inputs.get("drafts")
        if isinstance(drafts, list) and drafts and isinstance(drafts[0], dict) and drafts[0].get("slug") in self.raising:
            msg = "the model connection broke"
            raise ConnectionError(msg)
        self.stand_ins.append(StandInCall(tool="write_note", arguments={"knowledge_base": "research"}, answer="checked, not written: ok"))
        if isinstance(output_type, type) and issubclass(output_type, BaseModel):
            return output_type(outcome=self.outcome, feedback="a scripted verdict")
        return ["one", "two"]


class ScriptedRunCode:
    """A code runner answering `value` for every case, recording the output type it was asked for."""

    def __init__(self, value: object) -> None:
        self.value = value
        self.ran: list[dict[str, object]] = []
        self.output_types: list[type] = []

    async def __call__(self, step: Program, output_type: type, /, **inputs: object) -> object:
        self.ran.append(dict(inputs))
        self.output_types.append(output_type)
        return self.value


def _files(root: Path) -> list[str]:
    return sorted(path.name for path in root.iterdir()) if root.exists() else []


class TestPerformStepCases:
    async def test_runs_each_case_once_keeps_the_report_and_reads_it_back_the_next_time(self, tmp_path: Path) -> None:
        calls: StandInCalls = [StandInCall(tool="write_note", arguments={}, answer="an earlier step's call")]
        perform = ScriptedPerform(calls)
        reports = FileCaseReports(tmp_path / "reports")
        step = _briefing()

        report = await perform_step_cases(step, "st.check_drafts.v3", TWO_CASES, StepRegistry(perform=perform), reports, calls)

        assert report.step_id == "st.check_drafts.v3"
        assert report.step_hash == step_content_address(step)
        assert [(result.name, result.passed, result.errored) for result in report.results] == [
            ("a sound draft", True, False),
            ("an empty draft", False, False),
        ]
        assert "'reject'" in report.results[1].reason
        assert report.stand_in_calls == 2, "the calls the list held before this run were counted"
        assert report.blocked_by is None
        assert report.no_cases is False
        assert len(perform.performed) == 2
        assert _files(tmp_path / "reports") == [f"{report.step_hash}-{report.cases_hash}.json"]

        again = ScriptedPerform(calls)
        kept = await perform_step_cases(step, "st.check_drafts.v3", TWO_CASES, StepRegistry(perform=again), reports, calls)

        assert again.performed == [], "a step version with a kept report ran its cases again"
        assert kept == report

    async def test_a_step_holding_a_tool_outside_the_table_is_blocked_and_nothing_runs(self, tmp_path: Path) -> None:
        calls: StandInCalls = []
        perform = ScriptedPerform(calls)

        report = await perform_step_cases(
            _briefing(tools=("search", "run_command")), "st.check_drafts.v3", TWO_CASES, StepRegistry(perform=perform), FileCaseReports(tmp_path), calls
        )

        assert report.blocked_by == "run_command"
        assert report.results == ()
        assert perform.performed == []
        assert _files(tmp_path) == []

    async def test_a_step_holding_resolve_entities_runs_its_cases_with_the_write_recorded(self, tmp_path: Path) -> None:
        calls: StandInCalls = []
        perform = ScriptedPerform(calls)

        report = await perform_step_cases(
            _briefing(tools=("read_page", "resolve_entities")), "st.link.v1", TWO_CASES, StepRegistry(perform=perform), FileCaseReports(tmp_path), calls
        )

        assert report.blocked_by is None
        assert len(perform.performed) == 2

    async def test_a_report_with_an_errored_case_is_returned_and_not_kept_so_the_next_run_tries_again(self, tmp_path: Path) -> None:
        calls: StandInCalls = []
        perform = ScriptedPerform(calls, raising=frozenset({"two"}))
        reports = FileCaseReports(tmp_path)

        report = await perform_step_cases(_briefing(), "st.check_drafts.v3", TWO_CASES, StepRegistry(perform=perform), reports, calls)

        errored = report.results[1]
        assert (errored.name, errored.passed, errored.errored) == ("an empty draft", False, True)
        assert errored.reason == "errored before any evaluator judged it: ConnectionError: the model connection broke"
        assert report.results[0].errored is False
        assert _files(tmp_path) == [], "a report holding a case that errored was kept"

        again = ScriptedPerform(calls)
        retried = await perform_step_cases(_briefing(), "st.check_drafts.v3", TWO_CASES, StepRegistry(perform=again), reports, calls)

        assert len(again.performed) == 2, "the cases did not run again after an errored case"
        assert [result.errored for result in retried.results] == [False, False]
        assert len(_files(tmp_path)) == 1

    @pytest.mark.parametrize(
        "cases",
        [
            {
                "cases": [{"name": "a sound draft", "inputs": {"drafts": []}}],
                "evaluators": [{"LLMJudge": {"rubric": "The verdict names a reason."}}],
            },
            {
                "cases": [
                    {"name": "a sound draft", "inputs": {"drafts": []}, "evaluators": [{"LLMJudge": {"rubric": "The verdict names a reason."}}]}
                ],
            },
        ],
        ids=["for every case", "for one case"],
    )
    async def test_a_dataset_whose_llm_judge_names_no_model_is_refused_before_any_case_runs(
        self, tmp_path: Path, cases: dict[str, JsonValue]
    ) -> None:
        calls: StandInCalls = []
        perform = ScriptedPerform(calls)

        with pytest.raises(CaseRunRefusedError, match="name a model this deployment's tiers can serve"):
            await perform_step_cases(_briefing(), "st.check_drafts.v3", cases, StepRegistry(perform=perform), FileCaseReports(tmp_path), calls)

        assert perform.performed == []
        assert _files(tmp_path) == []

    async def test_a_step_with_no_cases_reports_so_at_no_cost(self, tmp_path: Path) -> None:
        calls: StandInCalls = []
        perform = ScriptedPerform(calls)
        registry = StepRegistry(perform=perform)

        absent = await perform_step_cases(_briefing(), "st.check_drafts.v3", None, registry, FileCaseReports(tmp_path), calls)
        empty = await perform_step_cases(_briefing(), "st.check_drafts.v3", {"cases": []}, registry, FileCaseReports(tmp_path), calls)

        assert absent.no_cases is True
        assert empty.no_cases is True
        assert absent.results == ()
        assert perform.performed == []
        assert _files(tmp_path) == []

    async def test_a_programs_cases_run_through_run_code_with_its_declared_output_type(self, tmp_path: Path) -> None:
        run_code = ScriptedRunCode(3)
        program = Program(name="count_pages", source="len(pages)", inputs={"pages": Shape(json_schema={"type": "array"})}, output=Shape(json_schema={"type": "integer"}))
        cases: dict[str, JsonValue] = {
            "cases": [{"name": "three pages", "inputs": {"pages": ["a", "b", "c"]}, "expected_output": 3}],
            "evaluators": ["EqualsExpected"],
        }

        report = await perform_step_cases(program, "st.count_pages.v1", cases, StepRegistry(run_code=run_code), FileCaseReports(tmp_path), [])

        assert run_code.ran == [{"pages": ["a", "b", "c"]}]
        assert run_code.output_types == [int]
        assert report.results == (CaseResult(name="three pages", passed=True, errored=False, reason="EqualsExpected: True"),)

    async def test_a_briefing_whose_output_is_a_list_runs_on_that_list_type(self, tmp_path: Path) -> None:
        calls: StandInCalls = []
        perform = ScriptedPerform(calls)
        step = _briefing(output=Shape(json_schema={"type": "array", "items": {"type": "string"}}))
        cases: dict[str, JsonValue] = {
            "cases": [{"name": "two slugs", "inputs": {"drafts": []}, "expected_output": ["one", "two"]}],
            "evaluators": ["EqualsExpected"],
        }

        report = await perform_step_cases(step, "st.check_drafts.v4", cases, StepRegistry(perform=perform), FileCaseReports(tmp_path), calls)

        assert perform.output_types == [list[str]]
        assert [result.passed for result in report.results] == [True]

    async def test_an_agent_step_has_no_case_run(self, tmp_path: Path) -> None:
        step = AgentStep(name="explore", agent="explorer", output=VERDICT)

        report = await perform_step_cases(step, "st.explore.v1", TWO_CASES, StepRegistry(), FileCaseReports(tmp_path), [])

        assert report.no_cases is True
        assert report.results == ()
        assert _files(tmp_path) == []

    async def test_a_step_whose_output_names_another_steps_type_has_no_case_run(self, tmp_path: Path) -> None:
        calls: StandInCalls = []
        perform = ScriptedPerform(calls)

        report = await perform_step_cases(
            _briefing(output=SameAs(like="flag_ideas")), "st.merge.v2", TWO_CASES, StepRegistry(perform=perform), FileCaseReports(tmp_path), calls
        )

        assert report.no_cases is True
        assert perform.performed == []

    async def test_a_registry_that_cannot_run_the_step_is_refused_and_keeps_nothing(self, tmp_path: Path) -> None:
        program = Program(name="count_pages", source="len(pages)", output=Shape(json_schema={"type": "integer"}))

        with pytest.raises(CaseRunRefusedError, match="no sandbox"):
            await perform_step_cases(program, "st.count_pages.v1", TWO_CASES, StepRegistry(perform=ScriptedPerform([])), FileCaseReports(tmp_path), [])

        assert _files(tmp_path) == []


class TestFileCaseReports:
    def test_a_second_put_under_the_same_content_address_keeps_the_first_report(self, tmp_path: Path) -> None:
        reports = FileCaseReports(tmp_path)
        first = CaseReport(step_id="st.a.v1", step_hash="s", cases_hash="c", results=(), blocked_by=None, no_cases=False, stand_in_calls=1)
        second = first.model_copy(update={"stand_in_calls": 2})

        reports.put(first)
        reports.put(second)

        assert reports.get("s", "c") == first
        assert reports.get("s", "other") is None
