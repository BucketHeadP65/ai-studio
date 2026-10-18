"""The retention receipts of one knowledge base, one JSON file per request key under its hidden `.engram/` folder."""

from __future__ import annotations

import hashlib

from pathlib import Path, PurePosixPath

from core.files import atomic_write
from pydantic import ValidationError

from studio.knowledge_base.retention.domain import RetentionReceipt, RetentionRefusal
from studio.knowledge_base.retention.exceptions import RetentionRefusedError

RECEIPTS_DIR = PurePosixPath(".engram") / "retention"
"""Where a knowledge base keeps its retention receipts, relative to its root. Nothing under it is indexed."""


class RetentionReceipts:
    """One knowledge base's retention receipts, one file per request key, named by the key's digest."""

    def __init__(self, root: Path) -> None:
        """Hold the knowledge base root the receipts sit under.

        Args:
            root: The knowledge base's folder.
        """
        self._root = root

    def path_for(self, idempotency_key: str) -> Path:
        """Where one key's receipt lives. No key ever names a path itself.

        Args:
            idempotency_key: The request's key.

        Returns:
            The receipt file's path, named by the key's SHA-256.
        """
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return self._root / RECEIPTS_DIR / f"{digest}.json"

    def read(self, idempotency_key: str) -> RetentionReceipt | None:
        """This key's receipt.

        Args:
            idempotency_key: The request's key.

        Returns:
            The receipt, or `None` when there is none.

        Raises:
            RetentionRefusedError: A file is there and cannot be read, is not a receipt, or names a different key.
        """
        path = self.path_for(idempotency_key)
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as error:
            msg = f"the record of this request could not be read at {path}: {error}"
            raise RetentionRefusedError(RetentionRefusal.UNREADABLE_RECEIPT, msg) from error
        try:
            receipt = RetentionReceipt.model_validate_json(content)
        except ValidationError as error:
            msg = f"the record of this request at {path} is no longer readable: {error}"
            raise RetentionRefusedError(RetentionRefusal.UNREADABLE_RECEIPT, msg) from error
        if receipt.idempotency_key != idempotency_key:
            msg = "the record found for this request belongs to a different one"
            raise RetentionRefusedError(RetentionRefusal.UNREADABLE_RECEIPT, msg)
        return receipt

    def write(self, receipt: RetentionReceipt) -> None:
        """Put `receipt` on disk whole, replacing what its key's receipt said before.

        Args:
            receipt: The receipt to write.

        Raises:
            RetentionRefusedError: The receipt could not be written.
        """
        path = self.path_for(receipt.idempotency_key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, receipt.model_dump_json(indent=2))
        except OSError as error:
            msg = f"the record of this request could not be written at {path}: {error}"
            raise RetentionRefusedError(RetentionRefusal.TARGET_UNWRITABLE, msg) from error
