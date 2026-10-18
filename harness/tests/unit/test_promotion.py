"""The promotion bridge: a custom capability joins the capability registry.

The whole closure, proven end to end in `test_the_full_promotion_closure`: custom capability ->
promote -> custom agent declares it -> workflow step runs it, all through pending->activate. The
sections above it pin the pieces that closure depends on in isolation:
the contract check (a declared serialization name, a dataclass declared on the class itself), the
persisted list's own refusal to skip a name silently, and the `capability_grants` gate a worker
document still meets even after promotion opens the way for the promoted set.
"""

from __future__ import annotations

import json
import re

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import AgentStep, FromNode, FromParameter, ParameterSpec, RunNode, Shape, Workflow, prepare, validate
from pydantic import BaseModel
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart

from harness.features.selfauthoring.exceptions import PromotionError
from harness.features.selfauthoring.infrastructure.agents import FileCustomAgents
from harness.features.selfauthoring.infrastructure.capabilities import FileCustomCapabilities
from harness.features.selfauthoring.infrastructure.promotion import (
    capability_grants_for,
    demote_capability,
    promote_capability,
    promoted_capability_types,
)
from tests.support.environment import world_over
from tests.support.selfauthoring import FakeActivationLog
from tests.support.streaming import streaming_function_model

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from core.ai.workflows.contracts import StepRegistry
    from pydantic import JsonValue
    from pydantic_ai.models.function import AgentInfo

BRIEFING = "Weigh whether the marker capability actually rode along."



LEDGER_SCHEMA = {"type": "object", "title": "LedgerIn", "properties": {"net": {"type": "integer"}}, "required": ["net"]}
VERDICT_SCHEMA = {"type": "object", "title": "Verdict", "properties": {"marker_seen": {"type": "boolean"}}, "required": ["marker_seen"]}

_MEETS_CONTRACT_SOURCE = """
from dataclasses import dataclass

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import FunctionToolset


@dataclass
class MarkerCapability(AbstractCapability):
    '''Meets the promotion contract: a declared serialization name (the class name, by default)
    and a dataclass declared on the class itself. Adds one tool present only when it is actually
    wired into a built agent. Not `frozen=True`: no `AbstractCapability` subclass can be, since
    the base class is itself a non-frozen dataclass -- see `promotion.py`'s module docstring.'''

    def get_toolset(self):
        def marker_tool() -> str:
            '''Present only when MarkerCapability is actually part of the agent.'''
            return "marker"

        return FunctionToolset([marker_tool])
"""

_OPTS_OUT_OF_SERIALIZATION_SOURCE = """
from dataclasses import dataclass

from pydantic_ai.capabilities import AbstractCapability


@dataclass
class OptsOut(AbstractCapability):
    '''A declared dataclass, but explicitly opts out of spec serialization.'''

    @classmethod
    def get_serialization_name(cls):
        return None
"""

_NOT_A_DATACLASS_SOURCE = """
from pydantic_ai.capabilities import AbstractCapability


class NotADataclass(AbstractCapability):
    '''Never decorated with @dataclass at all.'''
"""


def _custom_capabilities(tmp_path: Path) -> tuple[FileCustomCapabilities, FakeActivationLog]:
    activations = FakeActivationLog()
    return FileCustomCapabilities(tmp_path / "capabilities", activations=activations), activations


# --- the contract check, run against the class actually loaded via the store's own loader -----


def test_promoting_a_pending_capability_is_refused_pointing_at_activate(tmp_path: Path) -> None:
    store, _activations = _custom_capabilities(tmp_path)
    store.write("marker", _MEETS_CONTRACT_SOURCE)

    with pytest.raises(PromotionError, match=re.escape("harness activate capability marker")):
        promote_capability(store, "marker")


def test_promoting_an_unknown_name_is_refused(tmp_path: Path) -> None:
    store, _activations = _custom_capabilities(tmp_path)

    with pytest.raises(PromotionError, match=re.escape("No custom capability named 'nobody'.")):
        promote_capability(store, "nobody")


def test_promoting_a_capability_that_opted_out_of_serialization_names_that_half(tmp_path: Path) -> None:
    store, _activations = _custom_capabilities(tmp_path)
    store.write("opts_out", _OPTS_OUT_OF_SERIALIZATION_SOURCE)
    store.activate("opts_out")

    with pytest.raises(PromotionError, match="does not declare a serialization name"):
        promote_capability(store, "opts_out")


def test_promoting_a_capability_that_is_not_a_dataclass_at_all_names_that_half(tmp_path: Path) -> None:
    store, _activations = _custom_capabilities(tmp_path)
    store.write("plain", _NOT_A_DATACLASS_SOURCE)
    store.activate("plain")

    with pytest.raises(PromotionError) as excinfo:
        promote_capability(store, "plain")

    assert "not declared as a dataclass" in str(excinfo.value)
    # A plain subclass with no override still inherits a real serialization name from the base
    # class default (its own class name), so only the dataclass half is actually missing here.
    assert "does not declare a serialization name" not in str(excinfo.value)


def test_promoting_a_capability_that_meets_the_contract_succeeds(tmp_path: Path) -> None:
    store, _activations = _custom_capabilities(tmp_path)
    store.write("marker", _MEETS_CONTRACT_SOURCE)
    store.activate("marker")

    record = promote_capability(store, "marker")

    assert record.name == "marker"
    assert record.class_name == "MarkerCapability"


def test_promotion_is_idempotent(tmp_path: Path) -> None:
    store, _activations = _custom_capabilities(tmp_path)
    store.write("marker", _MEETS_CONTRACT_SOURCE)
    store.activate("marker")

    promote_capability(store, "marker")
    promote_capability(store, "marker")

    [only_once] = promoted_capability_types(store)
    assert only_once.__name__ == "MarkerCapability"


def test_promotion_is_attested_through_the_shared_log(tmp_path: Path) -> None:
    store, activations = _custom_capabilities(tmp_path)
    store.write("marker", _MEETS_CONTRACT_SOURCE)
    store.activate("marker")
    activations.events.clear()

    promote_capability(store, "marker")

    assert [(event.name, event.action, event.via, event.kind) for event in activations.events] == [
        ("marker", "activate", "cli", "capability_promoted")
    ]


# --- the persisted promoted list: human-diffable, never a silent skip -------------------------


def test_the_promoted_list_is_a_small_sorted_json_file_beside_the_manifest(tmp_path: Path) -> None:
    store, _activations = _custom_capabilities(tmp_path)
    store.write("zebra", _MEETS_CONTRACT_SOURCE.replace("MarkerCapability", "Zebra"))
    store.write("apple", _MEETS_CONTRACT_SOURCE.replace("MarkerCapability", "Apple"))
    store.activate("zebra")
    store.activate("apple")

    promote_capability(store, "zebra")
    promote_capability(store, "apple")

    promoted_file = tmp_path / "capabilities" / "promoted.json"
    assert promoted_file.exists()
    content = promoted_file.read_text()
    assert '"apple"' in content
    assert '"zebra"' in content
    # Sorted, so promoting in either order produces the same diff.
    assert content.index('"apple"') < content.index('"zebra"')


def test_demoting_a_promoted_capability_removes_it(tmp_path: Path) -> None:
    store, activations = _custom_capabilities(tmp_path)
    store.write("marker", _MEETS_CONTRACT_SOURCE)
    store.activate("marker")
    promote_capability(store, "marker")
    activations.events.clear()

    assert demote_capability(store, "marker") is True

    assert promoted_capability_types(store) == ()
    assert [(event.name, event.action, event.via, event.kind) for event in activations.events] == [
        ("marker", "deactivate", "cli", "capability_promoted")
    ]


def test_demoting_a_name_that_was_never_promoted_reports_absence(tmp_path: Path) -> None:
    store, activations = _custom_capabilities(tmp_path)

    assert demote_capability(store, "nobody") is False
    assert activations.events == []


# --- promotion is DERIVED from activation, never tracked in parallel --------------------------
#
# The owner's off switch (deactivate/disable) and the owner's own re-authoring tool must both
# actually revoke a promotion -- otherwise the pending gate leaks: promoted.json would keep
# naming a capability the manifest honestly shows disabled, and a later `promoted_capability_types()`
# call (at the next environment-build) would still load it.


def test_deactivating_a_promoted_capability_demotes_it(tmp_path: Path) -> None:
    store, activations = _custom_capabilities(tmp_path)
    store.write("marker", _MEETS_CONTRACT_SOURCE)
    store.activate("marker")
    promote_capability(store, "marker")
    activations.events.clear()

    store.deactivate("marker")

    assert promoted_capability_types(store) == ()
    assert capability_grants_for(promoted_capability_types(store)) == ()
    kinds = [(event.name, event.action, event.via, event.kind) for event in activations.events]
    assert ("marker", "deactivate", "cli", "capability_activated") in kinds
    assert ("marker", "deactivate", "cli", "capability_promoted") in kinds


def test_the_agents_own_disable_door_also_demotes_a_promoted_capability(tmp_path: Path) -> None:
    store, activations = _custom_capabilities(tmp_path)
    store.write("marker", _MEETS_CONTRACT_SOURCE)
    store.activate("marker")
    promote_capability(store, "marker")
    activations.events.clear()

    store.disable("marker")

    assert promoted_capability_types(store) == ()
    kinds = [(event.name, event.action, event.via, event.kind) for event in activations.events]
    assert ("marker", "deactivate", "agent", "capability_promoted") in kinds


def test_reauthoring_an_active_promoted_capability_demotes_it(tmp_path: Path) -> None:
    """The owner's own re-authoring tool swaps in never-reviewed code.

    `write_custom_capability` rewriting an active, promoted name means any promotion of the code it
    replaces must not survive the swap.
    """
    store, activations = _custom_capabilities(tmp_path)
    store.write("marker", _MEETS_CONTRACT_SOURCE)
    store.activate("marker")
    promote_capability(store, "marker")
    activations.events.clear()

    store.write("marker", _MEETS_CONTRACT_SOURCE)

    assert promoted_capability_types(store) == ()
    kinds = [(event.name, event.action, event.via, event.kind) for event in activations.events]
    assert ("marker", "deactivate", "agent", "capability_activated") in kinds
    assert ("marker", "deactivate", "agent", "capability_promoted") in kinds


def test_reauthoring_a_promoted_capability_never_reexecutes_the_new_source_via_the_registry(tmp_path: Path) -> None:
    """The marker-file canary.

    Rewritten, never-reviewed code must not run again just because the name it replaced was
    promoted -- proves the demotion actually closes the execution path, not only the bookkeeping.
    """
    canary = tmp_path / "canary.txt"
    canary_source = f"""
from pathlib import Path
from dataclasses import dataclass

from pydantic_ai.capabilities import AbstractCapability

Path({str(canary)!r}).write_text("executed")


@dataclass
class MarkerCapability(AbstractCapability):
    pass
"""
    store, _activations = _custom_capabilities(tmp_path)
    store.write("marker", _MEETS_CONTRACT_SOURCE)
    store.activate("marker")
    promote_capability(store, "marker")

    store.write("marker", canary_source)  # write()'s own validation legitimately imports this once
    assert canary.exists(), "sanity: write()'s own validation really did import the new source"
    canary.unlink()  # reset -- this test is about a SECOND execution, not the first

    assert promoted_capability_types(store) == (), "demoted by write(), so nothing is left to load"
    assert not canary.exists(), "promoted_capability_types() must never load the demoted, unreviewed source"


def test_promoted_capability_types_refuses_a_record_that_is_no_longer_active(tmp_path: Path) -> None:
    """The defensive backstop.

    Even if `promoted.json` and the manifest disagree -- a hand-edit, corruption, or a bug
    elsewhere -- the loader never executes code for a non-active record. The store's own API
    should make this unreachable, so the corruption is simulated directly by hand-editing the
    manifest file, exactly the drift the store's own plugins exist to prevent.
    """
    canary = tmp_path / "canary.txt"
    canary_source = f"""
from pathlib import Path
from dataclasses import dataclass

from pydantic_ai.capabilities import AbstractCapability

Path({str(canary)!r}).write_text("executed")


@dataclass
class MarkerCapability(AbstractCapability):
    pass
"""
    store, _activations = _custom_capabilities(tmp_path)
    store.write("marker", canary_source)  # write()'s own validation legitimately imports this once
    store.activate("marker")
    promote_capability(store, "marker")
    canary.unlink()  # reset -- this test is about the SECOND execution, not the first

    manifest_path = tmp_path / "capabilities" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["capabilities"][0]["status"] = "disabled"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(PromotionError, match="not active"):
        promoted_capability_types(store)

    assert not canary.exists(), "the defensive check must refuse BEFORE load_capability_instance ever runs"


# --- the loader: `promoted_capability_types`, the one thing every custom_capability_types= site reads


def test_promoted_capability_types_resolves_the_real_classes(tmp_path: Path) -> None:
    store, _activations = _custom_capabilities(tmp_path)
    store.write("marker", _MEETS_CONTRACT_SOURCE)
    store.activate("marker")
    promote_capability(store, "marker")

    [resolved] = promoted_capability_types(store)

    assert resolved.__name__ == "MarkerCapability"
    assert resolved.get_serialization_name() == "MarkerCapability"


def test_promoted_capability_types_refuses_loudly_when_the_source_is_gone(tmp_path: Path) -> None:
    """A promoted name whose source no longer exists is a loud error, never a silent skip."""
    store, _activations = _custom_capabilities(tmp_path)
    # Hand-write the promoted list directly, naming a capability nobody ever wrote --
    # simulating a hand-edited, git-tracked manifest that has drifted from the capability store.
    (tmp_path / "capabilities").mkdir(parents=True)
    (tmp_path / "capabilities" / "promoted.json").write_text('{"names": ["ghost"]}')

    with pytest.raises(PromotionError, match="ghost") as excinfo:
        promoted_capability_types(store)

    assert "promoted.json" in str(excinfo.value)


def test_capability_grants_for_derives_serialization_names(tmp_path: Path) -> None:
    store, _activations = _custom_capabilities(tmp_path)
    store.write("marker", _MEETS_CONTRACT_SOURCE)
    store.activate("marker")
    promote_capability(store, "marker")

    grants = capability_grants_for(promoted_capability_types(store))

    assert grants == ("MarkerCapability",)


# --- the capability_grants gate still fences off anything not promoted ------------------------


async def test_a_worker_declaring_a_capability_not_in_the_grants_list_is_refused_cold(tmp_path: Path) -> None:
    """Promotion opens the way for the promoted set -- it does not blow the gate open for everyone else.

    `Thinking` is a real, always-registered built-in capability, so this proves the refusal is
    about `capability_grants` specifically, not about registry membership.
    """
    environment = await world_over(streaming_function_model(_answers_marker_seen), state=tmp_path)

    report = validate(_weighing({"instructions": BRIEFING, "capabilities": ["Thinking"]}), environment)

    assert not report.ok
    assert any("Thinking" in issue.detail and "not granted" in issue.detail for issue in report.issues)


# --- the closure: custom capability -> promote -> custom agent declares it -> step runs it -


def _answers_marker_seen(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    del messages
    saw_marker = any(tool.name == "marker_tool" for tool in info.function_tools)
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"marker_seen": saw_marker})])


def _weighing(agent: Mapping[str, JsonValue] | str) -> Workflow:
    return Workflow(
        name="weigh-the-marker",
        version=1,
        description="A worker reads a activations; whether it saw the marker tool is the actual assertion.",
        parameters={"activations": ParameterSpec(description="the activations", json_schema=LEDGER_SCHEMA)},
        inline=(AgentStep(name="weigh", agent=agent, inputs={"activations": Shape(json_schema=LEDGER_SCHEMA)}, output=Shape(json_schema=VERDICT_SCHEMA)),),
        nodes=(RunNode(id="weigh", work="weigh", inputs={"activations": FromParameter(parameter="activations")}),),
        output=FromNode(node="weigh"),
    )


async def _weighed(environment: StepRegistry, agent: Mapping[str, JsonValue] | str) -> object:
    ready = prepare(_weighing(agent), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("weigh")
    assert implementation is not None
    return await implementation.function(activations={"net": 100})


async def test_the_full_promotion_closure(tmp_path: Path) -> None:
    """Custom capability -> promote -> custom agent declares it -> workflow step runs it.

    Every gate crossed for real, nothing faked but the LLM seat: a pending capability refuses
    promotion; a custom agent declaring an unpromoted-but-written capability is refused at
    author time, pointing at `promote`; after promotion and activation, a workflow step naming
    the agent by name resolves and RUNS with the promoted capability's tool actually offered to
    the model -- real registry resolution throughout.
    """
    custom = tmp_path / "custom"
    capabilities = FileCustomCapabilities(custom / "capabilities", activations=FakeActivationLog())
    agents = FileCustomAgents(custom / "agents", activations=FakeActivationLog())

    capabilities.write("marker_capability", _MEETS_CONTRACT_SOURCE)
    with pytest.raises(PromotionError, match="not active"):
        promote_capability(capabilities, "marker_capability")

    capabilities.activate("marker_capability")

    document = f"instructions: {BRIEFING!r}\ncapabilities:\n  - MarkerCapability\n"
    pending_record = agents.write("weigher", document)
    assert pending_record.last_error is not None
    assert "MarkerCapability" in pending_record.last_error
    assert "harness promote capability marker_capability" in pending_record.last_error

    promote_capability(capabilities, "marker_capability")
    promoted = promoted_capability_types(capabilities)
    assert [cls.__name__ for cls in promoted] == ["MarkerCapability"]

    agents_with_registry = FileCustomAgents(custom / "agents", custom_capability_types=promoted, activations=FakeActivationLog())
    record = agents_with_registry.write("weigher", document)
    assert record.last_error is None
    agents_with_registry.activate("weigher")

    environment = await world_over(streaming_function_model(_answers_marker_seen), state=tmp_path)

    report = validate(_weighing("weigher"), environment)
    assert report.ok, report.issues

    produced = await _weighed(environment, "weigher")

    assert isinstance(produced, BaseModel)
    assert produced.model_dump()["marker_seen"] is True, "the promoted capability's tool was not actually offered to the model"
