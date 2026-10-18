"""Fake Summarizer: deterministic output, recorded calls as public state."""

from __future__ import annotations


class FakeSummarizer:
    """Deterministic in-memory `Summarizer` stand-in, records every title it was asked to summarize."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.model_id = "fake-summarizer"

    async def summarize(self, *, title: str, body: str) -> str:
        """Return a deterministic `"Summary of {title}."`, recording `title` in call order.

        Args:
            title: The title to summarize.
            body: The body, which is ignored.

        Returns:
            The summary.
        """
        self.calls.append(title)
        return f"Summary of {title}."
