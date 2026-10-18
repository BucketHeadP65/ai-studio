"""`PassthroughConverter`: the file a provider already rendered is the result, and an unreadable one is a refusal."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from engram.sources.contracts import ConversionError

from engram_connectors.conversion.contracts import PassthroughConverter

if TYPE_CHECKING:
    from pathlib import Path


async def test_the_rendered_file_is_the_result_under_the_given_label(tmp_path: Path) -> None:
    path = tmp_path / "yt-abc.md"
    path.write_text("# A talk\n\nThe task list survives compaction.\n", encoding="utf-8")

    result = await PassthroughConverter("intake").to_markdown(path)

    assert result.markdown.startswith("# A talk")
    assert result.converter == "intake"
    assert result.page_count == 1


async def test_a_cache_file_that_cannot_be_read_becomes_a_conversion_error(tmp_path: Path) -> None:
    """The provider failed to write its cache file, so the sync learns it from the converter's own refusal."""
    missing = tmp_path / "never-written.md"

    with pytest.raises(ConversionError, match=r"never-written\.md"):
        await PassthroughConverter("intake").to_markdown(missing)
