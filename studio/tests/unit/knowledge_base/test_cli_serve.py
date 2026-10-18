r"""Unit tests for the `memory search` + `memory fetch` CLI commands (`src/studio/knowledge_base/cli.py`).

Driven by :class:`typer.testing.CliRunner` with the container *factory* injected
through `ctx.obj` (no monkeypatching), exactly as
`test_cli_index.py` does for index. The fake factory returns a fake async
container that resolves the read-side dependencies the search/fetch commands need:

* a :class:`~engram.retrieval.queries.SearchHandler` built over a
  :class:`~engram.testing.fakes.gateway.FakeSqlGateway` (records the SQL + params the
  handler bound) and a :class:`~engram.testing.fakes.embedding.FakeEmbedding` (records
  whether it was asked to embed at all);
* a :class:`~engram.retrieval.queries.FetchHandler`, built fresh over the same fake gateway plus
  `--collection` (mirroring how `search` builds its own handler fresh, never a DI singleton).

So the command's resolve → build the :class:`Search`/:class:`Fetch` message → dispatch →
render → exit-code path is exercised with no database, knowledge base, or network.

Pinned behaviour:

* a search renders the ranked hits (id, level, title/snippet, score) and exits 0;
* `--bm25-only` runs hybrid-free: the embedder is **never resolved nor called**
  (`FakeEmbedding` records zero embed calls and the container is never asked for the
  `BatchEmbedder`) — so BM25-only works with no Azure env;
* `-k`, `--level`, and repeatable `--facet key=value` are plumbed into the
  :class:`Search` message (asserted via the bound params + row cap);
* `fetch` renders the unit (level, title, body) and surfaces the truncation marker;
* both exit 1 with a clean message — not a traceback — when the gateway read fails.
"""

from __future__ import annotations

import json

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from core.ai.embedding import BatchEmbedder
from core.cqrs.contracts import JobQueries, MemoryJobs
from core.db import redact_db
from engram.indexing.contracts import KnowledgeBaseReader
from engram.retrieval.domain import ColumnInfo, DescribeSchema, ObjectInfo, SchemaDescription, SqlGateway, SqlResult
from engram.retrieval.envelope import EnvelopeHandler
from engram.retrieval.queries import DescribeSchemaHandler, FetchHandler, GraphExpandHandler, GraphPathsHandler, SearchHandler
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.gateway import FakeSqlGateway
from engram.testing.fakes.knowledge_base_reader import FakeKnowledgeBaseReader
from engram.testing.specs import research_spec
from mcp.shared.memory import create_connected_server_and_client_session as connect_in_memory
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from typer.testing import CliRunner

from studio.knowledge_base.cli import _build_mcp_server, _make_feedback_sink, _mcp_profile_name, app
from studio.knowledge_base.settings import DatabaseSettings, KnowledgeBaseSettings, RetrievalSettings
from studio.knowledge_base.wiring.contracts import AdminSqlGateway, ReaderSqlGateway, SearchReranking

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

runner = CliRunner()


@pytest.fixture(autouse=True)
def _ambient_knowledge_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this file runs inside a real knowledge base by cwd, its spec the bundled research default verbatim.

    A test that names its own `--knowledge-base` and chdirs elsewhere is unaffected — its own
    `monkeypatch.chdir` runs after this one.
    """
    root = tmp_path / "ambient-knowledge-base"
    root.mkdir()
    (root / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
    monkeypatch.chdir(root)


def _search_result() -> SqlResult:
    """One ranked api.search row in the function's RETURNS TABLE column order."""
    row = {
        "id": "paper.lightrag-2025#c1",
        "hit_level": "claim",
        "page_id": "paper.lightrag-2025",
        "page_title": "LightRAG",
        "snippet": "LightRAG consistently outperforms GraphRAG",
        "score": 0.87,
    }
    return SqlResult(rows=(row,), columns=("id", "hit_level", "page_id", "page_title", "snippet", "score"))


def _fetch_result(*, body: str = "LightRAG wins on the large UltraDomain subsets.") -> SqlResult:
    """One api.fetch row (id, level, title, body) for a claim unit."""
    row = {"id": "paper.lightrag-2025#c1", "level": "claim", "title": "benchmark-result", "body": body}
    return SqlResult(rows=(row,), columns=("id", "level", "title", "body"))


class _BoomGateway:
    """A :class:`SqlGateway` whose ``rows`` raises — an unreachable DB / failed read."""

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        del sql, params, cap
        msg = "connection refused"
        raise ConnectionError(msg)

    async def api_objects(self) -> SchemaDescription:  # pragma: no cover — search/fetch never introspect
        raise NotImplementedError


class _UnreachableGateway:
    """An admin-gateway stand-in whose every read raises the dead-port connection failure."""

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        del sql, params, cap
        msg = "[Errno 61] Connect call failed ('127.0.0.1', 5434)"
        raise ConnectionRefusedError(msg)

    async def api_objects(self) -> SchemaDescription:  # pragma: no cover (the identity announce never introspects)
        raise NotImplementedError


@dataclass
class _Container:
    """Fake async DI container resolving exactly the serve read-side deps the CLI asks for.

    Records every resolved key in `requested` so a test can assert the `--bm25-only`
    path never reaches for the :class:`BatchEmbedder` (the whole lazy-embedder contract).
    `admin_gateway` and `reader` back the identity announce that runs before every search or
    fetch, separate from `gateway` (the reader-role read the command itself runs), so a search
    or fetch result scripted with no `path`/`content_hash` columns never confuses the freshness
    read.
    """

    gateway: SqlGateway
    embedder: FakeEmbedding
    admin_gateway: SqlGateway = field(default_factory=FakeSqlGateway)
    reader: KnowledgeBaseReader = field(default_factory=FakeKnowledgeBaseReader)
    requested: list[type] = field(default_factory=list)
    jobs: JobQueries = field(default_factory=MemoryJobs)

    async def get(self, key: type) -> object:
        self.requested.append(key)
        if key is ReaderSqlGateway:
            return self.gateway  # the fake satisfies the structural SqlGateway the handler needs
        if key is AdminSqlGateway:
            return self.admin_gateway  # the identity announce's admin read, a separate fake
        if key is KnowledgeBaseReader:
            return self.reader
        if key is BatchEmbedder:
            return self.embedder
        if key is JobQueries:
            return self.jobs
        if key is RetrievalSettings:
            return RetrievalSettings()
        if key is SearchReranking:
            return SearchReranking(reranking=None)
        msg = f"_Container has no registration for {key!r}"
        raise KeyError(msg)


@dataclass
class _Factory:
    """Zero-arg `ctx.obj` factory returning a fresh fake container around a recording gateway.

    ``gateway`` is concretely a :class:`FakeSqlGateway` so tests can read its recorded
    ``last`` call; the failure-path tests use :func:`_boom_factory` instead.
    """

    gateway: FakeSqlGateway = field(default_factory=FakeSqlGateway)
    embedder: FakeEmbedding = field(default_factory=FakeEmbedding)

    def __post_init__(self) -> None:
        self.container = _Container(self.gateway, self.embedder)

    def __call__(self) -> _Container:
        return self.container


def _boom_factory() -> object:
    """A `ctx.obj` factory whose reader gateway raises on every read (the clean-failure path).

    The admin gateway stays a plain, working fake so the identity announce succeeds and the
    failure this test drives is the search/fetch read itself, not the announce.
    """
    container = _Container(_BoomGateway(), FakeEmbedding())
    return lambda: container


def _unreachable_factory() -> object:
    """A `ctx.obj` factory whose admin gateway is unreachable (the clean two-line refusal path)."""
    container = _Container(_gateway(_search_result()), FakeEmbedding(), admin_gateway=_UnreachableGateway())
    return lambda: container


def _gateway(result: SqlResult) -> FakeSqlGateway:
    """A :class:`FakeSqlGateway` scripted to return ``result`` from its next ``rows`` call."""
    return FakeSqlGateway(result=result)


class TestSearch:
    def test_happy_path_renders_hits_and_exits_zero(self):
        factory = _Factory(gateway=_gateway(_search_result()))

        result = runner.invoke(app, ["search", "graph rag"], obj=factory)

        assert result.exit_code == 0, result.stdout
        assert "paper.lightrag-2025#c1" in result.stdout
        assert "claim" in result.stdout
        assert "0.87" in result.stdout

    def test_each_hits_folded_units_print_indented_under_it_with_their_level(self):
        folded = [{"id": "paper.lightrag-2025#s2", "hit_level": "section"}, {"id": "src.lightrag:4", "hit_level": "raw"}]
        row = {**_search_result().rows[0], "folded_units": folded}
        factory = _Factory(gateway=_gateway(SqlResult(rows=(row,), columns=(*_search_result().columns, "folded_units"))))

        result = runner.invoke(app, ["search", "graph rag", "--bm25-only"], obj=factory)

        lines = result.stdout.splitlines()
        hit = next(index for index, line in enumerate(lines) if "paper.lightrag-2025#c1" in line)
        assert result.exit_code == 0, result.stdout
        assert [line.strip() for line in lines[hit + 1 : hit + 3]] == ["paper.lightrag-2025#s2  [section]", "src.lightrag:4  [raw]"]
        assert all(line.startswith(" ") for line in lines[hit + 1 : hit + 3])

    def test_default_search_is_hybrid_and_embeds_the_query_once(self):
        factory = _Factory(gateway=_gateway(_search_result()))

        result = runner.invoke(app, ["search", "graph rag"], obj=factory)

        assert result.exit_code == 0, result.stdout
        # Hybrid path: the query was embedded exactly once and a non-NULL halfvec was bound.
        assert factory.embedder.texts == ["graph rag"]
        assert factory.gateway.last.params["qvec"] is not None

    def test_bm25_only_never_resolves_or_calls_the_embedder(self):
        factory = _Factory(gateway=_gateway(_search_result()))

        result = runner.invoke(app, ["search", "graph rag", "--bm25-only"], obj=factory)

        assert result.exit_code == 0, result.stdout
        # The whole lazy-embedder contract: no embed call AND the container was never even
        # asked for the BatchEmbedder (its provider, hence Azure, is never constructed).
        assert factory.embedder.texts == []
        assert BatchEmbedder not in factory.container.requested
        assert factory.gateway.last.params["qvec"] is None  # NULL halfvec → BM25-only RRF leg

    def test_k_and_facets_are_plumbed_into_the_search_message(self):
        factory = _Factory(gateway=_gateway(_search_result()))

        result = runner.invoke(
            app,
            [
                "search",
                "graph rag",
                "-k",
                "5",
                "--level",
                "claim",
                "--facet",
                "tasks=retrieval",
                "--facet",
                "tasks=indexing",
                "--facet",
                "stage=eval",
            ],
            obj=factory,
        )

        assert result.exit_code == 0, result.stdout
        bound = factory.gateway.last
        assert bound.cap == 5
        assert bound.params["k"] == 5
        assert bound.params["level"] == "claim"
        facets = bound.params["facets"]
        assert isinstance(facets, str)
        assert json.loads(facets) == {"tasks": ["retrieval", "indexing"], "stage": ["eval"]}

    def test_a_facet_without_equals_is_a_clean_usage_error(self):
        factory = _Factory(gateway=_gateway(_search_result()))

        result = runner.invoke(app, ["search", "graph rag", "--facet", "no-equals-sign"], obj=factory)

        assert result.exit_code != 0
        assert "Traceback" not in result.stdout

    def test_no_hits_exits_zero_with_a_message(self):
        factory = _Factory(gateway=_gateway(SqlResult(rows=(), columns=())))

        result = runner.invoke(app, ["search", "nothing matches", "--bm25-only"], obj=factory)

        assert result.exit_code == 0, result.stdout
        assert "no" in result.stdout.lower()

    def test_gateway_failure_exits_one_cleanly(self):
        result = runner.invoke(app, ["search", "graph rag", "--bm25-only"], obj=_boom_factory())

        assert result.exit_code == 1
        assert "Traceback" not in result.stdout

    def test_an_explicit_knowledge_base_flag_is_never_refused_from_inside_another_knowledge_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        named = tmp_path / "named"
        (named / "Units").mkdir(parents=True)
        (named / "_meta").mkdir()
        (named / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
        cwd = tmp_path / "alpha-cwd"
        cwd.mkdir()
        (cwd / "knowledge-base-schema.json").write_text(json.dumps({"slug": "alpha", "page_types": [{"page_type": "note"}]}), encoding="utf-8")
        monkeypatch.chdir(cwd)
        factory = _Factory(gateway=_gateway(_search_result()))

        result = runner.invoke(app, ["search", "graph rag", "--knowledge-base", str(named)], obj=factory)

        assert result.exit_code == 0, result.stdout
        assert "two knowledge bases in play." not in result.stderr


class TestSearchUnreachableDatabase:
    """An unreachable database is two clean lines and exit 1, never a `search failed` traceback."""

    def test_prints_the_knowledge_base_and_unreachable_lines_and_exits_one_with_no_search_failed_text(self, tmp_path: Path) -> None:
        named = tmp_path / "named"
        (named / "Units").mkdir(parents=True)
        (named / "_meta").mkdir()
        (named / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")

        result = runner.invoke(app, ["search", "graph rag", "--knowledge-base", str(named)], obj=_unreachable_factory())

        lines = result.stdout.splitlines()
        assert result.exit_code == 1
        assert lines[0].startswith("knowledge base ")
        assert lines[1].startswith("db ")
        assert lines[1].endswith("unreachable, connection refused.")
        assert len(lines) == 2
        assert "search failed" not in result.stdout
        assert "Traceback" not in result.stdout


class TestFetch:
    def test_renders_the_unit_and_exits_zero(self):
        factory = _Factory(gateway=_gateway(_fetch_result()))

        result = runner.invoke(app, ["fetch", "paper.lightrag-2025#c1"], obj=factory)

        assert result.exit_code == 0, result.stdout
        assert "paper.lightrag-2025#c1" in result.stdout
        assert "claim" in result.stdout
        assert "UltraDomain" in result.stdout
        assert factory.gateway.last.params["unit_id"] == "paper.lightrag-2025#c1"

    def test_truncation_marker_is_surfaced(self):
        truncated = _fetch_result(body="A very long card body … [truncated — fetch sections instead]")
        factory = _Factory(gateway=_gateway(truncated))

        result = runner.invoke(app, ["fetch", "paper.lightrag-2025"], obj=factory)

        assert result.exit_code == 0, result.stdout
        assert "truncated" in result.stdout.lower()

    def test_unknown_id_exits_one(self):
        factory = _Factory(gateway=_gateway(SqlResult(rows=(), columns=())))

        result = runner.invoke(app, ["fetch", "paper.does-not-exist"], obj=factory)

        assert result.exit_code == 1
        assert "paper.does-not-exist" in result.stdout

    def test_an_explicit_knowledge_base_flag_is_never_refused_from_inside_another_knowledge_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        named = tmp_path / "named"
        (named / "Units").mkdir(parents=True)
        (named / "_meta").mkdir()
        (named / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
        cwd = tmp_path / "alpha-cwd"
        cwd.mkdir()
        (cwd / "knowledge-base-schema.json").write_text(json.dumps({"slug": "alpha", "page_types": [{"page_type": "note"}]}), encoding="utf-8")
        monkeypatch.chdir(cwd)
        factory = _Factory(gateway=_gateway(_fetch_result()))

        result = runner.invoke(app, ["fetch", "paper.lightrag-2025#c1", "--knowledge-base", str(named)], obj=factory)

        assert result.exit_code == 0, result.stdout
        assert "two knowledge bases in play." not in result.stderr

    def test_gateway_failure_exits_one_cleanly(self):
        result = runner.invoke(app, ["fetch", "paper.lightrag-2025#c1"], obj=_boom_factory())

        assert result.exit_code == 1
        assert "Traceback" not in result.stdout


class TestMcpProfileName:
    """The MCP tool surface comes from the knowledge base's `knowledge-base-schema.json` profile; the env var only overrides when non-default."""

    def test_knowledge_base_profile_drives_when_override_is_default(self):
        assert _mcp_profile_name("code-mode", "full") == "code-mode"

    def test_explicit_override_wins_over_the_knowledge_base(self):
        assert _mcp_profile_name("code-mode", "graph-only") == "graph-only"

    def test_both_default_resolves_full(self):
        assert _mcp_profile_name("full", "full") == "full"


class TestFeedbackSink:
    async def test_appends_one_jsonl_entry_per_call(self, tmp_path: Path):
        path = tmp_path / "_meta" / "mcp-feedback.jsonl"
        sink = _make_feedback_sink(path)

        await sink(category="feature", message="add CSV export", tool="run_sql")
        await sink(category="bug", message="fetch crashed on a long id", tool=None)

        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert (first["category"], first["message"], first["tool"]) == ("feature", "add CSV export", "run_sql")
        assert first["created_at"]  # stamped by the sink
        assert json.loads(lines[1])["tool"] is None


class TestRedactDb:
    def test_drops_credentials_and_keeps_host_port_db(self):
        redacted = redact_db("postgresql+asyncpg://user:secret@db.example.com:5432/knowledge_bases?ssl=require")

        assert redacted == "db.example.com:5432/knowledge_bases"
        assert "secret" not in redacted
        assert "user" not in redacted


class TestHelp:
    def test_search_help_lists_the_flags(self):
        result = runner.invoke(app, ["search", "--help"])

        assert result.exit_code == 0
        assert "bm25-only" in result.stdout

    def test_fetch_help_exits_zero(self):
        result = runner.invoke(app, ["fetch", "--help"])

        assert result.exit_code == 0


def _make_knowledge_base(tmp_path: Path) -> KnowledgeBaseSettings:
    """A real `KnowledgeBaseSettings` over a minimal on-disk knowledge base carrying the research template's own schema (slug `research`)."""
    (tmp_path / "Units").mkdir()
    (tmp_path / "_meta").mkdir()
    (tmp_path / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
    return KnowledgeBaseSettings(path=tmp_path)


def _inert_engine() -> AsyncEngine:
    """A real, never-connected `AsyncEngine`.

    `_build_mcp_server`'s post-build `dispose()` call needs a genuine engine to act on, and this
    one never opens a socket.
    """
    return create_async_engine("postgresql+asyncpg://admin:pw@localhost:5433/knowledge_bases", poolclass=NullPool)


class _FakeReaderSqlGateway(ReaderSqlGateway):
    """A `ReaderSqlGateway` whose reads delegate to a wrapped `FakeSqlGateway`.

    A real subtype (so `isinstance` narrowing resolves it for this DI key), constructed over a
    real, never-connected engine that its overridden methods never touch.
    """

    def __init__(self, engine: AsyncEngine, fake: FakeSqlGateway) -> None:
        super().__init__(engine)
        self._fake = fake

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        return await self._fake.rows(sql, params, cap=cap)

    async def api_objects(self, names: tuple[str, ...] = ()) -> SchemaDescription:
        return await self._fake.api_objects(names)


class _FakeAdminSqlGateway(AdminSqlGateway):
    """An `AdminSqlGateway` whose reads delegate to a wrapped `FakeSqlGateway` (see `_FakeReaderSqlGateway`)."""

    def __init__(self, engine: AsyncEngine, fake: FakeSqlGateway) -> None:
        super().__init__(engine)
        self._fake = fake

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        return await self._fake.rows(sql, params, cap=cap)

    async def api_objects(self, names: tuple[str, ...] = ()) -> SchemaDescription:
        return await self._fake.api_objects(names)


def _two_object_schema() -> SchemaDescription:
    """A tiny two-object schema, enough to prove a `names` filter actually narrows the answer."""
    return SchemaDescription(
        objects=(
            ObjectInfo(
                name="api.cards", kind="view", comment="Compact page rows.", columns=(ColumnInfo(name="id", data_type="text", comment="Page id."),)
            ),
            ObjectInfo(name="api.search", kind="function", comment="Hybrid search.", columns=()),
        )
    )


class TestFakeGatewaysFilterApiObjectsByName:
    """Drift guard: `_FakeReaderSqlGateway`/`_FakeAdminSqlGateway` must keep passing `names` through.

    `ReadOnlySqlGateway.api_objects` filters when `names` is given; these two subtypes exist so the
    DI container resolves a real `ReaderSqlGateway`/`AdminSqlGateway` instance, and if either one
    silently dropped `names` again, `describe_schema(objects=[...])` would answer every object
    instead of just the ones asked for.
    """

    async def test_describe_schema_through_the_reader_fake_answers_only_the_named_object(self):
        fake = FakeSqlGateway(schema=_two_object_schema())
        handler = DescribeSchemaHandler(_FakeReaderSqlGateway(_inert_engine(), fake))

        rendered = await handler(DescribeSchema(objects=("api.cards",)))

        assert "api.cards" in rendered
        assert "Page id." in rendered
        assert "api.search" not in rendered

    async def test_the_admin_fake_filters_the_same_way(self):
        fake = FakeSqlGateway(schema=_two_object_schema())
        admin = _FakeAdminSqlGateway(_inert_engine(), fake)

        description = await admin.api_objects(names=("cards",))

        assert {obj.name for obj in description.objects} == {"api.cards"}


@dataclass
class _McpServerContainer:
    """Fake async DI container resolving exactly the deps `_build_mcp_server` asks for.

    `gateway` backs the reader role (the built server's `search`/`fetch`). `admin_gateway` backs
    the admin role the freshness stamp reads through, a separate fake since the stamp's manifest
    and indexed-at queries are shaped nothing like a search or fetch row. `reader` backs the
    stamp's file walk, and `engine` is a real, never-connected engine standing in for the pooled
    admin engine.
    """

    knowledge_base: KnowledgeBaseSettings
    gateway: FakeSqlGateway = field(default_factory=FakeSqlGateway)
    admin_gateway: FakeSqlGateway = field(default_factory=FakeSqlGateway)
    embedder: FakeEmbedding = field(default_factory=FakeEmbedding)
    reader: FakeKnowledgeBaseReader = field(default_factory=FakeKnowledgeBaseReader)
    retrieval: RetrievalSettings = field(default_factory=RetrievalSettings)
    search_reranking: SearchReranking = field(default_factory=lambda: SearchReranking(reranking=None))
    engine: AsyncEngine = field(default_factory=_inert_engine)

    async def get[T](self, klass: type[T]) -> T:
        served = "research"
        services: dict[type, object] = {
            ReaderSqlGateway: _FakeReaderSqlGateway(self.engine, self.gateway),
            AdminSqlGateway: _FakeAdminSqlGateway(self.engine, self.admin_gateway),
            DescribeSchemaHandler: DescribeSchemaHandler(self.gateway),
            GraphExpandHandler: GraphExpandHandler(self.gateway, served),
            GraphPathsHandler: GraphPathsHandler(self.gateway, served),
            SearchHandler: SearchHandler(self.gateway, self.embedder),
            FetchHandler: FetchHandler(self.gateway, served),
            EnvelopeHandler: EnvelopeHandler(self.gateway),
            RetrievalSettings: self.retrieval,
            SearchReranking: self.search_reranking,
            KnowledgeBaseSettings: self.knowledge_base,
            DatabaseSettings: DatabaseSettings(
                url="postgresql+asyncpg://u:p@h:5433/knowledge_bases", reader_url="postgresql+asyncpg://r:p@h:5433/knowledge_bases"
            ),
            BatchEmbedder: self.embedder,
            KnowledgeBaseReader: self.reader,
            AsyncEngine: self.engine,
        }
        service = services.get(klass)
        if not isinstance(service, klass):
            msg = f"_McpServerContainer has no registration for {klass!r}"
            raise KeyError(msg)
        return service


class TestBuildMcpServerStamp:
    """`_build_mcp_server` builds a freshness `stamp` from the admin gateway and the knowledge base reader."""

    async def test_search_answers_carry_a_knowledge_base_stamp(self, tmp_path: Path):
        rows = ({"id": "paper.lightrag-2025", "hit_level": "card", "page_id": "paper.lightrag-2025", "snippet": "LightRAG", "score": 0.91},)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        container = _McpServerContainer(knowledge_base=_make_knowledge_base(tmp_path), gateway=gateway)

        server = await _build_mcp_server(container, authoring=False)

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool("search", {"query": "graph rag"})

        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["knowledge_base"]["slug"] == "research"


def _make_auto_knowledge_base(tmp_path: Path) -> KnowledgeBaseSettings:
    """A knowledge base like `_make_knowledge_base`'s whose schema lets the stored switches decide its retrieval."""
    settings = _make_knowledge_base(tmp_path)
    spec = research_spec()
    auto = spec.model_copy(update={"retrieval": spec.retrieval.model_copy(update={"mode": "auto"})})
    (tmp_path / "knowledge-base-schema.json").write_text(auto.model_dump_json(), encoding="utf-8")
    return settings


def _stored_switches(*, graph: bool, embeddings: bool) -> dict[str, SqlResult]:
    """A `routed` script answering `api.retrieval_switches` with these two switches."""
    stored = {"embeddings": embeddings, "sql": False, "graph": graph, "private": False}
    return {"api.retrieval_switches": SqlResult(rows=({"switches": stored},), columns=("switches",))}


class _UnreachableReaderGateway(FakeSqlGateway):
    """A reader fake whose every read raises the dead-port connection failure."""

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        del sql, params, cap
        msg = "[Errno 61] Connect call failed ('127.0.0.1', 5434)"
        raise ConnectionRefusedError(msg)


class TestBuildMcpServerSwitches:
    """`studio mcp` over an auto-mode knowledge base serves what its stored switches decided."""

    _HIT = ({"id": "paper.lightrag-2025", "hit_level": "card", "page_id": "paper.lightrag-2025", "snippet": "LightRAG", "score": 0.91},)

    async def _served(self, container: _McpServerContainer, tmp_path: Path) -> set[str]:
        server = await _build_mcp_server(container, settings_path=tmp_path / "no-registry.json")
        async with connect_in_memory(server) as client:
            await client.initialize()
            listing = await client.list_tools()
        return {tool.name for tool in listing.tools}

    async def test_a_graph_switch_that_is_off_withholds_the_graph_tools(self, tmp_path: Path):
        gateway = FakeSqlGateway(routed=_stored_switches(graph=False, embeddings=True))
        container = _McpServerContainer(knowledge_base=_make_auto_knowledge_base(tmp_path), gateway=gateway)

        served = await self._served(container, tmp_path)

        assert {"search", "fetch"} <= served
        assert "graph_expand" not in served
        assert "find_paths" not in served

    async def test_a_graph_switch_that_is_on_serves_the_graph_tools(self, tmp_path: Path):
        gateway = FakeSqlGateway(routed=_stored_switches(graph=True, embeddings=True))
        container = _McpServerContainer(knowledge_base=_make_auto_knowledge_base(tmp_path), gateway=gateway)

        served = await self._served(container, tmp_path)

        assert {"search", "fetch", "graph_expand", "find_paths"} <= served

    async def test_an_unreachable_database_still_builds_the_server_with_every_switch_off(self, tmp_path: Path):
        container = _McpServerContainer(knowledge_base=_make_auto_knowledge_base(tmp_path), gateway=_UnreachableReaderGateway())

        served = await self._served(container, tmp_path)

        assert {"search", "fetch"} <= served
        assert "graph_expand" not in served

    async def test_a_fixed_mode_knowledge_base_never_reads_the_switches(self, tmp_path: Path):
        gateway = FakeSqlGateway(routed=_stored_switches(graph=False, embeddings=False))
        container = _McpServerContainer(knowledge_base=_make_knowledge_base(tmp_path), gateway=gateway)

        served = await self._served(container, tmp_path)

        assert {"search", "fetch", "graph_expand", "find_paths"} <= served
        assert not any("api.retrieval_switches" in call.sql for call in gateway.rows_calls)

    async def test_an_embeddings_switch_that_is_off_runs_search_keyword_only(self, tmp_path: Path):
        routed = {**_stored_switches(graph=True, embeddings=False), "api.search": SqlResult(rows=self._HIT, columns=())}
        embedder = FakeEmbedding()
        container = _McpServerContainer(knowledge_base=_make_auto_knowledge_base(tmp_path), gateway=FakeSqlGateway(routed=routed), embedder=embedder)
        server = await _build_mcp_server(container, settings_path=tmp_path / "no-registry.json")

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool("search", {"query": "graph rag"})

        assert result.isError is False
        assert embedder.texts == []

    async def test_an_embeddings_switch_that_is_on_runs_the_vector_leg(self, tmp_path: Path):
        routed = {**_stored_switches(graph=True, embeddings=True), "api.search": SqlResult(rows=self._HIT, columns=())}
        embedder = FakeEmbedding()
        container = _McpServerContainer(knowledge_base=_make_auto_knowledge_base(tmp_path), gateway=FakeSqlGateway(routed=routed), embedder=embedder)
        server = await _build_mcp_server(container, settings_path=tmp_path / "no-registry.json")

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool("search", {"query": "graph rag"})

        assert result.isError is False
        assert embedder.texts == ["graph rag"]
