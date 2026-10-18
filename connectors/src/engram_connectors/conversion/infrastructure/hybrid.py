"""A converter that tries the arXiv HTML route first and falls back to Azure DI over the PDF."""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.sources.contracts import ConversionError

if TYPE_CHECKING:
    from pathlib import Path

    from engram.sources.contracts import ConversionResult, DocumentConverter


class HybridConverter:
    """`DocumentConverter` composing an HTML-first converter with a PDF (Azure DI) fallback.

    It falls back to the DI converter only when the HTML converter raises `ConversionError`.
    Every other exception propagates unchanged.
    """

    def __init__(self, html: DocumentConverter, fallback: DocumentConverter) -> None:
        """Store the HTML-first converter and the Azure DI fallback.

        Args:
            html: The preferred converter (arXiv HTML route).
            fallback: The fallback converter (Azure DI over the PDF).
        """
        self._html = html
        self._fallback = fallback

    async def to_markdown(self, pdf_path: Path, *, source_url: str | None = None) -> ConversionResult:
        """Convert through the HTML route, falling back to Azure DI on `ConversionError`.

        Args:
            pdf_path: Absolute path to the PDF (used by the DI fallback).
            source_url: The item's web URL, forwarded to both converters. The
                HTML route needs it to resolve the arXiv id.

        Returns:
            The HTML converter's result, or the DI converter's result when the
            HTML route raised `ConversionError`.

        Raises:
            ConversionError: If the DI fallback also fails.
            Exception: Any other error from the HTML route propagates unchanged.
        """
        try:
            return await self._html.to_markdown(pdf_path, source_url=source_url)
        except ConversionError as html_error:
            try:
                return await self._fallback.to_markdown(pdf_path, source_url=source_url)
            except ConversionError as fallback_error:
                raise fallback_error from html_error
