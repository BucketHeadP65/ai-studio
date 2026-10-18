"""The activation log: one append-only file recording every act on a custom item, whatever its type."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from harness.features.selfauthoring.domain import ActivationEvent

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pydantic_ai_harness.capability_creation import AuthoredCapability

    from harness.features.selfauthoring.domain import (
        AgentActivated,
        CapabilityActivated,
        CustomAgent,
        CustomProfile,
        CustomStep,
        ProfileActivated,
        StepActivated,
    )
    from harness.features.selfauthoring.internal_contracts import ActivationLog
    from harness.settings import StateSettings

_EVENT: TypeAdapter[ActivationEvent] = TypeAdapter(ActivationEvent)


class NoActivations:
    """`ActivationLog` for custom items that are only read: nothing is flipped there, so nothing is recorded.

    A caller that lists, resolves or loads active items builds its custom items over this one, so a
    read never lands an act in the deployment's own log.
    """

    def record(self, event: ActivationEvent) -> ActivationEvent:
        """Hand `event` straight back, recorded nowhere.

        Args:
            event: The act that would have been recorded.

        Returns:
            The same event.
        """
        return event


class FileActivationLog:
    """`ActivationLog` over one append-only JSONL file. History is never edited, only extended."""

    def __init__(self, path: Path) -> None:
        """Hold the file every event is appended to.

        Args:
            path: The JSONL file. Its folder is created on the first write.
        """
        self._path = path

    def record(self, event: ActivationEvent) -> ActivationEvent:
        """Append `event` as one new line.

        Args:
            event: The act to record.

        Returns:
            The same event.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
        return event

    def events(self) -> tuple[ActivationEvent, ...]:
        """Every readable line, in order. A torn tail costs one line, never the read.

        Returns:
            The events, oldest first. Empty when the file cannot be read.
        """
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ()
        out: list[ActivationEvent] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                out.append(_EVENT.validate_json(line))
            except ValueError:
                continue
        return tuple(out)


def activations_under(state: StateSettings) -> ActivationLog:
    """The activation log under `state`'s home, the one file every custom item records through.

    Every caller builds its log here, so the CLI, the HTTP routes and the agent's own tools record the
    same acts in one place.

    Args:
        state: The deployment's state settings.

    Returns:
        The log at `state.activation_log`.
    """
    return FileActivationLog(state.activation_log)


def record_overwrite(
    previous: AuthoredCapability | CustomStep | CustomAgent | CustomProfile | None,
    name: str,
    *,
    event_type: type[CapabilityActivated | StepActivated | AgentActivated | ProfileActivated],
    activations: ActivationLog,
    revoke: Callable[[str], bool] | None,
) -> None:
    """Record an overwrite of an active custom item as its deactivation from `agent`.

    Nothing happens unless `previous` was active. The deactivation is recorded first, then `revoke` runs.

    Args:
        previous: The item's manifest entry before the write, or `None` when the name was new.
        name: The item's name.
        event_type: The activation event of the item's type.
        activations: Where the deactivation is recorded.
        revoke: What withdraws anything else that rests on the old code, such as a capability's promotion.
            `None` when nothing rests on it.
    """
    if previous is None or previous.status != "active":
        return
    activations.record(event_type(name=name, action="deactivate", via="agent"))
    if revoke is not None:
        revoke(name)
