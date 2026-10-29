"""A step ships its own dataset, and `harness evals run` performs it through the real executor.

Covers `OutcomeMatches` (the one evaluator a case names), and the runner: reading the dataset
back, resolving a step off a library, and performing every case through a `perform` callable --
here a fake, in `harness evals run` the environment's own `registry.perform`.
"""

from __future__ import annotations

import warnings

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import Briefing, InlineStep, Program, Shape
from core.ai.workflows.domain.vocabulary import SameAs
from pydantic import BaseModel
from pydantic_evals import Case, Dataset

from harness.cli import _dataset_path
from harness.features.evals.domain.step_evaluation import OutcomeMatches, bind_collection, report_failed
from harness.features.evals.exceptions import StepNotABriefingError, StepOutputReusesAnotherError
from harness.features.evals.infrastructure.files import load_dataset
from harness.features.evals.service.step_evaluation import resolve_step, run_step_evals
from harness.settings import CHECKOUT

if TYPE_CHECKING:
    from pathlib import Path

DATASET_PATH = CHECKOUT.parent / "templates" / "repo-docs" / "workflows" / "evals" / "check-adrs.yaml"

VERDICT_SHAPE = Shape(
    json_schema={
        "type": "object",
        "title": "Verdict",
        "properties": {"outcome": {"enum": ["approve", "reject", "cannot_judge"]}, "feedback": {"type": "string"}},
        "required": ["outcome", "feedback"],
    }
)

STEP = Briefing(
    name="check_adrs",
    instructions="Read every draft and answer approve, reject, or cannot_judge for the owner, with feedback.",
    inputs={
        "drafts": Shape(json_schema={"type": "array", "items": {"type": "object"}}),
        "collection": Shape(json_schema={"type": "string"}),
    },
    output=VERDICT_SHAPE,
    tools=("search", "fetch"),
)


class Verdict(BaseModel):
    """A stand-in for the model `build_type` would build from `VERDICT_SHAPE`."""

    outcome: str
    feedback: str


class FakeReusableStepQueries:
    """A library holding exactly the steps handed to it at construction."""

    def __init__(self, steps: dict[str, InlineStep]) -> None:
        self.steps = steps

    async def fetch_step(self, step_id: str) -> InlineStep:
        return self.steps[step_id]


class FakePerform:
    """A perform that always answers a fixed outcome, scriptable through the public attribute.

    Records every call's inputs, so a test can check a case reached it with the inputs the
    dataset gave it.
    """

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    async def __call__(self, task: Briefing, output_type: type, /, **inputs: object) -> object:
        self.calls.append(dict(inputs))
        return output_type(outcome=self.outcome, feedback="a fixed verdict")


class TestOutcomeMatches:
    """Scores 1.0 on a match, 0.0 on a mismatch, and names both in the reason either way."""

    async def test_scores_and_explains_a_match_and_a_mismatch(self) -> None:
        dataset: Dataset = Dataset(
            name="probe",
            cases=[
                Case(name="matches", inputs={"outcome": "approve"}, expected_output={"outcome": "approve"}),
                Case(name="mismatches", inputs={"outcome": "approve"}, expected_output={"outcome": "reject"}),
            ],
            evaluators=[OutcomeMatches()],
        )

        async def task(inputs: dict[str, str]) -> Verdict:
            return Verdict(outcome=inputs["outcome"], feedback="fixed")

        report = await dataset.evaluate(task)
        by_name = {case.name: case for case in report.cases}

        matched = by_name["matches"].scores["OutcomeMatches"]
        assert matched.value == 1.0
        assert "approve" in (matched.reason or "")

        mismatched = by_name["mismatches"].scores["OutcomeMatches"]
        assert mismatched.value == 0.0
        assert "approve" in (mismatched.reason or "")
        assert "reject" in (mismatched.reason or "")


class TestLoadDataset:
    """The shelved dataset file, in `Dataset.from_file` form."""

    def test_reads_the_four_cases_and_wires_outcome_matches(self) -> None:
        dataset = load_dataset(DATASET_PATH)

        assert [case.name for case in dataset.cases] == [
            "sound draft, a real decision",
            "restates the design, decides nothing",
            "wrong sections, Summary and Notes",
            "empty drafts list",
        ]
        assert len(dataset.evaluators) == 1
        assert isinstance(dataset.evaluators[0], OutcomeMatches)

    def test_reads_with_the_dataset_types_named_so_nothing_warns(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            dataset = load_dataset(DATASET_PATH)

        assert [str(warning.message) for warning in caught] == []
        assert dataset.cases[0].inputs["collection"] == "repo-docs"


class TestDatasetPath:
    """`<artifacts>/evals/<step>.yaml`, resolved against the workspace root (`CHECKOUT.parent`)."""

    def test_the_default_artifacts_directory_resolves_to_this_apps_own_workflow_files(self) -> None:
        path = _dataset_path("check_adrs", artifacts="harness/workflows")

        assert path == CHECKOUT.parent / "harness" / "workflows" / "evals" / "check-adrs.yaml"

    def test_a_templates_own_artifacts_directory_resolves_the_same_dataset_the_shelf_ships(self) -> None:
        path = _dataset_path("check_adrs", artifacts="templates/repo-docs/workflows")

        assert path == DATASET_PATH

    def test_loads_a_real_dataset_from_a_temporary_artifacts_directory(self, tmp_path: Path) -> None:
        evals_dir = tmp_path / "evals"
        evals_dir.mkdir()
        (evals_dir / "probe-step.yaml").write_text("cases:\n  - name: one\n    inputs: {}\n", encoding="utf-8")

        path = _dataset_path("probe_step", artifacts=str(tmp_path))

        assert path == evals_dir / "probe-step.yaml"
        dataset = load_dataset(path)
        assert [case.name for case in dataset.cases] == ["one"]


class TestResolveStep:
    """A step id resolves through the same library read `save` uses."""

    async def test_returns_the_briefing_the_shelf_holds(self) -> None:
        library = FakeReusableStepQueries({"st.check_adrs.v1": STEP})

        resolved = await resolve_step(library, "st.check_adrs.v1")

        assert resolved is STEP

    async def test_refuses_a_step_that_is_not_a_briefing(self) -> None:
        program = Program(name="fuse", source="1", output=VERDICT_SHAPE)
        library = FakeReusableStepQueries({"st.fuse.v1": program})

        with pytest.raises(StepNotABriefingError):
            await resolve_step(library, "st.fuse.v1")


class TestBindCollection:
    """`--knowledge-base` fills in a case's `collection` only when the case itself names none."""

    def test_fills_in_collection_only_when_a_case_names_none(self) -> None:
        dataset = Dataset(
            name="probe",
            cases=[
                Case(name="no collection", inputs={"drafts": []}),
                Case(name="own collection", inputs={"drafts": [], "collection": "other-knowledge-base"}),
            ],
        )

        bind_collection(dataset, "repo-docs")

        by_name = {case.name: case for case in dataset.cases}
        assert by_name["no collection"].inputs["collection"] == "repo-docs"
        assert by_name["own collection"].inputs["collection"] == "other-knowledge-base"


class TestRunStepEvals:
    """Performs every case through `perform`, the same call a workflow run makes."""

    async def test_the_report_says_which_cases_passed_and_which_failed(self) -> None:
        dataset = Dataset(
            name="check_adrs probe",
            cases=[
                Case(name="approve one", inputs={"drafts": [], "collection": "repo-docs"}, expected_output={"outcome": "approve"}),
                Case(name="approve two", inputs={"drafts": [], "collection": "repo-docs"}, expected_output={"outcome": "approve"}),
                Case(name="reject expected", inputs={"drafts": [], "collection": "repo-docs"}, expected_output={"outcome": "reject"}),
            ],
            evaluators=[OutcomeMatches()],
        )
        perform = FakePerform(outcome="approve")

        report = await run_step_evals(dataset, perform, STEP)

        by_name = {case.name: case for case in report.cases}
        assert by_name["approve one"].scores["OutcomeMatches"].value == 1.0
        assert by_name["approve two"].scores["OutcomeMatches"].value == 1.0
        assert by_name["reject expected"].scores["OutcomeMatches"].value == 0.0
        assert report_failed(report) is True
        assert len(perform.calls) == 3
        assert perform.calls[0] == {"drafts": [], "collection": "repo-docs"}

    async def test_report_not_failed_when_every_case_passes(self) -> None:
        dataset = Dataset(
            name="check_adrs probe",
            cases=[Case(name="approve", inputs={"drafts": [], "collection": "repo-docs"}, expected_output={"outcome": "approve"})],
            evaluators=[OutcomeMatches()],
        )
        perform = FakePerform(outcome="approve")

        report = await run_step_evals(dataset, perform, STEP)

        assert report_failed(report) is False

    async def test_refuses_a_step_whose_output_reuses_another_steps_type(self) -> None:
        step = Briefing(
            name="draft_variant",
            instructions="An instructions string long enough to satisfy this grammar's own minimum length rule.",
            output=SameAs(like="draft_adr"),
        )
        dataset = Dataset(name="probe", cases=[Case(name="x", inputs={})])
        perform = FakePerform(outcome="approve")

        with pytest.raises(StepOutputReusesAnotherError):
            await run_step_evals(dataset, perform, step)
