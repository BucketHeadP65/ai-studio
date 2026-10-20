"""Cross-command unit tests for the `memory` typer CLI (`src/studio/knowledge_base/cli.py`).

Driven by :class:`typer.testing.CliRunner`. The container *factory* is
injectable through the Typer context: production leaves `ctx.obj` unset and a
real container is built from env; tests pass `obj=<factory>` to
`CliRunner.invoke` so the commands resolve services from a fake — no
monkeypatching (the `ctx.obj` seam).

What lives here is the cross-cutting contract every command shares: `--help` works
for the whole surface, and the `ctx.obj` container-factory seam is honoured on a
real command (a command builds its container from the injected factory, never from
env). Per-command behaviour is covered by `test_cli_serve.py` (search/fetch),
`test_cli_note.py` (the note subcommands) and `test_cli_index.py` (index/status).

It also pins the `studio mcp --authoring` wiring: the flag threads through
`_build_mcp_server` and is the *only* way the two opt-in MCP eval-authoring tools
(`propose_eval_seeds` / `submit_eval_cases`) become reachable. The default (read-only)
surface must expose NEITHER, the opt-in path BOTH; both directions are pinned by listing
the server's registered tools over the SDK's in-memory client session.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
import typer

from core.ai.embedding import BatchEmbedder
from engram.evals.commands import KnowledgeBaseGatewayFactory, PolicyEvaluatorFactory, SeedRepositoryFactory
from engram.evals.generation.contracts import GenerationConfig
from engram.evals.recommendation.infrastructure.mcp_recommendation import DIAGNOSE_STRUCTURE, SUBMIT_RECOMMENDATION, WHAT_IF_EDGES
from engram.evals.shared.contracts import Archetype
from engram.evals.shared.domain import DEFAULT_DIFFICULTY_THRESHOLDS
from engram.evals.tuning.policy import RetrievalPolicy, save_policy
from engram.indexing.contracts import KnowledgeBaseReader
from engram.retrieval.envelope import EnvelopeHandler
from engram.retrieval.infrastructure.mcp_server import PROPOSE_SEEDS, SUBMIT_CASES
from engram.retrieval.queries import DescribeSchemaHandler, FetchHandler, GraphExpandHandler, GraphPathsHandler, SearchHandler
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.knowledge_base_reader import FakeKnowledgeBaseReader
from engram.testing.fakes.recommendation import FakeKnowledgeBaseGateway
from engram.testing.specs import research_spec
from mcp.shared.memory import create_connected_server_and_client_session as connect_in_memory
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from typer.testing import CliRunner

from studio.knowledge_base.cli import _STUDIO_BIN, _build_mcp_server, _mcp_server_after_identity, app
from studio.knowledge_base.cli_support import ActiveKnowledgeBase
from studio.knowledge_base.settings import ApplicationSettings, DatabaseSettings, EvalsSettings, KnowledgeBaseSettings, RetrievalSettings
from studio.knowledge_base.wiring.contracts import AdminSqlGateway, ReaderSqlGateway, SearchReranking

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping
    from pathlib import Path

    from engram.evals.generation.contracts import SupportsSeeding
    from engram.evals.shared.domain import QaDifficulty, Seed
    from engram.evals.tuning.internal_contracts import PolicyEvaluator
    from engram.retrieval.domain import SqlResult
    from mcp.server.fastmcp import FastMCP
    from mcp.types import Tool

runner = CliRunner()

# The full top-level surface that must appear in `--help` (plus the `note`/`export` groups).
_TOP_LEVEL_COMMANDS = ["index", "status", "search", "fetch", "mcp"]
_NOTE_SUBCOMMANDS = ["validate", "build", "build-batch", "normalize", "verify", "schema"]


class _ProbeFactory:
    """Zero-arg `ctx.obj` container factory that records whether the CLI called it.

    The seam contract is that a command builds its container *from this factory* (so a
    test can wire fakes) rather than reading env. We return an empty object: the command
    will fail to resolve its services and exit 1 — but only *after* invoking the factory,
    which is precisely what proves the seam (no env/settings/network was touched).
    """

    def __init__(self) -> None:
        self.called = False

    def __call__(self) -> object:
        self.called = True
        return object()


class TestHelp:
    def test_top_level_help_exits_zero(self):
        result = runner.invoke(app, ["--help"])

        assert result.exit_code == 0

    def test_top_level_help_lists_every_command(self):
        result = runner.invoke(app, ["--help"])

        for command in [*_TOP_LEVEL_COMMANDS, "note", "export"]:
            assert command in result.stdout

    def test_note_help_lists_the_subcommands(self):
        result = runner.invoke(app, ["note", "--help"])

        assert result.exit_code == 0
        for subcommand in _NOTE_SUBCOMMANDS:
            assert subcommand in result.stdout

    def test_mcp_help_lists_the_knowledge_base_flag(self):
        result = runner.invoke(app, ["mcp", "--help"])

        assert result.exit_code == 0
        assert "--knowledge-base" in result.stdout

    def test_mcp_help_lists_the_authoring_flag(self):
        result = runner.invoke(app, ["mcp", "--help"])

        assert result.exit_code == 0
        assert "--authoring" in result.stdout

    def test_version_prints_a_version_and_exits_zero(self):
        result = runner.invoke(app, ["--version"])

        assert result.exit_code == 0
        assert result.stdout.startswith("studio ")

    def test_a_bare_invocation_still_prints_help(self):
        result = runner.invoke(app, [])

        assert "Usage" in result.output


class TestContainerOverrideSeam:
    def test_injected_factory_is_used_instead_of_building_one(self, tmp_path: Path) -> None:
        """A real command builds its container from the injected `ctx.obj` factory.

        `search` is a fully-implemented command; with a probe factory whose container
        resolves nothing, the command exits 1 — but the probe records that it *was* the
        factory the command built from (rather than the env-backed default), proving the
        single composition seam is honoured end to end. `--knowledge-base` names a real knowledge base so
        resolution succeeds and the seam is reached at all.
        """
        (tmp_path / "_meta").mkdir(parents=True, exist_ok=True)
        (tmp_path / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
        factory = _ProbeFactory()

        result = runner.invoke(app, ["search", "a query", "--knowledge-base", str(tmp_path)], obj=factory)

        assert factory.called is True
        assert result.exit_code == 1


def _make_knowledge_base(tmp_path: Path) -> KnowledgeBaseSettings:
    """A real `KnowledgeBaseSettings` over a minimal on-disk knowledge base carrying the research template's schema.

    `_build_mcp_server` reads the knowledge base's spec from disk (`load_knowledge_base_schema`), so the test
    materialises the `knowledge-base-schema.json` the settings validator requires (the research template's own,
    whose `retrieval.profile` is `full`) and a `_meta/` folder.
    """
    (tmp_path / "_meta").mkdir(parents=True, exist_ok=True)
    (tmp_path / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
    return KnowledgeBaseSettings(path=tmp_path)


def _inert_reader_gateway() -> ReaderSqlGateway:
    """A real `ReaderSqlGateway` over an unconnected engine (the gateway is wired, never queried here).

    Constructing the async engine opens no connection, and these tests only list the registered tools
    (they never invoke `run_sql`), so the gateway is a genuine `ReaderSqlGateway` the fake resolves for
    that DI key without any database. `NullPool` keeps it from holding idle connections regardless.
    """
    engine = create_async_engine("postgresql+asyncpg://reader:pw@localhost:5433/knowledge_bases", poolclass=NullPool)
    return ReaderSqlGateway(engine)


def _inert_admin_gateway() -> AdminSqlGateway:
    """A real `AdminSqlGateway` over an unconnected engine, the freshness stamp's read side.

    `_build_mcp_server` resolves this to build its `stamp` closure, but none of this file's tests
    call a tool that invokes `stamp`, so the gateway never runs a query.
    """
    engine = create_async_engine("postgresql+asyncpg://admin:pw@localhost:5433/knowledge_bases", poolclass=NullPool)
    return AdminSqlGateway(engine)


def _inert_engine() -> AsyncEngine:
    """A real, never-connected `AsyncEngine`.

    `_build_mcp_server` disposes this engine before returning the server, and disposing an
    unconnected engine opens no socket.
    """
    return create_async_engine("postgresql+asyncpg://admin:pw@localhost:5433/knowledge_bases", poolclass=NullPool)


class _FakePolicyEvaluatorFactory:
    """A no-op `PolicyEvaluatorFactory`: never invoked here (the tuning tools are listed, not called).

    `_build_mcp_server`'s `--authoring` path resolves this factory and stores it on the server's
    tuning deps without ever building an evaluator (the tuning tools are only listed in these
    tests). The `__call__` therefore raises, asserting at runtime that no evaluator is built on the list
    path; its declared `PolicyEvaluator` return type keeps the structural `PolicyEvaluatorFactory`
    protocol satisfied without a `cast`.
    """

    def __call__(self, model: str | None) -> PolicyEvaluator:
        del model
        msg = "the MCP wiring tests never build a PolicyEvaluator"
        raise NotImplementedError(msg)


def _knowledge_base_gateway_factory() -> KnowledgeBaseGatewayFactory:
    """A `KnowledgeBaseGatewayFactory`: an async-CM yielding a canned recommendation gateway (the one-per-command shape).

    Never queried here (the recommendation tools are listed, not called), so the default `FakeKnowledgeBaseGateway`
    with its empty canned reads suffices for `_build_mcp_server`'s `--authoring` gate to fire.
    """

    @asynccontextmanager
    async def factory() -> AsyncGenerator[FakeKnowledgeBaseGateway]:
        yield FakeKnowledgeBaseGateway()

    return factory


@dataclass
class _McpContainer:
    """Fake `Resolver` resolving exactly the deps `_build_mcp_server` asks for, narrowed by isinstance.

    The serve read-side handlers ride a single inert `ReaderSqlGateway`; `SearchHandler` also takes a
    `FakeEmbedding`. `_build_mcp_server` also resolves an `AdminSqlGateway` and a `KnowledgeBaseReader` to
    build its freshness stamp, and an `AsyncEngine` it disposes before returning, all inert here since
    no test in this file calls a tool that reads the stamp. The authoring path additionally resolves
    `SeedRepositoryFactory` (a fresh seed repository per propose call), `GenerationConfig` (the single
    source of the difficulty thresholds the gate stamps onto a kept case), and `KnowledgeBaseGatewayFactory`
    (a fresh recommendation knowledge base gateway per recommend call). `requested` records every resolved key so a test
    can assert the read-only path never reaches for the authoring deps. Each lookup is narrowed with
    `isinstance(service, klass)` (the `FakeContainer` idiom), so `get` honestly returns `T`.
    """

    knowledge_base: KnowledgeBaseSettings
    seed_repo_factory: SeedRepositoryFactory
    retrieval: RetrievalSettings = field(default_factory=RetrievalSettings)
    knowledge_base_gateway_factory: KnowledgeBaseGatewayFactory = field(default_factory=_knowledge_base_gateway_factory)
    policy_evaluator_factory: PolicyEvaluatorFactory = field(default_factory=_FakePolicyEvaluatorFactory)
    gateway: ReaderSqlGateway = field(default_factory=_inert_reader_gateway)
    admin_gateway: AdminSqlGateway = field(default_factory=_inert_admin_gateway)
    reader: KnowledgeBaseReader = field(default_factory=FakeKnowledgeBaseReader)
    embedder: FakeEmbedding = field(default_factory=FakeEmbedding)
    engine: AsyncEngine = field(default_factory=_inert_engine)
    requested: list[type] = field(default_factory=list)

    async def get[T](self, klass: type[T]) -> T:
        self.requested.append(klass)
        services: dict[type, object] = {
            ReaderSqlGateway: self.gateway,
            AdminSqlGateway: self.admin_gateway,
            DescribeSchemaHandler: DescribeSchemaHandler(self.gateway),
            GraphExpandHandler: GraphExpandHandler(self.gateway, "research"),
            GraphPathsHandler: GraphPathsHandler(self.gateway, "research"),
            SearchHandler: SearchHandler(self.gateway, self.embedder),
            FetchHandler: FetchHandler(self.gateway, "research"),
            EnvelopeHandler: EnvelopeHandler(self.gateway),
            RetrievalSettings: self.retrieval,
            SearchReranking: SearchReranking(reranking=None),
            KnowledgeBaseSettings: self.knowledge_base,
            DatabaseSettings: DatabaseSettings(
                url="postgresql+asyncpg://u:p@h:5433/knowledge_bases", reader_url="postgresql+asyncpg://r:p@h:5433/knowledge_bases"
            ),
            BatchEmbedder: self.embedder,
            KnowledgeBaseReader: self.reader,
            AsyncEngine: self.engine,
            EvalsSettings: EvalsSettings(),
            SeedRepositoryFactory: self.seed_repo_factory,
            GenerationConfig: GenerationConfig(target_distribution={Archetype.PINPOINT: 1.0}, thresholds=DEFAULT_DIFFICULTY_THRESHOLDS),
            PolicyEvaluatorFactory: self.policy_evaluator_factory,
            KnowledgeBaseGatewayFactory: self.knowledge_base_gateway_factory,
        }
        service = services.get(klass)
        if not isinstance(service, klass):
            msg = f"_McpContainer has no registration for {klass!r}"
            raise KeyError(msg)
        return service


class _FakeSeeder:
    """A no-op `SupportsSeeding`: never invoked here (the authoring tools are listed, not called)."""

    async def _empty(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        del collection, page_ids, limit, salt, band, anchor_page_type
        return []

    pinpoint_seeds = _empty
    survey_seeds = _empty
    trace_seeds = _empty
    synthesis_seeds = _empty
    passage_seeds = _empty


def _seed_repo_factory() -> SeedRepositoryFactory:
    """A `SeedRepositoryFactory`: an async-CM yielding a no-op seeder (the one-repo-per-command shape)."""

    @asynccontextmanager
    async def factory() -> AsyncGenerator[SupportsSeeding]:
        yield _FakeSeeder()

    return factory


def _container(tmp_path: Path, retrieval: RetrievalSettings | None = None) -> _McpContainer:
    """A fake container over a minimal knowledge base and a no-op seed-repository factory.

    `retrieval` overrides the retrieval policy the build reads (the search-cap flag lives there);
    `None` resolves the shipped defaults.
    """
    return _McpContainer(
        knowledge_base=_make_knowledge_base(tmp_path), seed_repo_factory=_seed_repo_factory(), retrieval=retrieval or RetrievalSettings()
    )


async def _tool_names(server: FastMCP) -> frozenset[str]:
    """The names of the tools the built server registered (over the SDK's in-memory session)."""
    async with connect_in_memory(server) as client:
        await client.initialize()
        listing = await client.list_tools()
    return frozenset(tool.name for tool in listing.tools)


async def _served_search(server: FastMCP) -> Tool:
    """The `search` tool the built server published, as a connected MCP client sees it."""
    async with connect_in_memory(server) as client:
        await client.initialize()
        listing = await client.list_tools()
    [search] = [tool for tool in listing.tools if tool.name == "search"]
    return search


class TestBuildMcpServerSearchCap:
    """`retrieval.agent_tunes_k` is the switch that moves the search hit cap from agent to host.

    `_build_mcp_server` derives the cap source from the settings it already resolves and threads it
    into `create_mcp_server`, so a config-driven deployment pins `k` without any agent seeing it.
    """

    async def test_the_default_leaves_k_in_the_agents_hands(self, tmp_path: Path):
        server = await _build_mcp_server(_container(tmp_path), authoring=False)

        assert (await _served_search(server)).inputSchema["properties"]["k"]["default"] == 12

    async def test_a_pinned_cap_takes_k_out_of_the_served_tool(self, tmp_path: Path):
        container = _container(tmp_path, RetrievalSettings(agent_tunes_k=False, k_default=4))

        server = await _build_mcp_server(container, authoring=False)

        properties = (await _served_search(server)).inputSchema["properties"]
        assert "query" in properties
        assert "k" not in properties

    async def test_a_pinned_cap_leaves_the_description_offering_no_k(self, tmp_path: Path):
        container = _container(tmp_path, RetrievalSettings(agent_tunes_k=False, k_default=4))

        server = await _build_mcp_server(container, authoring=False)

        assert "`k`" not in ((await _served_search(server)).description or "")

    async def test_the_cap_value_alone_changes_nothing_while_the_agent_tunes_k(self, tmp_path: Path):
        # `k_default` is inert until the flag flips — one switch, so a half-set config cannot surprise.
        container = _container(tmp_path, RetrievalSettings(k_default=4))

        server = await _build_mcp_server(container, authoring=False)

        assert (await _served_search(server)).inputSchema["properties"]["k"]["default"] == 12


class TestBuildMcpServerAuthoring:
    """`_build_mcp_server`'s `authoring` flag is the only switch that reaches the opt-in tools (D8)."""

    async def test_authoring_true_registers_both_eval_authoring_tools(self, tmp_path: Path):
        container = _container(tmp_path)

        server = await _build_mcp_server(container, authoring=True)

        names = await _tool_names(server)
        assert PROPOSE_SEEDS in names
        assert SUBMIT_CASES in names

    async def test_authoring_false_is_read_only_with_neither_authoring_tool(self, tmp_path: Path):
        container = _container(tmp_path)

        server = await _build_mcp_server(container, authoring=False)

        names = await _tool_names(server)
        assert PROPOSE_SEEDS not in names
        assert SUBMIT_CASES not in names

    async def test_default_surface_is_read_only_and_never_resolves_authoring_deps(self, tmp_path: Path):
        container = _container(tmp_path)

        server = await _build_mcp_server(container, authoring=False)

        names = await _tool_names(server)
        assert PROPOSE_SEEDS not in names
        assert SUBMIT_CASES not in names
        # The read-only path constructs no authoring wiring at all: the container is never even
        # asked for the seed-repository factory, so a fabricated default surface cannot expose them.
        assert SeedRepositoryFactory not in container.requested
        assert GenerationConfig not in container.requested
        assert KnowledgeBaseGatewayFactory not in container.requested

    async def test_authoring_path_resolves_the_seed_repo_factory_and_thresholds_source(self, tmp_path: Path):
        container = _container(tmp_path)

        await _build_mcp_server(container, authoring=True)

        # The opt-in path sources the deps the gate needs: the per-command seed repository and the
        # single-source GenerationConfig (its `thresholds`), not a duplicated settings→domain mapping.
        assert SeedRepositoryFactory in container.requested
        assert GenerationConfig in container.requested

    async def test_authoring_true_registers_the_three_recommendation_tools(self, tmp_path: Path):
        container = _container(tmp_path)

        server = await _build_mcp_server(container, authoring=True)

        names = await _tool_names(server)
        assert {DIAGNOSE_STRUCTURE, WHAT_IF_EDGES, SUBMIT_RECOMMENDATION} <= names

    async def test_authoring_false_has_no_recommendation_tools(self, tmp_path: Path):
        container = _container(tmp_path)

        server = await _build_mcp_server(container, authoring=False)

        names = await _tool_names(server)
        assert {DIAGNOSE_STRUCTURE, WHAT_IF_EDGES, SUBMIT_RECOMMENDATION}.isdisjoint(names)

    async def test_authoring_path_resolves_the_knowledge_base_gateway_factory(self, tmp_path: Path):
        container = _container(tmp_path)

        await _build_mcp_server(container, authoring=True)

        # The recommendation gate sources the per-command knowledge base gateway (the baseline evaluator factory and
        # the recommendations directory ride the shared evals settings, already resolved for tuning).
        assert KnowledgeBaseGatewayFactory in container.requested


class TestBuildMcpServerLeveledFetch:
    """The leveled `fetch` must ride `studio mcp`, the production composition path.

    The wired `EnvelopeHandler` only changes `fetch`'s behavior when it survives the whole
    `_build_mcp_server` -> `create_mcp_server` -> `_register_single_source_tools` threading, so
    this goes through the real composition under the DEFAULT (non-authoring, no active-policy)
    profile and pins the leveled description — the observable difference between the flat and the
    leveled read.
    """

    async def test_fetch_is_leveled_under_the_default_profile(self, tmp_path: Path):
        container = _container(tmp_path)

        server = await _build_mcp_server(container, authoring=False)

        tools = await server.list_tools()
        fetch_description_text = next(tool.description or "" for tool in tools if tool.name == "fetch")
        assert "map" in fetch_description_text  # the leveled copy advertises the knowledge base map; the flat one never does


def _write_active_policy(tmp_path: Path, policy: RetrievalPolicy) -> None:
    """Save `policy` as the knowledge base's `_meta/active-policy.yaml` (the C9a artifact the serve layer reads)."""
    save_policy(policy, tmp_path / "_meta" / "active-policy.yaml")


class TestBuildMcpServerActivePolicy:
    """`_build_mcp_server` reads `<knowledge_base>/_meta/active-policy.yaml` when present (C9b serve-read tail).

    The active policy overrides the knowledge_base_schema strategy (the operator opted in by running
    `apply-policy`) and its `tool_descriptions` are threaded onto the live retrieval tools. An absent
    file leaves the default (full, shipped-descriptions) surface unchanged; a malformed file fails
    cleanly via `_fail`, exactly as an unknown profile name does.
    """

    async def test_active_policy_strategy_overrides_the_knowledge_base_schema_profile(self, tmp_path: Path):
        # The knowledge base declares no knowledge_base_schema → the default `full` surface (all four retrieval tools). A
        # saved `search-keyword` policy must win: the graph tools drop, only search/fetch survive.
        container = _container(tmp_path)  # materialises the knowledge base's _meta/ the active-policy file lives in
        _write_active_policy(tmp_path, RetrievalPolicy(instructions="i", strategy="search-keyword"))

        server = await _build_mcp_server(container)

        names = await _tool_names(server)
        assert {"search", "fetch"} <= names
        assert "graph_expand" not in names  # the tuned keyword strategy exposes no graph leg
        assert "find_paths" not in names

    async def test_active_policy_tool_descriptions_reach_the_live_tools(self, tmp_path: Path):
        container = _container(tmp_path)
        _write_active_policy(
            tmp_path,
            RetrievalPolicy(
                instructions="i", strategy="full", tool_descriptions={"search": "TUNED SEARCH", "graph_expand": "TUNED EXPAND"}
            ),
        )

        server = await _build_mcp_server(container)

        async with connect_in_memory(server) as client:
            await client.initialize()
            listing = await client.list_tools()
        by_name = {tool.name: (tool.description or "") for tool in listing.tools}
        assert by_name["search"].startswith("TUNED SEARCH")
        # An operator rewrites how a routed tool reads, never what it serves: which knowledge bases
        # `collection` reaches, and that a hit's `ref` is what opens it, still ride the tuned text.
        assert "No other collection is served here" in by_name["search"]
        assert by_name["graph_expand"] == "TUNED EXPAND"

    async def test_no_active_policy_file_keeps_the_default_full_surface(self, tmp_path: Path):
        # No active-policy file → the knowledge_base_schema default (full): all four retrieval tools, shipped descriptions.
        container = _container(tmp_path)

        server = await _build_mcp_server(container)

        names = await _tool_names(server)
        assert {"search", "fetch", "graph_expand", "find_paths"} <= names

    async def test_malformed_active_policy_fails_cleanly(self, tmp_path: Path):
        container = _container(tmp_path)
        (tmp_path / "_meta" / "active-policy.yaml").write_text("strategy: not-a-real-strategy\ninstructions: i\n", encoding="utf-8")

        with pytest.raises(typer.Exit):
            await _build_mcp_server(container)

    async def test_active_policy_instructions_reach_the_server(self, tmp_path: Path):
        # The tuned policy's `instructions` become the server-level guidance the MCP handshake
        # advertises, alongside the strategy/description overrides already applied and engram's own
        # always-on library paragraph (`create_mcp_server`).
        container = _container(tmp_path)
        _write_active_policy(tmp_path, RetrievalPolicy(instructions="TUNED INSTRUCTIONS", strategy="full"))

        server = await _build_mcp_server(container)

        assert server.instructions is not None
        assert "TUNED INSTRUCTIONS" in server.instructions

    async def test_no_active_policy_leaves_only_the_library_paragraph(self, tmp_path: Path):
        container = _container(tmp_path)

        server = await _build_mcp_server(container)

        assert server.instructions is not None
        assert "api.about" in server.instructions  # engram's always-on library paragraph, no tuned text riding with it


class TestMcpBannerIdentity:
    """The `studio mcp` startup banner surfaces the resolved binary identity alongside knowledge base + db (F61).

    A PATH-shadowing `${ENGRAM_BIN:-engram}` wrapper serving the wrong knowledge base becomes diagnosable: the
    banner now names the running engram binary (`bin=`) next to the knowledge base and db it already showed.
    """

    async def test_banner_includes_knowledge_base_db_and_binary_identity(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        container = _container(tmp_path)

        await _build_mcp_server(container)

        err = capsys.readouterr().err
        assert "studio mcp serving:" in err
        assert "knowledge_base=" in err
        assert "db=" in err
        assert "bin=" in err
        # The binary identity is the actual running engram entrypoint, not an empty token.
        assert f"bin={_STUDIO_BIN}" in err


class _UnreachableAdminGateway(AdminSqlGateway):
    """An `AdminSqlGateway` whose every read raises the dead-port connection failure."""

    def __init__(self) -> None:
        super().__init__(create_async_engine("postgresql+asyncpg://admin:pw@localhost:5433/knowledge_bases", poolclass=NullPool))

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        del sql, params, cap
        msg = "[Errno 61] Connect call failed ('127.0.0.1', 5434)"
        raise ConnectionRefusedError(msg)


def _mcp_settings(tmp_path: Path) -> ActiveKnowledgeBase:
    """The settings and knowledge base an `studio mcp` command carries, no env, no network."""
    return ActiveKnowledgeBase(application=ApplicationSettings(), knowledge_base=KnowledgeBaseSettings(path=tmp_path))


class TestMcpServesAnywayOnAnUnreachableDatabase:
    """`studio mcp` never exits on an unreachable database, it announces, warns, and serves anyway."""

    async def test_the_identity_lines_appear_and_the_server_still_builds(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        container = _container(tmp_path)
        container.admin_gateway = _UnreachableAdminGateway()

        server = await _mcp_server_after_identity(
            container, _mcp_settings(tmp_path), collection="research", authoring=False
        )

        lines = capsys.readouterr().err.splitlines()
        assert lines[0].startswith("knowledge base ")
        assert lines[1].endswith("unreachable, connection refused.")
        assert lines[2] == "serving anyway, every call fails until the database is reachable."
        assert server is not None
