"""A converter for sources whose connector already rendered the markdown: the file's bytes are the result."""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio

from engram.sources.contracts import ConversionError, ConversionResult

if TYPE_CHECKING:
    from pathlib import Path


class PassthroughConverter:
    """`DocumentConverter` that reads `pdf_path` as UTF-8 markdown and labels it `label`."""

    def __init__(self, label: str) -> None:
        """Store the converter label.

        Args:
            label: The converter label the mirror frontmatter carries.
        """
        self._label = label

    async def to_markdown(self, pdf_path: Path, *, source_url: str | None = None) -> ConversionResult:
        """Read back the markdown a connector already rendered at `pdf_path`.

        Args:
            pdf_path: The file the connector wrote the markdown to.
            source_url: Unused.

        Returns:
            The file's text under this converter's label, `page_count` 1 and no assets.

        Raises:
            ConversionError: `pdf_path` cannot be read (the connector failed to write its cache file).
        """
        del source_url
        try:
            text = await anyio.Path(pdf_path).read_text(encoding="utf-8")
        except OSError as error:
            raise ConversionError(str(error)) from error
        return ConversionResult(markdown=text, page_count=1, converter=self._label, assets={})
