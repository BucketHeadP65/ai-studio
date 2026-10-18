"""A deployment on the temporal engine composes its chat session.

The chat agent runs in this process and never inside a durable workflow, so it carries no durability
capability on either engine. Proven by running `harness_session` to the end with the temporal engine
selected, every builder that would open a database or an embedding client faked at the seam
`test_library_composition.py` fakes.
"""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from core.ai.disclosure.contracts import ProgressiveDisclosure
from core.settings import DatabaseSettings, InfrastructureSettings
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai_harness.memory import InMemoryStore

import harness.composition

from harness.adapters.notify import NoopNotifier
from harness.adapters.store import DatabaseBinding
from harness.adapters.studio_http import studio_http_client
from harness.composition import harness_session
from harness.domain.contracts import EmbeddingContract
from harness.features.running.infrastructure.durable import DurableTurns
from harness.features.running.spend_report import SpendReporter
from harness.features.scheduling.contracts import Scheduler
from harness.session import Runtime, SessionPool
from harness.settings import (
    HarnessSettings,
    KnowledgeBaseGuidanceSettings,
    LibrarySearchSettings,
    RunSettings,
    SelfAuthoringSettings,
    StoreSettings,
    WebSearchSettings,
)
from tests.support.environment import fake_plugins
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.request_parameters import FakeRequestParametersSink
from tests.support.fakes.schedules import FakeSchedules
from tests.support.fakes.sessions import APool
from tests.support.fakes.step_store import FakeContinuationCheckpoints, FakeStepStore
from tests.support.fakes.telemetry import FakeTelemetry
from tests.support.grants import app_knowledge_base_surface
from tests.support.runtime import a_runtime
from tests.support.state import a_session_log, a_state, a_transcript, records_under
from tests.support.tiers import tiers_under

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping, Sequence
    from pathlib import Path

    from engram import RetrievalKnowledgeBase
    from engram.authoring.contracts import AuthoringTools

_INFRASTRUCTURE = InfrastructureSettings(
    database=DatabaseSettings(url="postgresql+asyncpg://o:p@localhost/kb", reader_url="postgresql+asyncpg://r:p@localhost/kb")
)
"""Infrastructure settings the stubbed embedding contract carries, which nothing here connects with."""

A_FAKE_BINDING = DatabaseBinding(description="knowledge_base=fake:0/knowledge_bases (test)", named=True)
"""What this process would say it is about to open. Handed in, so nothing reads engram's settings file."""


class _FakePool:
    """Stands in for the `asyncpg.Pool` `store_connection` would open. Closing is all the session asks of it."""

    async def close(self) -> None:
        """Nothing to release, since this fake never opens a real connection."""


async def _fake_store_connection() -> _FakePool:
    return _FakePool()


@asynccontextmanager
async def _fake_store_ingest() -> AsyncGenerator[object]:
    async def ingest(payload: Mapping[str, object], *, collection: str) -> None:
        del payload, collection

    yield ingest


@asynccontextmanager
async def _fake_memory_store(*positional: object, **options: object) -> AsyncGenerator[InMemoryStore]:
    del positional, options
    yield InMemoryStore()


async def _no_vector(texts: Sequence[str]) -> list[list[float]]:
    return [[0.0] for _ in texts]


@dataclass
class _NoSql(AbstractCapability[Runtime]):
    """Fills the SQL plugin's slot, which reads a database this tier does not have."""


@asynccontextmanager
async def _fake_sql_plugin(*positional: object, **options: object) -> AsyncGenerator[_NoSql]:
    del positional, options
    yield _NoSql()


@asynccontextmanager
async def _fake_studio_read_tools(
    *positional: object, **mounted: object
) -> AsyncGenerator[tuple[RetrievalKnowledgeBase[object], ProgressiveDisclosure[object], AuthoringTools]]:
    """`(reader, disclosure, plugins)`, the shape `studio_read_tools` yields, over engram's real read capability."""
    del positional, mounted
    yield app_knowledge_base_surface(), ProgressiveDisclosure[object](), fake_plugins()


@pytest.fixture(autouse=True)
def _no_real_infrastructure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every builder the session calls that would open a database or an embedding client."""
    monkeypatch.setattr(harness.composition, "store_connection", _fake_store_connection)
    monkeypatch.setattr(harness.composition, "store_ingest", _fake_store_ingest)
    monkeypatch.setattr(harness.composition, "embedding_contract", lambda: EmbeddingContract(deployment="d", infrastructure=_INFRASTRUCTURE, dimensions=1))
    monkeypatch.setattr(harness.composition, "store_writer_dsn", lambda: "postgresql://nobody@nowhere/none")
    monkeypatch.setattr(harness.composition, "document_embedder", lambda **_contract: _no_vector)
    monkeypatch.setattr(harness.composition, "knowledge_base_embedder", lambda: _no_vector)
    monkeypatch.setattr(harness.composition, "embedded_memory_store", _fake_memory_store)
    monkeypatch.setattr(harness.composition, "knowledge_base_capability", _fake_sql_plugin)


async def _a_session(home: Path, run: RunSettings) -> Runtime:
    """`harness_session` run to the end on `run`, holding what the container would resolve for it."""
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(studio_http_client(HarnessSettings().studio_api))
        return await harness_session(
            stack,
            studio_client=client,
            store=StoreSettings(hybrid_search=False),
            library_search=LibrarySearchSettings(),
            guidance=KnowledgeBaseGuidanceSettings(),
            web_search=WebSearchSettings(),
            self_authoring=SelfAuthoringSettings(custom_root=home / "custom"),
            run=run,
            state=a_state(home),
            records=records_under(home),
            reporter=SpendReporter(FakeTelemetry()),
            session_log=a_session_log(home),
            transcript=a_transcript(home),
            turns=DurableTurns(checkpoints=FakeContinuationCheckpoints(), record=FakeStepStore()),
            parameters=FakeRequestParametersSink(),
            sessions=APool(SessionPool(a_runtime())),
            topics=FakeBroadcaster(),
            notifier=NoopNotifier(),
            tiers=tiers_under(home),
            binding=A_FAKE_BINDING,
            external_agents=dict,
            scheduler=Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()),
            open_read_tools=_fake_studio_read_tools,
        )


async def test_a_temporal_deployment_composes_its_chat_session(tmp_path: Path) -> None:
    session = await _a_session(tmp_path, RunSettings(run_engine="temporal"))

    durable: list[AbstractCapability[Runtime]] = []
    session.agent.root_capability.apply(lambda capability: durable.append(capability) if isinstance(capability, TemporalDurability) else None)

    assert durable == [], "the chat agent never runs inside a durable workflow, so nothing makes it durable"
