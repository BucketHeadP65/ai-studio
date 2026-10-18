"""Turns the run's own session agent takes with nobody watching.

`settled_turn` tells the conversation how a background run ended (finished, failed, stopped or
paused), as one turn of the session's own agent once the run is closed. `orchestrator_decider`
decides a gate that names no person: a declared orchestrator gate, or a person gate the run's
`gate_mode` converted. Both wait out a busy session rather than skip it. A gate's rejection reaches
neither. The graph hands its words to the rerun.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from core.ai.workflows.contracts import GateDecided, GateDecision
from core.observability import get_logger

from harness import session as session_state
from harness.features.running.domain.progress import GateVerdict
from harness.features.running.exceptions import OrchestratorUnavailableError
from harness.features.running.infrastructure import run_events
from harness.rendering import render_for_review

if TYPE_CHECKING:
    from core.ai.workflows.contracts import OrchestratorDecider

    from harness.session import PooledSessions, Runtime

_logger = get_logger(__name__)

_LIVE: set[asyncio.Task[None]] = set()
"""Every headless turn under way, held so the event loop is not its only reference."""

_BUSY_RETRY_SECONDS = 0.25
"""How long the first retry of a busy turn lock waits, before the backoff below doubles it."""

_BUSY_RETRY_CAP_SECONDS = 5.0
"""The backoff's ceiling: a busy session is retried at least this often, however long it takes."""


def settled_turn(session: Runtime, *, workflow: str, prompt: str, sessions: PooledSessions | None = None) -> asyncio.Task[None]:
    """Fire one headless turn carrying a settled run's outcome, waiting out a busy session.

    The turn waits for the session's own lock with a capped backoff, however long the live turn
    takes. This module holds the task, so a caller may drop it.

    Args:
        session: The session whose agent takes the turn.
        workflow: The workflow the run ran, named in logging if the turn fails.
        prompt: What the turn tells the agent about the run's outcome.
        sessions: The pool that arbitrates the session's turns, or `None` to run unguarded.

    Returns:
        The task running the turn, for a caller that wants to await it.
    """
    task = asyncio.create_task(_headless_turn(session, workflow=workflow, prompt=prompt, sessions=sessions))
    _LIVE.add(task)
    task.add_done_callback(_LIVE.discard)
    return task


async def _headless_turn(session: Runtime, *, workflow: str, prompt: str, sessions: PooledSessions | None = None) -> None:
    """Run `prompt` as one turn in `session`, waiting out whatever turn is already in flight there."""
    hold = await _await_turn(session, sessions)
    try:
        # Nobody reads this turn's result, so it must not be offered a verb that waits for a
        # person, even when it is fired from inside a plugin that has someone to ask.
        with session_state.not_answerable(), session_state.bind_workspace_root(session_state.workspace_root(session)):
            await session.agent.run(prompt, deps=session)
    except Exception:
        _logger.exception("headless turn for {workflow} failed", workflow=workflow)
    finally:
        if hold is not None:
            hold.release()


async def _begin_turn(session: Runtime, sessions: PooledSessions | None) -> session_state.TurnHold | None:
    """This session's turn lock from the pool, or `None` when there is no pool or it cannot address `session`."""
    if sessions is None:
        return None
    pool = await sessions.pool()
    if session is pool.default:
        return await pool.begin_default_turn()
    if session.session_id is not None:
        return await pool.begin_turn(session.session_id)
    return None


async def _await_turn(session: Runtime, sessions: PooledSessions | None) -> session_state.TurnHold | None:
    """This session's turn lock, retried on `SessionBusyError` with a capped doubling backoff for as long as it takes."""
    backoff = _BUSY_RETRY_SECONDS
    while True:
        try:
            return await _begin_turn(session, sessions)
        except session_state.SessionBusyError:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BUSY_RETRY_CAP_SECONDS)


async def _turn_within_gate_timeout(session: Runtime, gate_id: str, sessions: PooledSessions | None) -> session_state.TurnHold | None:
    """`_await_turn` bounded by `session.active.gate_timeout`, raising `OrchestratorUnavailableError` past it."""
    ceiling = session.active.gate_timeout
    if ceiling is None:
        return await _await_turn(session, sessions)
    try:
        return await asyncio.wait_for(_await_turn(session, sessions), timeout=ceiling)
    except TimeoutError:
        msg = f"the orchestrator never became available for gate {gate_id!r}. The session stayed busy past this run's own gate timeout."
        raise OrchestratorUnavailableError(msg) from None


def orchestrator_decider(session: Runtime, sessions: PooledSessions | None = None) -> OrchestratorDecider:
    """This session's own structured turn, which answers a gate that names no person.

    The decider runs one agent turn with `GateVerdict` as its output type, over the whole value
    uncut. It records the decision in the run's log as the orchestrator's, never a person's, and
    hands the graph the exact feedback for the rerun. It raises `OrchestratorUnavailableError` when
    the session stays busy past the run's gate timeout.

    Args:
        session: The session whose own agent decides every gate this decider is asked.
        sessions: The pool the turn takes this session's hold in, or `None` to decide unguarded.

    Returns:
        The decider, which takes a gate id, a charge and the value under review.
    """

    async def decide(gate_id: str, charge: str, value: object, /) -> GateDecision:
        rendered = render_for_review(value, limit=None)
        prompt = f"{charge}\n\nGate: {gate_id}\n\nWhat is under review:\n{rendered}"
        hold = await _turn_within_gate_timeout(session, gate_id, sessions)
        try:
            # `output_type=GateVerdict` drops the deferred marker, so a verb that asks a person
            # raises here rather than waits, even inside a plugin that has someone to ask.
            with (
                session_state.bind_active(session.active),
                session_state.not_answerable(),
                session_state.bind_workspace_root(session_state.workspace_root(session)),
            ):
                result = await session.agent.run(prompt, deps=session, output_type=GateVerdict)
        finally:
            if hold is not None:
                hold.release()
        verdict = result.output
        run_events.record(
            session.state,
            GateDecided(
                run_id=session.active.run_id or "",
                workflow_id=session.active.workflow_id,
                gate_id=gate_id,
                approved=verdict.approve,
                by="orchestrator",
            ),
        )
        return GateDecision(approve=verdict.approve, feedback=verdict.feedback, source="orchestrator")

    return decide
