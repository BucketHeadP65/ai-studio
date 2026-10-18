"""An in-memory `DocumentConverter` for reconcile tests.

Returns a canned `ConversionResult` and counts calls, recording the paths and the last
`source_url` hint. Any path in the optional `error_on` set raises `exc` instead of converting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from engram.sources.contracts import ConversionResult

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class FakeDocumentConverter:
    """Counts `to_markdown` calls and raises for paths in `error_on`.

    The result carries the `converter` label (`"fake"` by default) and the `assets` set on the fake.
    """

    error_on: set[Path] = field(default_factory=set)
    exc: Exception | None = None
    converter: str = "fake"
    assets: dict[str, bytes] = field(default_factory=dict)
    call_count: int = 0
    paths: list[Path] = field(default_factory=list)
    last_source_url: str | None = None

    async def to_markdown(self, pdf_path: Path, *, source_url: str | None = None) -> ConversionResult:
        """Record the call and answer canned Markdown.

        Args:
            pdf_path: The path to convert, recorded.
            source_url: The web URL hint, recorded as `last_source_url`.

        Returns:
            A one-page result with canned Markdown, `converter` and `assets`.

        Raises:
            Exception: `exc`, or a `RuntimeError` when `exc` is unset, for a path in `error_on`.
        """
        self.call_count += 1
        self.paths.append(pdf_path)
        self.last_source_url = source_url
        if pdf_path in self.error_on:
            raise self.exc if self.exc is not None else RuntimeError("scripted converter failure")
        return ConversionResult(markdown="# Faked\n", page_count=1, converter=self.converter, assets=self.assets)
