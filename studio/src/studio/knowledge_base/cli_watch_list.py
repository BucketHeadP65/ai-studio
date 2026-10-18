"""The `studio watch-list` command group: what a knowledge base keeps an eye on for new sources.

Filesystem-only, no DB or container (mirroring `studio source-queue`): `add`, `list` and `remove`
operate directly on `<knowledge_base>/_meta/watch-list.yaml` via `WatchListFile`. `studio sync` scans every
entry on its next run through the scan handler. The `watch_list_app` sub-Typer is added to the root `app`
by the entry module, so every command keeps its `studio watch-list <name>` invocation.
"""

from __future__ import annotations

from pathlib import Path

import typer

from engram.sources.contracts import WatchEntry, WatchListFile, known_scanners

from studio.knowledge_base.cli_support import _fail, announce_knowledge_base
from studio.knowledge_base.watch_list import WATCH_LIST_PATH

watch_list_app = typer.Typer(name="watch-list", help="What a knowledge base watches for new sources.", no_args_is_help=True)

_SCANNER_HELP = "Which scanner reads the entry: `xml` for an Atom or RSS document, or a scanner an installed package registers."
_REF_HELP = "What that scanner reads: the feed URL for `xml`, an account handle for `x`."


def _watch_list(knowledge_base: str) -> WatchListFile:
    """The watch-list file at `<knowledge_base>/_meta/watch-list.yaml`."""
    return WatchListFile(Path(knowledge_base) / WATCH_LIST_PATH)


def _checked_scanner(raw: str) -> str:
    """`raw` itself once it names a scanner this machine has, else a clean refusal listing the ones it does."""
    known = known_scanners()
    if raw not in known:
        _fail(f"unknown scanner {raw!r}, expected one of {', '.join(known)}")
    return raw


def _entry_line(entry: WatchEntry) -> str:
    scanned = entry.last_scanned.isoformat() if entry.last_scanned is not None else "never scanned"
    return f"{entry.scanner}  {entry.ref}  last scanned {scanned}"


@watch_list_app.command(name="add")
def watch_list_add_cmd(
    scanner: str = typer.Argument(..., help=_SCANNER_HELP),
    ref: str = typer.Argument(..., help=_REF_HELP),
    max_items: int = typer.Option(default=15, help="Cap on items pulled from this entry per scan."),
    knowledge_base: str = typer.Option(default=".", help="Knowledge base root whose watch list to append to."),
) -> None:
    """Append an entry to the knowledge base's watch list, reporting its scanner, ref and last scan.

    Idempotent on `(scanner, ref)`: an entry already on the list is reported as-is, never duplicated.
    """
    announce_knowledge_base(Path(knowledge_base).resolve())
    entry = _watch_list(knowledge_base).add(_checked_scanner(scanner), ref, max_items=max_items)
    typer.echo(_entry_line(entry))


@watch_list_app.command(name="list")
def watch_list_list_cmd(
    knowledge_base: str = typer.Option(default=".", help="Knowledge base root whose watch list to print."),
) -> None:
    """Print every watched entry, in file order."""
    announce_knowledge_base(Path(knowledge_base).resolve())
    entries = _watch_list(knowledge_base).entries()
    if not entries:
        typer.echo("The watch list is empty.")
        return
    for entry in entries:
        typer.echo(_entry_line(entry))


@watch_list_app.command(name="remove")
def watch_list_remove_cmd(
    scanner: str = typer.Argument(..., help=_SCANNER_HELP),
    ref: str = typer.Argument(..., help=_REF_HELP),
    knowledge_base: str = typer.Option(default=".", help="Knowledge base root whose watch list to edit."),
) -> None:
    """Drop an entry from the knowledge base's watch list."""
    announce_knowledge_base(Path(knowledge_base).resolve())
    if not _watch_list(knowledge_base).remove(scanner, ref):
        _fail(f"no such watch-list entry: {scanner} {ref}")
    typer.echo(f"{scanner}  {ref}  removed")
