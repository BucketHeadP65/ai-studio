"""Fill the BEIR cache: download a dataset's archive once and extract it beside the documents' cache files."""

from __future__ import annotations

import shutil

from typing import TYPE_CHECKING

from core.files import atomic_write_bytes, extract_zip

if TYPE_CHECKING:
    from pathlib import Path

    from core.net import ByteFetcher

DATASET_URL = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/{dataset}.zip"
"""Where the BEIR authors publish each dataset as one zip archive."""

MAX_ARCHIVE_BYTES = 500 * 1024 * 1024
"""The largest archive the download accepts."""

MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024
"""The most the archive may expand to."""


async def ensure_dataset(fetcher: ByteFetcher, dataset: str, cache_dir: Path, *, max_extracted_bytes: int = MAX_EXTRACTED_BYTES) -> Path:
    """The directory holding `dataset`'s files, downloading and extracting the archive only when it is missing.

    The archive is extracted into a temporary folder that becomes the cache only once every member is out, so an
    extraction cut short leaves no cache and the next call downloads again.

    Args:
        fetcher: The guarded download.
        dataset: The BEIR dataset name.
        cache_dir: The connector's cache root.
        max_extracted_bytes: The most the archive may expand to.

    Returns:
        `cache_dir/<dataset>/raw/<dataset>`, the directory with `corpus.jsonl`, `queries.jsonl` and `qrels/`.

    Raises:
        ValueError: When the archive holds a member outside its folder or expands past `max_extracted_bytes`.
    """
    raw = cache_dir / dataset / "raw"
    extracted = raw / dataset
    if (extracted / "corpus.jsonl").is_file():
        return extracted
    raw.mkdir(parents=True, exist_ok=True)
    staging = raw / f".{dataset}.partial"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        archive = staging / f"{dataset}.zip"
        atomic_write_bytes(archive, await fetcher.fetch_bytes(DATASET_URL.format(dataset=dataset), max_bytes=MAX_ARCHIVE_BYTES))
        extract_zip(archive, staging, max_bytes=max_extracted_bytes)
        shutil.rmtree(extracted, ignore_errors=True)
        (staging / dataset).rename(extracted)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return extracted
