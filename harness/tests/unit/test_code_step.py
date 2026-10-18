"""A step an artifact brings as a program: built, typed, refused, and run without a model.

The claim under test is a cost claim. A retrieval pipeline is mostly deterministic glue --
dedupe by id, fuse two ranked lists, filter, truncate, reshape -- and an artifact that can only
bring a briefing pays a model call for every one of those, on every run, forever. A code step
makes that glue free and repeatable.

The counter-claim gets as much room: a program is not a way out of the capability rule. It
reaches only the tools its `tools` grant names and only names the host offered, it cannot take a
registered step's name, and a run with no sandbox refuses it rather than reaching for `exec`.

`test_retrieval_glue_runs_with_no_model_anywhere` is the whole argument in one test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import (
    Briefing,
    FromNode,
    FromParameter,
    ParameterSpec,
    Program,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    prepare,
    run_workflow,
    validate,
)
from core.ai.workflows.domain.validator import IssueType
from core.ai.workflows.domain.vocabulary import SameAs
from pydantic import BaseModel, ValidationError
from pydantic_monty import AsyncMonty

from harness.adapters.environment import code_runner

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from core.ai.workflows.contracts import CodeRunner

BRIEFING = "Read the ledger and say, in one line, whether the quarter looks healthy and why it looks that way."

RANKED = {
    "type": "object",
    "title": "Ranked",
    "properties": {
        "ranked": {
            "type": "array",
            "items": {"type": "object", "properties": {"id": {"type": "string"}, "score": {"type": "number"}}, "required": ["id", "score"]},
        }
    },
    "required": ["ranked"],
}

HITS = {"type": "object", "title": "Hits", "properties": {"hits": {"type": "array", "items": {"type": "string"}}}, "required": ["hits"]}

FUSE = """
def fuse(runs, k):
    scores = {}
    for run in runs:
        for rank, doc_id in enumerate(run):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return scores

scores = fuse([dense['hits'], lexical['hits']], 60)
order = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
{'ranked': [{'id': doc_id, 'score': round(score, 6)} for doc_id, score in order]}
"""
"""Reciprocal-rank fusion over two ranked id lists, deduping by id. Real retrieval glue, and
every line of it is arithmetic a model would otherwise be paid to imitate."""


@pytest.fixture
async def sandbox() -> AsyncIterator[AsyncMonty]:
    """A fresh worker pool for each test, entered on the test's own event loop."""
    async with AsyncMonty(request_timeout=30.0) as pool:
        yield pool


@pytest.fixture
def runs_code(sandbox: AsyncMonty) -> CodeRunner:
    """The app's own runner, over a sandbox that reaches no tool at all."""
    return code_runner(sandbox, reads=(), call=_no_tools_here)


async def _no_tools_here(name: str, args: dict[str, object]) -> object:
    del args
    raise AssertionError(name)


class NoGatesHere:
    """These graphs carry no gate, so being asked to review one is the failure."""

    async def review(self, gate_id: str, value: object, *, note: str = "") -> bool:  # pragma: no cover - never reached
        del value, note
        raise AssertionError(gate_id)


def _kit(runs_code: CodeRunner | None = None, *, tools: tuple[str, ...] = (), perform: object = None) -> StepRegistry:
    """A registry on this run's terms: what it can execute, and what a brought step may hold."""
    return StepRegistry(perform=perform, grants=tools, run_code=runs_code)  # type: ignore[arg-type]


def _fusing(source: str = FUSE, *, tools: tuple[str, ...] = ()) -> Workflow:
    """One code step, fed by two parameters, and nothing else in the graph."""
    return Workflow(
        name="fuse-two-runs",
        version=1,
        description="Merge a dense and a lexical ranking with RRF, deduping by id.",
        parameters={
            "dense": ParameterSpec(description="the vector ranking", json_schema=HITS),
            "lexical": ParameterSpec(description="the keyword ranking", json_schema=HITS),
        },
        inline=(
            Program(
                name="fuse_rankings",
                source=source,
                inputs={"dense": Shape(json_schema=HITS), "lexical": Shape(json_schema=HITS)},
                output=Shape(json_schema=RANKED),
                tools=tools,
            ),
        ),
        nodes=(
            RunNode(
                id="fuse",
                work="fuse_rankings",
                inputs={"dense": FromParameter(parameter="dense"), "lexical": FromParameter(parameter="lexical")},
            ),
        ),
        output=FromNode(node="fuse"),
    )


def _refusals(spec: Workflow, registry: StepRegistry) -> list[str]:
    return [issue.detail for issue in validate(spec, registry).issues if issue.kind is IssueType.BAD_TASK_DEFINITION]


# --- the grammar ------------------------------------------------------------------------


def test_a_program_and_a_briefing_are_siblings_under_one_key() -> None:
    """`tasks` carries both kinds, so nothing downstream has to ask which list a step came from."""
    spec = Workflow(
        name="both-kinds",
        version=1,
        description="one step the artifact wrote as prose, one it wrote as a program",
        inline=(
            Briefing(name="weigh", instructions=BRIEFING, inputs={"hits": Shape(json_schema=HITS)}, output=Shape(json_schema=RANKED)),
            Program(name="fuse_rankings", source=FUSE, inputs={"hits": Shape(json_schema=HITS)}, output=Shape(json_schema=RANKED)),
        ),
        nodes=(RunNode(id="fuse", work="fuse_rankings", inputs={"hits": FromParameter(parameter="hits")}),),
        parameters={"hits": ParameterSpec(description="a ranking", json_schema=HITS)},
        output=FromNode(node="fuse"),
    )
    assert [type(step) for step in spec.inline] == [Briefing, Program]


def test_a_step_that_is_neither_prose_nor_a_program_earns_one_error() -> None:
    """The union idiom's whole point: one mistake, one sentence naming both branches."""
    with pytest.raises(ValidationError) as caught:
        Workflow(
            name="neither",
            version=1,
            description="a step carrying no briefing and no source",
            inline=({"name": "mystery", "output": {"json_schema": HITS}},),  # type: ignore[arg-type]
            nodes=(RunNode(id="mystery", work="mystery"),),
            output=FromNode(node="mystery"),
        )
    errors = caught.value.errors()
    assert len(errors) == 1, errors
    assert errors[0]["type"] == "step_not_recognised"
    assert "instructions" in errors[0]["msg"]
    assert "source" in errors[0]["msg"]


def test_a_program_may_not_carry_a_briefing() -> None:
    """A code step has no `instructions`: the two ways to bring a step do not blend."""
    with pytest.raises(ValidationError, match="instructions"):
        Program.model_validate({"name": "fuse_rankings", "source": FUSE, "instructions": BRIEFING, "output": {"json_schema": RANKED}})


def test_a_briefing_may_not_carry_a_program() -> None:
    """And the mirror: a task step has no `source`."""
    with pytest.raises(ValidationError, match="source"):
        Briefing.model_validate({"name": "weigh", "instructions": BRIEFING, "source": FUSE, "output": {"json_schema": RANKED}})


# --- the host supplies the capability ---------------------------------------------------


def test_a_run_with_no_sandbox_refuses_a_program_rather_than_guessing() -> None:
    """`run_code=None` is the code half of `perform=None`: secure by default, and it says so.

    The refusal has to be a *finding*, not a crash and not a silent skip -- an artifact that
    cannot run here should be told why while nothing has happened yet.
    """
    report = validate(_fusing(), _kit(None))
    assert not report.ok
    kinds = {issue.kind for issue in report.issues}
    assert IssueType.BAD_TASK_DEFINITION in kinds
    assert IssueType.UNKNOWN_WORK in kinds  # and the node naming it reads as the explanation
    assert any("cannot execute a program" in issue.detail for issue in report.issues)


def test_a_program_may_only_name_tools_the_host_offered(runs_code: CodeRunner) -> None:
    """The same check a briefing passes, and one finding for one cause.

    An artifact may invent a computation. It may not invent a database, a credential, or a tool.
    """
    refusals = _refusals(_fusing(tools=("search_text", "rm_rf")), _kit(runs_code, tools=("search_text",)))
    assert len(refusals) == 1
    assert "rm_rf" in refusals[0]
    assert "offered: search_text" in refusals[0]


def test_a_program_may_not_take_a_registered_name(runs_code: CodeRunner, registry: StepRegistry) -> None:
    """A stored artifact must not be able to redefine `publish` by shipping a program called that."""
    kit = _kit(runs_code)
    for name in registry.names:
        if (impl := registry.get(name)) is not None:
            kit.register(impl.function, name=name)
    spec = _fusing().model_copy(
        update={
            "inline": (Program(name="publish", source=FUSE, inputs={"dense": Shape(json_schema=HITS)}, output=Shape(json_schema=RANKED)),)
        }
    )
    assert any("never replace one" in refusal for refusal in _refusals(spec, kit))


async def test_an_ungranted_name_is_refused_inside_the_sandbox(runs_code: CodeRunner) -> None:
    """The last line of the capability rule, and the only one a bad artifact actually meets.

    The cold check refuses a grant the host never offered, but a program can also simply *call*
    something it never asked for. Only the names in its own grant exist inside the sandbox, so the
    call raises there -- the host is never asked, and there is nothing to filter after the fact.
    """
    spec = _fusing("hits = search_text(query='anything')\n{'ranked': []}")
    with pytest.raises(RuntimeError, match="failed in the sandbox") as caught:
        await run_workflow(spec, _kit(runs_code), params={"dense": {"hits": []}, "lexical": {"hits": []}}, approval=NoGatesHere())
    assert "search_text" in str(caught.value)


async def test_a_program_gets_no_clock(runs_code: CodeRunner) -> None:
    """A stored program is source this process did not write, so what it returns must not depend on when it ran."""
    spec = _fusing("import datetime\nstamp = datetime.datetime.now()\n{'ranked': []}")
    with pytest.raises(RuntimeError, match="failed in the sandbox"):
        await run_workflow(spec, _kit(runs_code), params={"dense": {"hits": []}, "lexical": {"hits": []}}, approval=NoGatesHere())


async def test_a_grant_this_runner_cannot_serve_is_refused_before_anything_runs(sandbox: AsyncMonty) -> None:
    """A program reaches this run's reads. The shell is a capability only a briefing holds.

    The host offers `run_command` to a brought step, so the cold check passes -- but a program has
    no agent to hold a capability, and a grant that quietly does nothing is worse than a refusal.
    So the runner refuses on sight rather than waiting for the program to call it, or not.
    """
    spec = _fusing(tools=("run_command",))
    kit = _kit(code_runner(sandbox, reads=("search_text",), call=_no_tools_here), tools=("run_command",))
    with pytest.raises(RuntimeError, match="run_command") as caught:
        await run_workflow(spec, kit, params={"dense": {"hits": []}, "lexical": {"hits": []}}, approval=NoGatesHere())
    assert "Make it a briefing, or drop the grant" in str(caught.value)


async def test_a_granted_tool_is_dispatched_by_the_host(sandbox: AsyncMonty) -> None:
    """The other half: a program that *was* granted a read calls it as an ordinary function.

    One `run_code`-shaped round trip per call, and the program keeps computing in between -- which
    is the shape that makes a program worth granting anything at all.
    """
    asked: list[tuple[str, dict[str, object]]] = []

    async def call(name: str, args: dict[str, object], /) -> object:
        asked.append((name, args))
        return {"hits": ["p9", "p8"]}

    spec = _fusing(
        "found = await search_text(query='ranking')\n{'ranked': [{'id': doc, 'score': 1.0} for doc in found['hits']]}",
        tools=("search_text",),
    )
    kit = _kit(code_runner(sandbox, reads=("search_text",), call=call), tools=("search_text",))
    result = await run_workflow(spec, kit, params={"dense": {"hits": []}, "lexical": {"hits": []}}, approval=NoGatesHere())

    assert asked == [("search_text", {"query": "ranking"})]
    assert isinstance(result, BaseModel)
    assert [row["id"] for row in result.model_dump()["ranked"]] == ["p9", "p8"]


# --- refusing what cannot be checked ----------------------------------------------------


def test_source_that_does_not_parse_is_a_cold_finding(runs_code: CodeRunner) -> None:
    """A syntax error belongs beside the wiring findings, not in the middle of a paid run."""
    refusals = _refusals(_fusing("{'ranked': [}"), _kit(runs_code))
    assert any("does not parse as Python" in refusal for refusal in refusals)


def test_a_program_importing_what_the_sandbox_lacks_is_refused_cold(runs_code: CodeRunner) -> None:
    """The sandbox has no third-party imports, so a program naming one can never run here."""
    refusals = _refusals(_fusing("import numpy\n{'ranked': []}"), _kit(runs_code))
    assert any("numpy" in refusal for refusal in refusals)
    assert any("json" in refusal for refusal in refusals), "and the refusal names what IS importable"


def test_a_program_that_ends_in_an_assignment_is_refused_cold(runs_code: CodeRunner) -> None:
    """The trailing expression is the return value; ending in an assignment returns nothing.

    Silent, and expensive: every input is fetched, the glue runs, and the step yields no value.
    """
    refusals = _refusals(_fusing("ranked = {'ranked': []}"), _kit(runs_code))
    assert any("last line" in refusal for refusal in refusals)


def test_a_program_that_ends_with_its_own_functions_name_is_refused_cold(runs_code: CodeRunner) -> None:
    """Wrap the work in a def, end with the def's bare name: an expression, so the old check passed.

    The step's value is then the function object, which fails the output contract only at run
    time -- this exact shape shipped twice in one live artifact's lineage before it was refused
    here. The refusal has to name the fix, not just the fault.
    """
    wrapped = "async def main():\n    return {'ranked': []}\nmain"
    refusals = _refusals(_fusing(wrapped), _kit(runs_code))
    assert any("'main'" in refusal for refusal in refusals), "name the function, so the author sees which line"
    assert any("function" in refusal for refusal in refusals)

    called = "def main():\n    return {'ranked': []}\nmain()"
    assert not _refusals(_fusing(called), _kit(runs_code)), "CALLING the function is fine -- the value is its result"


# --- indistinguishable from any other step ----------------------------------------------


def test_a_program_becomes_an_ordinary_step(runs_code: CodeRunner) -> None:
    """After building, the validator, the indexer and the screen cannot tell it apart."""
    ready = prepare(_fusing(), _kit(runs_code))
    assert ready.report.ok, ready.report.issues
    built = ready.registry.get("fuse_rankings")
    assert built is not None
    assert built.required == frozenset({"dense", "lexical"})
    assert built.output_type.__name__ == "Ranked"
    assert issubclass(built.input_types["dense"], BaseModel)


def test_a_program_describes_itself_with_its_own_docstring(runs_code: CodeRunner) -> None:
    """A program documents itself the ordinary Python way, so the graph has something to show.

    No second grammar field for prose about the code: a description beside a program is a comment
    that can disagree with it, and the module docstring cannot.
    """
    documented = prepare(_fusing(f'"""Fuse two rankings by RRF."""\n{FUSE}'), _kit(runs_code)).registry.get("fuse_rankings")
    assert documented is not None
    assert documented.function.__doc__ == "Fuse two rankings by RRF."

    bare = prepare(_fusing(), _kit(runs_code)).registry.get("fuse_rankings")
    assert bare is not None
    assert bare.function.__doc__ == "Runs the 'fuse_rankings' program this workflow brought."


def test_like_and_many_name_a_program_s_type(runs_code: CodeRunner) -> None:
    """`{like: ...}` reaches across kinds: typing is nominal, so a program's output is a real class."""
    spec = Workflow(
        name="fuse-then-weigh",
        version=1,
        description="a program produces a ranking and a briefing consumes a list of them",
        parameters={"dense": ParameterSpec(description="a ranking", json_schema=HITS)},
        inline=(
            Program(name="fuse_rankings", source=FUSE, inputs={"dense": Shape(json_schema=HITS)}, output=Shape(json_schema=RANKED)),
            Briefing(
                name="weigh", instructions=BRIEFING, inputs={"all": SameAs(like="fuse_rankings", many=True)}, output=Shape(json_schema=HITS)
            ),
        ),
        nodes=(
            RunNode(id="fuse", work="fuse_rankings", inputs={"dense": FromParameter(parameter="dense")}),
            RunNode(id="weigh", work="weigh", inputs={"all": FromNode(node="fuse")}),
        ),
        output=FromNode(node="weigh"),
    )
    ready = prepare(spec, _kit(runs_code, perform=_never_performed))
    fused, weighed = ready.registry.get("fuse_rankings"), ready.registry.get("weigh")
    assert fused is not None
    assert weighed is not None
    assert weighed.input_types["all"] == list[fused.output_type], "the SAME class the program produces"


async def _never_performed(task: Briefing, output_type: type, /, **inputs: object) -> object:  # pragma: no cover - typing only
    del task, output_type, inputs
    raise AssertionError


# --- replay safety, derived ---------------------------------------------------------------


def test_a_program_holding_no_tools_is_replay_safe_by_construction(runs_code: CodeRunner) -> None:
    """The one marker in this system that is computed rather than promised.

    A program granted nothing cannot reach anything outside, so re-executing it is unobservable.
    """
    built = prepare(_fusing(), _kit(runs_code)).registry.get("fuse_rankings")
    assert built is not None
    assert built.replay_safe


def test_a_program_holding_tools_is_not(runs_code: CodeRunner) -> None:
    """A grant is a reach, and a reach re-taken is a second visible effect."""
    built = prepare(_fusing(tools=("search_text",)), _kit(runs_code, tools=("search_text",))).registry.get("fuse_rankings")
    assert built is not None
    assert not built.replay_safe


# --- the argument ---------------------------------------------------------------------------


async def test_retrieval_glue_runs_with_no_model_anywhere(runs_code: CodeRunner) -> None:
    """The feature's whole case: real retrieval glue, indexed and run, with nothing paid to a model.

    Two ranked id lists arrive, reciprocal-rank fusion merges them, and duplicates fuse rather than
    repeat. The registry holds no `perform`, so a briefing could not run even if one were declared,
    and the assertion is on arithmetic rather than on plausible prose.
    """
    spec = _fusing()
    kit = _kit(runs_code)
    report = validate(spec, kit)
    assert report.ok, report.issues
    assert not kit.performs_briefings, "no model is reachable from this run at all"

    result = await run_workflow(
        spec, kit, params={"dense": {"hits": ["p2", "p1", "p3"]}, "lexical": {"hits": ["p1", "p4", "p2"]}}, approval=NoGatesHere()
    )

    assert isinstance(result, BaseModel)
    ranked = result.model_dump()["ranked"]
    assert [row["id"] for row in ranked] == ["p1", "p2", "p4", "p3"], "RRF order, deduped by id"
    assert len(ranked) == 4, "p1 and p2 appear in both runs and fuse into one row each"
    # p1 is second in the dense run and first in the lexical one, so RRF with k=60 fuses
    # 1/(60+2) and 1/(60+1). Arithmetic, checked to the digit the program rounds to -- which is
    # the point of not asking a model to do it.
    assert ranked[0]["score"] == pytest.approx(1 / 62 + 1 / 61, abs=5e-7)
