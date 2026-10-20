r"""The production bus writes its events to Postgres, never to memory (`src/studio/knowledge_base/container.py`).

Pinned through the real wireup graph, mirroring `test_container_sync.py`: the admin `AsyncEngine`
and the embedder are overridden with inert fakes so resolving the bus touches no Postgres or
Azure, and `PostgresOutbox` opens no connection at construction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.embedding import BatchEmbedder
from core.cqrs.contracts import MessageBus, PostgresOutbox
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.specs import research_spec
from sqlalchemy.ext.asyncio import AsyncEngine

from studio.knowledge_base.container import create_container
from studio.knowledge_base.settings import ApplicationSettings, KnowledgeBaseSettings, SourceSettings
from tests.support.http_app import write_settings_json

if TYPE_CHECKING:
    from pathlib import Path


def _knowledge_base_root(tmp_path: Path) -> Path:
    """A research knowledge base on disk, the shape `KnowledgeBaseSettings` accepts."""
    knowledge_base_root = tmp_path / "knowledge_base"
    (knowledge_base_root / "_meta").mkdir(parents=True)
    (knowledge_base_root / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
    return knowledge_base_root


def _settings(tmp_path: Path) -> ApplicationSettings:
    """A settings tree read from a real file under `tmp_path` — no env, no network."""
    return ApplicationSettings(
        json_file=write_settings_json(tmp_path / "application_settings.json"),
        sources=SourceSettings({"zotero": {"data_dir": str(tmp_path)}}),
    )


async def test_the_production_bus_uses_the_postgres_outbox(tmp_path: Path) -> None:
    container = create_container(_settings(tmp_path), knowledge_base=KnowledgeBaseSettings(path=_knowledge_base_root(tmp_path)))

    with (
        container.override.injectable(target=AsyncEngine, new=_StubEngine()),
        container.override.injectable(target=BatchEmbedder, new=FakeEmbedding()),
    ):
        bus = await container.get(MessageBus)

    assert isinstance(bus.outbox, PostgresOutbox)


class _StubEngine:
    """Inert stand-in for the admin `AsyncEngine` — never connects (wiring test only)."""
