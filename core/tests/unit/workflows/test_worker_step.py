"""A step an artifact brings as a whole declared agent: pydantic-ai's own AgentSpec, carried verbatim.

The claim under test is a reach claim, the sharpest one any brought step makes. A briefing is
prose and a program is arithmetic, but a worker is a whole agent -- its own instructions, its own
tools, its own capabilities -- and on paper that looks like a way to reach past whatever this host
permits. It is not, and every refusal here says so before a single model request happens: the
document has to validate as the framework's own `AgentSpec`, it may not declare what the host
already derives from the wiring, its instructions may not carry a template this grammar cannot
fill, and its capabilities are granted the same way tools are -- by name, and only the ones this
host actually offers.

`test_a_worker_becomes_an_ordinary_step` is the quiet half of the argument: once built, a worker
is indistinguishable from a briefing or a program to everything downstream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pydantic import BaseModel, JsonValue, ValidationError

from core.ai.workflows.contracts import (
    AgentStep,
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

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.ai.workflows.contracts import AgentResolver
    from core.ai.workflows.internal_contracts import AgentRunner

BRIEFING = "Read the ledger and say whether the quarter looks healthy."

LEDGER = {"type": "object", "title": "LedgerIn", "properties": {"net": {"type": "integer"}}, "required": ["net"]}

VERDICT = {"type": "object", "title": "Verdict", "properties": {"healthy": {"type": "boolean"}}, "required": ["healthy"]}


class NoGatesHere:
    """These graphs carry no gate, so being asked to review one is the failure."""

    async def review(self, gate_id: str, value: object, *, note: str = "") -> bool:  # pragma: no cover - never reached
        del value, note
        raise AssertionError(gate_id)


async def _stub_worker(step: AgentStep, output_type: type, /, **inputs: object) -> object:
    """A canned answer, so what is under test is the seam, never a model."""
    del step, inputs
    return output_type(healthy=True)


def _kit(
    run_agent: AgentRunner | None = None,
    *,
    capability_grants: tuple[str, ...] = (),
    tools: tuple[str, ...] = (),
    resolve_agent: AgentResolver | None = None,
) -> StepRegistry:
    """A registry on this run's terms: what it can build a worker with, and what it may hold."""
    return StepRegistry(run_agent=run_agent, capability_grants=capability_grants, grants=tools, resolve_agent=resolve_agent)


def _weighing(agent: Mapping[str, JsonValue] | str, *, tools: tuple[str, ...] = ()) -> Workflow:
    """One worker step, fed by one parameter, and nothing else in the graph."""
    return Workflow(
        name="weigh-the-quarter",
        version=1,
        description="A worker reads a ledger and says whether the quarter looks healthy.",
        parameters={"ledger": ParameterSpec(description="the ledger", json_schema=LEDGER)},
        inline=(
            AgentStep(
                name="weigh", agent=agent, inputs={"ledger": Shape(json_schema=LEDGER)}, output=Shape(json_schema=VERDICT), tools=tools
            ),
        ),
        nodes=(RunNode(id="weigh", work="weigh", inputs={"ledger": FromParameter(parameter="ledger")}),),
        output=FromNode(node="weigh"),
    )


def _refusals(spec: Workflow, registry: StepRegistry) -> list[str]:
    return [issue.detail for issue in validate(spec, registry).issues if issue.kind is IssueType.BAD_TASK_DEFINITION]


# --- the grammar ------------------------------------------------------------------------


def test_all_three_brought_kinds_are_siblings_under_one_key() -> None:
    """`tasks` carries all three kinds, so nothing downstream has to ask which list a step came from."""
    spec = Workflow(
        name="three-kinds",
        version=1,
        description="one step the artifact wrote as prose, one as a program, one as a whole agent",
        parameters={"ledger": ParameterSpec(description="the ledger", json_schema=LEDGER)},
        inline=(
            Briefing(name="weigh_by_hand", instructions=BRIEFING, inputs={"ledger": Shape(json_schema=LEDGER)}, output=Shape(json_schema=VERDICT)),
            Program(name="pass_through", source="ledger", inputs={"ledger": Shape(json_schema=LEDGER)}, output=Shape(json_schema=LEDGER)),
            AgentStep(name="weigh", agent={"instructions": BRIEFING}, inputs={"ledger": Shape(json_schema=LEDGER)}, output=Shape(json_schema=VERDICT)),
        ),
        nodes=(RunNode(id="weigh", work="weigh", inputs={"ledger": FromParameter(parameter="ledger")}),),
        output=FromNode(node="weigh"),
    )
    assert [type(step) for step in spec.inline] == [Briefing, Program, AgentStep]


def test_a_step_carrying_both_an_agent_and_a_program_earns_the_discriminator_message() -> None:
    """A document naming two of the three keys cannot say what it is -- the union's whole point."""
    with pytest.raises(ValidationError) as caught:
        Workflow(
            name="both",
            version=1,
            description="a step that brought two answers to one question",
            inline=(
                {  # type: ignore[arg-type]
                    "name": "mystery",
                    "agent": {"instructions": BRIEFING},
                    "source": "ledger",
                    "output": {"json_schema": VERDICT},
                },
            ),
            nodes=(RunNode(id="mystery", work="mystery"),),
            output=FromNode(node="mystery"),
        )
    errors = caught.value.errors()
    assert len(errors) == 1, errors
    assert errors[0]["type"] == "step_not_recognised"
    assert "agent" in errors[0]["msg"]
    assert "source" in errors[0]["msg"]


def test_a_worker_may_not_carry_a_briefing_or_a_program() -> None:
    """A worker step has no `instructions` and no `source`: the three ways to bring a step do not blend."""
    with pytest.raises(ValidationError, match="instructions"):
        AgentStep.model_validate({"name": "weigh", "agent": {"instructions": BRIEFING}, "instructions": BRIEFING, "output": {"json_schema": VERDICT}})


# --- the host supplies the capability ---------------------------------------------------


def test_a_worker_validates_clean_against_a_registry_with_a_stub_runner() -> None:
    """The base case: a minimal agent document, and a host that can build workers."""
    report = validate(_weighing({"instructions": BRIEFING}), _kit(_stub_worker))
    assert report.ok, report.issues


def test_a_run_with_no_agent_runner_refuses_a_worker_rather_than_guessing() -> None:
    """`run_agent=None` is the worker half of `run_code=None`: secure by default, and it says so."""
    report = validate(_weighing({"instructions": BRIEFING}), _kit(None))
    assert not report.ok
    kinds = {issue.kind for issue in report.issues}
    assert IssueType.BAD_TASK_DEFINITION in kinds
    assert IssueType.UNKNOWN_WORK in kinds  # and the node naming it reads as the explanation
    assert any("cannot run a worker" in issue.detail for issue in report.issues)


def test_a_worker_may_only_name_tools_the_host_offered() -> None:
    """The same check a briefing and a program pass, and one finding for one cause."""
    refusals = _refusals(_weighing({"instructions": BRIEFING}, tools=("search_text", "rm_rf")), _kit(_stub_worker, tools=("search_text",)))
    assert len(refusals) == 1
    assert "rm_rf" in refusals[0]
    assert "offered: search_text" in refusals[0]


# --- refusing what cannot be checked ------------------------------------------------------


def test_a_document_that_does_not_validate_as_an_agentspec_is_refused_cold() -> None:
    """A document that fails the framework's own model is refused with the framework's own error."""
    refusals = _refusals(_weighing({"instructions": BRIEFING, "name": 123}), _kit(_stub_worker))
    assert len(refusals) == 1
    assert "does not validate" in refusals[0]


def test_a_declared_output_schema_is_refused_because_the_host_derives_it() -> None:
    """The host derives `output_schema` from the step's own `output` slot; a document declaring one too can only disagree with it."""
    refusals = _refusals(_weighing({"instructions": BRIEFING, "output_schema": {"type": "object"}}), _kit(_stub_worker))
    assert len(refusals) == 1
    assert "output_schema" in refusals[0]
    assert "derived by the host" in refusals[0]


def test_a_declared_deps_schema_is_refused_the_same_way() -> None:
    """The mirror check: `deps_schema` is the other key the host would derive, and does not yet."""
    refusals = _refusals(_weighing({"instructions": BRIEFING, "deps_schema": {"type": "object"}}), _kit(_stub_worker))
    assert len(refusals) == 1
    assert "deps_schema" in refusals[0]
    assert "derived by the host" in refusals[0]


def test_declared_model_settings_are_refused_because_the_host_retains_the_request() -> None:
    """The document's `model_settings` would ride this host's authenticated call -- refused cold.

    Upstream `Agent.from_spec` merges the document's settings UNDER the host's, and a host passing
    none lets the document's win in full: `extra_headers` and `extra_body` go straight onto the
    HTTP request the host's credentials sign. The model was already the host's, always; the
    request's settings are too.
    """
    settings = {"instructions": BRIEFING, "model_settings": {"extra_headers": {"x-anything": "authors-choice"}}}
    refusals = _refusals(_weighing(settings), _kit(_stub_worker))
    assert len(refusals) == 1
    assert "model_settings" in refusals[0]
    assert "host" in refusals[0]


def test_the_other_host_retained_run_knobs_are_refused_by_name() -> None:
    """`retries`, `tool_timeout`, `end_strategy`, `metadata`: each is the host's run to configure."""
    cases: tuple[tuple[str, JsonValue], ...] = (
        ("retries", 99),
        ("tool_timeout", 3600),
        ("end_strategy", "exhaustive"),
        ("metadata", {"who": "author"}),
    )
    for key, value in cases:
        refusals = _refusals(_weighing({"instructions": BRIEFING, key: value}), _kit(_stub_worker))
        assert len(refusals) == 1, (key, refusals)
        assert key in refusals[0]


def test_templated_instructions_are_refused_because_no_deps_schema_is_derived_yet() -> None:
    """A `{{...}}` renders against `deps`, and this grammar does not yet derive one for a worker."""
    refusals = _refusals(_weighing({"instructions": "Hello {{name}}, read the ledger and say whether the quarter looks healthy."}), _kit(_stub_worker))
    assert len(refusals) == 1
    assert "templated instructions" in refusals[0]


def test_a_declared_capability_the_host_has_not_granted_is_refused() -> None:
    """Capabilities are granted the same way tools are -- by name, and only what this host offers."""
    refusals = _refusals(_weighing({"instructions": BRIEFING, "capabilities": ["shell"]}), _kit(_stub_worker))
    assert len(refusals) == 1
    assert "shell" in refusals[0]
    assert "not granted" in refusals[0]


def test_a_declared_capability_the_host_grants_is_accepted() -> None:
    """The other half: a capability this host actually offers builds clean, exactly like a tool."""
    report = validate(_weighing({"instructions": BRIEFING, "capabilities": ["shell"]}), _kit(_stub_worker, capability_grants=("shell",)))
    assert report.ok, report.issues


# --- a string names an agent registered elsewhere ------------------------------------------


def _resolver(document: Mapping[str, JsonValue], *, known_as: str = "weigher") -> AgentResolver:
    """A fake `AgentResolver`: answers `document` for `known_as`, refuses every other name."""

    def resolve(name: str) -> Mapping[str, JsonValue]:
        if name != known_as:
            msg = f"no authored agent named {name!r}"
            raise LookupError(msg)
        return document

    return resolve


def test_a_worker_agent_may_be_a_stored_name_that_resolves() -> None:
    """`agent` as a plain string is a reference, resolved before the same checks an inline document meets."""
    report = validate(_weighing("weigher"), _kit(_stub_worker, resolve_agent=_resolver({"instructions": BRIEFING})))
    assert report.ok, report.issues


def test_a_stored_reference_with_no_resolver_wired_is_refused_cold() -> None:
    """A host that never wired a registry refuses a reference by name, the same way it refuses a worker with no runner."""
    refusals = _refusals(_weighing("weigher"), _kit(_stub_worker))
    assert len(refusals) == 1
    assert "weigher" in refusals[0]
    assert "no agent registry is wired here" in refusals[0]


def test_a_stored_reference_that_does_not_resolve_is_refused_with_the_resolvers_own_message() -> None:
    """The resolver's `LookupError` is the refusal -- this grammar invents no second message for it."""
    refusals = _refusals(_weighing("nobody"), _kit(_stub_worker, resolve_agent=_resolver({"instructions": BRIEFING})))
    assert refusals == ["no authored agent named 'nobody'"]


def test_a_resolved_document_is_checked_exactly_like_an_inline_one() -> None:
    """Resolution happens first, then every cold check an inline document meets runs against the result."""
    resolver = _resolver({"instructions": BRIEFING, "output_schema": {"type": "object"}})
    refusals = _refusals(_weighing("weigher"), _kit(_stub_worker, resolve_agent=resolver))
    assert len(refusals) == 1
    assert "output_schema" in refusals[0]
    assert "derived by the host" in refusals[0]


def test_a_resolved_documents_capability_is_still_granted_by_name() -> None:
    """The capability-grant boundary applies to a resolved document exactly as it does to an inline one."""
    resolver = _resolver({"instructions": BRIEFING, "capabilities": ["shell"]})
    refusals = _refusals(_weighing("weigher"), _kit(_stub_worker, resolve_agent=resolver))
    assert len(refusals) == 1
    assert "shell" in refusals[0]
    assert "not granted" in refusals[0]


def test_a_stored_name_and_its_equivalent_inline_document_behave_identically() -> None:
    """A reference is a proposal about where the document lives, never about what it may do."""
    resolver = _resolver({"instructions": BRIEFING})
    by_name = prepare(_weighing("weigher"), _kit(_stub_worker, resolve_agent=resolver)).registry.get("weigh")
    inline = prepare(_weighing({"instructions": BRIEFING}), _kit(_stub_worker)).registry.get("weigh")
    assert by_name is not None
    assert inline is not None
    assert by_name.function.__doc__ == inline.function.__doc__
    assert by_name.required == inline.required
    assert by_name.output_type.__name__ == inline.output_type.__name__


async def test_running_a_stored_reference_calls_the_registered_runner_like_any_other_worker() -> None:
    """The whole point of a reference: once resolved and built, it runs exactly like an inline document."""
    spec = _weighing("weigher")
    result = await run_workflow(
        spec, _kit(_stub_worker, resolve_agent=_resolver({"instructions": BRIEFING})), params={"ledger": {"net": 400}}, approval=NoGatesHere()
    )
    assert isinstance(result, BaseModel)
    assert result.model_dump() == {"healthy": True}


# --- indistinguishable from any other step ------------------------------------------------


def test_a_worker_becomes_an_ordinary_step() -> None:
    """After building, the validator, the compiler and the screen cannot tell it apart."""
    ready = prepare(_weighing({"instructions": BRIEFING}), _kit(_stub_worker))
    assert ready.report.ok, ready.report.issues
    built = ready.registry.get("weigh")
    assert built is not None
    assert built.required == frozenset({"ledger"})
    assert built.output_type.__name__ == "Verdict"
    assert not built.replay_safe, "a worker calls a model -- nothing here can promise that call is unobservable to repeat"


def test_a_worker_describes_itself_from_the_agent_documents_own_description() -> None:
    """No second grammar field for prose about the agent: the document's own `description` is it."""
    described = prepare(_weighing({"instructions": BRIEFING, "description": "Weighs a quarter's health."}), _kit(_stub_worker)).registry.get("weigh")
    assert described is not None
    assert described.function.__doc__ == "Weighs a quarter's health."

    bare = prepare(_weighing({"instructions": BRIEFING}), _kit(_stub_worker)).registry.get("weigh")
    assert bare is not None
    assert bare.function.__doc__ == "Runs the 'weigh' agent this workflow brought."


# --- the argument -----------------------------------------------------------------------------


async def test_running_a_worker_calls_the_registered_runner_and_validates_its_answer() -> None:
    """The built step closes over `registry.run_agent`, and a run drives it like any other step."""
    spec = _weighing({"instructions": BRIEFING})
    result = await run_workflow(spec, _kit(_stub_worker), params={"ledger": {"net": 400}}, approval=NoGatesHere())

    assert isinstance(result, BaseModel)
    assert result.model_dump() == {"healthy": True}
