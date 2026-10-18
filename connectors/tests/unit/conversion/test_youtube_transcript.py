"""Unit tests for the `youtube-transcript` `PassthroughConverter`, the pass-through `DocumentConverter` for YouTube items.

`YouTubeConnector` renders the segmented transcript to `pdf_path` before the sync handler ever calls
`to_markdown` (see `engram.sources.youtube.provider`), so this converter's whole job is reading that
file back verbatim and stamping the `"youtube-transcript"` provenance label.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from engram.sources.contracts import ConversionError

from engram_connectors.conversion.contracts import PassthroughConverter

if TYPE_CHECKING:
    from pathlib import Path


async def test_returns_the_file_content_as_markdown(tmp_path: Path) -> None:
    cache_file = tmp_path / "abc123XYZ_-.md"
    cache_file.write_text("## 1 [00:00] Opening remarks\n\nHello world.\n", encoding="utf-8")
    converter = PassthroughConverter("youtube-transcript")

    result = await converter.to_markdown(cache_file, source_url="https://www.youtube.com/watch?v=abc123XYZ_-")

    assert result.markdown == "## 1 [00:00] Opening remarks\n\nHello world.\n"
    assert result.converter == "youtube-transcript"
    assert result.assets == {}


async def test_ignores_source_url(tmp_path: Path) -> None:
    cache_file = tmp_path / "video.md"
    cache_file.write_text("## 1 [00:00] Title\n\nBody.\n", encoding="utf-8")
    converter = PassthroughConverter("youtube-transcript")

    with_url = await converter.to_markdown(cache_file, source_url="https://www.youtube.com/watch?v=xyz")
    without_url = await converter.to_markdown(cache_file)

    assert with_url.markdown == without_url.markdown


async def test_raises_conversion_error_when_the_cache_file_is_missing(tmp_path: Path) -> None:
    converter = PassthroughConverter("youtube-transcript")

    with pytest.raises(ConversionError):
        await converter.to_markdown(tmp_path / "missing.md")
