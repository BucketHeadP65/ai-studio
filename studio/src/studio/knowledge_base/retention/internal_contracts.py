"""The retention slice's own ports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from studio.knowledge_base.retention.domain import RetentionReceipt


class Receipts(Protocol):
    """One knowledge base's retention receipts, one per request key."""

    def read(self, idempotency_key: str) -> RetentionReceipt | None:
        """This key's receipt.

        Args:
            idempotency_key: The request's key.

        Returns:
            The receipt, or `None` when there is none.

        Raises:
            RetentionRefusedError: A receipt is there and cannot be read, is not a receipt, or names a different key.
        """
        ...

    def write(self, receipt: RetentionReceipt) -> None:
        """Put `receipt` down whole, replacing what its key's receipt said before.

        Args:
            receipt: The receipt to write.

        Raises:
            RetentionRefusedError: The receipt could not be written.
        """
        ...
