"""Maintenance flags as the `harness flags` verbs print them: one line per flag in a list, and one whole flag."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.ai.workflows.contracts import FlagCard, MaintenanceFlag


def flag_line(card: FlagCard) -> str:
    """One flag as `harness flags list` prints it: its id, kind, workflow, step and summary, two spaces apart.

    A flag about a step names no workflow and prints `-` in its place.

    Args:
        card: The flag's card.

    Returns:
        The line, without a line break.
    """
    return "  ".join((card.id, card.kind, card.workflow or "-", card.step, card.summary))


def render_flag(flag_id: str, flag: MaintenanceFlag) -> str:
    """One whole flag as `harness flags show` prints it: its fields, its Evidence and Proposed change, and how to resolve it.

    The resolve line is printed only while the flag is open.

    Args:
        flag_id: The flag's page id.
        flag: The flag the page holds.

    Returns:
        The text, ending with a line break.
    """
    lines = [
        flag_id,
        flag.title,
        f"kind: {flag.kind}",
        f"status: {flag.status}",
        f"step: {flag.step}",
        f"step version: {flag.step_id} ({flag.step_hash})",
        f"workflow: {flag.workflow or 'none, the flag is about the step'}",
        f"raised: {flag.raised_at.isoformat()} by {flag.raised_by}",
    ]
    if flag.status == "resolved":
        lines += [f"resolved by: {flag.resolved_by}", f"reason: {flag.decision_reason}"]
    lines += [
        f"summary: {flag.summary}",
        "",
        "## Evidence",
        "",
        flag.evidence,
        "",
        "## Proposed change",
        "",
        flag.proposed_change or "(none)",
    ]
    if flag.status == "open":
        lines += ["", f'Resolve it with: harness flags resolve {flag_id} --by <your name> --reason "<why>"']
    return "\n".join(lines) + "\n"
