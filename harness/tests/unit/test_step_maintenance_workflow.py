"""The `step-maintenance` workflow, read off its own files: its graph, its model steps, their cases, and one whole run.

The graph, the steps and the case files come from `harness/workflows`, never from a copy typed out here. The whole run
is performed in this process over a library holding two consumers of one changed step: the registered steps run for
real over fakes, the program runs in the real sandbox, and the planner, the plan judge, the drafter and the repository
judge are scripted.
"""

from __future__ import annotations

import json
import re

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from core.ai.workflows.contracts import (
    Briefing,
    FromNode,
    FromParameter,
    JudgeGate,
    LiveTrialMarker,
    MapNode,
    ParameterSpec,
    Program,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    load_step_document,
    run_workflow,
    step_page_id,
    validate,
)
from core.ai.workflows.domain.vocabulary import VERDICTS, SameAs
from pydantic import TypeAdapter
from pydantic_evals.evaluators import Contains, LLMJudge
from pydantic_monty import AsyncMonty

from harness.adapters.environment import code_runner
from harness.domain.ports import CHECK_CHANGE_TOOL, JUDGE_TOOLS
from harness.features.evals.contracts import check_step_cases
from harness.features.evals.domain.step_evaluation import CaseDataset, OutcomeMatches
from harness.features.maintenance.contracts import (
    STEP_MAINTENANCE_AGENT,
    ConsumerList,
    ContractCheck,
    HandOver,
    LandingReport,
    MaintenancePlan,
    RepositoryChange,
    RepositoryVerdict,
)
from harness.features.maintenance.domain import CHANGED_STEPS, MAINTENANCE_WORKFLOW, PLAN_GATE
from harness.rendering import parallel_groups
from tests.support.grants import app_grants, app_reads
from tests.support.maintenance import (
    CHANGED,
    CHANGED_HASH,
    FEEDBACK,
    MaintenanceWorld,
    ScriptedMaintenanceModels,
    register_maintenance_steps,
    step_maintenance_workflow,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Mapping

    from core.ai.workflows.contracts import CodeRunner, GateDecision, StepDocument
    from core.ai.workflows.registry import StepImplementation
    from pydantic import JsonValue

WORKFLOW_FILES = Path(__file__).resolve().parents[2] / "workflows"
TEMPLATES = Path(__file__).resolve().parents[3] / "templates"
WORKFLOW_FILE = WORKFLOW_FILES / "workflows" / "step-maintenance.md"
MODEL_STEPS = ("plan_changes", "judge_plan", "draft_repository_change", "judge_repository_change")
"""The four steps of the workflow a model performs, each a step file of its own beside its case file."""
JUDGES = ("judge_plan", "judge_repository_change")


def _documents() -> dict[str, StepDocument]:
    """Every step file this app ships, by step name."""
    documents = [load_step_document(path.read_text(encoding="utf-8")) for path in sorted((WORKFLOW_FILES / "steps").glob("*.md"))]
    return {document.step.name: document for document in documents}


def _spec() -> Workflow:
    return step_maintenance_workflow()


def _step(name: str) -> Briefing:
    step = _documents()[name].step
    assert isinstance(step, Briefing), name
    return step


def _node(spec: Workflow, node_id: str) -> RunNode | MapNode | JudgeGate:
    node = next(node for node in spec.nodes if node.id == node_id)
    assert isinstance(node, RunNode | MapNode | JudgeGate), node_id
    return node


def _run(spec: Workflow, node_id: str) -> RunNode:
    node = _node(spec, node_id)
    assert isinstance(node, RunNode), node_id
    return node


async def _never_performed(task: Briefing, output_type: type, /, **inputs: object) -> object:
    """Validation only checks that this run can perform a briefing, never runs one."""
    del output_type, inputs
    msg = f"nothing should perform {task.name!r} here"
    raise AssertionError(msg)


async def _never_run(step: Program, output_type: type, /, **inputs: object) -> object:
    """Validation only checks that this run has a sandbox, never runs a program in it."""
    del output_type, inputs
    msg = f"nothing should run {step.name!r} here"
    raise AssertionError(msg)


async def _app_registry(run_code: CodeRunner = _never_run) -> StepRegistry:
    """A registry offering exactly what this app offers a declared step, with a sandbox, holding the step maintenance steps."""
    grantable = await app_grants()
    registry = StepRegistry(
        perform=_never_performed, grants=grantable, run_code=run_code, judge_tools=frozenset(JUDGE_TOOLS) & frozenset(grantable)
    )
    register_maintenance_steps(registry)
    return registry


def _depends_on(spec: Workflow) -> dict[str, set[str]]:
    """Every node each node waits on, directly or through others."""
    direct: dict[str, set[str]] = {}
    for node in spec.nodes:
        wires = [*node.inputs.values()] if isinstance(node, RunNode) else []
        if isinstance(node, MapNode):
            wires = [node.over, *node.inputs.values()]
        if isinstance(node, JudgeGate):
            wires = [node.reviews, *node.inputs.values()]
        direct[node.id] = {wire.node for wire in wires if isinstance(wire, FromNode)}
    closed: dict[str, set[str]] = {}

    def upstream(node_id: str) -> set[str]:
        if node_id not in closed:
            closed[node_id] = set()
            for producer in direct[node_id]:
                closed[node_id] |= {producer, *upstream(producer)}
        return closed[node_id]

    return {node_id: upstream(node_id) for node_id in direct}


# --- the graph --------------------------------------------------------------------------------------------------------


async def test_the_workflow_file_loads_and_validates_against_the_app_s_registry() -> None:
    report = validate(_spec(), await _app_registry())

    assert report.ok, report.issues


def test_the_workflow_takes_the_changed_step_ids_and_answers_with_the_hand_over_after_the_landing_its_judge_gates() -> None:
    spec = _spec()

    assert (spec.name, spec.version) == (MAINTENANCE_WORKFLOW, 1)
    assert spec.parameters == {
        CHANGED_STEPS: ParameterSpec(
            description=spec.parameters[CHANGED_STEPS].description, json_schema={"type": "string", "minLength": 1}, required=True
        )
    }
    assert spec.output == FromNode(node="hand_over_repository_changes")
    assert spec.result is not None
    assert spec.result.primary_work == "land_changes"
    assert [(gate.gate_id, gate.accepted_sources) for gate in spec.result.required_gates] == [(PLAN_GATE, ("judge",))]


def test_both_judges_hold_only_tools_a_judge_may_hold() -> None:
    for name in JUDGES:
        assert set(_step(name).tools) <= JUDGE_TOOLS, name


def test_the_plan_judge_reads_the_evidence_from_the_registered_steps_run_again_after_the_plan_and_sends_a_rejection_back() -> None:
    """The judge's evidence comes from `list_consumers`, `check_contracts` and `run_step_cases`, never from the planner.

    A rejection re-runs only what sits after `plan_changes`, so those steps run again there, handed the run's own
    `changed_step_ids` once the plan exists, and every attempt is judged on evidence made after it.
    """
    spec = _spec()
    judge = _node(spec, PLAN_GATE)
    upstream = _depends_on(spec)

    assert isinstance(judge, JudgeGate)
    assert (judge.work, judge.reviews, judge.approved_when, judge.rejected_to, judge.max_attempts) == (
        "judge_plan", FromNode(node="plan_changes"), "outcome", "plan_changes", 3,
    )
    assert judge.inputs == {
        "consumers": FromNode(node="relist_consumers"),
        "checks": FromNode(node="recheck_contracts"),
        "cases": FromNode(node="rerun_step_cases"),
    }
    assert [(_run(spec, node_id).work, _run(spec, node_id).inputs) for node_id in ("relist_consumers", "recheck_contracts", "rerun_step_cases")] == [
        ("list_consumers", {"changed_step_ids": FromNode(node="pass_changed_ids")}),
        ("check_contracts", {"consumers": FromNode(node="relist_consumers")}),
        ("run_step_cases", {"changed_step_ids": FromNode(node="pass_changed_ids")}),
    ]
    passed = _node(spec, "pass_changed_ids")
    assert passed.inputs == {"plan": FromNode(node="plan_changes"), "changed_step_ids": FromParameter(parameter=CHANGED_STEPS)}, (
        "the run's own parameter, so a plan that leaves a step out cannot hide its consumers from the judge"
    )
    for evidence in judge.inputs.values():
        assert isinstance(evidence, FromNode)
        assert "plan_changes" in upstream[evidence.node], evidence.node
    step = _step("judge_plan")
    assert step.inputs == {
        "plan": SameAs(like="prepare_plan"),
        "consumers": SameAs(like="list_consumers"),
        "checks": SameAs(like="check_contracts"),
        "cases": SameAs(like="run_step_cases"),
    }
    assert isinstance(step.output, Shape)
    properties = step.output.json_schema["properties"]
    assert set(properties) == {"outcome", "feedback"}
    assert tuple(properties["outcome"]["enum"]) == VERDICTS
    assert _node(spec, "land_changes").inputs == {"plan": FromNode(node=PLAN_GATE)}, "nothing lands unless the judge approved it"


def test_the_planner_drafts_from_prepare_plan_on_the_orchestrator_tier_and_its_brief_ends_on_check_change() -> None:
    spec = _spec()

    assert _node(spec, "prepare_plan").inputs == {
        "consumers": FromNode(node="list_consumers"),
        "checks": FromNode(node="check_contracts"),
        "cases": FromNode(node="run_step_cases"),
    }
    planner = _node(spec, "plan_changes")
    assert isinstance(planner, RunNode)
    assert planner.work == "plan_changes"
    assert planner.inputs == {"draft": FromNode(node="prepare_plan"), "consumers": FromNode(node="list_consumers")}, (
        "the planner decides by each pair's diff, goal and trust, which only the consumer list holds"
    )
    step = _step("plan_changes")
    assert step.inputs == {"draft": SameAs(like="prepare_plan"), "consumers": SameAs(like="list_consumers")}
    assert (step.output, step.tier) == (SameAs(like="prepare_plan"), "orchestrator")
    assert CHECK_CHANGE_TOOL in step.tools
    assert step.budget is not None
    assert step.budget.tool_calls is not None
    last = step.instructions.strip().split("\n\n")[-1]
    assert "check_change" in last, last


def test_the_drafter_checks_its_draft_with_check_change_and_answers_one_element_of_select_disagreements() -> None:
    drafter = _step("draft_repository_change")

    assert drafter.inputs == {"change": SameAs(like="select_disagreements", element=True)}
    assert drafter.output == SameAs(like="select_disagreements", element=True)
    assert CHECK_CHANGE_TOOL in drafter.tools
    assert drafter.budget is not None
    assert "check_change" in drafter.instructions.strip().split("\n\n")[-1]
    judge = _step("judge_repository_change")
    assert judge.inputs == {"draft": SameAs(like="select_disagreements", element=True)}
    assert judge.output == SameAs(like="repository_verdict")


def test_check_contracts_and_run_step_cases_run_concurrently() -> None:
    spec = _spec()
    upstream = _depends_on(spec)

    for checks, cases in (("check_contracts", "run_step_cases"), ("recheck_contracts", "rerun_step_cases")):
        assert cases not in upstream[checks]
        assert checks not in upstream[cases]
    assert "run_step_cases" in parallel_groups(spec).split(", "), "run_step_cases waits on nothing, so it runs beside check_contracts"


def test_the_report_and_the_repository_branch_start_after_the_landing_and_the_landing_waits_on_neither() -> None:
    spec = _spec()
    upstream = _depends_on(spec)

    assert _node(spec, "report_changes").inputs == {"landed": FromNode(node="land_changes")}
    assert _node(spec, "select_disagreements").inputs == {"landed": FromNode(node="land_changes"), "consumers": FromNode(node="list_consumers")}
    for branch in ("report_changes", "select_disagreements", "draft_repository_changes", "judge_repository_changes", "hand_over_repository_changes"):
        assert "land_changes" in upstream[branch], branch
        assert branch not in upstream["land_changes"], branch
    drafts = _node(spec, "draft_repository_changes")
    verdicts = _node(spec, "judge_repository_changes")
    assert isinstance(drafts, MapNode)
    assert isinstance(verdicts, MapNode)
    assert (drafts.turns, drafts.over, drafts.each, drafts.on_error) == (("draft_repository_change",), FromNode(node="select_disagreements"), "change", "skip")
    assert (verdicts.turns, verdicts.over, verdicts.each, verdicts.on_error) == (("judge_repository_change",), FromNode(node="draft_repository_changes"), "draft", "skip")
    assert _node(spec, "hand_over_repository_changes").inputs == {
        "report": FromNode(node="report_changes"),
        "disagreements": FromNode(node="select_disagreements"),
        "drafts": FromNode(node="draft_repository_changes"),
        "verdicts": FromNode(node="judge_repository_changes"),
    }


# --- no knowledge base vocabulary -------------------------------------------------------------------------------------

_PROSE_KEYS = frozenset({"title", "summary", "description", "instructions", "purpose", "produces", "owner_participation", "not_for", "label"})
"""Frontmatter keys whose value is prose: only its quoted and backticked words are read as names."""
_CODE_SPAN = re.compile(r"`([^`\n]+)`|\"([^\"\n]+)\"|'([^'\n]+)'")
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")


def _schemas() -> Iterator[Mapping[str, JsonValue]]:
    for path in sorted(TEMPLATES.glob("*/knowledge-base-schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(schema, dict)
        yield schema


def _listed(page_type: Mapping[str, JsonValue], key: str) -> list[str]:
    """The names a page type lists under `key`, each a bare string or a mapping's `name` or `slug`."""
    listed = page_type.get(key) or []
    assert isinstance(listed, list)
    names = [item if isinstance(item, str) else item.get("name") or item.get("slug") for item in listed if isinstance(item, str | dict)]
    return [name for name in names if isinstance(name, str)]


def _schema_names() -> tuple[set[str], set[str], set[str]]:
    """Every template schema's names: those distinctive anywhere, the id prefixes, and the plain words.

    Distinctive names are each template's slug (its collection name), each folder holding a slash, each heading, and
    every page type, field, status or section name holding an underscore or a hyphen. Plain words are the rest.
    """
    distinctive: set[str] = set()
    prefixes: set[str] = set()
    plain: set[str] = set()
    for schema in _schemas():
        slug = schema["slug"]
        assert isinstance(slug, str)
        distinctive.add(slug)
        page_types = schema["page_types"]
        assert isinstance(page_types, list)
        for page_type in page_types:
            assert isinstance(page_type, dict)
            names = [str(page_type["page_type"]), *_listed(page_type, "frontmatter_fields"), *_listed(page_type, "edge_fields")]
            names += [*_listed(page_type, "status_values"), *_listed(page_type, "canonical_sections"), *_listed(page_type, "folders")]
            folder = page_type.get("folder")
            names += [folder] if isinstance(folder, str) else []
            sections = page_type.get("canonical_sections")
            for section in sections if isinstance(sections, list) else []:
                if isinstance(section, dict) and isinstance(heading := section.get("heading"), str):
                    distinctive.add(heading)
            if isinstance(prefix := page_type.get("id_prefix"), str) and prefix:
                prefixes.add(prefix)
            for name in names:
                (distinctive if re.search(r"[_/-]", name) else plain).add(name)
    return distinctive, prefixes, plain


def _own_words() -> frozenset[str]:
    """The field names and closed values of the workflow grammar and of the types the maintenance steps hand along.

    A template schema may use the same English word (`map`, `flag`, `adopted`, `decision`), and the grammar's word or the
    maintenance step's own word is not that schema's. `draft`, a status two templates name, is also the name the planner
    and the repository judge give the value they are handed.
    """
    words: set[str] = {*VERDICTS, "draft"}

    def collect(schema: object) -> None:
        if isinstance(schema, dict):
            if isinstance(properties := schema.get("properties"), dict):
                words.update(properties)
            if isinstance(closed := schema.get("enum"), list):
                words.update(value for value in closed if isinstance(value, str))
            if isinstance(constant := schema.get("const"), str):
                words.add(constant)
            for value in schema.values():
                collect(value)
        elif isinstance(schema, list):
            for value in schema:
                collect(value)

    for model in (Workflow, ConsumerList, ContractCheck, MaintenancePlan, LandingReport, RepositoryChange, RepositoryVerdict, HandOver):
        collect(model.model_json_schema())
    return frozenset(words)


def _names_in_prose(text: str) -> set[str]:
    return {name for span in _CODE_SPAN.findall(text) for part in span if part for name in _NAME.findall(part)}


def _names_in_data(data: object, *, prose: bool = False) -> set[str]:
    """Every name a parsed document holds as data: its keys, its one-word values, and the quoted words of its prose."""
    if isinstance(data, dict):
        names: set[str] = set()
        for key, value in data.items():
            names.add(str(key))
            names |= _names_in_data(value, prose=str(key) in _PROSE_KEYS)
        return names
    if isinstance(data, list):
        return {name for value in data for name in _names_in_data(value)}
    if isinstance(data, str):
        if prose or re.search(r"\s", data):
            return _names_in_prose(data)
        return {data, *re.split(r"[.:/]", data)}
    return set()


def _maintenance_files() -> list[Path]:
    hyphened = [name.replace("_", "-") for name in MODEL_STEPS]
    return [WORKFLOW_FILE, *(WORKFLOW_FILES / "steps" / f"{name}.md" for name in hyphened), *(WORKFLOW_FILES / "evals" / f"{name}.yaml" for name in hyphened)]


def test_no_name_from_any_template_s_schema_appears_in_the_workflow_its_steps_or_their_cases() -> None:
    """A template's slug, folders, headings, id prefixes and distinctive names never appear, anywhere in the text.

    Its plain page type, field, status and section words never appear as a name: a key, a one-word value, or a quoted
    or backticked word in prose, unless the maintenance steps' own types define the same word.
    """
    distinctive, prefixes, plain = _schema_names()
    own = _own_words()
    found: list[str] = []
    for path in _maintenance_files():
        text = path.read_text(encoding="utf-8")
        found.extend(f"{path.name}: {name!r}" for name in distinctive if re.search(rf"(?<![\w/.-]){re.escape(name)}(?![\w/-])", text))
        found.extend(f"{path.name}: the id prefix {prefix!r}" for prefix in prefixes if re.search(rf"(?<![\w./-]){re.escape(prefix)}[a-z0-9]", text))
        if path.suffix == ".md":
            front, body = text.split("---", 2)[1:]
            names = _names_in_data(yaml.safe_load(front)) | _names_in_prose(body)
        else:
            names = _names_in_data(yaml.safe_load(text))
        found.extend(f"{path.name}: {name!r}" for name in sorted((names & plain) - own))

    assert found == []


# --- the cases --------------------------------------------------------------------------------------------------------


def _cases(name: str) -> dict[str, JsonValue]:
    loaded = yaml.safe_load((WORKFLOW_FILES / "evals" / f"{name.replace('_', '-')}.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _dataset(name: str) -> CaseDataset:
    return CaseDataset.from_dict(_cases(name), custom_evaluator_types=[OutcomeMatches], default_name=name)


async def _implementations() -> dict[str, StepImplementation]:
    """The four model steps built against the app's registry, each with its input types."""
    registry = await _app_registry()
    bringing = Workflow(
        name="every-model-step",
        version=1,
        description="The step maintenance workflow's model steps, brought and built so each can be read for its inputs.",
        parameters={"unused": ParameterSpec(description="never read", json_schema={"type": "object"})},
        inline=tuple(_step(name) for name in MODEL_STEPS),
        nodes=(),
        output=FromParameter(parameter="unused"),
    )
    built, problems = registry.including(bringing)
    assert problems == (), problems
    found = {name: built.get(name) for name in MODEL_STEPS}
    return {name: implementation for name, implementation in found.items() if implementation is not None}


async def test_every_case_file_loads_as_its_step_s_cases_and_each_case_fits_the_step_s_inputs() -> None:
    implementations = await _implementations()
    documents = _documents()

    for name in MODEL_STEPS:
        check_step_cases(step_page_id(name, documents[name].version), _cases(name))
        implementation = implementations[name]
        for case in _dataset(name).cases:
            assert set(case.inputs) == set(implementation.input_types), (name, case.name)
            for slot, value in case.inputs.items():
                TypeAdapter(implementation.input_types[slot]).validate_python(value)


def _outcome(expected: object) -> str:
    """The outcome a case of the plan judge expects."""
    assert isinstance(expected, dict)
    outcome = expected.get("outcome")
    assert isinstance(outcome, str)
    return outcome


def test_the_plan_judge_s_cases_approve_reject_a_broken_goal_and_a_failing_check_and_cannot_judge() -> None:
    dataset = _dataset("judge_plan")
    expected = [_outcome(case.expected_output) for case in dataset.cases]

    assert len(expected) >= 4
    assert expected.count("approve") >= 1
    assert expected.count("reject") >= 2
    assert expected.count("cannot_judge") >= 1
    assert any(isinstance(evaluator, OutcomeMatches) for evaluator in dataset.evaluators)
    empty = [case for case in dataset.cases if not MaintenancePlan.model_validate(case.inputs["plan"]).changes]
    assert [(ConsumerList.model_validate(case.inputs["consumers"]).pairs, _outcome(case.expected_output)) for case in empty] == [([], "approve")], (
        "a plan with nothing to land is approved when no consumer needs a change, or every new step would end in rejections"
    )


def test_the_repository_judge_s_cases_keep_one_draft_and_drop_another() -> None:
    dataset = _dataset("judge_repository_change")
    decisions = [
        evaluator.value["decision"]
        for case in dataset.cases
        for evaluator in case.evaluators
        if isinstance(evaluator, Contains) and isinstance(evaluator.value, dict)
    ]

    assert len(decisions) == len(dataset.cases)
    assert {"keep", "drop"} <= set(decisions)


def test_the_planner_and_the_drafter_are_graded_by_a_rubric_whose_judge_names_a_model() -> None:
    for name in ("plan_changes", "draft_repository_change"):
        dataset = _dataset(name)
        judges = [evaluator for case in dataset.cases for evaluator in (*dataset.evaluators, *case.evaluators) if isinstance(evaluator, LLMJudge)]
        assert dataset.cases, name
        assert len(judges) >= len(dataset.cases), name
        assert all(judge.model is not None and judge.rubric for judge in judges), name


# --- one whole run ----------------------------------------------------------------------------------------------------


@dataclass
class Scripted:
    """Performs each step in this process: a model step through the scripted models, every other step for real.

    `calls` keeps each node performed, in order.
    """

    models: ScriptedMaintenanceModels = field(default_factory=ScriptedMaintenanceModels)
    calls: list[str] = field(default_factory=list)

    async def perform(self, node_id: str, implementation: StepImplementation, kwargs: Mapping[str, object]) -> object:
        self.calls.append(node_id)
        answer = self.models.answer(implementation.name, implementation.output_type, kwargs)
        return answer if answer is not None else await implementation.function(**kwargs)


class NobodyDecides:
    """No person gate and no judge that cannot judge is reached in these runs."""

    async def review(self, gate_id: str, value: object, *, note: str = "") -> GateDecision | bool:
        del value
        msg = f"nobody should be asked about {gate_id!r} here: {note}"
        raise AssertionError(msg)


async def _no_call(name: str, args: dict[str, object], /) -> object:
    """The program reads nothing, so no tool is ever called from the sandbox."""
    del args
    msg = f"the program reads nothing, so {name!r} must never be called"
    raise AssertionError(msg)


async def _world_registry(world: MaintenanceWorld, sandbox: AsyncMonty) -> StepRegistry:
    """The app's grants, the real sandbox for the program, and the world's steps registered."""
    grantable = await app_grants()
    registry = StepRegistry(
        perform=_never_performed,
        grants=grantable,
        run_code=code_runner(sandbox, reads=await app_reads(), call=_no_call),
        judge_tools=frozenset(JUDGE_TOOLS) & frozenset(grantable),
    )
    world.steps.register(registry)
    return registry


@pytest.fixture
async def sandbox() -> AsyncIterator[AsyncMonty]:
    """A fresh worker pool for each test, entered on the test's own event loop."""
    async with AsyncMonty(request_timeout=30.0) as pool:
        yield pool


async def test_the_second_pass_reads_the_run_s_own_changed_steps_whatever_the_plan_holds(sandbox: AsyncMonty) -> None:
    """A plan that leaves a changed step out cannot hide that step's consumers from the judge."""
    built, problems = (await _app_registry(code_runner(sandbox, reads=await app_reads(), call=_no_call))).including(_spec())
    passing = built.get("pass_changed_ids")

    assert problems == ()
    assert passing is not None
    assert await passing.function(plan=MaintenancePlan(changes=[], left_for_next_run=[]), changed_step_ids="st.a.v2, st.b.v3") == "st.a.v2, st.b.v3"


async def test_one_run_adopts_one_consumer_on_live_trial_and_keeps_the_other_once_the_judge_approves_the_second_plan(sandbox: AsyncMonty) -> None:
    world = MaintenanceWorld()
    meeting, standup = await world.fill()
    scripted = Scripted()

    handed = await run_workflow(
        _spec(), await _world_registry(world, sandbox), params={CHANGED_STEPS: CHANGED}, approval=NobodyDecides(), executor=scripted
    )

    models = scripted.models
    assert models.performed == ["plan_changes", PLAN_GATE, "plan_changes", PLAN_GATE, "draft_repository_change", "judge_repository_change"]
    assert scripted.calls.count("land_changes") == 1
    last_judging = len(scripted.calls) - 1 - scripted.calls[::-1].index(PLAN_GATE)
    assert scripted.calls.index("land_changes") > last_judging, "nothing lands before the judge approves"
    assert models.repairs == [[], [(PLAN_GATE, 1, FEEDBACK)]], "the second plan reads the judge's feedback on the first"
    for evidence in ("relist_consumers", "recheck_contracts", "rerun_step_cases"):
        assert scripted.calls.count(evidence) == 2, "each attempt is judged on evidence made after its plan"
    judged = models.judged[0]
    assert isinstance(judged["consumers"], ConsumerList)
    assert [pair.workflow_id for pair in judged["consumers"].pairs] == [meeting, standup]
    checks = judged["checks"]
    assert isinstance(checks, list)
    assert all(isinstance(check, ContractCheck) and check.passed for check in checks)

    adopted = "wf.meeting-actions.v2"
    assert await world.library.current(meeting) == adopted
    assert world.store.written(adopted)["saved_by"] == STEP_MAINTENANCE_AGENT
    assert await world.library.fetch_live_trial(adopted) == LiveTrialMarker(previous=meeting, step="extract_actions", step_hash=CHANGED_HASH)
    assert await world.library.current(standup) == standup
    (kept,) = await world.library.fetch_consumer_record(standup)
    assert (kept.declined, kept.declined_because) == (CHANGED_HASH, "kept")

    assert isinstance(handed, HandOver)
    assert handed.flags == ["mf.change.extract_actions"]
    assert (handed.dropped, handed.problems) == ([], [])
    assert f"{adopted} adopts {CHANGED} on live trial in place of {meeting}" in handed.report
    assert f"{standup} keeps its copy instead of {CHANGED}" in handed.report
    flag = await world.library.fetch_flag("mf.change.extract_actions")
    assert flag is not None
    assert "`unowned` parameter" in flag.proposed_change
    assert len(world.notifier.sent) == 2, "one notice for the landing and one for the repository change"
