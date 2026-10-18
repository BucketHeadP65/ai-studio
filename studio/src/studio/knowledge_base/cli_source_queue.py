"""The `studio source-queue` command group: put any URL on the knowledge base's source queue.

The commands `add`, `list` and `retry` read and write `<knowledge_base>/_meta/source-queue.yaml` directly and need
no database. `studio sync` picks up every `requested` row, and every row resolved before, on its next run, through
the `intake` connector. The entry module adds `source_queue_app` to the root `app`, so each command runs as
`studio source-queue <name>`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import typer
import yaml

from engram.kb.contracts import KnowledgeBaseSchema
from pydantic import ValidationError

from studio.knowledge_base.cli_support import _fail, announce_knowledge_base
from studio.knowledge_base.source_queue import QUEUE_CONNECTOR, source_queue_at

if TYPE_CHECKING:
    from engram.sources.contracts import SourceQueue, SourceQueueEntry

source_queue_app = typer.Typer(
    name="source-queue", help="Append/list/retry any URL on the knowledge base's source queue.", no_args_is_help=True
)


def _queue(knowledge_base: str) -> SourceQueue:
    """The source queue at `<knowledge_base>/_meta/source-queue.yaml`."""
    try:
        schema = KnowledgeBaseSchema.from_path(Path(knowledge_base) / "knowledge-base-schema.json")
    except (OSError, ValueError):
        schema = None
    return source_queue_at(Path(knowledge_base), schema)


def _refuse_without_the_queue(knowledge_base: str) -> None:
    """Exit 1 when the knowledge base's schema cannot be read, or its sources are off or name no `intake` connector."""
    schema_path = Path(knowledge_base) / "knowledge-base-schema.json"
    try:
        schema = KnowledgeBaseSchema.from_path(schema_path)
    except (OSError, ValueError) as exc:
        _fail(f"Could not load the schema from {schema_path}: {exc}")
    if not (schema.sources.enabled and QUEUE_CONNECTOR in schema.sources.connectors):
        _fail(
            f"sources_without_queue: knowledge base {schema.slug!r} declares no source-queue connector. Set sources to "
            f'{{"enabled": true, "connectors": ["{QUEUE_CONNECTOR}"]}} in its knowledge-base-schema.json to accept URLs'
        )


def _entry_line(entry: SourceQueueEntry) -> str:
    line = f"{entry.key}  [{entry.status.value}]  {entry.url}"
    if entry.reason:
        line += f"  reason: {entry.reason}"
    return line


@source_queue_app.command(name="add")
def source_queue_add_cmd(
    url: str = typer.Argument(..., help="Any URL: YouTube, arXiv, X/Twitter, or a plain article/PDF link."),
    found_via: str | None = typer.Option(default=None, help="Where this URL was found, e.g. a watch-list entry's label."),
    knowledge_base: str = typer.Option(default=".", help="Knowledge base root whose source queue to append to."),
) -> None:
    """Append `url` to the knowledge base's source queue, reporting its row key and status.

    A URL whose row key is already on the queue, in any status, is reported as that row is, even when the
    URL is spelled differently. It is never added twice or reset to `requested`. A knowledge base whose schema declares no source-queue connector is refused.
    """
    announce_knowledge_base(Path(knowledge_base).resolve())
    _refuse_without_the_queue(knowledge_base)
    entry = _queue(knowledge_base).add(url, found_via=found_via)
    typer.echo(_entry_line(entry))


@source_queue_app.command(name="list")
def source_queue_list_cmd(
    knowledge_base: str = typer.Option(default=".", help="Knowledge base root whose source queue to list."),
) -> None:
    """Print every row of the knowledge base's source queue, in queue order."""
    announce_knowledge_base(Path(knowledge_base).resolve())
    try:
        entries = _queue(knowledge_base).entries()
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        _fail(f"could not read the source queue: {exc}")
    if not entries:
        typer.echo("The source queue is empty.")
        return
    for entry in entries:
        typer.echo(_entry_line(entry))


@source_queue_app.command(name="retry")
def source_queue_retry_cmd(
    key: str = typer.Argument(..., help="The queued row's key, e.g. arxiv-2401.01234."),
    knowledge_base: str = typer.Option(default=".", help="Knowledge base root whose source queue to update."),
) -> None:
    """Set a `failed` row back to `requested`. Any other row (absent, or not failed) has nothing to retry."""
    announce_knowledge_base(Path(knowledge_base).resolve())
    retried = _queue(knowledge_base).retry(key)
    if retried is None:
        typer.echo("nothing to retry")
        return
    typer.echo(_entry_line(retried))
