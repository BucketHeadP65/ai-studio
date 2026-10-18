"""`studio feedback`: read what agents filed through `submit_feedback`, so a filed report is never write-only."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import typer

from studio.knowledge_base.cli_support import FEEDBACK_FILE, announce_knowledge_base, read_feedback

feedback_app = typer.Typer(
    name="feedback", help="What agents reported about the retrieval tools, newest first.", invoke_without_command=True
)

_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400


def _age(created_at: str, now: datetime) -> str:
    """A person-sized age, `3 days ago`, `2 hours ago`, `just now`."""
    seconds = int((now - datetime.fromisoformat(created_at)).total_seconds())
    if seconds < _SECONDS_PER_MINUTE:
        return "just now"
    if seconds < _SECONDS_PER_HOUR:
        return f"{seconds // _SECONDS_PER_MINUTE} minutes ago"
    if seconds < _SECONDS_PER_DAY:
        return f"{seconds // _SECONDS_PER_HOUR} hours ago"
    return f"{seconds // _SECONDS_PER_DAY} days ago"


@feedback_app.callback()
def feedback_cmd(
    ctx: typer.Context,
    *,
    knowledge_base: str = typer.Option(default=".", help="Knowledge base root whose feedback file to read."),
    since: str | None = typer.Option(default=None, help="Only entries filed on or after this date (YYYY-MM-DD)."),
) -> None:
    """List every feedback entry a knowledge base holds, newest first, with its category, tool and age."""
    if ctx.invoked_subcommand is not None:
        return
    root = Path(knowledge_base).resolve()
    announce_knowledge_base(root)
    floor: date | None = None
    if since is not None:
        try:
            floor = date.fromisoformat(since)
        except ValueError:
            typer.echo(f"--since takes a date as YYYY-MM-DD, not {since!r}.", err=True)
            raise typer.Exit(code=2) from None
    entries, unreadable = read_feedback(root / FEEDBACK_FILE)
    now = datetime.now(UTC)
    shown = 0
    for entry in entries:
        if floor is not None and datetime.fromisoformat(entry.created_at).date() < floor:
            continue
        tool = entry.tool if entry.tool is not None else "no tool named"
        typer.echo(f"{_age(entry.created_at, now):>16}  {entry.category:<12} {tool:<16} {entry.message}")
        shown += 1
    if shown == 0 and not unreadable:
        typer.echo("no feedback filed." if floor is None else f"no feedback filed since {floor.isoformat()}.")
    for number in unreadable:
        typer.echo(f"line {number} could not be read as a feedback entry.")
