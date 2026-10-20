"""Unit specs for `engram_connectors.xai`, the shared xAI connection and how it is read out of a bundle."""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.config import KnowledgeBaseSettings, SourceResources

from engram_connectors.xai import XaiResource, xai_resource

if TYPE_CHECKING:
    from pathlib import Path


def _resources(tmp_path: Path, **settings: dict[str, str]) -> SourceResources:
    (tmp_path / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    return SourceResources(knowledge_base=KnowledgeBaseSettings(path=tmp_path), settings=settings)


def test_xai_resource_composes_on_a_bare_config() -> None:
    resource = XaiResource()
    assert resource.api_key == ""
    assert resource.model == "grok-4-fast-non-reasoning"


def test_xai_resource_carries_a_key_and_a_model() -> None:
    resource = XaiResource(api_key="xai-secret", model="grok-4-1-fast-non-reasoning")
    assert resource.api_key == "xai-secret"
    assert resource.model == "grok-4-1-fast-non-reasoning"


def test_an_absent_block_reads_as_no_connection(tmp_path: Path) -> None:
    assert xai_resource(_resources(tmp_path)) is None


def test_a_block_with_an_empty_key_reads_as_no_connection(tmp_path: Path) -> None:
    assert xai_resource(_resources(tmp_path, xai={"api_key": ""})) is None


def test_a_block_with_a_key_validates_into_the_resource(tmp_path: Path) -> None:
    resource = xai_resource(_resources(tmp_path, xai={"api_key": "xai-secret", "model": "grok-4-1-fast-non-reasoning"}))

    assert resource is not None
    assert resource.api_key == "xai-secret"
    assert resource.model == "grok-4-1-fast-non-reasoning"
