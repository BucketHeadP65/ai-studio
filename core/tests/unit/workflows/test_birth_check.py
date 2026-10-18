"""The birth check: does this workflow still do what the run that bore it actually did?

A stored procedure is only trustworthy if the thing it produced once, it still produces.
The attested run is the evidence -- it recorded its inputs and the *shape* of every value
it produced -- so the check is a comparison, not an opinion, and it comes in two costs:

* `check_birth` is free and structural: the spec still validates, the nodes that produced
  values still exist, and their steps still declare the types the run actually returned.
* `replay_birth` spends a real run against the origin inputs and compares what comes back.

Neither asks a model anything. Both are the Grove clause-to-eval pattern applied to a
workflow: the contract and its conformance check are one versioned unit.
"""

from __future__ import annotations

from pydantic import TypeAdapter

from core.ai.workflows.birth import replay_birth
from core.ai.workflows.contracts import (
    Briefing,
    FromNode,
    FromParameter,
    JudgeGate,
    MapNode,
    ParameterSpec,
    RunEvidence,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    WorkflowState,
    approved_gates,
    build_graph,
    check_birth,
    shapes_of,
    validate,
)
from tests.unit.workflows.conftest import ApproveAll, CallLog, Ledger, ReportDraft, ReportUrl, register_quarterly_steps


async def _born(spec: Workflow, registry: StepRegistry) -> RunEvidence:
    """Run it for real, and record what the run produced -- the birth evidence."""
    state = WorkflowState(params={"quarter": "Q2"})
    graph = build_graph(spec, registry, approval=ApproveAll())
    result = await graph.run(state=state)
    assert isinstance(result, ReportUrl)
    return RunEvidence(
        workflow_id="wf.report",
        outcome="completed",
        params={"quarter": "Q2"},
        produced=shapes_of(state.values),
        gates_approved=approved_gates(spec, state.values),
    )


def _registry(call_log: CallLog) -> StepRegistry:
    reg = StepRegistry()
    register_quarterly_steps(reg, call_log)
    return reg


async def test_an_unchanged_world_passes_the_free_check(quarterly_report: Workflow, call_log: CallLog) -> None:
    born = await _born(quarterly_report, _registry(call_log))
    assert born.produced["reconcile"] == "Ledger"
    assert check_birth(quarterly_report, _registry(call_log), born) == ()


async def test_a_step_that_now_returns_a_different_type_is_named(quarterly_report: Workflow, call_log: CallLog) -> None:
    """The terminal step on purpose: nothing consumes it, so the spec still validates.

    That isolates what is under test. Re-typing a mid-graph step breaks the wiring and
    the validator refuses it first -- correctly, but then this check proves nothing.
    """
    born = await _born(quarterly_report, _registry(call_log))

    changed = StepRegistry()
    register_quarterly_steps(changed, call_log, exclude=frozenset({"publish"}))

    async def publish(report: ReportDraft) -> str:  # was ReportUrl
        del report
        return "https://reports.example/9"

    changed.register(publish)
    findings = check_birth(quarterly_report, changed, born)
    assert any("publish" in finding and "ReportUrl" in finding for finding in findings)


async def test_a_node_the_run_produced_that_the_spec_no_longer_has_is_named(quarterly_report: Workflow, call_log: CallLog) -> None:
    born = await _born(quarterly_report, _registry(call_log))
    trimmed = quarterly_report.model_copy(update={"nodes": tuple(n for n in quarterly_report.nodes if n.id != "fetch_costs")})
    findings = check_birth(trimmed, _registry(call_log), born)
    assert any("fetch_costs" in finding for finding in findings)


async def test_a_spec_that_no_longer_validates_is_refused_not_crashed(quarterly_report: Workflow, call_log: CallLog) -> None:
    born = await _born(quarterly_report, _registry(call_log))
    thin = StepRegistry()
    register_quarterly_steps(thin, call_log, exclude=frozenset({"publish"}))
    findings = check_birth(quarterly_report, thin, born)
    assert findings, "an unvalidatable spec cannot pass its own birth check"
    assert any("publish" in finding for finding in findings)


async def test_a_replay_that_reproduces_the_shape_reports_nothing(quarterly_report: Workflow, call_log: CallLog) -> None:
    born = await _born(quarterly_report, _registry(call_log))
    findings = await replay_birth(quarterly_report, _registry(call_log), born, approval=ApproveAll())
    assert findings == (), "the same environment must reproduce the same shape"


async def test_a_replay_whose_shape_moved_is_reported(quarterly_report: Workflow, call_log: CallLog) -> None:
    """The expensive check catches exactly what the free one cannot: a step that declares one type and returns another.

    Nothing static can see this; only running it can.
    """
    born = await _born(quarterly_report, _registry(call_log))

    liar = StepRegistry()
    register_quarterly_steps(liar, call_log, exclude=frozenset({"publish"}))

    async def publish(report: ReportDraft) -> ReportUrl:
        del report
        return Ledger(net=0)  # type: ignore[return-value]  # declares ReportUrl, returns otherwise

    liar.register(publish)
    assert check_birth(quarterly_report, liar, born) == (), "the free check cannot see a lie about behaviour"
    findings = await replay_birth(quarterly_report, liar, born, approval=ApproveAll())
    assert any("publish" in finding and "Ledger" in finding for finding in findings)


async def test_a_replay_of_a_run_with_no_recorded_inputs_says_so(quarterly_report: Workflow, call_log: CallLog) -> None:
    bare = RunEvidence(workflow_id="wf.report", outcome="completed")
    findings = await replay_birth(quarterly_report, _registry(call_log), bare, approval=ApproveAll())
    assert any("has no inputs" in finding for finding in findings)


async def test_a_step_that_returns_a_typed_list_is_not_reported_as_drifted(call_log: CallLog) -> None:
    """A parameterised return type has to be spelled the same way on both sides of the check.

    Found on a live artifact minutes after a successful run: the view reported
    `node 'risks' produced list[Risk] at birth; the step behind it now declares list`, with
    nothing whatsoever changed in between. `shapes_of` reads a runtime list and records its
    element type; `_declared_shape` read `__name__` off the annotation, and `list[Risk].__name__`
    is `'list'`. So the two halves of the comparison spoke different languages, and every node
    whose step returns a typed collection failed its own birth check for ever.

    A check that cries wolf on healthy artifacts is worse than no check: the first thing anyone
    learns from it is to stop reading it.
    """
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log)

    async def rank_risks(ledger: Ledger) -> list[ReportDraft]:
        """A step that returns a typed collection, which is the ordinary case for a map source."""
        del ledger
        return [ReportDraft(text="one")]

    registry.register(rank_risks)
    spec = Workflow(
        name="typed-list",
        version=1,
        description="one node whose step returns a typed list",
        parameters={"quarter": ParameterSpec(description="quarter", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
            RunNode(id="fetch_costs", work="fetch_costs", inputs={"quarter": FromParameter(parameter="quarter")}),
            RunNode(
                id="reconcile", work="reconcile", inputs={"sales": FromNode(node="fetch_sales"), "costs": FromNode(node="fetch_costs")}
            ),
            RunNode(id="risks", work="rank_risks", inputs={"ledger": FromNode(node="reconcile")}),
        ),
        output=FromNode(node="risks"),
    )
    assert validate(spec, registry).ok, validate(spec, registry).issues

    state = WorkflowState(params={"quarter": "Q1"})
    await build_graph(spec, registry, approval=ApproveAll()).run(state=state)
    born = RunEvidence(workflow_id="wf.typed-list", outcome="completed", params={"quarter": "Q1"}, produced=shapes_of(state.values))

    assert born.produced["risks"] == "list[ReportDraft]", "the run really did record the element type"
    assert check_birth(spec, registry, born) == (), "and nothing has changed, so nothing has drifted"


def _chained_list_spec_and_registry(call_log: CallLog) -> tuple[Workflow, StepRegistry, Briefing, Briefing]:
    """A two-turn map (`flag_risk` then `draft_from_risk`), both turns `Briefing`s the spec brings.

    A multi-turn map runs briefings only, so both turns are `Briefing`s the spec itself brings,
    same as `test_validator.py` and `test_work_executor.py` brief one.
    """
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log)

    async def rank_risks(ledger: Ledger) -> list[str]:
        del ledger
        return ["one"]

    registry.register(rank_risks)

    async def perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        del task
        if "risk" in inputs:
            return TypeAdapter(output_type).validate_python(inputs["risk"])
        return TypeAdapter(output_type).validate_python({"text": inputs["flag_risk"]})

    registry.perform = perform

    flag_risk = Briefing(
        name="flag_risk",
        instructions="Flag the given risk, exactly as briefed here, with no other changes to it.",
        inputs={"risk": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema={"type": "string"}),
    )
    draft_from_risk = Briefing(
        name="draft_from_risk",
        instructions="Draft a report from the flagged risk, exactly as briefed here.",
        inputs={"flag_risk": Shape(json_schema={"type": "string"})},
        output=Shape(
            json_schema={"type": "object", "title": "ReportDraft", "properties": {"text": {"type": "string"}}, "required": ["text"]}
        ),
    )
    spec = Workflow(
        name="chained-list",
        version=1,
        description="rank risks, flag each one, then draft each flagged risk -- two turns per item",
        parameters={"quarter": ParameterSpec(description="quarter", json_schema={"type": "string"})},
        inline=(flag_risk, draft_from_risk),
        nodes=(
            RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
            RunNode(id="fetch_costs", work="fetch_costs", inputs={"quarter": FromParameter(parameter="quarter")}),
            RunNode(
                id="reconcile", work="reconcile", inputs={"sales": FromNode(node="fetch_sales"), "costs": FromNode(node="fetch_costs")}
            ),
            RunNode(id="risks", work="rank_risks", inputs={"ledger": FromNode(node="reconcile")}),
            MapNode(id="drafted", turns=("flag_risk", "draft_from_risk"), over=FromNode(node="risks"), each="risk"),
        ),
        output=FromNode(node="drafted"),
    )
    return spec, registry, flag_risk, draft_from_risk


async def _born_chained_list(spec: Workflow, registry: StepRegistry) -> RunEvidence:
    state = WorkflowState(params={"quarter": "Q1"})
    await build_graph(spec, registry, approval=ApproveAll()).run(state=state)
    return RunEvidence(workflow_id="wf.chained-list", outcome="completed", params={"quarter": "Q1"}, produced=shapes_of(state.values))


async def test_a_judge_gates_pass_through_is_not_compared_against_its_judging_steps_verdict(call_log: CallLog) -> None:
    """A judge gate records the value it passed through, so its birth shape is its source's, never its verdict's.

    The gate's `work` is the step that judges, and that step returns a verdict (`bool` here). The
    run stores the reviewed draft under the gate's id, not the verdict, so a check that read the
    judging step's declared type would report drift on every healthy judged artifact.
    """
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log)

    async def weigh(report: ReportDraft) -> bool:
        del report
        return True

    registry.register(weigh)
    spec = Workflow(
        name="judged",
        version=1,
        description="a judge gate between the draft and its publication",
        parameters={"quarter": ParameterSpec(description="quarter", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
            RunNode(id="fetch_costs", work="fetch_costs", inputs={"quarter": FromParameter(parameter="quarter")}),
            RunNode(
                id="reconcile", work="reconcile", inputs={"sales": FromNode(node="fetch_sales"), "costs": FromNode(node="fetch_costs")}
            ),
            RunNode(id="draft_report", work="draft_report", inputs={"ledger": FromNode(node="reconcile")}),
            JudgeGate(id="weighed", work="weigh", reviews=FromNode(node="draft_report"), rejected_to="draft_report"),
            RunNode(id="publish", work="publish", inputs={"report": FromNode(node="weighed")}),
        ),
        output=FromNode(node="publish"),
    )
    assert validate(spec, registry).ok, validate(spec, registry).issues

    state = WorkflowState(params={"quarter": "Q1"})
    await build_graph(spec, registry, approval=ApproveAll()).run(state=state)
    born = RunEvidence(workflow_id="wf.judged", outcome="completed", params={"quarter": "Q1"}, produced=shapes_of(state.values))

    assert born.produced["weighed"] == "ReportDraft", "the gate recorded the draft it passed through, not its verdict"
    assert check_birth(spec, registry, born) == ()


async def test_a_chained_maps_collected_shape_is_the_last_turns_output(call_log: CallLog) -> None:
    """A multi-turn map collects what its LAST turn produces, not its first -- birth must record that.

    Without the fix, the free check would declare `str` (`flag_risk`'s own output) while the run
    actually recorded `list[ReportDraft]` (what the second turn really returned), and a healthy
    artifact would fail its own birth check the moment it was born -- the exact "cries wolf" bug
    the typed-list test above pins for a plain map.
    """
    spec, registry, flag_risk, draft_from_risk = _chained_list_spec_and_registry(call_log)
    assert validate(spec, registry).ok, validate(spec, registry).issues

    born = await _born_chained_list(spec, registry)

    assert born.produced["drafted"] == "list[ReportDraft]", "the run really did collect the last turn's output"
    assert check_birth(spec, registry, born) == (), "and nothing has changed, so nothing has drifted"

    draft_from_risk_v2 = draft_from_risk.model_copy(update={"output": Shape(json_schema={"type": "string"})})  # was ReportDraft
    changed = spec.model_copy(update={"inline": (flag_risk, draft_from_risk_v2)})
    findings = check_birth(changed, registry, born)
    assert any("drafted" in finding and "ReportDraft" in finding for finding in findings)


async def test_a_multi_turn_maps_per_turn_keys_are_recognised_not_reported_as_unknown_nodes(call_log: CallLog) -> None:
    """Each turn's own collected list -- `values[f"{node.id}.{work}"]`, `_compiler.py`'s `_gather` -- is expected state.

    Without the fix, `check_birth` would see `drafted.flag_risk` and `drafted.draft_from_risk`,
    find no node of either id in the spec, and report both as "not in this spec any more" on
    every healthy multi-turn artifact's very first birth check.
    """
    spec, registry, *_ = _chained_list_spec_and_registry(call_log)
    assert validate(spec, registry).ok, validate(spec, registry).issues

    born = await _born_chained_list(spec, registry)

    assert set(born.produced) >= {"drafted", "drafted.flag_risk", "drafted.draft_from_risk"}, "both turns' own lists are observable state"
    assert born.produced["drafted.flag_risk"] == "list[str]"
    assert born.produced["drafted.draft_from_risk"] == "list[ReportDraft]"
    assert check_birth(spec, registry, born) == (), "the map's own key and both turn keys are all expected -- nothing has drifted"


async def test_a_changed_turns_own_shape_is_named_against_its_dotted_key() -> None:
    """A turn's own collected shape moving is compared against ITS output, reported against its own dotted key.

    The map's own key tracks its LAST turn only, so a non-last turn's shape drifting is
    something ONLY its dotted key can catch. Both turns read the mapped item directly rather
    than one feeding the other, so changing one turn's declared output cannot itself break the
    wiring -- the point here is the shape comparison, not the wiring.
    """

    async def rank_risks(quarter: str) -> list[str]:
        return [quarter]

    async def perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        del task, inputs
        return TypeAdapter(output_type).validate_python("flagged")

    registry = StepRegistry(perform=perform)
    registry.register(rank_risks)

    flag_risk = Briefing(
        name="flag_risk",
        instructions="Flag the given risk, exactly as briefed here, with no other changes to it.",
        inputs={"risk": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema={"type": "string"}),
    )
    settle_risk = Briefing(
        name="settle_risk",
        instructions="Settle the given risk, exactly as briefed here, independent of any other turn.",
        inputs={"risk": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema={"type": "string"}),
    )
    spec = Workflow(
        name="two-independent-turns",
        version=1,
        description="rank risks, flag and settle each one -- two independent turns per item",
        parameters={"quarter": ParameterSpec(description="quarter", json_schema={"type": "string"})},
        inline=(flag_risk, settle_risk),
        nodes=(
            RunNode(id="risks", work="rank_risks", inputs={"quarter": FromParameter(parameter="quarter")}),
            MapNode(id="drafted", turns=("flag_risk", "settle_risk"), over=FromNode(node="risks"), each="risk"),
        ),
        output=FromNode(node="drafted"),
    )
    assert validate(spec, registry).ok, validate(spec, registry).issues

    state = WorkflowState(params={"quarter": "Q1"})
    await build_graph(spec, registry, approval=ApproveAll()).run(state=state)
    born = RunEvidence(workflow_id="wf.two-independent-turns", outcome="completed", params={"quarter": "Q1"}, produced=shapes_of(state.values))

    assert born.produced["drafted.flag_risk"] == "list[str]"

    flag_risk_v2 = flag_risk.model_copy(  # was a bare string
        update={
            "output": Shape(
                json_schema={"type": "object", "title": "Flagged", "properties": {"flag": {"type": "string"}}, "required": ["flag"]}
            )
        }
    )
    changed = spec.model_copy(update={"inline": (flag_risk_v2, settle_risk)})
    assert validate(changed, registry).ok, validate(changed, registry).issues

    findings = check_birth(changed, registry, born)
    assert any("drafted.flag_risk" in finding and "now declares list[Flagged]" in finding for finding in findings), findings
