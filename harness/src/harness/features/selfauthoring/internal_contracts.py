"""The ports the self-authoring slice records and acts through."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_ai_harness.capability_creation import AuthoredCapability

    from harness.features.selfauthoring.domain import ActivationEvent, CustomAgent, CustomProfile, CustomStep


class ActivationLog(Protocol):
    """Where every act on a custom item is recorded, one activation event at a time."""

    def record(self, event: ActivationEvent) -> ActivationEvent:
        """Append `event` and hand it back.

        Args:
            event: The act to record.

        Returns:
            The same event.
        """
        ...


class CustomItems(Protocol):
    """The custom items of one item type, as the owner reviews them: listed, activated, disabled and deleted.

    Each act rewrites one manifest entry and records itself in the activation log.
    """

    def list_all(self) -> Sequence[AuthoredCapability | CustomStep | CustomAgent | CustomProfile]:
        """Every manifest entry, in insertion order.

        Returns:
            The entries.
        """
        ...

    def activate(self, name: str) -> bool:
        """Flip `name` active and record the act.

        Args:
            name: The name the item was written under.

        Returns:
            Whether an item had that name.

        Raises:
            ValueError: The item's last write failed validation. The message carries the stored error.
        """
        ...

    def deactivate(self, name: str) -> bool:
        """Flip `name` disabled and record the act.

        Args:
            name: The name the item was written under.

        Returns:
            Whether an item had that name.
        """
        ...

    def delete(self, name: str) -> bool:
        """Remove `name`'s manifest entry and its file, and record the act.

        Args:
            name: The name the item was written under.

        Returns:
            Whether an item had that name.

        Raises:
            ValueError: The item is still active. It has to be disabled first.
        """
        ...
