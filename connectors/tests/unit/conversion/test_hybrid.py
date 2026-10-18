"""Unit tests for `HybridConverter` — HTML-first with Azure DI fallback.

Two local fake converters stand in for the HTML and DI adapters: each records
whether it was called (and the `source_url` it received) and returns a canned
result, or raises. The tests assert the fallback control flow:

- HTML succeeds -> its result is returned and DI is never touched.
- HTML raises `ConversionError` -> DI is called and its result returned.
- the `source_url` hint is passed through to the HTML route.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from engram.sources.contracts import ConversionError, ConversionResult

from engram_connectors.conversion.contracts import HybridConverter

PDF_PATH = Path("paper.pdf")


@dataclass
class FakeConverter:
    """Records calls and returns `result`, or raises `error` when set."""

    result: ConversionResult = field(default_factory=lambda: ConversionResult(markdown="# fake\n", page_count=0, converter="fake"))
    error: Exception | None = None
    call_count: int = 0
    last_source_url: str | None = None

    async def to_markdown(self, pdf_path: Path, *, source_url: str | None = None) -> ConversionResult:
        """Record the call and `source_url`; raise `error` if scripted, else return `result`."""
        self.call_count += 1
        self.last_source_url = source_url
        if self.error is not None:
            raise self.error
        return self.result


async def test_returns_html_result_and_skips_di_when_html_succeeds() -> None:
    html = FakeConverter(result=ConversionResult(markdown="# html\n", page_count=0, converter="fake-html"))
    di = FakeConverter(result=ConversionResult(markdown="# di\n", page_count=3, converter="fake-di"))
    converter = HybridConverter(html, di)

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")

    assert result.markdown == "# html\n"
    assert html.call_count == 1
    assert di.call_count == 0


async def test_falls_back_to_di_when_html_raises_conversion_error() -> None:
    html = FakeConverter(error=ConversionError("not an arXiv HTML"))
    di = FakeConverter(result=ConversionResult(markdown="# di\n", page_count=7, converter="fake-di"))
    converter = HybridConverter(html, di)

    result = await converter.to_markdown(PDF_PATH, source_url="https://example.com/x.pdf")

    assert result.markdown == "# di\n"
    assert result.page_count == 7
    assert html.call_count == 1
    assert di.call_count == 1


async def test_passes_source_url_through_to_html_route() -> None:
    html = FakeConverter()
    di = FakeConverter()
    converter = HybridConverter(html, di)

    await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")

    assert html.last_source_url == "https://arxiv.org/abs/2412.15605"


async def test_propagates_non_conversion_errors_without_falling_back() -> None:
    html = FakeConverter(error=RuntimeError("network blew up"))
    di = FakeConverter()
    converter = HybridConverter(html, di)

    with pytest.raises(RuntimeError, match="network blew up"):
        await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")

    assert di.call_count == 0


async def test_chains_html_error_when_di_fallback_also_fails() -> None:
    html_error = ConversionError("not an arXiv HTML")
    di_error = ConversionError("azure DI quota exceeded")
    html = FakeConverter(error=html_error)
    di = FakeConverter(error=di_error)
    converter = HybridConverter(html, di)

    with pytest.raises(ConversionError, match="azure DI quota exceeded") as excinfo:
        await converter.to_markdown(PDF_PATH, source_url="https://example.com/x.pdf")

    assert excinfo.value is di_error
    assert excinfo.value.__cause__ is html_error
    assert html.call_count == 1
    assert di.call_count == 1
