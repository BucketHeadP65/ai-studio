"""Fill the MultiHop-RAG cache: download the dataset's two files once from a pinned revision and check their hashes."""

from __future__ import annotations

import hashlib
import shutil

from typing import TYPE_CHECKING

from core.files import atomic_write_bytes

from engram_connectors.multihop_rag.domain.dataset import ARTICLES_FILE, QUESTIONS_FILE, DatasetHashes
from engram_connectors.multihop_rag.exceptions import MultihopRagDatasetError

if TYPE_CHECKING:
    from pathlib import Path

    from core.net import ByteFetcher

DATASET_URL = "https://huggingface.co/datasets/yixuantt/MultiHopRAG/resolve/71ac0d0bd1f951d2d6b70311f7d2ae404e1ffa82/{file}"
"""Where each dataset file is published, at the one revision the connector reads."""

PINNED_HASHES = DatasetHashes(articles="20b61b5ab84de84a", questions="03cfb4926461f868")
"""The SHA-256 prefixes of the two files at the pinned revision."""

MAX_FILE_BYTES = 64 * 1024 * 1024
"""The largest dataset file the download accepts."""


async def ensure_dataset(fetcher: ByteFetcher, cache_dir: Path, hashes: DatasetHashes) -> Path:
    """The directory holding the dataset's two files, downloading them only when the cache lacks them.

    Both files land in a temporary folder that becomes the cache only once both match their hashes, so a
    download cut short or refused leaves no cache and the next call downloads again.

    Args:
        fetcher: The guarded download.
        cache_dir: The connector's cache root.
        hashes: The SHA-256 prefix each file must start with.

    Returns:
        `cache_dir/raw`, the directory with both dataset files.

    Raises:
        MultihopRagDatasetError: When a downloaded file's SHA-256 does not start with its pinned prefix.
        FetchError: When a dataset file cannot be downloaded.
    """
    raw = cache_dir / "raw"
    if (raw / ARTICLES_FILE).is_file() and (raw / QUESTIONS_FILE).is_file():
        return raw
    staging = cache_dir / ".raw.partial"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        for name, prefix in ((ARTICLES_FILE, hashes.articles), (QUESTIONS_FILE, hashes.questions)):
            data = await fetcher.fetch_bytes(DATASET_URL.format(file=name), max_bytes=MAX_FILE_BYTES)
            digest = hashlib.sha256(data).hexdigest()
            if not digest.startswith(prefix):
                msg = f"{name} at the pinned revision has SHA-256 {digest}, which does not start with the pinned {prefix}"
                raise MultihopRagDatasetError(msg)
            atomic_write_bytes(staging / name, data)
        shutil.rmtree(raw, ignore_errors=True)
        staging.rename(raw)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return raw
