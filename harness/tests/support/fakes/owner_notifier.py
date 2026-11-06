"""A `Notifier` that keeps what the owner would have been told, for a test that reads it."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeOwnerNotifier:
    """`Notifier` recording every `(title, message)` and answering `delivers`, or raising `failure` after recording it when set."""

    delivers: bool = True
    sent: list[tuple[str, str]] = field(default_factory=list)
    failure: Exception | None = None

    async def notify(self, title: str, message: str) -> bool:
        self.sent.append((title, message))
        if self.failure is not None:
            raise self.failure
        return self.delivers
