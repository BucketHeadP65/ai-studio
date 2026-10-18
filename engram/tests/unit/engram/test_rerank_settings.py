"""`RerankSettings` and `read_rerank_settings`: the `retrieval.rerank` block of the shared settings file."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from pydantic import ValidationError

from engram.config import RerankSettings, read_rerank_settings

if TYPE_CHECKING:
    from pathlib import Path


def test_the_defaults_rerank_snippets_one_request_per_hit() -> None:
    settings = RerankSettings()

    assert (settings.enabled, settings.passage, settings.requests) == (True, "snippet", "per_hit")


def test_the_block_is_read_from_the_settings_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"retrieval": {"dialect": "pg_textsearch", "rerank": {"passage": "chunk", "requests": "all_hits"}}}))

    settings = read_rerank_settings(path)

    assert (settings.passage, settings.requests) == ("chunk", "all_hits")


def test_a_file_without_the_block_reads_the_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"infrastructure": {}}))

    assert read_rerank_settings(path) == RerankSettings()


def test_the_default_caps_re_rank_requests_at_eight() -> None:
    assert RerankSettings().max_concurrent_requests == 8


def test_a_cap_below_one_is_refused() -> None:
    with pytest.raises(ValidationError):
        RerankSettings(max_concurrent_requests=0)
