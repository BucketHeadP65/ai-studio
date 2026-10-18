"""`ensure_dataset`: the cache is trusted only once a whole archive has been extracted."""

from __future__ import annotations

import io
import zipfile

from typing import TYPE_CHECKING

import pytest

from engram_connectors.beir.infrastructure.download import ensure_dataset

if TYPE_CHECKING:
    from pathlib import Path


def _archive(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, data in members.items():
            bundle.writestr(name, data)
    return buffer.getvalue()


class _CountingFetcher:
    def __init__(self, archive: bytes) -> None:
        self.archive = archive
        self.urls: list[str] = []

    async def fetch_bytes(self, url: str, *, max_bytes: int) -> bytes:
        del max_bytes
        self.urls.append(url)
        return self.archive


async def test_a_cut_short_extraction_leaves_no_cache_and_the_next_call_downloads_again(tmp_path: Path) -> None:
    fetcher = _CountingFetcher(_archive({"tiny/corpus.jsonl": b"x" * 10, "tiny/queries.jsonl": b"y" * 5_000}))

    with pytest.raises(ValueError, match="100 bytes"):
        await ensure_dataset(fetcher, "tiny", tmp_path, max_extracted_bytes=100)

    assert not (tmp_path / "tiny" / "raw" / "tiny" / "corpus.jsonl").exists()

    raw = await ensure_dataset(fetcher, "tiny", tmp_path)

    assert (raw / "queries.jsonl").read_bytes() == b"y" * 5_000
    assert len(fetcher.urls) == 2


async def test_a_whole_cache_is_reused_without_a_download(tmp_path: Path) -> None:
    fetcher = _CountingFetcher(_archive({"tiny/corpus.jsonl": b"{}\n"}))

    first = await ensure_dataset(fetcher, "tiny", tmp_path)
    second = await ensure_dataset(fetcher, "tiny", tmp_path)

    assert first == second
    assert len(fetcher.urls) == 1
