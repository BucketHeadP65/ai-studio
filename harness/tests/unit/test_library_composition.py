"""`library_composition` builds the library and the registry alone -- never the SQL plugin or the chat agent.

Unlike `harness_session`, `library_composition` must never reach `knowledge_base_capability` (the SQL plugin over
every registered knowledge base) or `build_agent` (the chat agent): neither `import_path` nor a step's own
`perform` touches either.
Proven here by making both fail loudly if called, then actually opening and closing a session
through every other builder `library_composition` does use, faked at the same seam `harness_session`
composes them through.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest

from core.ai.disclosure.contracts import ProgressiveDisclosure

import harness.composition

from harness.adapters.store import DatabaseBinding
from harness.composition import library_composition
from harness.settings import HarnessSettings, SelfAuthoringSettings, StoreSettings
from tests.support.environment import fake_engram, fake_plugins
from tests.support.state import records_under
from tests.support.tiers import tiers_under

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Mapping
    from pathlib import Path

    from core.ai.workflows.contracts import Ingest
    from engram.authoring.contracts import AuthoringTools

    from harness.domain.ports import KnowledgeBaseSurface


class _FakePool:
    """Stands in for the `asyncpg.Pool` `store_connection` would open -- closing is all `library_composition` asks of it."""

    async def close(self) -> None:
        """Nothing to release; this fake never opens a real connection."""


async def _fake_store_connection() -> _FakePool:
    return _FakePool()


@asynccontextmanager
async def _fake_store_ingest() -> AsyncGenerator[Ingest]:
    async def ingest(payload: Mapping[str, object], *, collection: str) -> None:
        del payload, collection

    yield ingest


@asynccontextmanager
async def _fake_studio_read_tools(
    *positional: object, **mounted: object
) -> AsyncGenerator[tuple[KnowledgeBaseSurface, ProgressiveDisclosure[object], AuthoringTools]]:
    """`(reader, disclosure, plugins)`, the shape `studio_read_tools` yields -- no registry read, no engine opened.

    Takes whatever arguments the composition mounts with, since what they are is
    `test_session_library_mount.py`'s claim, not this file's.
    """
    del positional, mounted
    yield fake_engram(), ProgressiveDisclosure[object](), fake_plugins()


A_FAKE_BINDING = DatabaseBinding(description="knowledge_base=fake:0/knowledge_bases (test)", named=True)
"""What this process would say it is about to open. Handed in, so nothing reads engram's settings file."""


def _refuse(name: str) -> Callable[..., object]:
    """A stand-in that fails the test the instant `library_composition` calls it."""

    def _fail(*_args: object, **_kwargs: object) -> object:
        pytest.fail(f"library_composition must never call {name}")

    return _fail


@pytest.fixture
def settings(tmp_path: Path) -> HarnessSettings:
    """This deployment as a console verb builds it, with every home under `tmp_path` and no embedding."""
    return HarnessSettings(
        store=StoreSettings(hybrid_search=False), self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom")
    )


@pytest.fixture(autouse=True)
def _no_real_infrastructure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every builder `library_composition` is allowed to call, faked at the seam `harness_session` shares.

    The library is not among them: it is a parameter now, so every case hands its own in.
    `knowledge_base_capability` and `build_agent` are deliberately left out of this fixture and patched per
    test instead, each wired to fail the instant it is called.
    """
    monkeypatch.setattr(harness.composition, "store_connection", _fake_store_connection)
    monkeypatch.setattr(harness.composition, "store_ingest", _fake_store_ingest)


async def test_registry_session_builds_neither_the_sql_door_nor_the_chat_agent(monkeypatch: pytest.MonkeyPatch, settings: HarnessSettings, tmp_path: Path) -> None:
    """`harness save` and `harness evals run` need neither -- proven, not just asserted from the source."""
    monkeypatch.setattr(harness.composition, "knowledge_base_capability", _refuse("knowledge_base_capability"))
    monkeypatch.setattr(harness.composition, "build_agent", _refuse("build_agent"))

    async with library_composition(
        settings, records_under(tmp_path), tiers_under(tmp_path), A_FAKE_BINDING, open_read_tools=_fake_studio_read_tools
    ) as (store, registry):
        assert store is not None
        assert registry.perform is not None


async def test_registry_session_decides_gates_only_by_refusing(monkeypatch: pytest.MonkeyPatch, settings: HarnessSettings, tmp_path: Path) -> None:
    """A judge-gated workflow validates against this registry, and asking it to decide a gate is refused, never waved through."""
    monkeypatch.setattr(harness.composition, "knowledge_base_capability", _refuse("knowledge_base_capability"))
    monkeypatch.setattr(harness.composition, "build_agent", _refuse("build_agent"))

    async with library_composition(
        settings, records_under(tmp_path), tiers_under(tmp_path), A_FAKE_BINDING, open_read_tools=_fake_studio_read_tools
    ) as (_store, registry):
        assert registry.decides_gates
        assert registry.decide_gate is not None
        with pytest.raises(RuntimeError, match="only a harness session runs a workflow"):
            await registry.decide_gate("check_flags", "review the flags", {"flags": []})
