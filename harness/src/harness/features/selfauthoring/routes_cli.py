"""The `harness` CLI verbs: the owner's review and activation of the agent's own custom items.

`harness.cli` adds `serve` and its other verbs beside these. Every command imports the service and the
file adapters inside its own body, so importing the CLI loads neither of them until a verb runs. No verb
loads FastAPI or Wireup.

`capability`, `step`, `agent` and `profile` are the item types. Workflows are not one: they stay in
their own content-addressed library, behind `save_workflow`. `promote` and `demote` act on
capabilities only, joining one to the registry a custom `AgentSpec` document declares capabilities
from, and the agent has no equivalent of either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

import typer

from harness.features.selfauthoring.domain import CUSTOM_ITEM_DIRECTORIES
from harness.features.selfauthoring.exceptions import PromotionError, SelectionError
from harness.settings import HarnessSettings, console_is_closed, custom_root

if TYPE_CHECKING:
    from harness.features.selfauthoring.infrastructure.capabilities import FileCustomCapabilities
    from harness.features.selfauthoring.internal_contracts import ActivationLog, CustomItems
    from harness.settings import StateSettings

harness_app = typer.Typer(
    name="harness",
    help="Review, activate, and select the agent's own custom capabilities, agents, scripts, and profiles.",
    no_args_is_help=True,
)


CONSOLE_IS_THE_OWNERS = (
    "Refused: this changes what every later turn can do, so it is the owner's to make. Ask the person for it instead of running it here."
)
"""What a console verb answers a caller with nobody to ask. Addressed to the agent reading it, and it
names the next move rather than only the refusal."""


def _refuse_without_a_person() -> None:
    """Stop a console verb reached from a shell no person is watching, such as a delegate's or a scheduled step's."""
    if console_is_closed():
        fail(CONSOLE_IS_THE_OWNERS)


def fail(message: str) -> NoReturn:
    """Echo `message` and exit with code 1.

    Args:
        message: The refusal to print.

    Raises:
        typer.Exit: Always, with code 1.
    """
    typer.echo(message)
    raise typer.Exit(code=1)


def _activations(state: StateSettings) -> ActivationLog:
    """Where a console verb's own act is recorded, under `state`'s home."""
    from harness.features.selfauthoring.infrastructure.activation_log import activations_under  # noqa: PLC0415 (the activation log loads only when a verb runs)

    return activations_under(state)


def _custom_items(item_type: str) -> CustomItems:
    """The custom items of `item_type`, refusing by name an item type that is not one of the four."""
    from harness.features.selfauthoring.service import custom_items_named  # noqa: PLC0415 (the service loads only when a verb runs)

    settings = HarnessSettings()
    items = custom_items_named(item_type, custom_root(settings.self_authoring), activations=_activations(settings.state))
    if items is None:
        fail(f"unknown custom-item kind {item_type!r}; only {', '.join(CUSTOM_ITEM_DIRECTORIES)} are wired so far")
    return items


def _custom_capabilities() -> FileCustomCapabilities:
    """The custom capabilities promotion acts on, the only item type it has."""
    from harness.features.selfauthoring.service import custom_items_for  # noqa: PLC0415 (the service loads only when a verb runs)

    settings = HarnessSettings()
    return custom_items_for("capability", custom_root(settings.self_authoring), activations=_activations(settings.state))


@harness_app.command(name="list")
def list_cmd(kind: str = typer.Argument(..., help="Custom kind: capability, agent, step, profile.")) -> None:
    """List every custom item of `kind`, with its status and any validation error."""
    from harness.features.selfauthoring.service import descriptor_of  # noqa: PLC0415 (the service loads only when a verb runs)

    items = _custom_items(kind)
    records = items.list_all()
    if not records:
        typer.echo(f"No custom {kind} yet.")
        return
    for record in records:
        suffix = f". ERROR: {record.last_error}" if record.last_error else ""
        typer.echo(f"{record.name}  [{record.status}]  {descriptor_of(record)}{suffix}")


@harness_app.command(name="activate")
def activate_cmd(
    kind: str = typer.Argument(..., help="Custom kind: capability, agent, step, profile."),
    name: str = typer.Argument(..., help="Name of the custom item to activate."),
) -> None:
    """Activate `name`: it loads on the next agent run. Records the activation.

    Refused, naming the stored error, when the last write of `name` failed validation. Activating
    broken code is never the fix.
    """
    _refuse_without_a_person()
    items = _custom_items(kind)
    try:
        activated = items.activate(name)
    except ValueError as exc:
        fail(str(exc))
    if not activated:
        fail(f"No custom {kind} named {name!r}.")
    typer.echo(f"Activated {kind} {name!r}.")


@harness_app.command(name="disable")
def disable_cmd(
    kind: str = typer.Argument(..., help="Custom kind: capability, agent, step, profile."),
    name: str = typer.Argument(..., help="Name of the custom item to disable."),
) -> None:
    """Disable `name`: it stops loading on the next agent run. Records the deactivation."""
    _refuse_without_a_person()
    items = _custom_items(kind)
    if not items.deactivate(name):
        fail(f"No custom {kind} named {name!r}.")
    typer.echo(f"Disabled {kind} {name!r}.")


@harness_app.command(name="delete")
def delete_cmd(
    kind: str = typer.Argument(..., help="Custom kind: capability, agent, step, profile."),
    name: str = typer.Argument(..., help="Name of the custom item to delete."),
) -> None:
    """Delete `name` outright: its manifest entry and its source file. Records the deletion.

    Refused while `name` is active, so the composition never changes as a side effect of tidying the
    library. Disable it first.
    """
    _refuse_without_a_person()
    items = _custom_items(kind)
    try:
        deleted = items.delete(name)
    except ValueError as exc:
        fail(str(exc))
    if not deleted:
        fail(f"No custom {kind} named {name!r}.")
    typer.echo(f"Deleted {kind} {name!r}.")


@harness_app.command(name="plugins")
def plugins_cmd(
    off: str = typer.Option("", "--off", help="Turn the named plugin off. It hides on the very next turn."),
    on: str = typer.Option("", "--on", help="Turn the named plugin on. It shows on the very next turn."),
) -> None:
    """List the agent's built-in plugins, or flip one with --on/--off. Flips need no restart."""
    from harness import plugins  # noqa: PLC0415 (the plugins module loads only when this verb runs)

    state = HarnessSettings().state
    if off or on:
        # Listing is a read and stays open to anyone. Flipping one decides what every later turn holds.
        _refuse_without_a_person()
    for name, enabled in ((off, False), (on, True)):
        if not name:
            continue
        try:
            plugins.set_plugin(state, name, enabled=enabled)
        except ValueError as exc:
            fail(str(exc))
        typer.echo(f"Turned plugin {name!r} {'on' if enabled else 'off'}; it takes effect on the next turn.")
    if off or on:
        return
    disabled = plugins.disabled_plugins(state)
    for plugin in plugins.TOGGLEABLE:
        typer.echo(f"{plugin.name}  [{'off' if plugin.name in disabled else 'on'}]  {plugin.description}")
    for plugin in plugins.ALWAYS_ON:
        typer.echo(f"{plugin.name}  [always on]  {plugin.description}")


@harness_app.command(name="profile")
def profile_cmd(
    name: str = typer.Argument("", help="Active profile to put in effect. Empty with no --clear just prints the selection."),
    *,
    clear: bool = typer.Option(default=False, help="Clear the selection. The hand-set plugins govern again."),
) -> None:
    """Select which profile governs the composition, or show or clear the selection.

    Takes effect immediately, including for a conversation the server may be holding. The selection
    route in AI Factory Studio refuses mid-conversation, and this verb trusts the operator.
    """
    if name or clear:
        # Printing the selection is a read. Changing it governs every session afterwards.
        _refuse_without_a_person()
    from harness.features.selfauthoring.service import select_profile, selected_profile_name  # noqa: PLC0415 (the service loads only when a verb runs)

    settings = HarnessSettings()
    root = custom_root(settings.self_authoring)
    if clear:
        select_profile(None, root=root, state=settings.state)
        typer.echo("Selection cleared. The hand-set plugins govern again.")
        return
    if not name:
        selected = selected_profile_name(settings.state)
        typer.echo(f"Selected profile: {selected}" if selected else "No profile selected. The hand-set plugins govern.")
        return
    try:
        select_profile(name, root=root, state=settings.state)
    except SelectionError as exc:
        fail(str(exc))
    typer.echo(f"Profile {name!r} is in effect from the next turn onward.")


@harness_app.command(name="promote")
def promote_cmd(
    kind: str = typer.Argument(..., help="Custom kind: capability (promotion's only kind so far)."),
    name: str = typer.Argument(..., help="Name of the custom capability to promote."),
) -> None:
    """Promote `name` into the capability registry: a custom agent can then declare it by name.

    Requires `name` to already be active, and checks it against the promotion contract (a
    declared serialization name and a config dataclass declared on the class itself). Takes
    effect on the next session or environment build, never the current one. Records the act.
    """
    _refuse_without_a_person()
    if kind != "capability":
        fail(f"promotion applies only to capabilities, not {kind!r}. There is nothing to promote here yet.")
    from harness.features.selfauthoring.infrastructure.promotion import promote_capability  # noqa: PLC0415 (promotion loads only when a verb runs)

    try:
        record = promote_capability(_custom_capabilities(), name)
    except PromotionError as exc:
        fail(str(exc))
    typer.echo(
        f"Promoted capability {record.name!r} ({record.class_name}). Custom agents can now declare it by its "
        f"serialization name, from the next session or environment-build onward."
    )


@harness_app.command(name="demote")
def demote_cmd(
    kind: str = typer.Argument(..., help="Custom kind: capability (promotion's only kind so far)."),
    name: str = typer.Argument(..., help="Name of the custom capability to demote."),
) -> None:
    """Remove `name` from the capability registry: a custom agent can no longer declare it. Records the act."""
    _refuse_without_a_person()
    if kind != "capability":
        fail(f"promotion applies only to capabilities, not {kind!r}. There is nothing to demote here yet.")
    from harness.features.selfauthoring.infrastructure.promotion import demote_capability  # noqa: PLC0415 (promotion loads only when a verb runs)

    if not demote_capability(_custom_capabilities(), name):
        fail(f"Capability {name!r} is not currently promoted.")
    typer.echo(f"Demoted capability {name!r}.")
