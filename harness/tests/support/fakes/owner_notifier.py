"""A `Notifier` that keeps what the owner would have been told, for a test that reads it."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeOwnerNotifier:
    """`Notifier` recording every `(title, message)` and answering `delivers`."""

    delivers: bool = True
    sent: list[tuple[str, str]] = field(default_factory=list)

    async def notify(self, title: str, message: str) -> bool:
        self.sent.append((title, message))
        return self.delivers
