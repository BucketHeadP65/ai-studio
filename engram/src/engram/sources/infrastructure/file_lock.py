"""The file lock a queue file is held under while it is rewritten: exclusive for a rewrite, shared for anything else."""

from __future__ import annotations

import fcntl

from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Generator
    from contextlib import AbstractContextManager
    from pathlib import Path


class QueueLock(Protocol):
    """How one queue file is held while it is rewritten."""

    def __call__(self, path: Path, *, exclusive: bool) -> AbstractContextManager[None]:
        """Hold `path`'s lock for the block: exclusive for a rewrite, shared for anything else.

        The lock is released when the block exits, whether it returns or raises.

        Args:
            path: The file the lock guards.
            exclusive: `True` for a rewrite, `False` for a shared hold.

        Returns:
            A context manager that holds the lock for its block.
        """
        ...


@contextmanager
def flock_lock(path: Path, *, exclusive: bool) -> Generator[None]:
    """`fcntl.flock` on `<queue dir>/.<queue name>.lock`, created beside the queue file and never removed.

    The lock is on a separate file because the queue file is replaced on every write, and a lock
    on a replaced file guards nothing. It is a dotfile, which the editors that browse a knowledge
    base's folders leave alone.

    Args:
        path: The file the lock guards.
        exclusive: `True` for a rewrite, `False` for a shared hold.

    Yields:
        Nothing. The lock is held for the block.
    """
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
