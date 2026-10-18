"""The `studio decisions` and `studio decide` commands, which work a knowledge base's open decisions.

`studio decisions` reads and `studio decide` writes the page files of the decision page types the knowledge
base's schema declares, with no database and no container. `decisions_app` has no name of its own, so mounting it on the root
`app` puts both commands at the top level, never under a `decisions` group.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import typer

from engram.retrieval.contracts import decide_page, list_decisions

from studio.knowledge_base.cli_support import _fail, announce_knowledge_base, load_knowledge_base_schema_or_fail

if TYPE_CHECKING:
    from engram.kb import contracts as kb
    from engram.retrieval.contracts import DecidedPage

decisions_app = typer.Typer()

_LINK_OPTION = typer.Option([], "--link", help="A link field to write, as field=id. Repeatable, ids accumulate under the same field.")


def _resolve(knowledge_base: str) -> tuple[Path, kb.KnowledgeBaseSchema]:
    """The knowledge base root and its schema, exiting 1 when the root has no `knowledge-base-schema.json`."""
    knowledge_base_path = Path(knowledge_base)
    return knowledge_base_path, load_knowledge_base_schema_or_fail(knowledge_base_path)


def _row_line(row: DecidedPage) -> str:
    return f"{row.id}  [{row.page_type}]  {row.title}"


def _warn_skipped(skipped: tuple[str, ...]) -> None:
    """Echo one stderr line per page that failed to parse, so a listing still reports them."""
    for name in skipped:
        typer.echo(f"skipped {name}: not a valid page", err=True)


def _parse_links(pairs: list[str]) -> dict[str, list[str]]:
    """Parse repeated `--link field=id` options into `{field: [id, ...]}`, accumulating repeats of a field."""
    links: dict[str, list[str]] = {}
    for pair in pairs:
        field, sep, page_id = pair.partition("=")
        if not sep:
            msg = f"--link expects field=id, got {pair!r}"
            raise typer.BadParameter(msg)
        links.setdefault(field, []).append(page_id)
    return links


@decisions_app.command(name="decisions")
def decisions_cmd(
    knowledge_base: str = typer.Option(default=".", help="Knowledge base root whose open decisions to list."),
    *,
    count: bool = typer.Option(default=False, help="Print only the row count."),
) -> None:
    """Print every open-decision page, one line per row, or just the row count with `--count`.

    A page that fails to parse is skipped and reported as one warning line on stderr, rather than
    failing the whole listing.
    """
    announce_knowledge_base(Path(knowledge_base).resolve(), err=True)  # stdout carries the row count with --count, a scriptable value
    knowledge_base_path, schema = _resolve(knowledge_base)
    listing = list_decisions(knowledge_base_path, schema)
    _warn_skipped(listing.skipped)
    if count:
        typer.echo(str(len(listing.rows)))
        return
    if not listing.rows:
        typer.echo("No decisions.")
        return
    for row in listing.rows:
        typer.echo(_row_line(row))


@decisions_app.command(name="decide")
def decide_cmd(
    page_id: str = typer.Argument(..., help="The page's id, e.g. flag.try-planning-reminders."),
    status: str = typer.Option(..., "--status", help="The status to move the page to."),
    link: list[str] = _LINK_OPTION,
    reason: str = typer.Option(default="", help="A reason recorded on the page's decision_reason: line."),
    knowledge_base: str = typer.Option(default=".", help="Knowledge base root the page lives under."),
) -> None:
    """Move one page to `--status`, write any given `--link` fields, and print its new line."""
    announce_knowledge_base(Path(knowledge_base).resolve())
    knowledge_base_path, schema = _resolve(knowledge_base)
    if not status:
        _fail("--status is empty. Give one of the statuses the page's page type allows")
    try:
        row = decide_page(knowledge_base_path, schema, page_id, status, links=_parse_links(link), reason=reason)
    except (LookupError, ValueError) as exc:
        _fail(str(exc))
    typer.echo(_row_line(row))
    typer.echo(f"written to the page, the indexed knowledge base reads it after the next index: studio index --knowledge-base {knowledge_base_path}", err=True)
