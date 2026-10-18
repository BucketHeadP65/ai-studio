"""A custom step, once activated, is a real step: wireable, callable, and fingerprinted.

`write_custom_step`'s whole point is growing the vocabulary a workflow spec can be wired from --
this proves the closure end to end: pending, then activated, the step's function is loaded into
a `StepRegistry`, a spec naming it passes `verify_savable`, the function actually runs, and its
contract is stamped into `fingerprint(...).steps`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.workflows.contracts import FromNode, FromParameter, ParameterSpec, RunNode, StepRegistry, Workflow, fingerprint, verify_savable

from harness.features.selfauthoring.infrastructure.steps import FileCustomSteps
from tests.support.selfauthoring import FakeActivationLog

if TYPE_CHECKING:
    from pathlib import Path

_DOUBLE_SOURCE = """
async def double(x: int) -> int:
    '''Doubles x.'''
    return x * 2
"""


def _spec_naming(work: str) -> Workflow:
    return Workflow(
        name="double-x",
        version=1,
        description="Doubles x.",
        parameters={"x": ParameterSpec(description="Input value", json_schema={"type": "integer"})},
        nodes=(RunNode(id="double", work=work, inputs={"x": FromParameter(parameter="x")}),),
        output=FromNode(node="double"),
    )


async def test_an_authored_step_pending_then_activated_becomes_callable_from_a_savable_workflow(tmp_path: Path) -> None:
    store = FileCustomSteps(tmp_path / "steps", activations=FakeActivationLog())
    store.write("double", _DOUBLE_SOURCE)
    spec = _spec_naming("double")

    pending_registry = StepRegistry()
    store.load_active(pending_registry)
    assert "double" not in pending_registry, "written but never activated -- not wired into any spec yet"

    assert store.activate("double") is True
    registry = StepRegistry()
    store.load_active(registry)

    verify_savable(spec, registry)  # does not raise: the step's contract wires cleanly

    implementation = registry.get("double")
    assert implementation is not None
    result = await implementation.function(x=21)
    assert result == 42, "the registered callable is the authored function, not a stand-in"


async def test_fingerprint_of_a_run_using_an_authored_step_includes_its_hash(tmp_path: Path) -> None:
    store = FileCustomSteps(tmp_path / "steps", activations=FakeActivationLog())
    store.write("double", _DOUBLE_SOURCE)
    store.activate("double")
    registry = StepRegistry()
    store.load_active(registry)
    spec = _spec_naming("double")

    stamped = fingerprint(spec, registry)

    assert "double" in stamped.steps
    implementation = registry.get("double")
    assert implementation is not None
    assert stamped.steps["double"], "the stamp is a non-empty hash of the step's introspected contract"
