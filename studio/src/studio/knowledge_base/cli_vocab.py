"""The `studio vocab` command group, which governs the controlled vocabulary through its files.

Propose a facet value seen while authoring (`propose`), review the pending proposals (`list`), and either
add it to `_meta/vocabulary/facets.md` (`approve`) or drop it (`reject`). The commands touch only files, never
the database or the container. The entry module adds `vocab_app` to the root app as `studio vocab`.
"""

from __future__ import annotations

from pathlib import Path

import typer

from engram.indexing.contracts import ProposalUnknownError, VocabGovernance

from studio.knowledge_base.cli_support import _fail, announce_knowledge_base

vocab_app = typer.Typer(
    name="vocab", help="Propose, review, and approve new controlled-vocabulary values.", no_args_is_help=True
)


@vocab_app.command(name="propose")
def vocab_propose_cmd(
    *,
    facet: str = typer.Option(..., "--facet", help="Facet key the value belongs to (e.g. cuisine)."),
    value: str = typer.Option(..., "--value", help="The unregistered facet value to propose."),
    page: str = typer.Option(..., "--page", help="Id of the page where the value was seen."),
    knowledge_base: str = typer.Option(default=".", help="Knowledge base root."),
) -> None:
    """Record a pending `(facet, value)` proposal in `_meta/vocabulary/pending.yaml`.

    Proposing the same facet and value again keeps the existing proposal unchanged and adds no second one.
    """
    announce_knowledge_base(Path(knowledge_base).resolve())
    governance = VocabGovernance(Path(knowledge_base))
    proposal = governance.propose(facet=facet, value=value, page_id=page)
    typer.echo(f"Proposed {proposal.facet}={proposal.value!r} (seen at {proposal.page_id}, {proposal.proposed_at.isoformat()})")


@vocab_app.command(name="list")
def vocab_list_cmd(*, knowledge_base: str = typer.Option(default=".", help="Knowledge base root.")) -> None:
    """List every pending vocab proposal, oldest first (read-only)."""
    announce_knowledge_base(Path(knowledge_base).resolve())
    rows = VocabGovernance(Path(knowledge_base)).pending()
    if not rows:
        typer.echo("No pending vocab proposals.")
        return
    typer.echo(f"Pending vocab proposals: {len(rows)}")
    for row in rows:
        typer.echo(f"  {row.facet}={row.value!r}  seen at {row.page_id}  proposed {row.proposed_at.isoformat()}")


@vocab_app.command(name="approve")
def vocab_approve_cmd(
    *,
    facet: str = typer.Option(..., "--facet", help="Facet key of the proposal to approve."),
    value: str = typer.Option(..., "--value", help="Value of the proposal to approve."),
    knowledge_base: str = typer.Option(default=".", help="Knowledge base root."),
) -> None:
    """Add an approved value to `_meta/vocabulary/facets.md` and clear its pending proposal.

    A value already in the vocabulary leaves the file unchanged, and the pending proposal is cleared either way.
    """
    announce_knowledge_base(Path(knowledge_base).resolve())
    governance = VocabGovernance(Path(knowledge_base))
    try:
        governance.approve(facet=facet, value=value)
    except ProposalUnknownError as exc:
        _fail(str(exc))
    typer.echo(f"Approved {facet}={value!r}: registered and cleared from pending.")


@vocab_app.command(name="reject")
def vocab_reject_cmd(
    *,
    facet: str = typer.Option(..., "--facet", help="Facet key of the proposal to reject."),
    value: str = typer.Option(..., "--value", help="Value of the proposal to reject."),
    knowledge_base: str = typer.Option(default=".", help="Knowledge base root."),
) -> None:
    """Drop a pending proposal. The vocabulary file is never touched."""
    announce_knowledge_base(Path(knowledge_base).resolve())
    governance = VocabGovernance(Path(knowledge_base))
    try:
        governance.reject(facet=facet, value=value)
    except ProposalUnknownError as exc:
        _fail(str(exc))
    typer.echo(f"Rejected {facet}={value!r}: removed from pending, vocabulary untouched.")
