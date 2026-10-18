"""Fake for the source queue's own file lock: a recording stand-in for `flock_lock`."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path


@dataclass
class FakeQueueLock:
    """Stands in for `SourceQueue`'s file lock: records every hold, exclusive or not.

    `before_first` is another process getting the lock first and landing its own rows: it runs
    inside the first hold before the block, so a queue that read the file before taking the lock
    loses those rows and the test says so.
    """

    holds: list[bool] = field(default_factory=list)
    before_first: Callable[[], None] | None = None

    @contextmanager
    def __call__(self, path: Path, *, exclusive: bool) -> Generator[None]:
        """Record this hold, run `before_first` on the first one, and yield.

        Args:
            path: The lock file path, ignored.
            exclusive: Whether the hold is exclusive, recorded in `holds`.

        Yields:
            Nothing, while the hold is open.
        """
        del path
        first = not self.holds
        self.holds.append(exclusive)
        if first and self.before_first is not None:
            self.before_first()
        yield
