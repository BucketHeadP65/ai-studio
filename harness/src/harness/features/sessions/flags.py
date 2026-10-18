"""`flag_friction`: the agent's own report that something ground against it.

A tool, a skill, an instruction, a workflow, another agent, or the agent itself can grind against
a turn. This capability gives the agent one verb to say so, typed and recorded rather than left
as a stray sentence in its reply.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_ai import FunctionToolset, RunContext
from pydantic_ai.capabilities import AbstractCapability

from harness.domain.contracts import FrictionFlagged, FrictionTargetType, Recorded
from harness.session import Runtime

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass
class FrictionFlagging(AbstractCapability[Runtime]):
    """The agent's own report that something ground: a tool, a skill, the instructions, itself."""

    def get_instructions(self) -> str:
        """The standing instructions for when and how to file a flag.

        Returns:
            The instructions, the same text on every call.
        """
        return (
            "A `flag_friction` tool files a report when something gets in your way: a tool that confused you, "
            "a skill or instruction that misled you, a workflow that fought you, or a mistake of your own worth tuning. "
            "One flag per distinct grievance, with the exact name and a concrete note."
        )

    def get_toolset(self) -> FunctionToolset[Runtime]:
        """The one tool this capability exposes.

        Returns:
            A toolset holding `flag_friction`.
        """

        def flag_friction(ctx: RunContext[Runtime], target_kind: FrictionTargetType, target: str, note: str, tool_call_id: str | None = None) -> str:
            # The session the flag is filed in is its subject. The projection reads it from here,
            # never from where the log entry sits in the session log.
            ctx.deps.session_log.record(
                FrictionFlagged(
                    target_kind=target_kind,
                    target=target,
                    note=note,
                    tool_call_id=tool_call_id,
                    source="live",
                    subject_session_id=ctx.deps.session_id,
                )
            )
            return f"flagged {target_kind} {target!r}"

        return FunctionToolset([flag_friction])


def friction_lines(records: Sequence[Recorded], *, target_kind: FrictionTargetType | None = None) -> list[str]:
    """Every filed flag among a session log's entries as one line each, newest first.

    Each line carries when it was filed, the friction target type and name of what ground, the subject session's
    first eight characters (or `no session`), and the note.

    Args:
        records: The session log's entries.
        target_kind: The friction target type to narrow to, or `None` for every type.

    Returns:
        The lines, newest first.
    """
    lines: list[str] = []
    for record in reversed(records):
        event = record.event
        if not isinstance(event, FrictionFlagged) or (target_kind is not None and event.target_kind != target_kind):
            continue
        session = event.subject_session_id[:8] if event.subject_session_id is not None else "no session"
        lines.append(f"{record.at.isoformat(timespec='minutes')}  {event.target_kind:<12} {event.target:<20} {session:<10} {event.note}")
    return lines
