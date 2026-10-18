"""How the two sessions mount the library: no preferred surface, the cross-search answer, the studio's client.

All three are wiring facts, keywords the composition either passes or does not, so they are pinned
at the seam rather than inferred from the source: the composition is run until it reaches
`studio_read_tools`, and what it asked for there is what this file asserts.

The two sessions get their answers from different places, and that difference is itself pinned
here: `harness_session` takes the client and the reading policy as arguments, so the mount carries
exactly what the container handed it, while `library_composition` (the CLI's) takes the whole
settings root a console verb built for itself.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from functools import partial
from typing import TYPE_CHECKING, NoReturn

import httpx
import pytest

from core.settings import DatabaseSettings, InfrastructureSettings, OpenRouterSettings
from engram.config import RerankSettings
from engram.retrieval.contracts import Reranking

from harness.adapters.knowledge_base import BOOT_PATIENCE, KnowledgeBaseReading
from harness.adapters.notify import NoopNotifier
from harness.adapters.store import DatabaseBinding
from harness.adapters.studio_http import studio_http_client
from harness.composition import harness_session, library_composition
from harness.features.running.infrastructure.durable import DurableTurns
from harness.features.running.spend_report import SpendReporter
from harness.features.scheduling.contracts import Scheduler
from harness.session import ActiveRun, SessionPool, bind_active
from harness.settings import (
    HarnessSettings,
    KnowledgeBaseGuidanceSettings,
    LibrarySearchSettings,
    RunSettings,
    SelfAuthoringSettings,
    StoreSettings,
    WebSearchSettings,
)
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.request_parameters import FakeRequestParametersSink
from tests.support.fakes.schedules import FakeSchedules
from tests.support.fakes.sessions import APool
from tests.support.fakes.step_store import FakeContinuationCheckpoints, FakeStepStore
from tests.support.fakes.telemetry import FakeTelemetry
from tests.support.runtime import a_runtime
from tests.support.state import a_session_log, a_state, a_transcript, records_under
from tests.support.tiers import tiers_under

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
    from pathlib import Path

_A_DATABASE = DatabaseSettings(url="postgresql+asyncpg://owner:pw@db.example:5432/kb", reader_url="postgresql+asyncpg://reader:pw@db.example:5432/kb")

A_FAKE_BINDING = DatabaseBinding(description="knowledge_base=fake:0/knowledge_bases (test)", named=True)
"""What this process would say it is about to open. Handed in, so nothing reads engram's settings file."""


class _MountStoppedError(Exception):
    """Raised by the `studio_read_tools` probe once it has recorded how the composition mounted the library."""


class _StudioReadToolsProbe:
    """Stands in for `studio_read_tools`, recording the keywords the composition mounted it with, then stopping.

    Returns itself as the async context manager, which refuses to open rather than building a
    reader over a database this tier does not have.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], Mapping[str, object]]] = []

    def __call__(self, *args: object, **kwargs: object) -> _StudioReadToolsProbe:
        self.calls.append((args, kwargs))
        return self

    async def __aenter__(self) -> NoReturn:
        raise _MountStoppedError

    async def __aexit__(self, *details: object) -> None:
        del details


@pytest.fixture
def settings(tmp_path: Path) -> HarnessSettings:
    """This deployment as a console verb builds it, with the custom root under `tmp_path`."""
    return HarnessSettings(self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"))


@pytest.fixture
async def studio_client(settings: HarnessSettings) -> AsyncIterator[httpx.AsyncClient]:
    """The one client the container hands the session, so nothing here composes a second one."""
    async with studio_http_client(settings.studio_api) as client:
        yield client


def _a_session(
    client: httpx.AsyncClient,
    home: Path,
    *,
    library_search: LibrarySearchSettings | None = None,
    infrastructure: InfrastructureSettings | None = None,
    rerank: RerankSettings | None = None,
) -> Callable[[_StudioReadToolsProbe], Awaitable[object]]:
    """`harness_session` holding the collaborators the container resolves for it, so only the mount varies."""
    composed = partial(
        harness_session,
        studio_client=client,
        store=StoreSettings(),
        library_search=library_search if library_search is not None else LibrarySearchSettings(),
        guidance=KnowledgeBaseGuidanceSettings(),
        web_search=WebSearchSettings(),
        self_authoring=SelfAuthoringSettings(custom_root=home / "custom"),
        run=RunSettings(),
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
        infrastructure=infrastructure,
        rerank=rerank,
    )

    async def opened(probe: _StudioReadToolsProbe) -> object:
        async with AsyncExitStack() as stack:
            return await composed(stack, open_read_tools=probe)

    return opened


def _the_registry_session(settings: HarnessSettings, home: Path) -> Callable[[_StudioReadToolsProbe], Awaitable[object]]:
    """`library_composition` holding what a console verb builds for itself, so only the mount varies."""

    async def opened(probe: _StudioReadToolsProbe) -> object:
        async with library_composition(settings, records_under(home), tiers_under(home), A_FAKE_BINDING, open_read_tools=probe) as composed:
            return composed

    return opened


async def _mounted_with(session: Callable[[_StudioReadToolsProbe], Awaitable[object]]) -> tuple[tuple[object, ...], Mapping[str, object]]:
    """Run `session` until it mounts the library, and answer the arguments it mounted it with."""
    probe = _StudioReadToolsProbe()

    with pytest.raises(_MountStoppedError):
        await session(probe)

    assert len(probe.calls) == 1
    return probe.calls[0]


async def test_harness_session_mounts_the_library_with_no_default_knowledge_base_source(
    studio_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """No preferred surface: engram then fans a `knowledge_base`-less search out over every plugin."""
    positional, keyword = await _mounted_with(_a_session(studio_client, tmp_path))

    assert positional == (), "a positional third argument would be a default knowledge base source too"
    assert keyword.get("default_knowledge_base_source") is None


async def test_registry_session_mounts_the_library_the_same_way(settings: HarnessSettings, tmp_path: Path) -> None:
    """A saved step reads the same library the chat agent does, so neither may name a base the other cannot."""
    positional, keyword = await _mounted_with(_the_registry_session(settings, tmp_path))

    assert positional == ()
    assert keyword.get("default_knowledge_base_source") is None


class TestTheCrossSearchAnswerReachesTheMount:
    """Whether an unnamed read is served or refused reaches `studio_read_tools` either way it is answered.

    The session is handed the answer, so a deployment that flips
    `APP_LIBRARY_SEARCH__CROSS_KNOWLEDGE_BASE_SEARCH` moves the container's section and the session
    with it. The registry session reads the same section off the settings root a console verb built.
    """

    async def test_the_default_is_off_so_a_read_must_name_its_knowledge_base(
        self, studio_client: httpx.AsyncClient, tmp_path: Path
    ) -> None:
        _positional, keyword = await _mounted_with(_a_session(studio_client, tmp_path))

        assert _reading(keyword).cross_search is False

    async def test_the_session_mounts_with_the_answer_it_was_given(self, studio_client: httpx.AsyncClient, tmp_path: Path) -> None:
        session = _a_session(studio_client, tmp_path, library_search=LibrarySearchSettings(cross_knowledge_base_search=True))

        _positional, keyword = await _mounted_with(session)

        assert _reading(keyword).cross_search is True

    async def test_the_registry_session_reads_the_setting_off_its_own_root(self, tmp_path: Path) -> None:
        """The CLI's session opens with no container, so the root it built for itself is what answers."""
        settings = HarnessSettings(
            library_search=LibrarySearchSettings(cross_knowledge_base_search=True),
            self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"),
        )

        _positional, keyword = await _mounted_with(_the_registry_session(settings, tmp_path))

        assert _reading(keyword).cross_search is True


class TestTheRegistryOfKnowledgeBasesIsTheStudios:
    """Which knowledge bases exist is the studio's answer, so the mount is handed the studio's client."""

    async def test_the_session_mounts_with_the_client_it_was_given(self, studio_client: httpx.AsyncClient, tmp_path: Path) -> None:
        """One client per process: the session mounts the one it holds rather than opening a second."""
        _positional, keyword = await _mounted_with(_a_session(studio_client, tmp_path))

        assert keyword["client"] is studio_client

    async def test_the_registry_session_opens_its_own_on_the_studio_api(self, settings: HarnessSettings, tmp_path: Path) -> None:
        _positional, keyword = await _mounted_with(_the_registry_session(settings, tmp_path))

        client = keyword["client"]
        assert isinstance(client, httpx.AsyncClient), "the library is opened over the studio's API, never over a settings file"
        assert client.base_url == httpx.URL(settings.studio_api.base_url)


class TestOnlyTheStartingSessionWaitsForTheStudio:
    """Only a process just starting waits for the studio; an already-running one tries once.

    `harness_session` backs a process that is just starting (the HTTP lifespan, the ACP entry
    point) and races the studio's own boot, so it hands the mount `BOOT_PATIENCE`. The registry
    session backs an already-running console verb, so it mounts with no override at all -- one try,
    the same as every other request-time read.
    """

    async def test_harness_session_mounts_with_boot_patience(self, studio_client: httpx.AsyncClient, tmp_path: Path) -> None:
        _positional, keyword = await _mounted_with(_a_session(studio_client, tmp_path))

        assert keyword.get("patience") == BOOT_PATIENCE

    async def test_the_registry_session_mounts_with_no_patience_override(self, settings: HarnessSettings, tmp_path: Path) -> None:
        _positional, keyword = await _mounted_with(_the_registry_session(settings, tmp_path))

        assert "patience" not in keyword


class TestThePassCeilingCountsTheBoundRun:
    """Both sessions hand the mount the run the calling step works for, so a policy's `per_pass_cap` counts one run.

    The answer is read on each call: the run bound around the step, or `None` when no run is making the call.
    """

    async def test_harness_session_mounts_with_the_bound_run(self, studio_client: httpx.AsyncClient, tmp_path: Path) -> None:
        _positional, keyword = await _mounted_with(_a_session(studio_client, tmp_path))

        assert _current_run(keyword)() is None
        with bind_active(ActiveRun(run_id="run-7")):
            assert _current_run(keyword)() == "run-7"

    async def test_the_registry_session_mounts_with_the_bound_run(self, settings: HarnessSettings, tmp_path: Path) -> None:
        _positional, keyword = await _mounted_with(_the_registry_session(settings, tmp_path))

        assert _current_run(keyword)() is None
        with bind_active(ActiveRun(run_id="run-8")):
            assert _current_run(keyword)() == "run-8"


def _current_run(keyword: Mapping[str, object]) -> Callable[[], object]:
    """The run source the mount was given."""
    current_run = keyword["current_run"]
    assert callable(current_run)
    return current_run


def _reading(keyword: Mapping[str, object]) -> KnowledgeBaseReading:
    """The reading policy the mount was given, as the one type `studio_read_tools` declares."""
    reading = keyword["reading"]
    assert isinstance(reading, KnowledgeBaseReading)
    return reading


class TestTheRerankingReachesTheMount:
    """The session opens its knowledge bases with the re-ranking its settings describe, or none."""

    async def test_settings_with_a_key_mount_a_reranking(self, studio_client: httpx.AsyncClient, tmp_path: Path) -> None:
        infrastructure = InfrastructureSettings(database=_A_DATABASE, openrouter=OpenRouterSettings(api_key="k"))
        session = _a_session(studio_client, tmp_path, infrastructure=infrastructure, rerank=RerankSettings())

        _positional, keyword = await _mounted_with(session)

        assert isinstance(keyword.get("rerank"), Reranking)

    async def test_no_settings_mount_none(self, studio_client: httpx.AsyncClient, tmp_path: Path) -> None:
        _positional, keyword = await _mounted_with(_a_session(studio_client, tmp_path))

        assert keyword.get("rerank") is None

    async def test_a_disabled_block_mounts_none(self, studio_client: httpx.AsyncClient, tmp_path: Path) -> None:
        infrastructure = InfrastructureSettings(database=_A_DATABASE, openrouter=OpenRouterSettings(api_key="k"))
        session = _a_session(studio_client, tmp_path, infrastructure=infrastructure, rerank=RerankSettings(enabled=False))

        _positional, keyword = await _mounted_with(session)

        assert keyword.get("rerank") is None
