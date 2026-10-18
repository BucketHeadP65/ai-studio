"""Steps this host registers for every workflow, which a workflow file may name but never defines."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from harness.adapters.notify import NoopNotifier

if TYPE_CHECKING:
    from harness.adapters.notify import Notifier


class NotifyOwner(Protocol):
    """The `notify_owner` step as a caller holds it."""

    async def __call__(self, collection: str, written: int, needs_you: int = 0) -> str:
        """Say how many pages await review and how many repairs need the owner, notifying when any do.

        Args:
            collection: The knowledge base the run worked in.
            written: How many pages the run wrote for the owner's review.
            needs_you: How many repairs the run could not finish or must not do.

        Returns:
            One sentence saying what awaits the owner and whether the notification went out.
        """
        ...


def _awaiting(collection: str, written: int, needs_you: int) -> str:
    """The sentence that says what awaits the owner."""
    said = f"{written} pages awaiting review in {collection}"
    if needs_you == 1:
        return f"{said}, 1 repair needs you"
    if needs_you > 1:
        return f"{said}, {needs_you} repairs need you"
    return said


def notify_owner_step(notifier: Notifier) -> NotifyOwner:
    """Build the `notify_owner` step, which says what a run left for the owner and notifies when anything is.

    A failed notification is reported in the step's reply and never raised into the run.

    Args:
        notifier: Where the desktop notification is posted.

    Returns:
        The step function.
    """

    async def notify_owner(collection: str, written: int, needs_you: int = 0) -> str:
        """Tell the owner what awaits them in `collection`, with a desktop notification when any page or repair does."""
        said = _awaiting(collection, written, needs_you)
        if written <= 0 and needs_you <= 0:
            return said
        if isinstance(notifier, NoopNotifier):
            return f"{said}, no notifier on this host"
        try:
            delivered = await notifier.notify("Awaiting your review", said)
        except Exception as exc:  # noqa: BLE001 (any notifier failure is reported in the reply, never raised into the run)
            return f"{said}, notification failed: {exc}"
        return f"{said}, notified" if delivered else f"{said}, notification failed"

    return notify_owner
