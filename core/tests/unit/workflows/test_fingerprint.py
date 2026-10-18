"""The fingerprint: what environment an artifact was built against, checkable at retrieval.

Staleness is the empirically dominant failure mode of a store of intent: the environment drifts
under the artifact, and nothing says so until a run fails strangely. The fingerprint makes
drift mechanical -- hash the contract of every step the spec names, hash the grant surface,
carry whatever stamps the host adds -- and `drift` names exactly what moved.

What it deliberately is NOT: a declared precondition. Nothing here is typed by an author;
everything is derived from the registry at save time and re-derived at fetch time, which is
the same ruling that killed declared `requires` lists -- nothing declared, nothing to go
stale *about the staleness check itself*.
"""

from __future__ import annotations

import hashlib

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import (
    AgentResolver,
    AgentStep,
    FromNode,
    FromParameter,
    MapNode,
    ParameterSpec,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    WorkflowFingerprint,
    drift,
    fingerprint,
)
from tests.unit.workflows.conftest import CallLog, Ledger, ReportUrl, register_quarterly_steps

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import JsonValue

BRIEFING = "Read the ledger and say whether the quarter looks healthy."

LEDGER = {"type": "object", "title": "LedgerIn", "properties": {"net": {"type": "integer"}}, "required": ["net"]}

VERDICT = {"type": "object", "title": "Verdict", "properties": {"healthy": {"type": "boolean"}}, "required": ["healthy"]}


def _registry(call_log: CallLog) -> StepRegistry:
    reg = StepRegistry()
    register_quarterly_steps(reg, call_log)
    return reg


def _resolver(document: Mapping[str, JsonValue], *, known_as: str = "weigher") -> AgentResolver:
    """A fake `AgentResolver`: answers `document` for `known_as`, refuses every other name."""

    def resolve(name: str) -> Mapping[str, JsonValue]:
        if name != known_as:
            msg = f"no authored agent named {name!r}"
            raise LookupError(msg)
        return document

    return resolve


def _weighing(agent: Mapping[str, JsonValue] | str) -> Workflow:
    """One worker step, fed by one parameter, and nothing else in the graph."""
    return Workflow(
        name="weigh-the-quarter",
        version=1,
        description="A worker reads a ledger and says whether the quarter looks healthy.",
        parameters={"ledger": ParameterSpec(description="the ledger", json_schema=LEDGER)},
        inline=(AgentStep(name="weigh", agent=agent, inputs={"ledger": Shape(json_schema=LEDGER)}, output=Shape(json_schema=VERDICT)),),
        nodes=(RunNode(id="weigh", work="weigh", inputs={"ledger": FromParameter(parameter="ledger")}),),
        output=FromNode(node="weigh"),
    )


def test_same_world_same_fingerprint(quarterly_report: Workflow, call_log: CallLog) -> None:
    first = fingerprint(quarterly_report, _registry(call_log))
    second = fingerprint(quarterly_report, _registry(call_log))
    assert first == second
    assert drift(first, second) == ()


def test_a_changed_step_contract_is_named(quarterly_report: Workflow, call_log: CallLog) -> None:
    recorded = fingerprint(quarterly_report, _registry(call_log))

    changed = StepRegistry()
    register_quarterly_steps(changed, call_log, exclude=frozenset({"publish"}))

    async def publish(report: object, channel: str) -> str:  # a second parameter: the contract moved
        del report, channel
        return "url"

    changed.register(publish)
    findings = drift(recorded, fingerprint(quarterly_report, changed))
    assert len(findings) == 1
    assert "publish" in findings[0]


def test_only_steps_the_spec_names_are_stamped(quarterly_report: Workflow, call_log: CallLog) -> None:
    reg = _registry(call_log)

    async def unrelated(x: str) -> str:
        return x

    reg.register(unrelated)
    stamped = fingerprint(quarterly_report, reg)
    assert "unrelated" not in stamped.steps, "a step the spec never names cannot make it stale"
    assert set(stamped.steps) == {"fetch_sales", "fetch_costs", "reconcile", "draft_report", "publish"}


def test_fingerprint_uses_the_complete_referenced_steps_traversal(quarterly_report: Workflow, call_log: CallLog, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Workflow] = []

    def only_publish(spec: Workflow) -> tuple[str, ...]:
        seen.append(spec)
        return ("publish",)

    monkeypatch.setattr("core.ai.workflows.domain.fingerprint.referenced_steps", only_publish)

    stamped = fingerprint(quarterly_report, _registry(call_log))

    assert seen == [quarterly_report]
    assert set(stamped.steps) == {"publish"}


def test_world_stamps_are_carried_and_compared(quarterly_report: Workflow, call_log: CallLog) -> None:
    recorded = fingerprint(quarterly_report, _registry(call_log), environment={"model": "gpt-5.2"})
    current = fingerprint(quarterly_report, _registry(call_log), environment={"model": "gpt-6"})
    findings = drift(recorded, current)
    assert len(findings) == 1
    assert "model" in findings[0]
    assert "gpt-5.2" in findings[0]
    assert "gpt-6" in findings[0]


def test_a_stamp_the_current_world_no_longer_offers_is_drift(quarterly_report: Workflow, call_log: CallLog) -> None:
    recorded = fingerprint(quarterly_report, _registry(call_log), environment={"model": "gpt-5.2"})
    current = fingerprint(quarterly_report, _registry(call_log))
    assert drift(recorded, current) != ()


def test_a_step_missing_from_the_current_world_is_drift_not_a_crash(quarterly_report: Workflow, call_log: CallLog) -> None:
    recorded = fingerprint(quarterly_report, _registry(call_log))
    thin = StepRegistry()
    register_quarterly_steps(thin, call_log, exclude=frozenset({"publish"}))
    findings = drift(recorded, fingerprint(quarterly_report, thin))
    assert any("publish" in finding for finding in findings)


def _chained_map_spec() -> Workflow:
    """One map whose second turn is a real, separately stamped step call."""
    return Workflow(
        name="chained-map",
        version=1,
        description="Fan out over a seeded list, investigate each item, then settle each one.",
        parameters={"quarter": ParameterSpec(description="quarter", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="items", work="seed", inputs={"quarter": FromParameter(parameter="quarter")}),
            MapNode(id="each", turns=("investigate_item", "settle"), over=FromNode(node="items"), each="item"),
        ),
        output=FromNode(node="each"),
    )


def _chained_map_registry() -> StepRegistry:
    async def seed(quarter: str) -> list[str]:
        return [quarter]

    async def investigate_item(item: str) -> Ledger:
        del item
        return Ledger(net=1)

    async def settle(net: Ledger) -> ReportUrl:
        del net
        return ReportUrl(url="x")

    registry = StepRegistry()
    for fn in (seed, investigate_item, settle):
        registry.register(fn)
    return registry


def test_a_maps_second_turn_is_stamped_like_any_other_step() -> None:
    stamped = fingerprint(_chained_map_spec(), _chained_map_registry())

    assert set(stamped.steps) == {"seed", "investigate_item", "settle"}, "every turn is a real step call, stamped like the rest"


def test_a_changed_second_turn_contract_is_drift() -> None:
    recorded = fingerprint(_chained_map_spec(), _chained_map_registry())

    changed = StepRegistry()

    async def seed(quarter: str) -> list[str]:
        return [quarter]

    async def investigate_item(item: str) -> Ledger:
        del item
        return Ledger(net=1)

    async def settle(net: Ledger, channel: str) -> ReportUrl:  # a second parameter: the contract moved
        del net, channel
        return ReportUrl(url="x")

    for fn in (seed, investigate_item, settle):
        changed.register(fn)
    findings = drift(recorded, fingerprint(_chained_map_spec(), changed))

    assert len(findings) == 1
    assert "settle" in findings[0]


def test_fingerprint_round_trips_as_json(quarterly_report: Workflow, call_log: CallLog) -> None:
    stamped = fingerprint(quarterly_report, _registry(call_log), environment={"model": "gpt-5.2"})
    revived = WorkflowFingerprint.model_validate_json(stamped.model_dump_json())
    assert revived == stamped
    assert drift(revived, stamped) == ()


# --- a node's own configuration is never stamped, only the steps it names --------------------

_PINNED_CHAINED_MAP_FINGERPRINT = "7997ecc5305c9e2b91fd280d29ba391998991029c73994a95db61bfb62e3f43b"
"""The digest of `fingerprint(_chained_map_spec(), _chained_map_registry())` under this pydantic
and this grammar, pinned before `MapNode.on_error` existed. See the test."""


def test_a_maps_on_error_left_at_default_does_not_move_the_fingerprint() -> None:
    """The fingerprint stamps registered step contracts, never a node's own configuration.

    `on_error` is exactly that: a map's own field, invisible to `fingerprint` by construction --
    only the turn names in `.turns` ever reach `.steps`. Adding it to the grammar must not
    re-key an artifact that never sets it, and this pins the digest from before the field
    existed to prove it stayed put.
    """
    stamped = fingerprint(_chained_map_spec(), _chained_map_registry())
    digest = hashlib.sha256(stamped.model_dump_json().encode()).hexdigest()
    assert digest == _PINNED_CHAINED_MAP_FINGERPRINT


# --- a worker's STRING agent reference is stamped; an inline document is not -----------------


def test_a_string_agent_reference_is_stamped() -> None:
    resolver = _resolver({"instructions": BRIEFING})
    kit = StepRegistry(run_agent=None, resolve_agent=resolver)

    stamped = fingerprint(_weighing("weigher"), kit)

    assert "weigh" in stamped.agents
    assert stamped.steps == {}, "a brought step is never a registered step, whichever form its agent takes"


def test_an_inline_agent_document_is_not_stamped() -> None:
    """The travel-inside premise genuinely holds for an inline document -- nothing external can move under it."""
    kit = StepRegistry(run_agent=None)

    stamped = fingerprint(_weighing({"instructions": BRIEFING}), kit)

    assert stamped.agents == {}


def test_a_repointed_name_after_reauthoring_is_drift() -> None:
    """The whole point: one spec identity, one recorded fingerprint, a name that now resolves to different content."""
    recorded = fingerprint(_weighing("weigher"), StepRegistry(run_agent=None, resolve_agent=_resolver({"instructions": BRIEFING})))

    reauthored = _resolver({"instructions": "A completely different briefing -- re-authored, then re-activated."})
    current = fingerprint(_weighing("weigher"), StepRegistry(run_agent=None, resolve_agent=reauthored))

    findings = drift(recorded, current)
    assert len(findings) == 1
    assert "weigh" in findings[0]


def test_an_agent_deactivated_between_save_and_a_later_check_is_drift_not_a_crash() -> None:
    """A live drift check must be able to report a deactivation, not blow up trying to."""
    recorded = fingerprint(_weighing("weigher"), StepRegistry(run_agent=None, resolve_agent=_resolver({"instructions": BRIEFING})))

    now_unresolvable = StepRegistry(run_agent=None, resolve_agent=_resolver({"instructions": BRIEFING}, known_as="someone_else"))
    current = fingerprint(_weighing("weigher"), now_unresolvable)

    assert current.agents == {}, "a name the resolver cannot currently find is absent, exactly like a missing step"
    findings = drift(recorded, current)
    assert any("weigh" in finding for finding in findings)


def test_fingerprinting_a_string_reference_with_no_resolver_at_all_refuses_loudly() -> None:
    """Only the total absence of a resolver refuses cold -- a host-capability mismatch, not a missing document."""
    kit = StepRegistry(run_agent=None)

    with pytest.raises(LookupError, match="no agent registry is wired here"):
        fingerprint(_weighing("weigher"), kit)


def test_fingerprinting_a_name_that_was_never_authored_is_simply_absent() -> None:
    """A wired resolver that has never heard of the name behaves exactly like the deactivated case, not a crash."""
    kit = StepRegistry(run_agent=None, resolve_agent=_resolver({"instructions": BRIEFING}))

    stamped = fingerprint(_weighing("nobody"), kit)

    assert stamped.agents == {}


def test_agents_round_trip_as_json() -> None:
    stamped = fingerprint(_weighing("weigher"), StepRegistry(run_agent=None, resolve_agent=_resolver({"instructions": BRIEFING})))
    revived = WorkflowFingerprint.model_validate_json(stamped.model_dump_json())
    assert revived == stamped
    assert drift(revived, stamped) == ()
