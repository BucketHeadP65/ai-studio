"""The person gate, wired to a browser instead of a console.

Waiting at a gate writes to the session and awaits a future that an HTTP handler resolves.
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.ai.workflows.contracts import DecisionSource, GateDecision, GateReached, JudgeGate, PersonGate
from core.ai.workflows.contracts import orchestrated_gates as _declared_orchestrated_gates
from core.observability import get_logger

from harness.adapters.contracts import NoTopics
from harness.features.running.domain.progress import GateVerdict, PendingReview
from harness.features.running.infrastructure import run_events
from harness.rendering import render_for_review

if TYPE_CHECKING:
    from collections.abc import Coroutine, Mapping

    from core.ai.workflows.contracts import OrchestratorDecider, Workflow
    from core.cqrs.contracts import TopicSink

    from harness.features.running.internal_contracts import GateAsker
    from harness.session import Runtime

_logger = get_logger(__name__)


def with_reviewers_note(rendered: str, note: str) -> str:
    """`rendered`, prefixed with what a judge said when it could not decide.

    Every surface that shows a waiting value to a person words a `cannot_judge` escalation through this.

    Args:
        rendered: The value under review, rendered for reading.
        note: What the judge said. A blank `note` leaves `rendered` untouched.

    Returns:
        The text a person reads at the gate.
    """
    if not note:
        return rendered
    return f"The reviewer could not judge this and said: {note}\n\n{rendered}"


@dataclass(frozen=True)
class ChatApproval:
    """The gate a run waits at, answered by automode, the entry's own asker, the run's orchestrator or a person.

    The graph calls `review`, which holds the run until one of them answers. Nothing downstream
    runs in the meantime.
    """

    session: Runtime
    workflow: str
    """The workflow this run was launched from, as the waiting card names it."""
    spec: Workflow
    topics: TopicSink = field(default_factory=NoTopics)
    """Where this run's progress is announced. `NoTopics` for a process with no change channel."""
    timeout_seconds: float | None = 900.0
    """How long one reach of a gate waits before it is refused with nobody having decided. `None`
    waits forever. The gate's own `max_attempts` sets how many such refusals a run survives."""
    asker: GateAsker | None = None
    """The entry's own way of asking, where it has one. `None` is the browser: hold and wait."""
    decisions: dict[str, list[bool]] = field(default_factory=dict)
    """Every verdict this run's gates received, per gate, in order."""
    _auto: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    """How many times automode passed each gate, never counted in `decisions`."""
    _orchestrated: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    """How many times this run's own orchestrator approved each person gate under `gate_mode="orchestrate"`."""

    def rejections(self) -> dict[str, int]:
        """How many times each gate was refused, for the evidence record.

        Returns:
            Gate id to refusal count. A gate nobody refused does not appear.
        """
        return {gate_id: refused for gate_id, verdicts in self.decisions.items() if (refused := verdicts.count(False))}

    def auto_gates(self) -> tuple[str, ...]:
        """The gates automode passed with nobody asked.

        Returns:
            Their gate ids, in the order each was first passed.
        """
        return tuple(self._auto)

    def orchestrated_gates(self) -> tuple[str, ...]:
        """The person gates this run's own orchestrator decided in place of a person, and approved.

        Only the gates `gate_mode="orchestrate"` converted. `combined_orchestrated_gates` adds the
        declared orchestrator gates.

        Returns:
            Their gate ids, in the order each was first approved.
        """
        return tuple(self._orchestrated)

    async def review(self, gate_id: str, value: object, *, note: str = "") -> GateDecision:
        """Put `value` in front of whichever decider answers `gate_id`, and wait for its answer.

        Tried in this order: automode, the entry's own asker, this run's own orchestrator under
        `gate_mode="orchestrate"`, then the browser. Automode and the gate mode are read live off the
        session, so a flip mid-run applies at the next gate. The orchestrator and the browser share
        one wait and one timeout.

        Args:
            gate_id: Which gate this is.
            value: What is being reviewed.
            note: What a judge said when it could not decide, shown ahead of the value.

        Returns:
            The decision, naming who decided and what they said. A timeout is a rejection whose source is `"timeout"`.
        """
        run_events.record(
            self.session.state,
            GateReached(run_id=self.session.active.run_id or "", workflow_id=self.session.active.workflow_id, gate_id=gate_id),
        )
        if self.session.active.auto_approve:
            self._auto[gate_id] = self._auto.get(gate_id, 0) + 1
            _logger.info("gate {gate} auto-approved, nobody asked", gate=gate_id)
            return GateDecision(approve=True, source="auto")
        reviewing = with_reviewers_note(render_for_review(value), note)
        if self.asker is not None:
            verdict = await self.asker.ask(gate_id, reviewing)
            self.decisions.setdefault(gate_id, []).append(verdict)
            return GateDecision(approve=verdict, source="person")
        gate = next((node for node in self.spec.nodes if isinstance(node, PersonGate | JudgeGate) and node.id == gate_id), None)
        decide = self.session.registry.decide_gate
        decision: Coroutine[None, None, GateVerdict] | asyncio.Future[GateVerdict]
        if self.session.active.gate_mode == "orchestrate" and decide is not None:
            orchestrated = True
            decision = self._decide_in_place_of_the_owner(decide, gate_id, value, note)
        else:
            orchestrated = False
            decision = self._park(gate_id, gate, reviewing)
        timed_out = False
        try:
            verdict = await decision if self.timeout_seconds is None else await asyncio.wait_for(decision, timeout=self.timeout_seconds)
        except TimeoutError:
            # A refusal, recorded as the deadline's own and never as a person's. The run reruns
            # the gate's target and reaches this gate again, so a deployment with no way to
            # answer a gate spends that gate's reaches and stops, rather than waiting for ever.
            _logger.warning("gate {gate} timed out with nobody deciding", gate=gate_id)
            timed_out = True
            verdict = GateVerdict(approve=False, feedback="Nobody decided this gate before it timed out.")
        finally:
            self.session.active.pending = None
            self.session.active.decision = None
            self.topics.publish(f"run.{self.session.active.workflow_id}")
        self.decisions.setdefault(gate_id, []).append(verdict.approve)
        if orchestrated and verdict.approve:
            self._orchestrated[gate_id] = self._orchestrated.get(gate_id, 0) + 1
        decided_by: DecisionSource = "timeout" if timed_out else "orchestrator" if orchestrated else "person"
        return GateDecision(approve=verdict.approve, feedback=verdict.feedback, source=decided_by)

    async def _decide_in_place_of_the_owner(self, decide: OrchestratorDecider, gate_id: str, value: object, note: str = "") -> GateVerdict:
        """Ask this run's own orchestrator to decide in place of the owner, with a judge's `note` added to the charge."""
        charge = (
            f"Decide in place of the owner at gate {gate_id!r}. Review what is presented and approve it "
            "or reject it with concrete feedback."
        )
        if note:
            charge += f" The reviewer could not judge it and said: {note}"
        decision = await decide(gate_id, charge, value)
        return GateVerdict(approve=decision.approve, feedback=decision.feedback)

    def _park(self, gate_id: str, gate: PersonGate | JudgeGate | None, reviewing: str) -> asyncio.Future[GateVerdict]:
        """Stop the run at `gate_id` until a person answers through `/api/decide`."""
        decision: asyncio.Future[GateVerdict] = asyncio.get_running_loop().create_future()
        self.session.active.decision = decision
        self.session.active.pending = PendingReview(
            gate_id=gate_id,
            workflow=self.workflow,
            reviewing=reviewing,
            rejection_reruns=gate.rejected_to if gate is not None else "(unknown)",
        )
        _logger.info("gate {gate} waiting for a human", gate=gate_id)
        self.topics.publish(f"run.{self.session.active.workflow_id}")
        return decision


def combined_orchestrated_gates(session: Runtime, spec: Workflow, values: Mapping[str, object]) -> tuple[str, ...]:
    """Every gate this run's own orchestrator decided: a declared orchestrator gate, or a converted person gate.

    It reads the converted gates off `session.active.orchestrated_gates`, so code with no handle on
    the run's `ChatApproval`, such as a stop, can call it.

    Args:
        session: The session the run belongs to.
        spec: The workflow the run ran.
        values: The run's values, which say which declared orchestrator gates released a value.

    Returns:
        The gate ids, each once, the declared gates first.
    """
    return tuple(dict.fromkeys((*_declared_orchestrated_gates(spec, values), *session.active.orchestrated_gates())))
