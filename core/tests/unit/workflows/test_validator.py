"""The electrician's truth table: one fixture per issue kind, and a clean bill for the real spec."""

from __future__ import annotations

import pytest

from pydantic import BaseModel, JsonValue

from core.ai.workflows.contracts import (
    Briefing,
    FromNode,
    FromParameter,
    InlineStep,
    MapNode,
    Node,
    ParameterSpec,
    PersonGate,
    RunNode,
    Shape,
    StepRegistry,
    WiringIssue,
    Workflow,
    build_graph,
    validate,
)
from core.ai.workflows.domain.validator import IssueType, judge_verdict_problem, static_type


def _kinds(spec: Workflow, registry: StepRegistry) -> set[IssueType]:
    return {issue.kind for issue in validate(spec, registry).issues}


def _spec(
    nodes: tuple, output: FromNode | FromParameter, *, inline: tuple[InlineStep, ...] = (), **params: ParameterSpec
) -> Workflow:
    return Workflow(
        name="fixture", version=1, description="validator fixture", parameters=params, inline=inline, nodes=nodes, output=output
    )


QUARTER = ParameterSpec(description="quarter", json_schema={"type": "string"})

_STRING = Shape(json_schema={"type": "string"})


class NoteDraft(BaseModel):
    body: str


class Rendered(BaseModel):
    document: str
    note_draft: NoteDraft


async def _stub_perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
    """Never called: `validate` only checks a briefing's declared shape, not its behaviour."""
    raise NotImplementedError


def _briefing_registry() -> StepRegistry:
    return StepRegistry(perform=_stub_perform)


def _briefing(name: str, inputs: dict[str, Shape], output: Shape) -> Briefing:
    instructions = f"Perform the turn named {name!r}, start to finish, exactly as briefed here."
    return Briefing(name=name, instructions=instructions, inputs=inputs, output=output)


def test_valid_spec_is_clean(quarterly_report: Workflow, registry: StepRegistry) -> None:
    report = validate(quarterly_report, registry)
    assert report.ok, [issue.model_dump() for issue in report.issues]


def test_duplicate_node_id(registry: StepRegistry) -> None:
    nodes = (
        RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
        RunNode(id="fetch_sales", work="fetch_costs", inputs={"quarter": FromParameter(parameter="quarter")}),
    )
    assert IssueType.DUPLICATE_NODE_ID in _kinds(_spec(nodes, FromNode(node="fetch_sales"), quarter=QUARTER), registry)


def test_unknown_node_ref(registry: StepRegistry) -> None:
    nodes = (RunNode(id="reconcile", work="reconcile", inputs={"sales": FromNode(node="ghost"), "costs": FromNode(node="ghost")}),)
    assert IssueType.UNKNOWN_NODE_REF in _kinds(_spec(nodes, FromNode(node="reconcile")), registry)


def test_unknown_parameter_ref(registry: StepRegistry) -> None:
    nodes = (RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="ghost")}),)
    assert IssueType.UNKNOWN_PARAMETER_REF in _kinds(_spec(nodes, FromNode(node="fetch_sales")), registry)


def test_unknown_work(registry: StepRegistry) -> None:
    nodes = (RunNode(id="x", work="not_registered", inputs={}),)
    assert IssueType.UNKNOWN_WORK in _kinds(_spec(nodes, FromNode(node="x")), registry)


def test_the_refusal_names_what_this_run_can_execute(registry: StepRegistry) -> None:
    """The refusal teaches the vocabulary, which is what lets an author hold none in advance.

    This is load-bearing rather than a nicety. The agent is told no inventory at all -- no
    list of registered steps anywhere in its prompt -- so this message is how it finds out, and one
    refusal has to be enough. `is not registered` alone would leave it guessing a name per turn.
    """
    nodes = (RunNode(id="x", work="not_registered", inputs={}),)
    detail = next(issue.detail for issue in validate(_spec(nodes, FromNode(node="x")), registry).issues)
    assert "not_registered" in detail, "say which name failed"
    for name in registry.names:
        assert name in detail, f"and every name that would have worked -- {name} is missing"


def test_input_mismatch_missing_and_extra(registry: StepRegistry) -> None:
    nodes = (
        RunNode(id="a", work="fetch_sales", inputs={}),  # missing required `quarter`
        RunNode(
            id="b", work="fetch_costs", inputs={"quarter": FromParameter(parameter="quarter"), "bogus": FromParameter(parameter="quarter")}
        ),
    )
    report = validate(_spec(nodes, FromNode(node="a"), quarter=QUARTER), registry)
    mismatches = [issue for issue in report.issues if issue.kind is IssueType.INPUT_MISMATCH]
    assert {issue.where for issue in mismatches} == {"a", "b"}


def test_type_mismatch(registry: StepRegistry) -> None:
    nodes = (
        RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
        # publish expects a ReportDraft; wiring it SalesData must be caught cold.
        RunNode(id="publish", work="publish", inputs={"report": FromNode(node="fetch_sales")}),
    )
    assert IssueType.TYPE_MISMATCH in _kinds(_spec(nodes, FromNode(node="publish"), quarter=QUARTER), registry)


def test_type_mismatch_through_gate_pass_through(registry: StepRegistry) -> None:
    """The gate's type is its source's type -- a bad wire hiding behind a gate is still caught."""
    nodes = (
        RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
        PersonGate(id="approve", reviews=FromNode(node="fetch_sales"), rejected_to="fetch_sales"),
        RunNode(id="publish", work="publish", inputs={"report": FromNode(node="approve")}),
    )
    assert IssueType.TYPE_MISMATCH in _kinds(_spec(nodes, FromNode(node="publish"), quarter=QUARTER), registry)


def test_wire_cycle(registry: StepRegistry) -> None:
    nodes = (
        RunNode(id="a", work="reconcile", inputs={"sales": FromNode(node="b"), "costs": FromNode(node="b")}),
        RunNode(id="b", work="reconcile", inputs={"sales": FromNode(node="a"), "costs": FromNode(node="a")}),
    )
    assert IssueType.WIRE_CYCLE in _kinds(_spec(nodes, FromNode(node="a")), registry)


def test_rejected_to_unknown_node(registry: StepRegistry) -> None:
    nodes = (
        RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
        PersonGate(id="approve", reviews=FromNode(node="fetch_sales"), rejected_to="ghost"),
    )
    assert IssueType.BAD_REJECTED_TO in _kinds(_spec(nodes, FromNode(node="approve"), quarter=QUARTER), registry)


def test_rejected_to_must_be_upstream(registry: StepRegistry) -> None:
    """A rejection must rewind the gate's own history, never jump sideways."""
    nodes = (
        RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
        RunNode(id="fetch_costs", work="fetch_costs", inputs={"quarter": FromParameter(parameter="quarter")}),
        PersonGate(id="approve", reviews=FromNode(node="fetch_sales"), rejected_to="fetch_costs"),
    )
    assert IssueType.BAD_REJECTED_TO in _kinds(_spec(nodes, FromNode(node="approve"), quarter=QUARTER), registry)


def test_a_node_nothing_reads_is_a_dead_end(registry: StepRegistry) -> None:
    """The bug a live agent found: this used to validate, save, and then fail to compile."""
    nodes = (
        RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
        RunNode(id="fetch_costs", work="fetch_costs", inputs={"quarter": FromParameter(parameter="quarter")}),
    )
    report = validate(_spec(nodes, FromNode(node="fetch_sales"), quarter=QUARTER), registry)
    assert IssueType.DEAD_END_NODE in {issue.kind for issue in report.issues}
    assert [issue.where for issue in report.issues if issue.kind is IssueType.DEAD_END_NODE] == ["fetch_costs"]


def test_validated_specs_compile(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """The invariant the dead-end rule exists to restore, asserted rather than assumed.

    `build_graph` runs `pydantic_graph`'s own structural validation, so if a clean report
    can still produce a graph the library refuses, this fails.
    """

    class NeverAsked:
        async def review(self, gate_id: str, value: object, *, note: str = "") -> bool:
            msg = "compiling must not ask for approval"
            raise AssertionError(msg)

    assert validate(quarterly_report, registry).ok
    build_graph(quarterly_report, registry, approval=NeverAsked())


def test_parameter_wire_scalar_mismatch_is_caught_cold(registry: StepRegistry) -> None:
    """A string-declared parameter feeding an int slot fails on every run -- so it fails here, not there.

    The bug a live artifact shipped: a question string wired into two integer slots validated
    clean, saved, and then died at runtime on the first question of a headless sweep. The
    declared schema and the step's own type were both known cold; nobody compared them.
    """

    async def truncate(items: str, limit: int) -> str:
        del limit
        return items

    registry.register(truncate)
    nodes = (
        RunNode(
            id="cut",
            work="truncate",
            inputs={"items": FromParameter(parameter="quarter"), "limit": FromParameter(parameter="quarter")},
        ),
    )
    report = validate(_spec(nodes, FromNode(node="cut"), quarter=QUARTER), registry)
    mismatches = [issue for issue in report.issues if issue.kind is IssueType.TYPE_MISMATCH]
    assert {issue.where for issue in mismatches} == {"cut.inputs[limit]"}, "the string->str wire beside it is fine"
    detail = mismatches[0].detail
    assert "quarter" in detail, "name the parameter, so the author knows which wire"
    assert "string" in detail, "name the declared side, so the author knows which one to fix"
    assert "int" in detail, "and the expecting side"


def test_parameter_wire_widening_and_non_scalar_stay_silent(registry: StepRegistry) -> None:
    """Only the confident scalar cases are judged; everything else stays a runtime check.

    integer->float is honest widening; an object schema against a dataclass slot is a
    comparison this validator cannot make without guessing, and guesses do not belong here.
    """

    async def scale(factor: float, config: dict[str, str]) -> float:
        del config
        return factor

    registry.register(scale)
    nodes = (
        RunNode(
            id="s",
            work="scale",
            inputs={"factor": FromParameter(parameter="count"), "config": FromParameter(parameter="settings")},
        ),
    )
    spec = _spec(
        nodes,
        FromNode(node="s"),
        count=ParameterSpec(description="an integer into a float slot", json_schema={"type": "integer"}),
        settings=ParameterSpec(description="an object only runtime can judge", json_schema={"type": "object"}),
    )
    assert IssueType.TYPE_MISMATCH not in _kinds(spec, registry)


def test_parameter_wire_scalar_mismatch_in_map_inputs(registry: StepRegistry) -> None:
    """A map's shared inputs follow the same rule as a run node's -- same wire, same finding."""

    async def seeds(quarter: str) -> list[str]:
        return [quarter]

    async def grow(seed: str, depth: int) -> str:
        del depth
        return seed

    registry.register(seeds)
    registry.register(grow)
    nodes = (
        RunNode(id="plant", work="seeds", inputs={"quarter": FromParameter(parameter="quarter")}),
        MapNode(
            id="g", turns=("grow",), over=FromNode(node="plant"), each="seed", inputs={"depth": FromParameter(parameter="quarter")}
        ),
    )
    report = validate(_spec(nodes, FromNode(node="g"), quarter=QUARTER), registry)
    mismatches = [issue for issue in report.issues if issue.kind is IssueType.TYPE_MISMATCH]
    assert {issue.where for issue in mismatches} == {"g.inputs[depth]"}


def test_bad_max_parallel(registry: StepRegistry) -> None:
    async def seeds(quarter: str) -> list[str]:
        return [quarter]

    async def grow(seed: str) -> str:
        return seed

    registry.register(seeds)
    registry.register(grow)
    nodes = (
        RunNode(id="plant", work="seeds", inputs={"quarter": FromParameter(parameter="quarter")}),
        MapNode(id="g", turns=("grow",), over=FromNode(node="plant"), each="seed", max_parallel=0),
    )
    assert IssueType.BAD_MAX_PARALLEL in _kinds(_spec(nodes, FromNode(node="g"), quarter=QUARTER), registry)


def test_bad_abort_after_repeats(registry: StepRegistry) -> None:
    async def seeds(quarter: str) -> list[str]:
        return [quarter]

    async def grow(seed: str) -> str:
        return seed

    registry.register(seeds)
    registry.register(grow)
    nodes = (
        RunNode(id="plant", work="seeds", inputs={"quarter": FromParameter(parameter="quarter")}),
        MapNode(id="g", turns=("grow",), over=FromNode(node="plant"), each="seed", abort_after_repeats=0),
    )
    assert IssueType.BAD_ABORT_AFTER_REPEATS in _kinds(_spec(nodes, FromNode(node="g"), quarter=QUARTER), registry)


# --- turns: a multi-turn map is one worker's conversation, checked turn by turn


def test_turn_one_must_declare_the_mapped_parameter(registry: StepRegistry) -> None:
    async def seeds(quarter: str) -> list[str]:
        return [quarter]

    async def needs_no_source(collection: str) -> str:
        return collection

    registry.register(seeds)
    registry.register(needs_no_source)
    nodes = (
        RunNode(id="plant", work="seeds", inputs={"quarter": FromParameter(parameter="quarter")}),
        MapNode(
            id="g",
            turns=("needs_no_source",),
            over=FromNode(node="plant"),
            each="source",
            inputs={"collection": FromParameter(parameter="quarter")},
        ),
    )
    report = validate(_spec(nodes, FromNode(node="g"), quarter=QUARTER), registry)
    mismatches = [issue for issue in report.issues if issue.kind is IssueType.INPUT_MISMATCH]
    assert any("first turn" in issue.detail and "needs_no_source" in issue.detail for issue in mismatches)


def test_a_later_turn_feeds_on_the_previous_output() -> None:
    """`digest(source, collection)` then `relate(digest, collection)` -- `collection` shared, `digest` fed automatically."""
    registry = _briefing_registry()

    async def seeds(quarter: str) -> list[str]:
        return [quarter]

    registry.register(seeds)
    digest = _briefing("digest", {"source": _STRING, "collection": _STRING}, _STRING)
    relate = _briefing("relate", {"digest": _STRING, "collection": _STRING}, _STRING)
    nodes = (
        RunNode(id="plant", work="seeds", inputs={"quarter": FromParameter(parameter="quarter")}),
        MapNode(
            id="g",
            turns=("digest", "relate"),
            over=FromNode(node="plant"),
            each="source",
            inputs={"collection": FromParameter(parameter="quarter")},
        ),
    )
    report = validate(_spec(nodes, FromNode(node="g"), inline=(digest, relate), quarter=QUARTER), registry)
    assert report.ok, report.issues


def test_two_uncovered_parameters_on_one_turn_are_refused() -> None:
    registry = _briefing_registry()

    async def seeds(quarter: str) -> list[str]:
        return [quarter]

    registry.register(seeds)
    digest = _briefing("digest", {"source": _STRING}, _STRING)
    relate = _briefing("relate", {"a": _STRING, "b": _STRING}, _STRING)
    nodes = (
        RunNode(id="plant", work="seeds", inputs={"quarter": FromParameter(parameter="quarter")}),
        MapNode(id="g", turns=("digest", "relate"), over=FromNode(node="plant"), each="source"),
    )
    report = validate(_spec(nodes, FromNode(node="g"), inline=(digest, relate), quarter=QUARTER), registry)
    assert any(
        issue.kind is IssueType.INPUT_MISMATCH and issue.where == "g.turns[relate]" and "a" in issue.detail and "b" in issue.detail
        for issue in report.issues
    )


def test_the_feed_type_must_match_the_previous_output() -> None:
    registry = _briefing_registry()

    async def seeds(quarter: str) -> list[str]:
        return [quarter]

    registry.register(seeds)
    digest = _briefing("digest", {"source": _STRING}, _STRING)
    digest_shape = Shape(
        json_schema={"type": "object", "title": "Digest", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    )
    relate = _briefing("relate", {"digest": digest_shape}, _STRING)
    nodes = (
        RunNode(id="plant", work="seeds", inputs={"quarter": FromParameter(parameter="quarter")}),
        MapNode(id="g", turns=("digest", "relate"), over=FromNode(node="plant"), each="source"),
    )
    report = validate(_spec(nodes, FromNode(node="g"), inline=(digest, relate), quarter=QUARTER), registry)
    mismatches = [issue for issue in report.issues if issue.kind is IssueType.TYPE_MISMATCH]
    assert any("str" in issue.detail and "Digest" in issue.detail for issue in mismatches)


def test_an_inputs_key_no_turn_declares_is_refused(registry: StepRegistry) -> None:
    async def seeds(quarter: str) -> list[str]:
        return [quarter]

    async def grow(seed: str) -> str:
        return seed

    registry.register(seeds)
    registry.register(grow)
    nodes = (
        RunNode(id="plant", work="seeds", inputs={"quarter": FromParameter(parameter="quarter")}),
        MapNode(id="g", turns=("grow",), over=FromNode(node="plant"), each="seed", inputs={"bogus": FromParameter(parameter="quarter")}),
    )
    assert IssueType.INPUT_MISMATCH in _kinds(_spec(nodes, FromNode(node="g"), quarter=QUARTER), registry)


def test_a_multi_turn_map_refuses_a_non_briefing_turn() -> None:
    registry = _briefing_registry()

    async def seeds(quarter: str) -> list[str]:
        return [quarter]

    async def app_step(digest: str) -> str:
        return digest

    registry.register(seeds)
    registry.register(app_step)
    digest = _briefing("digest", {"source": _STRING}, _STRING)
    nodes = (
        RunNode(id="plant", work="seeds", inputs={"quarter": FromParameter(parameter="quarter")}),
        MapNode(id="g", turns=("digest", "app_step"), over=FromNode(node="plant"), each="source"),
    )
    report = validate(_spec(nodes, FromNode(node="g"), inline=(digest,), quarter=QUARTER), registry)
    bad = [issue for issue in report.issues if issue.kind is IssueType.BAD_TURN_KIND]
    assert len(bad) == 1
    assert "app_step" in bad[0].detail
    assert "a multi turn map runs briefings only" in bad[0].detail


def test_a_wrong_kind_turn_reports_only_its_own_issue() -> None:
    registry = _briefing_registry()

    async def seeds(quarter: str) -> list[str]:
        return [quarter]

    async def app_step(digest: str, note: str) -> str:
        return digest + note

    registry.register(seeds)
    registry.register(app_step)
    digest = _briefing("digest", {"source": _STRING}, _STRING)
    nodes = (
        RunNode(id="plant", work="seeds", inputs={"quarter": FromParameter(parameter="quarter")}),
        MapNode(
            id="g",
            turns=("digest", "app_step"),
            over=FromNode(node="plant"),
            each="source",
            inputs={"note": FromParameter(parameter="quarter")},
        ),
    )
    report = validate(_spec(nodes, FromNode(node="g"), inline=(digest,), quarter=QUARTER), registry)
    assert report.issues == (
        WiringIssue(
            kind=IssueType.BAD_TURN_KIND,
            where="g.turns",
            detail="a multi turn map runs briefings only, turn 'app_step' is a step this app registered",
        ),
    )


def test_duplicate_turn_names_are_refused(registry: StepRegistry) -> None:
    async def seeds(quarter: str) -> list[str]:
        return [quarter]

    async def grow(seed: str) -> str:
        return seed

    registry.register(seeds)
    registry.register(grow)
    nodes = (
        RunNode(id="plant", work="seeds", inputs={"quarter": FromParameter(parameter="quarter")}),
        MapNode(id="g", turns=("grow", "grow"), over=FromNode(node="plant"), each="seed"),
    )
    assert IssueType.DUPLICATE_TURN_NAME in _kinds(_spec(nodes, FromNode(node="g"), quarter=QUARTER), registry)


def test_a_turn_wire_resolves_to_that_turns_list() -> None:
    registry = _briefing_registry()

    async def seeds(quarter: str) -> list[str]:
        return [quarter]

    registry.register(seeds)
    digest = _briefing("digest", {"source": _STRING}, _STRING)
    relate_shape = Shape(
        json_schema={"type": "object", "title": "RelateResult", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}
    )
    relate = _briefing("relate", {"digest": _STRING}, relate_shape)
    nodes: dict[str, Node] = {
        "plant": RunNode(id="plant", work="seeds", inputs={"quarter": FromParameter(parameter="quarter")}),
        "sources": MapNode(id="sources", turns=("digest", "relate"), over=FromNode(node="plant"), each="source"),
    }
    spec = _spec(tuple(nodes.values()), FromNode(node="sources"), inline=(digest, relate), quarter=QUARTER)
    built, problems = registry.including(spec)
    assert not problems, problems

    relate_impl = built.get("relate")
    assert relate_impl is not None
    assert relate_impl.output_type.__name__ == "RelateResult"
    assert static_type(FromNode(node="sources", turn="relate"), nodes, built) == list[relate_impl.output_type]


def test_a_turn_wire_on_a_run_node_is_refused(registry: StepRegistry) -> None:
    nodes = (
        RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
        RunNode(id="publish", work="publish", inputs={"report": FromNode(node="fetch_sales", turn="draft")}),
    )
    assert IssueType.BAD_TURN_WIRE in _kinds(_spec(nodes, FromNode(node="publish"), quarter=QUARTER), registry)


def test_a_turn_wire_naming_an_absent_turn_is_refused(registry: StepRegistry) -> None:
    async def seeds(quarter: str) -> list[str]:
        return [quarter]

    async def grow(seed: str) -> str:
        return seed

    registry.register(seeds)
    registry.register(grow)
    nodes = (
        RunNode(id="plant", work="seeds", inputs={"quarter": FromParameter(parameter="quarter")}),
        MapNode(id="g", turns=("grow",), over=FromNode(node="plant"), each="seed"),
        RunNode(id="publish", work="publish", inputs={"report": FromNode(node="g", turn="nonexistent")}),
    )
    assert IssueType.BAD_TURN_WIRE in _kinds(_spec(nodes, FromNode(node="publish"), quarter=QUARTER), registry)


def test_a_turn_wire_on_a_single_turn_map_is_refused(registry: StepRegistry) -> None:
    """A single-turn map publishes no dotted key -- a `turn` wire on it must be refused, not run into a `KeyError`."""

    async def seeds(quarter: str) -> list[str]:
        return [quarter]

    async def grow(seed: str) -> str:
        return seed

    registry.register(seeds)
    registry.register(grow)
    nodes = (
        RunNode(id="plant", work="seeds", inputs={"quarter": FromParameter(parameter="quarter")}),
        MapNode(id="g", turns=("grow",), over=FromNode(node="plant"), each="seed"),
        RunNode(id="publish", work="publish", inputs={"report": FromNode(node="g", turn="grow")}),
    )
    report = validate(_spec(nodes, FromNode(node="publish"), quarter=QUARTER), registry)

    assert not report.ok
    bad = [issue for issue in report.issues if issue.kind is IssueType.BAD_TURN_WIRE]
    assert bad, report.issues
    assert "runs one turn" in bad[0].detail
    assert "g" in bad[0].detail


def test_a_turn_wire_on_a_multi_turn_map_still_validates() -> None:
    """A multi-turn map's turn wire is untouched by the single-turn refusal."""
    registry = _briefing_registry()

    async def seeds(quarter: str) -> list[str]:
        return [quarter]

    registry.register(seeds)
    digest = _briefing("digest", {"source": _STRING}, _STRING)
    relate = _briefing("relate", {"digest": _STRING}, _STRING)
    nodes = (
        RunNode(id="plant", work="seeds", inputs={"quarter": FromParameter(parameter="quarter")}),
        MapNode(id="sources", turns=("digest", "relate"), over=FromNode(node="plant"), each="source"),
    )
    report = validate(_spec(nodes, FromNode(node="sources", turn="relate"), inline=(digest, relate), quarter=QUARTER), registry)

    assert report.ok, report.issues


def test_all_issues_reported_at_once(registry: StepRegistry) -> None:
    """The electrician reports everything it finds, never just the first fault."""
    nodes = (
        RunNode(id="x", work="not_registered", inputs={"bad": FromParameter(parameter="ghost")}),
        PersonGate(id="approve", reviews=FromNode(node="missing"), rejected_to="also_missing"),
    )
    kinds = _kinds(_spec(nodes, FromNode(node="x")), registry)
    assert {IssueType.UNKNOWN_WORK, IssueType.UNKNOWN_PARAMETER_REF, IssueType.UNKNOWN_NODE_REF, IssueType.BAD_REJECTED_TO} <= kinds


@pytest.mark.parametrize(
    ("result", "problem"),
    [
        ({"primary_work": "missing", "summary": "x"}, "primary"),
        ({"primary_work": "plain_run", "empty_primary": "noop", "summary": "x"}, "map"),
        ({"primary_work": "items", "required_gates": [{"gate_id": "plain_run", "accepted_sources": ["judge"]}], "summary": "x"}, "gate"),
        (
            {
                "primary_work": "items",
                "artifacts": [
                    {"id": "brief", "label": "Brief", "source": {"node": "missing"}, "media_type": "text/markdown"}
                ],
                "summary": "x",
            },
            "wire",
        ),
    ],
)
def test_result_declaration_is_checked_with_the_graph(result: JsonValue, problem: str) -> None:
    async def collect() -> list[str]:
        return ["item"]

    async def draft(item: str) -> str:
        return item

    async def plain(items: list[str]) -> str:
        return "plain"

    registry = StepRegistry()
    for work in (collect, draft, plain):
        registry.register(work)
    spec = Workflow.model_validate(
        {
            "name": "checked-result",
            "version": 1,
            "description": "A graph used to check result declarations against its real nodes.",
            "nodes": [
                {"kind": "run", "id": "collect", "work": "collect"},
                {"kind": "map", "id": "items", "turns": ["draft"], "over": {"node": "collect"}, "each": "item"},
                {"kind": "run", "id": "plain_run", "work": "plain", "inputs": {"items": {"node": "items"}}},
            ],
            "output": {"node": "plain_run"},
            "result": result,
        }
    )

    report = validate(spec, registry)

    assert any(problem in issue.where or problem in issue.detail for issue in report.issues), report.issues


def test_result_primary_work_may_name_a_run_when_its_empty_value_is_unknown() -> None:
    async def render() -> str:
        return "# Brief"

    registry = StepRegistry()
    registry.register(render)
    spec = Workflow.model_validate(
        {
            "name": "run-result",
            "version": 1,
            "description": "A direct render has no collection whose empty state could be a no-op.",
            "nodes": [{"kind": "run", "id": "render", "work": "render"}],
            "output": {"node": "render"},
            "result": {"summary": "The rendered brief.", "primary_work": "render"},
        }
    )

    assert validate(spec, registry).ok


def test_result_artifact_ids_are_unique() -> None:
    async def collect() -> list[str]:
        return ["item"]

    async def draft(item: str) -> str:
        return item

    async def render(value: list[str]) -> Rendered:
        return Rendered(document="# Brief", note_draft=NoteDraft(body=value[0]))

    registry = StepRegistry()
    for work in (collect, draft, render):
        registry.register(work)
    spec = Workflow.model_validate(
        {
            "name": "duplicate-artifacts",
            "version": 1,
            "description": "A graph with two labels that would otherwise select the same artifact id.",
            "nodes": [
                {"kind": "run", "id": "collect", "work": "collect"},
                {"kind": "map", "id": "items", "turns": ["draft"], "over": {"node": "collect"}, "each": "item"},
                {"kind": "run", "id": "render", "work": "render", "inputs": {"value": {"node": "items"}}},
            ],
            "output": {"node": "render"},
            "result": {
                "summary": "Rendered artifacts.",
                "primary_work": "items",
                "artifacts": [
                    {"id": "brief", "label": "Brief", "source": {"node": "render"}, "field": "document", "media_type": "text/markdown"},
                    {"id": "brief", "label": "Draft", "source": {"node": "render"}, "field": "note_draft", "media_type": "application/json"},
                ],
            },
        }
    )

    duplicates = [issue for issue in validate(spec, registry).issues if issue.kind is IssueType.BAD_RESULT]

    assert [(issue.where, issue.detail) for issue in duplicates] == [
        ("result.artifacts[1].id", "artifact id 'brief' appears more than once. Choose a unique id"),
    ]


def test_a_cyclic_gate_artifact_with_a_field_reports_the_cycle_without_recursive_inference() -> None:
    spec = Workflow.model_validate(
        {
            "name": "cyclic-result",
            "version": 1,
            "description": "A malformed gate cycle must be reported without recursing through an artifact source.",
            "nodes": [
                {"kind": "gate", "id": "first", "reviews": {"node": "second"}, "rejected_to": "second"},
                {"kind": "gate", "id": "second", "reviews": {"node": "first"}, "rejected_to": "first"},
                {"kind": "map", "id": "items", "turns": ["draft"], "over": {"node": "first"}, "each": "item"},
            ],
            "output": {"node": "items"},
            "result": {
                "summary": "A malformed cycle.",
                "primary_work": "items",
                "artifacts": [
                    {"id": "brief", "label": "Brief", "source": {"node": "first"}, "field": "document", "media_type": "text/markdown"}
                ],
            },
        }
    )

    report = validate(spec, StepRegistry())

    assert IssueType.WIRE_CYCLE in {issue.kind for issue in report.issues}
    assert all(issue.where != "result.artifacts[0].field" for issue in report.issues)


def test_result_artifacts_check_selected_fields_against_the_rendered_value() -> None:
    async def collect() -> list[str]:
        return ["item"]

    async def draft(item: str) -> NoteDraft:
        return NoteDraft(body=item)

    async def review(value: list[NoteDraft]) -> bool:
        return bool(value)

    async def render(value: list[NoteDraft]) -> Rendered:
        return Rendered(document="# Brief", note_draft=NoteDraft(body=value[0].body))

    registry = StepRegistry()
    for work in (collect, draft, review, render):
        registry.register(work)
    payload = {
        "name": "automation-result",
        "version": 1,
        "description": "Automate drafts, review them, then render the reviewed output for delivery.",
        "nodes": [
            {"kind": "run", "id": "collect", "work": "collect"},
            {"kind": "map", "id": "draft_automation", "turns": ["draft"], "over": {"node": "collect"}, "each": "item"},
            {
                "kind": "judge",
                "id": "review_automation",
                "work": "review",
                "reviews": {"node": "draft_automation"},
                "rejected_to": "draft_automation",
            },
            {"kind": "run", "id": "render", "work": "render", "inputs": {"value": {"node": "review_automation"}}},
        ],
        "output": {"node": "render"},
        "result": {
            "summary": "Reviewed automation output.",
            "primary_work": "draft_automation",
            "required_gates": [{"gate_id": "review_automation", "accepted_sources": ["judge"]}],
            "artifacts": [
                {"id": "document", "label": "Document", "source": {"node": "render"}, "field": "document", "media_type": "text/markdown"},
                {"id": "note_draft", "label": "Note draft", "source": {"node": "render"}, "field": "note_draft", "media_type": "application/json"},
            ],
        },
    }

    assert validate(Workflow.model_validate(payload), registry).ok

    for artifact, problem in (
        ({"id": "missing", "label": "Missing", "source": {"node": "render"}, "field": "missing", "media_type": "application/json"}, "field"),
        ({"id": "scalar", "label": "Scalar", "source": {"node": "review_automation"}, "field": "value", "media_type": "application/json"}, "object"),
        ({"id": "wrong", "label": "Wrong", "source": {"node": "render"}, "field": "note_draft", "media_type": "text/markdown"}, "string"),
    ):
        invalid = {**payload, "result": {**payload["result"], "artifacts": [artifact]}}
        report = validate(Workflow.model_validate(invalid), registry)
        assert any(problem in issue.where or problem in issue.detail for issue in report.issues), report.issues


# --- judge_verdict_problem: the pure function `_check_judge_node` and `WorkflowBuilder.judge` share


def test_judge_verdict_problem_accepts_a_bare_bool_with_no_approved_when() -> None:
    assert judge_verdict_problem(bool, None, work="score") is None


def test_judge_verdict_problem_refuses_a_non_bool_output_with_no_approved_when() -> None:
    detail = judge_verdict_problem(str, None, work="score")

    assert detail is not None
    assert "set `approved_when`" in detail


def test_judge_verdict_problem_refuses_an_output_type_with_no_fields_to_read_a_verdict_from() -> None:
    """The no-`model_fields` branch.

    `approved_when` names a field, but `str` is not even a shape with fields at all -- there is
    nowhere for a verdict to live.
    """
    detail = judge_verdict_problem(str, "approved", work="score")

    assert detail == "work 'score' returns str, which has no fields to read a verdict from"
