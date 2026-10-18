"""Unit specs for the knowledge base HTTP API (`studio.knowledge_base.http.api`).

The app serves the dashboard's `/api` contract: ``GET /api/graph`` (the Graph station read),
``GET /api/knowledge-bases`` (the knowledge-base registry merged with indexed DB vitals), ``POST
/api/knowledge-bases`` (scaffold + register a new knowledge base), and ``GET /api/knowledge-bases/{name}/schema`` (a
registered knowledge base's parsed spec). These pin each route's contract plus the app frame around it:

* CORS opens exactly the dashboard origin;
* an unexpected failure comes back as the uniform ``{error, detail}`` body, never a traceback;
* every knowledge bases refusal path (name collision, non-empty target directory without `force`, an
  unregistered name on the spec read, an invalid spec body) answers the same uniform shape.
"""

from __future__ import annotations

import asyncio
import json
import sys

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from core.cqrs.contracts import Broadcaster, JobPolicy, MemoryJobs
from core.supervisor import SupervisedChild
from engram.retrieval.domain import SqlResult
from engram.sources.contracts import SourceQueue, SourceQueueStatus, WatchListFile, claimants_for
from engram.sources.infrastructure.xml_scanner import XML_SCANNER
from httpx import ASGITransport, AsyncClient

from studio.knowledge_base.http.api import create_app
from studio.knowledge_base.http.api_models import error_response
from studio.knowledge_base.http.exceptions import KnowledgeBaseRefusal, unknown_knowledge_base_error
from studio.knowledge_base.refresh.contracts import REFRESH_KIND, REFRESH_POLICY, RefreshJob
from studio.knowledge_base.settings import ApplicationSettings
from tests.support.fakes.topics import FakeListener, FakeNotifier
from tests.support.http_app import AppWiring, raw_stream, stop_stream, wiring, write_settings_json

if TYPE_CHECKING:
    from asyncio.subprocess import Process
    from collections.abc import AsyncGenerator, AsyncIterator
    from pathlib import Path

    from pydantic import JsonValue


def _knowledge_base(tmp_path: Path) -> Path:
    """A minimal knowledge base tree satisfying `KnowledgeBaseSettings.path` validation."""
    for sub in ("Units", "_meta"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _settings(tmp_path: Path) -> ApplicationSettings:
    """Application settings for the app factory, read from a real settings file (never read by the graph route itself)."""
    return ApplicationSettings(json_file=write_settings_json(tmp_path / "application_settings.json"))


def _write_settings_json(path: Path, knowledge_bases: dict[str, dict[str, JsonValue]] | None = None) -> None:
    """Write a real settings JSON file on disk (the knowledge bases routes re-read this per request)."""
    write_settings_json(path, knowledge_bases)


def _settings_at(settings_path: Path) -> ApplicationSettings:
    """`ApplicationSettings` reloaded from the on-disk registry JSON.

    A knowledge base is not configuration: the process serving the registry acts on none, so no
    knowledge base tree is threaded through this reload.
    """
    return ApplicationSettings(json_file=settings_path)


def _knowledge_bases_app_settings(tmp_path: Path, *, active_collection: str = "active-knowledge-base") -> tuple[ApplicationSettings, Path]:
    """Application settings plus the on-disk settings JSON file the knowledge bases routes target.

    The knowledge base at `tmp_path / "active"` carries its own valid `knowledge-base-schema.json` (collection
    `active_collection`) but starts out unregistered, against an empty on-disk registry a test
    populates as it goes.
    """
    settings_path = tmp_path / "application_settings.json"
    _knowledge_base_with_spec(tmp_path / "active", "active", collection=active_collection)
    _write_settings_json(settings_path)
    return _settings_at(settings_path), settings_path


def _minimal_spec_payload(slug: str, *, collection: str | None = None) -> dict[str, JsonValue]:
    """The smallest `KnowledgeBaseSchema` JSON body that passes validation: one page type, and an explicit collection.

    `collection` defaults to `slug` so two test knowledge bases built from different slugs never collide on
    the shared `RetrievalSpec.collection` default (`"research"`).
    """
    return {
        "slug": slug,
        "page_types": [{"page_type": "concept", "id_prefix": "c-"}],
        "retrieval": {"collection": collection if collection is not None else slug},
    }


def _knowledge_base_with_spec(root: Path, slug: str, *, collection: str | None = None) -> Path:
    """A knowledge base directory satisfying `KnowledgeBaseSettings.path` validation AND carrying its own `knowledge-base-schema.json`."""
    _knowledge_base(root)
    (root / "knowledge-base-schema.json").write_text(json.dumps(_minimal_spec_payload(slug, collection=collection)), encoding="utf-8")
    return root


def _template_dir(templates_root: Path, slug: str, *, collection: str | None = None, purpose: str = "") -> Path:
    """A fake template under `templates_root`: `knowledge-base-schema.json`, a titled `README.md`, and an optional `_meta/purpose.md`.

    `purpose` stands in for a template's own guidance: when set, a test can assert it survives
    the scaffold write untouched.
    """
    template_path = templates_root / slug
    template_path.mkdir(parents=True)
    (template_path / "knowledge-base-schema.json").write_text(json.dumps(_minimal_spec_payload(slug, collection=collection)), encoding="utf-8")
    (template_path / "README.md").write_text(f"# {slug}\n", encoding="utf-8")
    if purpose:
        meta = template_path / "_meta"
        meta.mkdir()
        (meta / "purpose.md").write_text(purpose, encoding="utf-8")
    return template_path


def _fake_broadcaster() -> Broadcaster:
    """A broadcaster over fake connections, so a lifespan run in a test never opens Postgres."""
    return Broadcaster("postgresql://u:p@h/db", notifier=FakeNotifier(), listener=FakeListener())


@asynccontextmanager
async def _client(
    settings: ApplicationSettings, container: AppWiring, *, settings_path: Path | None = None, jobs: MemoryJobs | None = None
) -> AsyncGenerator[AsyncClient]:
    """An httpx client over the app (ASGI transport, lifespan run, no listening socket).

    `base_url` is a `TrustedHostMiddleware`-allowed host (`localhost`) -- an ordinary test call is
    not itself the "foreign host" scenario `TestTrustedHost` covers, so it must not trip that gate.
    """
    app = create_app(
        settings,
        container=container.container,
        settings_path=settings_path,
        jobs=jobs if jobs is not None else MemoryJobs(),
        broadcaster=_fake_broadcaster(),
    )
    # raise_app_exceptions=False lets the app's catch-all exception handler answer (the wire
    # contract under test) instead of the transport re-raising into the test.
    async with (
        AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://localhost") as client,
        _lifespan(app),
    ):
        yield client


@asynccontextmanager
async def _lifespan(app) -> AsyncGenerator[None]:
    async with app.router.lifespan_context(app):
        yield


class TestGraph:
    async def test_a_knowledge_base_param_scopes_the_read_to_that_knowledge_base_s_collection(self, tmp_path: Path):
        knowledge_base_path = _knowledge_base_with_spec(tmp_path / "demo", "demo", collection="demo-collection")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"demo": {"path": str(knowledge_base_path), "description": ""}})
        settings = ApplicationSettings(json_file=settings_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.get("/api/graph", params={"knowledge_base": "demo", "page_type": "paper", "rel": "outperforms"})

        assert response.status_code == 200
        # A graph answer is the subgraph plus the block every bounded retrieval result carries: how much
        # of it came back, which knowledge base answered, and what to read next.
        assert set(response.json()) == {"nodes", "edges", "window", "reach", "knowledge_base", "next_steps"}
        assert container.gateway.last.params == {"collection": "demo-collection", "page_type": "paper", "rel": "outperforms"}

    async def test_answers_the_named_knowledge_base_s_collection_when_the_server_has_no_active_knowledge_base(self, tmp_path: Path):
        knowledge_base_path = _knowledge_base_with_spec(tmp_path / "demo", "demo", collection="demo-collection")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"demo": {"path": str(knowledge_base_path), "description": ""}})
        settings = ApplicationSettings(json_file=settings_path)

        container = wiring()
        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.get("/api/graph", params={"knowledge_base": "demo"})

        assert response.status_code == 200
        assert container.gateway.last.params["collection"] == "demo-collection"

    async def test_a_missing_knowledge_base_param_is_refused(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)

        async with _client(settings, wiring(), settings_path=settings_path) as client:
            response = await client.get("/api/graph")

        assert response.status_code == 400
        assert response.json()["error"] == "knowledge_base_required"

    async def test_an_unknown_knowledge_base_is_a_404(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)

        async with _client(settings, wiring(), settings_path=settings_path) as client:
            response = await client.get("/api/graph", params={"knowledge_base": "ghost"})

        assert response.status_code == 404
        assert response.json()["error"] == "unknown_knowledge_base"


class TestCors:
    async def test_localhost_origin_is_allowed(self, tmp_path: Path):
        container = wiring()

        async with _client(_settings(tmp_path), container) as client:
            response = await client.get("/api/graph", headers={"Origin": "http://localhost:3031"})

        assert response.headers.get("access-control-allow-origin") == "http://localhost:3031"

    async def test_the_loopback_address_origin_is_allowed(self, tmp_path: Path):
        container = wiring()

        async with _client(_settings(tmp_path), container) as client:
            response = await client.get("/api/graph", headers={"Origin": "http://127.0.0.1:3031"})

        assert response.headers.get("access-control-allow-origin") == "http://127.0.0.1:3031"

    async def test_a_foreign_origin_is_not_allowed(self, tmp_path: Path):
        container = wiring()

        async with _client(_settings(tmp_path), container) as client:
            response = await client.get("/api/graph", headers={"Origin": "http://evil.example.com"})

        assert "access-control-allow-origin" not in response.headers


class TestTrustedHost:
    async def test_a_foreign_host_header_is_refused(self, tmp_path: Path):
        app = create_app(_settings(tmp_path), container=wiring().container, jobs=MemoryJobs(), broadcaster=_fake_broadcaster())

        async with (
            AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://evil.example.com") as client,
            _lifespan(app),
        ):
            response = await client.get("/api/graph")

        assert response.status_code == 400

    async def test_localhost_and_127_0_0_1_are_both_allowed(self, tmp_path: Path):
        knowledge_base_path = _knowledge_base_with_spec(tmp_path / "demo", "demo")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"demo": {"path": str(knowledge_base_path), "description": ""}})
        settings = ApplicationSettings(json_file=settings_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            localhost_response = await client.get("/api/graph", params={"knowledge_base": "demo"})

        assert localhost_response.status_code == 200

        app = create_app(
            settings, container=wiring().container, settings_path=settings_path, jobs=MemoryJobs(), broadcaster=_fake_broadcaster()
        )
        async with (
            AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://127.0.0.1") as client,
            _lifespan(app),
        ):
            loopback_response = await client.get("/api/graph", params={"knowledge_base": "demo"})

        assert loopback_response.status_code == 200


class TestErrors:
    async def test_unexpected_failure_is_a_uniform_500_body(self, tmp_path: Path):
        knowledge_base_path = _knowledge_base_with_spec(tmp_path / "demo", "demo")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"demo": {"path": str(knowledge_base_path), "description": ""}})
        settings = ApplicationSettings(json_file=settings_path)
        container = wiring()
        container.gateway.result = SqlResult(rows=({"nonsense": object()},), columns=("nonsense",))

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.get("/api/graph", params={"knowledge_base": "demo"})

        assert response.status_code == 500
        assert set(response.json()) == {"error", "detail"}


class TestErrorResponse:
    def test_extra_fields_follow_error_and_detail_in_the_body(self):
        response = error_response(409, "refresh_in_progress", "a refresh is live.", extra={"job_id": 7})

        assert response.status_code == 409
        body = json.loads(bytes(response.body))
        assert list(body) == ["error", "detail", "job_id"]
        assert body == {"error": "refresh_in_progress", "detail": "a refresh is live.", "job_id": 7}

    def test_without_extra_the_body_is_error_and_detail(self):
        response = error_response(404, "unknown_job", "no job with id 3")

        assert json.loads(bytes(response.body)) == {"error": "unknown_job", "detail": "no job with id 3"}


class TestUnknownKnowledgeBaseError:
    def test_names_the_knowledge_base_nothing_registers(self):
        error = unknown_knowledge_base_error("ghost")

        assert error.refusal is KnowledgeBaseRefusal.UNKNOWN_KNOWLEDGE_BASE
        assert str(error) == "no knowledge base named 'ghost' is registered"


_UNKNOWN_NAME_REQUESTS: tuple[tuple[str, str, dict[str, JsonValue] | None], ...] = (
    ("GET", "/api/knowledge-bases/ghost/schema", None),
    ("GET", "/api/graph?knowledge_base=ghost", None),
    ("GET", "/api/knowledge-bases/ghost/refresh-runs", None),
    ("GET", "/api/knowledge-bases/ghost/decisions", None),
    ("POST", "/api/knowledge-bases/ghost/source-queue", {"url": "https://example.com/paper"}),
    ("POST", "/api/knowledge-bases/ghost/source-queue/some-key/retry", None),
    ("POST", "/api/knowledge-bases/ghost/watch-list", {"scanner": "xml", "ref": "https://example.com/feed.xml"}),
    ("DELETE", "/api/knowledge-bases/ghost/watch-list?scanner=xml&ref=https://example.com/feed.xml", None),
    ("PATCH", "/api/knowledge-bases/ghost/pages/c-one", {"status": "accepted"}),
    ("POST", "/api/knowledge-bases/ghost/refresh", None),
    ("PUT", "/api/knowledge-bases/ghost/schedule", {"every_seconds": 600}),
    ("DELETE", "/api/knowledge-bases/ghost/schedule", None),
    ("DELETE", "/api/knowledge-bases/ghost/registration", None),
)


class TestUnknownKnowledgeBase:
    @pytest.mark.parametrize(("method", "url", "body"), _UNKNOWN_NAME_REQUESTS)
    async def test_every_route_answers_the_same_404_for_a_name_nothing_registers(
        self, tmp_path: Path, method: str, url: str, body: dict[str, JsonValue] | None
    ):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)

        async with _client(settings, wiring(), settings_path=settings_path) as client:
            response = await client.request(method, url, json=body)

        assert response.status_code == 404
        assert response.json() == {"error": "unknown_knowledge_base", "detail": "no knowledge base named 'ghost' is registered"}


class TestKnowledgeBasesList:
    async def test_lists_registered_and_indexed_states(self, tmp_path: Path):
        registered_only = _knowledge_base(tmp_path / "registered_only")
        registered_and_indexed = _knowledge_base(tmp_path / "both")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(
            settings_path,
            knowledge_bases={
                "registered-only": {"path": str(registered_only), "description": "not indexed yet"},
                "registered-and-indexed": {"path": str(registered_and_indexed), "description": "live"},
            },
        )
        settings = _settings_at(settings_path)
        container = wiring()
        indexed_time = datetime(2026, 8, 20, tzinfo=UTC)
        container.gateway.result = SqlResult(
            rows=(
                {"collection": "registered-and-indexed", "pages": 12, "page_types": ["concept", "paper"], "last_indexed": indexed_time},
                {"collection": "indexed-only", "pages": 3, "page_types": ["note"], "last_indexed": None},
            ),
            columns=("collection", "pages", "page_types", "last_indexed"),
        )

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.get("/api/knowledge-bases")

        assert response.status_code == 200
        by_name = {row["name"]: row for row in response.json()["knowledge_bases"]}
        assert by_name.keys() == {"registered-only", "registered-and-indexed", "indexed-only"}

        assert by_name["registered-only"] == {
            "name": "registered-only",
            "registered": True,
            "indexed": False,
            "broken": False,
            "path": str(registered_only),
            "description": "not indexed yet",
            "pages": None,
            "page_types": [],
            "last_indexed": None,
            "private": False,
            "collection": None,
        }
        assert by_name["registered-and-indexed"]["registered"] is True
        assert by_name["registered-and-indexed"]["indexed"] is True
        assert by_name["registered-and-indexed"]["pages"] == 12
        assert by_name["registered-and-indexed"]["page_types"] == ["concept", "paper"]
        assert by_name["indexed-only"]["registered"] is False
        assert by_name["indexed-only"]["indexed"] is True
        assert by_name["indexed-only"]["path"] is None

    async def test_an_empty_registry_still_lists_indexed_collections(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()
        container.gateway.result = SqlResult(
            rows=({"collection": "orphaned", "pages": 1, "page_types": ["note"], "last_indexed": None},),
            columns=("collection", "pages", "page_types", "last_indexed"),
        )

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.get("/api/knowledge-bases")

        assert response.status_code == 200
        names = {row["name"] for row in response.json()["knowledge_bases"]}
        assert names == {"orphaned"}


class TestKnowledgeBasesListPrivacyAndCollection:
    """Every row says whether a library may hold it, and which collection its rows carry.

    The two fields an agent host reads this listing for: it opens its library over every name it
    may see, and addresses each door by the collection the knowledge base's own spec declares.
    """

    async def test_an_ordinary_row_is_not_private_and_carries_its_collection(self, tmp_path: Path):
        public = _knowledge_base_with_spec(tmp_path / "public", "public", collection="public-collection")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"public": {"path": str(public), "description": ""}})
        settings = _settings_at(settings_path)

        async with _client(settings, wiring(), settings_path=settings_path) as client:
            response = await client.get("/api/knowledge-bases")

        assert response.status_code == 200
        row = next(row for row in response.json()["knowledge_bases"] if row["name"] == "public")
        assert row["private"] is False
        assert row["collection"] == "public-collection"

    async def test_a_row_declaring_private_is_private(self, tmp_path: Path):
        vault = _knowledge_base_with_spec(tmp_path / "vault", "vault")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"vault": {"path": str(vault), "description": "", "private": True}})
        settings = _settings_at(settings_path)

        async with _client(settings, wiring(), settings_path=settings_path) as client:
            response = await client.get("/api/knowledge-bases")

        assert response.status_code == 200
        row = next(row for row in response.json()["knowledge_bases"] if row["name"] == "vault")
        assert row["private"] is True, "the deployment's own registry row keeps it out of any library"
        assert row["collection"] == "vault", "private is about the library, the collection is still named"

    async def test_a_schema_declaring_retrieval_private_is_private(self, tmp_path: Path):
        vault = _knowledge_base(tmp_path / "vault")
        spec = _minimal_spec_payload("vault")
        spec["retrieval"] = {"collection": "vault", "private": True}
        (vault / "knowledge-base-schema.json").write_text(json.dumps(spec), encoding="utf-8")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"vault": {"path": str(vault), "description": ""}})
        settings = _settings_at(settings_path)

        async with _client(settings, wiring(), settings_path=settings_path) as client:
            response = await client.get("/api/knowledge-bases")

        assert response.status_code == 200
        row = next(row for row in response.json()["knowledge_bases"] if row["name"] == "vault")
        assert row["private"] is True, "the knowledge base's own declaration is enough, with no registry flag at all"

    async def test_a_row_whose_spec_cannot_be_read_still_lists(self, tmp_path: Path):
        spec_less = _knowledge_base(tmp_path / "spec-less")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"spec-less": {"path": str(spec_less), "description": ""}})
        settings = _settings_at(settings_path)

        async with _client(settings, wiring(), settings_path=settings_path) as client:
            response = await client.get("/api/knowledge-bases")

        assert response.status_code == 200
        row = next(row for row in response.json()["knowledge_bases"] if row["name"] == "spec-less")
        assert row["collection"] is None
        assert row["private"] is False


class TestKnowledgeBasesListStaleRow:
    """A stale row must degrade to one missing/flagged row, never a whole-route failure.

    A registry row whose directory moved or was deleted after the settings file was last validated.
    """

    async def test_a_stale_row_does_not_500_the_list(self, tmp_path: Path):
        healthy = _knowledge_base(tmp_path / "healthy")
        gone = tmp_path / "gone"
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"healthy": {"path": str(healthy), "description": ""}})
        settings = _settings_at(settings_path)  # succeeds: no stale row yet

        # Add the stale row directly to the on-disk JSON, bypassing ApplicationSettings' own
        # strict construction-time validation -- simulating a directory that moved or was deleted
        # sometime AFTER the file was last successfully loaded (the app never reconstructs a full
        # ApplicationSettings on every request, only the knowledge bases routes' own lenient readers do).
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        data["knowledge_bases"]["stale"] = {"path": str(gone), "description": "moved away"}
        settings_path.write_text(json.dumps(data), encoding="utf-8")

        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.get("/api/knowledge-bases")

        assert response.status_code == 200
        by_name = {row["name"]: row for row in response.json()["knowledge_bases"]}
        assert by_name["healthy"]["broken"] is False
        assert by_name["healthy"]["registered"] is True
        assert by_name["stale"]["broken"] is True
        assert by_name["stale"]["registered"] is True
        assert by_name["stale"]["path"] == str(gone)
        # Never the raw pydantic validation text, only the controlled `broken` signal.
        assert "validation error" not in response.text.lower()

    async def test_post_still_works_with_a_stale_row_present(self, tmp_path: Path):
        active = _knowledge_base_with_spec(tmp_path / "active", "active", collection="active-knowledge-base")
        gone = tmp_path / "gone"
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"active-knowledge-base": {"path": str(active), "description": ""}})
        settings = _settings_at(settings_path)

        data = json.loads(settings_path.read_text(encoding="utf-8"))
        data["knowledge_bases"]["stale"] = {"path": str(gone), "description": ""}
        settings_path.write_text(json.dumps(data), encoding="utf-8")

        container = wiring()
        target = tmp_path / "new-knowledge-base"

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.post("/api/knowledge-bases", json={"name": "demo", "path": str(target), "schema": _minimal_spec_payload("demo")})

        assert response.status_code == 200
        assert (target / "knowledge-base-schema.json").is_file()


class TestTemplatesRoute:
    async def test_lists_templates_under_engram_templates(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        _template_dir(templates_dir, "widgets")
        monkeypatch.setenv("ENGRAM_TEMPLATES", str(templates_dir))
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.get("/api/templates")

        assert response.status_code == 200
        assert response.json() == {"templates": [{"slug": "widgets", "title": "widgets", "path": str(templates_dir / "widgets")}]}

    async def test_no_templates_directory_answers_an_empty_list(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ENGRAM_TEMPLATES", str(tmp_path / "missing"))
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.get("/api/templates")

        assert response.status_code == 200
        assert response.json() == {"templates": []}


class TestKnowledgeBasesCreate:
    async def test_scaffolds_and_registers_a_new_knowledge_base(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()
        target = tmp_path / "new-knowledge-base"

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.post(
                "/api/knowledge-bases",
                json={"name": "demo", "path": str(target), "schema": _minimal_spec_payload("demo"), "description": "a demo knowledge base"},
            )

        assert response.status_code == 200
        body = response.json()
        assert "knowledge-base-schema.json" in body["written"]
        assert (target / "knowledge-base-schema.json").is_file()

        registered = json.loads(settings_path.read_text(encoding="utf-8"))["knowledge_bases"]
        assert registered["demo"] == {"path": str(target), "description": "a demo knowledge base"}

    async def test_refuses_a_name_already_in_the_registry(self, tmp_path: Path):
        existing = _knowledge_base(tmp_path / "existing")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"demo": {"path": str(existing), "description": ""}})
        settings = _settings_at(settings_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.post(
                "/api/knowledge-bases", json={"name": "demo", "path": str(tmp_path / "new"), "schema": _minimal_spec_payload("demo")}
            )

        assert response.status_code == 409
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "name_collision"

    @pytest.mark.parametrize("force", [False, True])
    async def test_refuses_a_path_already_registered_under_a_different_name(self, tmp_path: Path, force: bool):
        existing_path = _knowledge_base(tmp_path / "existing")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"other": {"path": str(existing_path), "description": ""}})
        settings = _settings_at(settings_path)
        container = wiring()
        before = settings_path.read_text(encoding="utf-8")

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.post(
                "/api/knowledge-bases", json={"name": "demo", "path": str(existing_path), "schema": _minimal_spec_payload("demo"), "force": force}
            )

        assert response.status_code == 409
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "path_collision"
        assert "other" in response.json()["detail"]
        assert settings_path.read_text(encoding="utf-8") == before

    async def test_refuses_a_non_empty_target_directory_without_force(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()
        target = tmp_path / "occupied"
        target.mkdir()
        (target / "existing.txt").write_text("keep me", encoding="utf-8")

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.post("/api/knowledge-bases", json={"name": "demo", "path": str(target), "schema": _minimal_spec_payload("demo")})

        assert response.status_code == 409
        assert response.json()["error"] == "directory_not_empty"
        assert (target / "knowledge-base-schema.json").exists() is False

    async def test_force_overwrites_a_non_empty_target_directory(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()
        target = tmp_path / "occupied"
        target.mkdir()
        (target / "existing.txt").write_text("keep me", encoding="utf-8")

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.post(
                "/api/knowledge-bases", json={"name": "demo", "path": str(target), "schema": _minimal_spec_payload("demo"), "force": True}
            )

        assert response.status_code == 200
        assert (target / "knowledge-base-schema.json").is_file()

    async def test_an_invalid_spec_is_refused_uniformly(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.post(
                "/api/knowledge-bases", json={"name": "demo", "path": str(tmp_path / "new"), "schema": {"slug": "demo", "page_types": []}}
            )

        assert response.status_code == 422
        assert set(response.json()) == {"error", "detail"}

    async def test_refuses_a_target_under_a_hidden_directory(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()
        target = tmp_path / ".claude" / "new-knowledge-base"

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.post("/api/knowledge-bases", json={"name": "demo", "path": str(target), "schema": _minimal_spec_payload("demo")})

        assert response.status_code == 400
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "hidden_directory"
        assert not target.exists()

    async def test_refuses_when_the_target_itself_is_a_hidden_directory(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()
        target = tmp_path / ".ssh"

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.post("/api/knowledge-bases", json={"name": "demo", "path": str(target), "schema": _minimal_spec_payload("demo")})

        assert response.status_code == 400
        assert response.json()["error"] == "hidden_directory"

    async def test_refuses_a_name_that_does_not_match_the_specs_collection(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()
        target = tmp_path / "new-knowledge-base"

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.post(
                "/api/knowledge-bases",
                json={"name": "marketing", "path": str(target), "schema": _minimal_spec_payload("demo", collection="totally-different")},
            )

        assert response.status_code == 400
        assert set(response.json()) == {"error", "detail"}
        assert response.json()["error"] == "name_collection_mismatch"
        assert not target.exists()

    async def test_creates_from_a_template_slug(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        _template_dir(templates_dir, "widgets", purpose="# Widgets purpose\n\nTemplate text.\n")
        monkeypatch.setenv("ENGRAM_TEMPLATES", str(templates_dir))
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()
        target = tmp_path / "new-knowledge-base"

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.post("/api/knowledge-bases", json={"name": "widgets", "path": str(target), "template": "widgets"})

        assert response.status_code == 200
        assert (target / "knowledge-base-schema.json").is_file()
        assert (target / "_meta" / "purpose.md").read_text(encoding="utf-8") == "# Widgets purpose\n\nTemplate text.\n"

        registered = json.loads(settings_path.read_text(encoding="utf-8"))["knowledge_bases"]
        assert registered["widgets"]["path"] == str(target)

    async def test_creating_from_a_template_with_force_keeps_the_template_s_guidance_pages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        templates_dir = tmp_path / "templates"
        _template_dir(templates_dir, "widgets", purpose="# Widgets purpose\n\nTemplate text.\n")
        monkeypatch.setenv("ENGRAM_TEMPLATES", str(templates_dir))
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()
        target = tmp_path / "occupied"
        target.mkdir()
        (target / "existing.txt").write_text("keep me", encoding="utf-8")

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.post("/api/knowledge-bases", json={"name": "widgets", "path": str(target), "template": "widgets", "force": True})

        assert response.status_code == 200
        assert (target / "_meta" / "purpose.md").read_text(encoding="utf-8") == "# Widgets purpose\n\nTemplate text.\n"

    async def test_refuses_schema_and_template_together(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        templates_dir = tmp_path / "templates"
        _template_dir(templates_dir, "widgets")
        monkeypatch.setenv("ENGRAM_TEMPLATES", str(templates_dir))
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.post(
                "/api/knowledge-bases",
                json={"name": "demo", "path": str(tmp_path / "new"), "schema": _minimal_spec_payload("demo"), "template": "widgets"},
            )

        assert response.status_code == 400
        assert response.json()["error"] == "schema_and_template"

    async def test_refuses_neither_schema_nor_template(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.post("/api/knowledge-bases", json={"name": "demo", "path": str(tmp_path / "new")})

        assert response.status_code == 400
        assert response.json()["error"] == "schema_or_template_required"

    async def test_refuses_an_unknown_template(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ENGRAM_TEMPLATES", str(tmp_path / "empty-templates"))
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.post("/api/knowledge-bases", json={"name": "demo", "path": str(tmp_path / "new"), "template": "nonexistent"})

        assert response.status_code == 400
        assert response.json()["error"] == "unknown_template"


class TestKnowledgeBaseSpec:
    async def test_returns_the_registered_knowledge_base_spec(self, tmp_path: Path):
        knowledge_base_path = _knowledge_base(tmp_path / "demo")
        spec_payload = _minimal_spec_payload("demo")
        (knowledge_base_path / "knowledge-base-schema.json").write_text(json.dumps(spec_payload), encoding="utf-8")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"demo": {"path": str(knowledge_base_path), "description": ""}})
        settings = _settings_at(settings_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.get("/api/knowledge-bases/demo/schema")

        assert response.status_code == 200
        assert response.json()["slug"] == "demo"

    async def test_refuses_an_unknown_name(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.get("/api/knowledge-bases/ghost/schema")

        assert response.status_code == 404
        assert set(response.json()) == {"error", "detail"}


_WATCH_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
_QUEUE_CONNECTORS = claimants_for(("intake",))

# The research template's own `flag` page type (templates/research/knowledge-base-schema.json),
# restated here, decision block included, so a minimal test knowledge base can parse and rewrite flag pages
# without depending on that template at runtime.
_FLAG_PAGE_TYPE: dict[str, JsonValue] = {
    "page_type": "flag",
    "id_prefix": "flag.",
    "folder": "Wiki/Flags",
    "frontmatter_fields": [
        {"name": "flag_kind", "type": "scalar", "required": True, "choices": ["idea", "feature", "risk", "question"]},
        {"name": "about", "type": "link-list", "required": True},
        {"name": "resolved_by", "type": "link-list"},
    ],
    "status_values": ["pending", "accepted", "dismissed", "completed"],
    "canonical_sections": ["why", "proposal", "evidence"],
    "edge_fields": ["about", "resolved_by"],
    "decision": {
        "open": "pending",
        "outcomes": [{"status": "accepted"}, {"status": "dismissed"}, {"status": "completed", "link": "resolved_by"}],
    },
}

FLAG = """---
id: flag.one
type: flag
title: Try planning reminders
summary: A live task list re-injected every turn.
status: pending
flag_kind: idea
about: ["[[talk.agents-that-plan]]"]
---

## Why

Because compaction drops the plan.

## Proposal

Mount the planning capability.

## RunEvidence

talk.agents-that-plan#findings
"""


def _queue_knowledge_base(root: Path, slug: str) -> Path:
    """A registered-shape knowledge base declaring the queue connector and the flag page type."""
    _knowledge_base(root)
    payload: dict[str, JsonValue] = {
        "slug": slug,
        "page_types": [_FLAG_PAGE_TYPE],
        "retrieval": {"collection": slug},
        "sources": {
            "enabled": True,
            "connectors": ["intake"],
            "key_field": "zotero_key",
            "digest_link_field": "digest_of",
            "hash_field": "source_hash",
            "digest_types": {"paper": "flag"},
        },
    }
    (root / "knowledge-base-schema.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


@pytest.fixture
def knowledge_base_path(tmp_path: Path) -> Path:
    """A registered-shape knowledge base named `tube`, declaring the queue connector and the flag page type."""
    return _queue_knowledge_base(tmp_path / "tube", "tube")


@pytest.fixture
async def app_client(knowledge_base_path: Path, tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """An `AsyncClient` over the app, with `knowledge_base_path` registered under the name `tube`."""
    settings_path = tmp_path / "application_settings.json"
    _write_settings_json(settings_path, knowledge_bases={"tube": {"path": str(knowledge_base_path), "description": ""}})
    settings = _settings_at(settings_path)
    async with _client(settings, wiring(), settings_path=settings_path) as client:
        yield client


class TestKnowledgeBaseDetail:
    async def test_merges_registry_vitals_validation_and_the_queue(self, tmp_path: Path):
        knowledge_base_path = _queue_knowledge_base(tmp_path / "tube", "tube")
        SourceQueue(knowledge_base_path / "_meta" / "source-queue.yaml", connectors=_QUEUE_CONNECTORS).add(_WATCH_URL)
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"tube": {"path": str(knowledge_base_path), "description": "vids"}})
        settings = _settings_at(settings_path)
        container = wiring()
        container.gateway.routed = {
            "kb.pages": SqlResult(
                rows=({"collection": "tube", "pages": 3, "page_types": ["talk"], "last_indexed": datetime(2026, 8, 20, tzinfo=UTC)},),
                columns=(),
            ),
            "validation_store": SqlResult(
                rows=({"page_id": "talk.a", "severity": "error", "rule": "dangling-link", "message": "unresolved", "anchor": None},),
                columns=(),
            ),
        }

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.get("/api/knowledge-bases/tube")

        assert response.status_code == 200
        body = response.json()
        assert (body["registered"], body["indexed"], body["pages"]) == (True, True, 3)
        assert body["description"] == "vids"
        assert body["sources_connector"] == "intake"
        assert body["sources_connectors"] == ["intake"]
        assert [issue["rule"] for issue in body["validation"]] == ["dangling-link"]
        assert [row["key"] for row in body["source_queue"]] == ["yt-dQw4w9WgXcQ"]
        assert body["source_queue"][0]["status"] == "requested"
        assert (body["last_refresh_run"], body["schedule"]) == (None, None)

    async def test_the_identity_field_names_the_knowledge_base_root_and_collection(self, app_client: AsyncClient, knowledge_base_path: Path) -> None:
        response = await app_client.get("/api/knowledge-bases/tube")

        assert response.status_code == 200
        identity = response.json()["identity"]
        assert identity["slug"] == "tube"
        assert identity["root"] == str(knowledge_base_path)
        assert identity["collection"] == "tube"

    async def test_a_registered_knowledge_base_with_no_knowledge_base_schema_carries_no_identity_and_logs_the_studio_init_fix(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        bare = _knowledge_base(tmp_path / "bare")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"bare": {"path": str(bare), "description": ""}})
        settings = _settings_at(settings_path)

        with caplog.at_level("INFO", logger="studio.knowledge_base.http.service"):
            async with _client(settings, wiring(), settings_path=settings_path) as client:
                response = await client.get("/api/knowledge-bases/bare")

        assert response.status_code == 200
        assert response.json()["identity"] is None
        assert "studio init --template" in caplog.text  # the actionable message, no longer silently swallowed

    async def test_a_registered_knowledge_base_with_no_knowledge_base_schema_reads_its_identity_off_the_indexed_spec(self, tmp_path: Path) -> None:
        bare = _knowledge_base(tmp_path / "bare")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"bare": {"path": str(bare), "description": ""}})
        settings = _settings_at(settings_path)
        container = wiring()
        container.gateway.routed = {
            "api.indexed_schema": SqlResult(rows=({"spec": _minimal_spec_payload("bare")},), columns=("spec",))
        }

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.get("/api/knowledge-bases/bare")

        assert response.status_code == 200
        identity = response.json()["identity"]
        assert identity is not None
        assert identity["collection"] == "bare"

    async def test_a_indexed_only_name_carries_no_identity(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()
        container.gateway.routed = {
            "kb.pages": SqlResult(rows=({"collection": "orphan", "pages": 7, "page_types": ["paper"], "last_indexed": None},), columns=())
        }

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.get("/api/knowledge-bases/orphan")

        assert response.status_code == 200
        assert response.json()["identity"] is None

    async def test_detail_lists_the_connectors_and_the_watch_list(self, app_client: AsyncClient, knowledge_base_path: Path) -> None:
        WatchListFile(knowledge_base_path / "_meta" / "watch-list.yaml").add(XML_SCANNER, "https://blog.example.org/feed.xml")
        response = await app_client.get("/api/knowledge-bases/tube")
        assert response.status_code == 200
        body = response.json()
        assert body["sources_connectors"] == ["intake"]
        assert body["watch_list"][0]["ref"] == "https://blog.example.org/feed.xml"
        assert "flags" not in body
        assert "flags_skipped" not in body

    async def test_a_malformed_queued_row_leaves_the_rest_of_the_detail_standing(self, app_client: AsyncClient, knowledge_base_path: Path) -> None:
        (knowledge_base_path / "_meta" / "source-queue.yaml").write_text("- key: yt-abc\n  kind: not-a-kind\n", encoding="utf-8")
        WatchListFile(knowledge_base_path / "_meta" / "watch-list.yaml").add(XML_SCANNER, "https://blog.example.org/feed.xml")

        response = await app_client.get("/api/knowledge-bases/tube")

        assert response.status_code == 200
        body = response.json()
        assert body["source_queue"] == []
        assert body["watch_list"][0]["ref"] == "https://blog.example.org/feed.xml", "the unreadable file blanks only itself"

    async def test_a_malformed_watch_list_leaves_the_rest_of_the_detail_standing(self, app_client: AsyncClient, knowledge_base_path: Path) -> None:
        (knowledge_base_path / "_meta" / "watch-list.yaml").write_text("- kind: xml\n", encoding="utf-8")  # a row with no ref at all
        SourceQueue(knowledge_base_path / "_meta" / "source-queue.yaml", connectors=_QUEUE_CONNECTORS).add(_WATCH_URL)

        response = await app_client.get("/api/knowledge-bases/tube")

        assert response.status_code == 200
        body = response.json()
        assert body["watch_list"] == []
        assert [row["key"] for row in body["source_queue"]] == ["yt-dQw4w9WgXcQ"]

    async def test_an_unparseable_watch_list_is_not_a_500(self, app_client: AsyncClient, knowledge_base_path: Path) -> None:
        (knowledge_base_path / "_meta" / "watch-list.yaml").write_text("scanner: [unclosed\n", encoding="utf-8")

        response = await app_client.get("/api/knowledge-bases/tube")

        assert response.status_code == 200
        assert response.json()["watch_list"] == []

    async def test_a_indexed_only_name_still_answers_with_vitals(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()
        container.gateway.routed = {
            "kb.pages": SqlResult(rows=({"collection": "orphan", "pages": 7, "page_types": ["paper"], "last_indexed": None},), columns=()),
            "validation_store": SqlResult(rows=(), columns=()),
        }

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.get("/api/knowledge-bases/orphan")

        assert response.status_code == 200
        body = response.json()
        assert (body["registered"], body["indexed"], body["pages"]) == (False, True, 7)
        assert (body["sources_connector"], body["source_queue"]) == (None, [])

    async def test_an_unknown_name_is_a_404(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.get("/api/knowledge-bases/ghost")

        assert response.status_code == 404
        assert response.json()["error"] == "unknown_knowledge_base"


class TestKnowledgeBaseSourceQueue:
    async def test_adds_a_requested_row_to_the_queue(self, tmp_path: Path):
        knowledge_base_path = _queue_knowledge_base(tmp_path / "tube", "tube")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"tube": {"path": str(knowledge_base_path), "description": ""}})
        settings = _settings_at(settings_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.post("/api/knowledge-bases/tube/source-queue", json={"url": _WATCH_URL})

        assert response.status_code == 200
        assert (response.json()["key"], response.json()["status"]) == ("yt-dQw4w9WgXcQ", "requested")
        rows = SourceQueue(knowledge_base_path / "_meta" / "source-queue.yaml", connectors=_QUEUE_CONNECTORS).entries()
        assert [row.key for row in rows] == ["yt-dQw4w9WgXcQ"]

    async def test_refuses_a_knowledge_base_without_the_queue_connector(self, tmp_path: Path):
        knowledge_base_path = _knowledge_base_with_spec(tmp_path / "papers", "papers")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"papers": {"path": str(knowledge_base_path), "description": ""}})
        settings = _settings_at(settings_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.post("/api/knowledge-bases/papers/source-queue", json={"url": _WATCH_URL})

        assert response.status_code == 400
        assert response.json()["error"] == "sources_without_queue"

    async def test_the_queue_accepts_any_url_on_a_queue_knowledge_base(self, app_client: AsyncClient) -> None:
        response = await app_client.post("/api/knowledge-bases/tube/source-queue", json={"url": "https://arxiv.org/abs/2401.01234"})
        assert response.status_code == 200
        assert response.json()["key"] == "arxiv-2401.01234"

    async def test_an_unknown_knowledge_base_is_a_404(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.post("/api/knowledge-bases/ghost/source-queue", json={"url": _WATCH_URL})

        assert response.status_code == 404
        assert response.json()["error"] == "unknown_knowledge_base"

    async def test_an_unreadable_ledger_is_a_409(self, app_client: AsyncClient, knowledge_base_path: Path) -> None:
        (knowledge_base_path / "_meta" / "source-queue.yaml").write_text("source_type: [unclosed\n", encoding="utf-8")

        response = await app_client.post("/api/knowledge-bases/tube/source-queue", json={"url": _WATCH_URL})

        assert response.status_code == 409
        assert response.json()["error"] == "source_queue_unreadable"


class TestKnowledgeBaseIntakeRetry:
    async def test_retries_a_failed_row(self, app_client: AsyncClient, knowledge_base_path: Path) -> None:
        ledger = SourceQueue(knowledge_base_path / "_meta" / "source-queue.yaml", connectors=_QUEUE_CONNECTORS)
        entry = ledger.add(_WATCH_URL)
        ledger.mark(entry.key, SourceQueueStatus.FAILED, reason="fetch timed out")

        response = await app_client.post(f"/api/knowledge-bases/tube/source-queue/{entry.key}/retry")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "requested"
        assert body["reason"] is None

    async def test_nothing_to_retry_is_a_409(self, app_client: AsyncClient, knowledge_base_path: Path) -> None:
        ledger = SourceQueue(knowledge_base_path / "_meta" / "source-queue.yaml", connectors=_QUEUE_CONNECTORS)
        entry = ledger.add(_WATCH_URL)
        ledger.mark(entry.key, SourceQueueStatus.RESOLVED)

        response = await app_client.post(f"/api/knowledge-bases/tube/source-queue/{entry.key}/retry")

        assert response.status_code == 409
        assert response.json()["error"] == "nothing_to_retry"

    async def test_an_unreadable_ledger_is_a_409(self, app_client: AsyncClient, knowledge_base_path: Path) -> None:
        (knowledge_base_path / "_meta" / "source-queue.yaml").write_text("source_type: [unclosed\n", encoding="utf-8")

        response = await app_client.post("/api/knowledge-bases/tube/source-queue/yt-abc/retry")

        assert response.status_code == 409
        assert response.json()["error"] == "source_queue_unreadable"


class TestKnowledgeBaseWatchList:
    _WATCH_URL = "https://www.youtube.com/feeds/videos.xml?channel_id=UCabc"

    async def test_watch_list_add_and_remove(self, app_client: AsyncClient) -> None:
        added = await app_client.post("/api/knowledge-bases/tube/watch-list", json={"scanner": XML_SCANNER, "ref": self._WATCH_URL})
        assert added.status_code == 200
        assert added.json()["scanner"] == XML_SCANNER
        removed = await app_client.delete("/api/knowledge-bases/tube/watch-list", params={"scanner": XML_SCANNER, "ref": self._WATCH_URL})
        assert removed.json() == {"removed": True}

    async def test_a_scanner_this_machine_has_none_of_is_refused_naming_the_ones_there_are(self, app_client: AsyncClient) -> None:
        response = await app_client.post("/api/knowledge-bases/tube/watch-list", json={"scanner": "podcast", "ref": self._WATCH_URL})

        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "unknown_scanner"
        assert XML_SCANNER in body["detail"]

    async def test_removing_an_entry_never_added_answers_removed_false(self, app_client: AsyncClient) -> None:
        response = await app_client.delete("/api/knowledge-bases/tube/watch-list", params={"scanner": XML_SCANNER, "ref": "https://ghost.example.org/feed.xml"})

        assert response.status_code == 200
        assert response.json() == {"removed": False}

    async def test_an_unreadable_watch_list_on_add_is_a_409(self, app_client: AsyncClient, knowledge_base_path: Path) -> None:
        (knowledge_base_path / "_meta" / "watch-list.yaml").write_text("scanner: [unclosed\n", encoding="utf-8")

        response = await app_client.post("/api/knowledge-bases/tube/watch-list", json={"scanner": XML_SCANNER, "ref": self._WATCH_URL})

        assert response.status_code == 409
        assert response.json()["error"] == "watch_list_unreadable"

    async def test_an_unreadable_watch_list_on_remove_is_a_409(self, app_client: AsyncClient, knowledge_base_path: Path) -> None:
        (knowledge_base_path / "_meta" / "watch-list.yaml").write_text("scanner: [unclosed\n", encoding="utf-8")

        response = await app_client.delete("/api/knowledge-bases/tube/watch-list", params={"scanner": XML_SCANNER, "ref": self._WATCH_URL})

        assert response.status_code == 409
        assert response.json()["error"] == "watch_list_unreadable"


class TestKnowledgeBaseDecisions:
    async def test_lists_open_decisions_and_outcomes_by_page_type(self, app_client: AsyncClient, knowledge_base_path: Path) -> None:
        (knowledge_base_path / "Wiki" / "Flags").mkdir(parents=True)
        (knowledge_base_path / "Wiki" / "Flags" / "flag.one.md").write_text(FLAG, encoding="utf-8")

        response = await app_client.get("/api/knowledge-bases/tube/decisions")

        assert response.status_code == 200
        body = response.json()
        assert body["rows"][0]["id"] == "flag.one"
        assert "path" not in body["rows"][0], "the row's host filesystem path never rides the wire"
        assert body["skipped"] == []
        assert body["outcomes_by_page_type"]["flag"] == [
            {"status": "accepted", "link": None},
            {"status": "dismissed", "link": None},
            {"status": "completed", "link": "resolved_by"},
        ]

    async def test_a_malformed_page_is_named_in_skipped(self, app_client: AsyncClient, knowledge_base_path: Path) -> None:
        flags_dir = knowledge_base_path / "Wiki" / "Flags"
        flags_dir.mkdir(parents=True)
        (flags_dir / "flag.one.md").write_text(FLAG, encoding="utf-8")
        (flags_dir / "flag.broken.md").write_text("not a flag page\n", encoding="utf-8")

        response = await app_client.get("/api/knowledge-bases/tube/decisions")

        assert response.status_code == 200
        body = response.json()
        assert [row["id"] for row in body["rows"]] == ["flag.one"]
        assert body["skipped"] == ["flag.broken.md"]

    async def test_decisions_on_an_unknown_knowledge_base_is_a_404(self, tmp_path: Path) -> None:
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        async with _client(settings, wiring(), settings_path=settings_path) as client:
            response = await client.get("/api/knowledge-bases/ghost/decisions")
        assert response.status_code == 404
        assert response.json()["error"] == "unknown_knowledge_base"

    async def test_decisions_on_a_registered_knowledge_base_with_no_knowledge_base_schema_names_the_studio_init_fix(self, tmp_path: Path) -> None:
        bare = _knowledge_base(tmp_path / "bare")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"bare": {"path": str(bare), "description": ""}})
        settings = _settings_at(settings_path)

        async with _client(settings, wiring(), settings_path=settings_path) as client:
            response = await client.get("/api/knowledge-bases/bare/decisions")

        assert response.status_code == 404
        body = response.json()
        assert body["error"] == "spec_not_found"
        assert "studio init --template" in body["detail"]
        assert "knowledge-base-schema.json" in body["detail"]

    async def test_decisions_on_a_registered_knowledge_base_with_no_knowledge_base_schema_falls_back_to_the_indexed_spec(self, tmp_path: Path) -> None:
        bare = _knowledge_base(tmp_path / "bare")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"bare": {"path": str(bare), "description": ""}})
        settings = _settings_at(settings_path)
        container = wiring()
        container.gateway.routed = {
            "api.indexed_schema": SqlResult(rows=({"spec": _minimal_spec_payload("bare")},), columns=("spec",))
        }

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.get("/api/knowledge-bases/bare/decisions")

        assert response.status_code == 200
        assert response.json() == {"rows": [], "skipped": [], "outcomes_by_page_type": {}}

    async def test_decisions_on_a_registered_knowledge_base_with_a_broken_spec_keeps_the_generic_message(self, tmp_path: Path) -> None:
        broken = _knowledge_base(tmp_path / "broken")
        (broken / "knowledge-base-schema.json").write_text("not json", encoding="utf-8")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"broken": {"path": str(broken), "description": ""}})
        settings = _settings_at(settings_path)

        async with _client(settings, wiring(), settings_path=settings_path) as client:
            response = await client.get("/api/knowledge-bases/broken/decisions")

        assert response.status_code == 404
        body = response.json()
        assert body["error"] == "spec_not_found"
        assert body["detail"] == "could not load the schema for 'broken'"
        assert "studio init --template" not in body["detail"]

    async def test_page_decision_patch_writes_the_page_and_queues_the_index(self, app_client: AsyncClient, knowledge_base_path: Path) -> None:
        (knowledge_base_path / "Wiki" / "Flags").mkdir(parents=True)
        (knowledge_base_path / "Wiki" / "Flags" / "flag.one.md").write_text(FLAG, encoding="utf-8")

        response = await app_client.patch("/api/knowledge-bases/tube/pages/flag.one", json={"status": "dismissed"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "dismissed"
        assert "path" not in body
        assert body["index"] == "queued"
        job = await app_client.get(f"/api/jobs/{body['job_id']}")
        assert job.status_code == 200
        assert (job.json()["kind"], job.json()["key"]) == (REFRESH_KIND, "tube")
        assert "status: dismissed" in (knowledge_base_path / "Wiki" / "Flags" / "flag.one.md").read_text(encoding="utf-8")

    async def test_a_second_decision_while_the_run_is_queued_rides_that_run(self, app_client: AsyncClient, knowledge_base_path: Path) -> None:
        (knowledge_base_path / "Wiki" / "Flags").mkdir(parents=True)
        (knowledge_base_path / "Wiki" / "Flags" / "flag.one.md").write_text(FLAG, encoding="utf-8")
        first = await app_client.patch("/api/knowledge-bases/tube/pages/flag.one", json={"status": "accepted"})

        second = await app_client.patch("/api/knowledge-bases/tube/pages/flag.one", json={"status": "dismissed"})

        assert second.json()["index"] == "in_progress"
        assert second.json()["job_id"] == first.json()["job_id"]

    async def test_completing_with_a_link_field_writes_the_links(self, app_client: AsyncClient, knowledge_base_path: Path) -> None:
        (knowledge_base_path / "Wiki" / "Flags").mkdir(parents=True)
        (knowledge_base_path / "Wiki" / "Flags" / "flag.one.md").write_text(FLAG, encoding="utf-8")

        response = await app_client.patch(
            "/api/knowledge-bases/tube/pages/flag.one",
            json={"status": "completed", "links": {"resolved_by": ["q.planning", "[[pb.reminders]]"]}},
        )

        assert response.status_code == 200
        assert response.json()["links"]["resolved_by"] == ["q.planning", "pb.reminders"]
        text = (knowledge_base_path / "Wiki" / "Flags" / "flag.one.md").read_text(encoding="utf-8")
        assert 'resolved_by: ["[[q.planning]]", "[[pb.reminders]]"]' in text

    async def test_an_unknown_page_is_a_404(self, app_client: AsyncClient) -> None:
        missing = await app_client.patch("/api/knowledge-bases/tube/pages/flag.two", json={"status": "accepted"})
        assert missing.status_code == 404
        assert missing.json()["error"] == "unknown_page"

    async def test_the_patch_route_on_a_registered_knowledge_base_with_no_knowledge_base_schema_names_the_studio_init_fix(self, tmp_path: Path) -> None:
        bare = _knowledge_base(tmp_path / "bare")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"bare": {"path": str(bare), "description": ""}})
        settings = _settings_at(settings_path)

        async with _client(settings, wiring(), settings_path=settings_path) as client:
            response = await client.patch("/api/knowledge-bases/bare/pages/flag.one", json={"status": "accepted"})

        assert response.status_code == 404
        body = response.json()
        assert body["error"] == "spec_not_found"
        assert "studio init --template" in body["detail"]

    async def test_an_invalid_status_is_a_409_invalid_decision(self, app_client: AsyncClient, knowledge_base_path: Path) -> None:
        (knowledge_base_path / "Wiki" / "Flags").mkdir(parents=True)
        (knowledge_base_path / "Wiki" / "Flags" / "flag.one.md").write_text(FLAG, encoding="utf-8")

        response = await app_client.patch("/api/knowledge-bases/tube/pages/flag.one", json={"status": "not-a-status"})

        assert response.status_code == 409
        assert response.json()["error"] == "invalid_decision"

    async def test_an_empty_status_is_a_409_invalid_decision_and_leaves_the_page_as_it_was(
        self, app_client: AsyncClient, knowledge_base_path: Path
    ) -> None:
        page = knowledge_base_path / "Wiki" / "Flags" / "flag.one.md"
        page.parent.mkdir(parents=True)
        page.write_text(FLAG, encoding="utf-8")

        response = await app_client.patch("/api/knowledge-bases/tube/pages/flag.one", json={"status": "", "links": {"resolved_by": ["q.x"]}})

        assert response.status_code == 409
        assert response.json()["error"] == "invalid_decision"
        assert page.read_text(encoding="utf-8") == FLAG


class TestKnowledgeBaseRegistration:
    async def test_registers_an_existing_knowledge_base_directory(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        target = _knowledge_base_with_spec(tmp_path / "existing", "existing")
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.put("/api/knowledge-bases/existing/registration", json={"path": str(target), "description": "found on disk"})

        assert response.status_code == 200
        registry = json.loads(settings_path.read_text(encoding="utf-8"))["knowledge_bases"]
        assert registry["existing"]["path"] == str(target)
        # No other row is written alongside it: this process has no active knowledge base to protect.
        assert list(registry) == ["existing"]

    async def test_refuses_a_directory_that_is_not_knowledge_base_shaped(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        bare = tmp_path / "bare"
        bare.mkdir()
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.put("/api/knowledge-bases/bare/registration", json={"path": str(bare)})

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_path"

    async def test_refuses_a_name_collision(self, tmp_path: Path):
        knowledge_base_path = _knowledge_base_with_spec(tmp_path / "demo", "demo")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"demo": {"path": str(knowledge_base_path), "description": ""}})
        settings = _settings_at(settings_path)
        container = wiring()
        other = _knowledge_base_with_spec(tmp_path / "other", "other")

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.put("/api/knowledge-bases/demo/registration", json={"path": str(other)})

        assert response.status_code == 409
        assert response.json()["error"] == "name_collision"

    async def test_unregisters_a_row_leaving_files_untouched(self, tmp_path: Path):
        active = _knowledge_base_with_spec(tmp_path / "active", "active")
        extra = _knowledge_base_with_spec(tmp_path / "extra", "extra")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(
            settings_path, knowledge_bases={"active": {"path": str(active), "description": ""}, "extra": {"path": str(extra), "description": ""}}
        )
        settings = _settings_at(settings_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.delete("/api/knowledge-bases/extra/registration")

        assert response.status_code == 200
        registry = json.loads(settings_path.read_text(encoding="utf-8"))["knowledge_bases"]
        assert "extra" not in registry
        assert (extra / "knowledge-base-schema.json").is_file()  # unregistering never deletes the knowledge base

    async def test_any_registered_knowledge_base_can_be_unregistered(self, tmp_path: Path):
        first = _knowledge_base_with_spec(tmp_path / "first", "first")
        second = _knowledge_base_with_spec(tmp_path / "second", "second")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(
            settings_path, knowledge_bases={"first": {"path": str(first), "description": ""}, "second": {"path": str(second), "description": ""}}
        )
        settings = ApplicationSettings(json_file=settings_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.delete("/api/knowledge-bases/first/registration")

        assert response.status_code == 200
        assert list(json.loads(settings_path.read_text(encoding="utf-8"))["knowledge_bases"]) == ["second"]

    async def test_unregistering_an_unknown_name_is_a_404(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.delete("/api/knowledge-bases/ghost/registration")

        assert response.status_code == 404
        assert response.json()["error"] == "unknown_knowledge_base"


class TestKnowledgeBaseRegistrationPreservesSchedule:
    async def test_reregistering_at_the_same_path_carries_the_schedule_forward(self, tmp_path: Path):
        target = _knowledge_base_with_spec(tmp_path / "demo", "demo")
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            first = await client.put("/api/knowledge-bases/demo/registration", json={"path": str(target), "description": "first"})
            assert first.status_code == 200

            schedule_response = await client.put("/api/knowledge-bases/demo/schedule", json={"every_seconds": 600})
            assert schedule_response.status_code == 200

            reregister = await client.put("/api/knowledge-bases/demo/registration", json={"path": str(target), "description": "refreshed"})
            assert reregister.status_code == 200

            detail = await client.get("/api/knowledge-bases/demo")

        assert detail.json()["schedule"] == {"every_seconds": 600, "daily_at": None}
        registry = json.loads(settings_path.read_text(encoding="utf-8"))["knowledge_bases"]
        assert registry["demo"]["description"] == "refreshed"
        assert registry["demo"]["schedule"] == {"every_seconds": 600}

    async def test_a_fresh_registration_has_no_schedule(self, tmp_path: Path):
        target = _knowledge_base_with_spec(tmp_path / "demo", "demo")
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.put("/api/knowledge-bases/demo/registration", json={"path": str(target)})

        assert response.status_code == 200
        registry = json.loads(settings_path.read_text(encoding="utf-8"))["knowledge_bases"]
        assert "schedule" not in registry["demo"]
        assert "library" not in registry["demo"], "an ordinary row never restates the default"

    async def test_reregistering_keeps_a_private_row_private(self, tmp_path: Path):
        """A privacy declaration outlives a re-registration, the same way a schedule does."""
        target = _knowledge_base_with_spec(tmp_path / "vault", "vault")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"vault": {"path": str(target), "description": "", "private": True}})
        settings = _settings_at(settings_path)

        async with _client(settings, wiring(), settings_path=settings_path) as client:
            reregister = await client.put("/api/knowledge-bases/vault/registration", json={"path": str(target), "description": "refreshed"})
            assert reregister.status_code == 200

            listing = await client.get("/api/knowledge-bases")

        registry = json.loads(settings_path.read_text(encoding="utf-8"))["knowledge_bases"]
        assert registry["vault"]["private"] is True
        assert next(row for row in listing.json()["knowledge_bases"] if row["name"] == "vault")["private"] is True


def _register_plain_knowledge_base(tmp_path: Path, name: str) -> tuple[ApplicationSettings, Path, Path]:
    """A registered knowledge base with no source layer — `(settings, knowledge_base_path, settings_path)`."""
    knowledge_base_path = _knowledge_base_with_spec(tmp_path / name, name)
    settings_path = tmp_path / "application_settings.json"
    _write_settings_json(settings_path, knowledge_bases={name: {"path": str(knowledge_base_path), "description": ""}})
    return _settings_at(settings_path), knowledge_base_path, settings_path


async def _finished_run(jobs: MemoryJobs, name: str, *, result: str) -> None:
    """Put one completed `kb.refresh` job on the ledger, as the worker would have left it."""
    answer = await jobs.enqueue(REFRESH_KIND, name, RefreshJob(knowledge_base=name, trigger="schedule"), policy=JobPolicy())
    claimed = await jobs.claim([REFRESH_KIND], worker="test")
    assert claimed is not None
    assert claimed.id == answer.job_id
    await jobs.complete(answer.job_id, result=result)


class TestIngestRun:
    async def test_enqueues_a_manual_job_and_answers_202_with_its_id(self, tmp_path: Path):
        settings, _knowledge_base_path, settings_path = _register_plain_knowledge_base(tmp_path, "demo")
        jobs = MemoryJobs()

        async with _client(settings, wiring(), settings_path=settings_path, jobs=jobs) as client:
            response = await client.post("/api/knowledge-bases/demo/refresh")

        assert response.status_code == 202
        job_id = response.json()["job_id"]
        assert response.headers["location"] == f"/api/jobs/{job_id}"
        queued = await jobs.read(job_id)
        assert queued is not None
        assert (queued.kind, queued.key, queued.state) == (REFRESH_KIND, "demo", "queued")
        assert queued.payload == {"knowledge_base": "demo", "trigger": "manual"}
        assert (queued.max_attempts, queued.lease_seconds) == (REFRESH_POLICY.max_attempts, REFRESH_POLICY.lease_seconds)

    async def test_a_second_request_while_one_is_live_is_409_with_the_id_to_watch(self, tmp_path: Path):
        settings, _knowledge_base_path, settings_path = _register_plain_knowledge_base(tmp_path, "demo")
        jobs = MemoryJobs()

        async with _client(settings, wiring(), settings_path=settings_path, jobs=jobs) as client:
            first = await client.post("/api/knowledge-bases/demo/refresh")
            second = await client.post("/api/knowledge-bases/demo/refresh")

        assert second.status_code == 409
        assert second.json()["error"] == "refresh_in_progress"
        assert second.json()["job_id"] == first.json()["job_id"]

    async def test_the_409_body_is_error_detail_and_job_id_in_that_order(self, tmp_path: Path):
        settings, _knowledge_base_path, settings_path = _register_plain_knowledge_base(tmp_path, "demo")

        async with _client(settings, wiring(), settings_path=settings_path, jobs=MemoryJobs()) as client:
            first = await client.post("/api/knowledge-bases/demo/refresh")
            second = await client.post("/api/knowledge-bases/demo/refresh")

        assert list(second.json()) == ["error", "detail", "job_id"]
        assert second.json() == {
            "error": "refresh_in_progress",
            "detail": "a refresh of 'demo' is already queued or underway.",
            "job_id": first.json()["job_id"],
        }

    async def test_an_unregistered_name_is_a_404_and_enqueues_nothing(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        jobs = MemoryJobs()

        async with _client(settings, wiring(), settings_path=settings_path, jobs=jobs) as client:
            response = await client.post("/api/knowledge-bases/ghost/refresh")

        assert response.status_code == 404
        assert response.json()["error"] == "unknown_knowledge_base"
        assert jobs.jobs == {}

    async def test_a_broken_registry_row_is_a_409_naming_the_fix(self, tmp_path: Path):
        active = _knowledge_base_with_spec(tmp_path / "active", "active")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"active": {"path": str(active), "description": ""}})
        settings = _settings_at(settings_path)
        # The stale row is written directly to the on-disk file, bypassing `ApplicationSettings`'
        # own construction-time validation -- standing in for a directory that moved or was deleted
        # after the settings file was last successfully loaded.
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        data["knowledge_bases"]["stale"] = {"path": str(tmp_path / "gone")}
        settings_path.write_text(json.dumps(data), encoding="utf-8")
        jobs = MemoryJobs()

        async with _client(settings, wiring(), settings_path=settings_path, jobs=jobs) as client:
            response = await client.post("/api/knowledge-bases/stale/refresh")

        assert response.status_code == 409
        assert response.json()["error"] == "broken_knowledge_base"
        assert jobs.jobs == {}


class TestJobRead:
    async def test_reads_one_job_by_id(self, tmp_path: Path):
        settings, _knowledge_base_path, settings_path = _register_plain_knowledge_base(tmp_path, "demo")
        jobs = MemoryJobs()

        async with _client(settings, wiring(), settings_path=settings_path, jobs=jobs) as client:
            job_id = (await client.post("/api/knowledge-bases/demo/refresh")).json()["job_id"]
            response = await client.get(f"/api/jobs/{job_id}")

        assert response.status_code == 200
        body = response.json()
        assert (body["id"], body["kind"], body["key"], body["state"]) == (job_id, REFRESH_KIND, "demo", "queued")

    async def test_an_unknown_id_is_a_404(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)

        async with _client(settings, wiring(), settings_path=settings_path) as client:
            response = await client.get("/api/jobs/404")

        assert response.status_code == 404
        assert response.json()["error"] == "unknown_job"


class TestKnowledgeBaseRefreshRuns:
    async def test_lists_finished_runs_newest_first(self, tmp_path: Path):
        settings, _knowledge_base_path, settings_path = _register_plain_knowledge_base(tmp_path, "demo")
        jobs = MemoryJobs()
        await _finished_run(jobs, "demo", result="first")
        await _finished_run(jobs, "demo", result="second")

        async with _client(settings, wiring(), settings_path=settings_path, jobs=jobs) as client:
            response = await client.get("/api/knowledge-bases/demo/refresh-runs")

        assert response.status_code == 200
        body = response.json()
        assert [row["detail"] for row in body["refresh_runs"]] == ["second", "first"]
        assert all(row["outcome"] == "ok" for row in body["refresh_runs"])

    async def test_a_queued_job_is_not_a_finished_pass_yet(self, tmp_path: Path):
        settings, _knowledge_base_path, settings_path = _register_plain_knowledge_base(tmp_path, "demo")
        jobs = MemoryJobs()

        async with _client(settings, wiring(), settings_path=settings_path, jobs=jobs) as client:
            await client.post("/api/knowledge-bases/demo/refresh")
            response = await client.get("/api/knowledge-bases/demo/refresh-runs")

        assert response.json() == {"refresh_runs": []}

    async def test_no_finished_passes_is_an_empty_list(self, tmp_path: Path):
        settings, _knowledge_base_path, settings_path = _register_plain_knowledge_base(tmp_path, "demo")

        async with _client(settings, wiring(), settings_path=settings_path) as client:
            response = await client.get("/api/knowledge-bases/demo/refresh-runs")

        assert response.status_code == 200
        assert response.json() == {"refresh_runs": []}

    async def test_an_unregistered_name_is_a_404(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)

        async with _client(settings, wiring(), settings_path=settings_path) as client:
            response = await client.get("/api/knowledge-bases/ghost/refresh-runs")

        assert response.status_code == 404
        assert response.json()["error"] == "unknown_knowledge_base"

    async def test_the_detail_endpoint_surfaces_the_latest_finished_run(self, tmp_path: Path):
        settings, _knowledge_base_path, settings_path = _register_plain_knowledge_base(tmp_path, "demo")
        container = wiring()
        container.gateway.routed = {"kb.pages": SqlResult(rows=(), columns=()), "validation_store": SqlResult(rows=(), columns=())}
        jobs = MemoryJobs()
        await _finished_run(jobs, "demo", result="indexed 3 pages, 3 embedded")

        async with _client(settings, container, settings_path=settings_path, jobs=jobs) as client:
            response = await client.get("/api/knowledge-bases/demo")

        assert response.status_code == 200
        assert response.json()["last_refresh_run"] == {
            **response.json()["last_refresh_run"],
            "outcome": "ok",
            "detail": "indexed 3 pages, 3 embedded",
            "trigger": "schedule",
        }


class TestKnowledgeBaseSchedule:
    async def test_sets_a_schedule_preserving_everything_else(self, tmp_path: Path):
        knowledge_base_path = _knowledge_base_with_spec(tmp_path / "demo", "demo")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(settings_path, knowledge_bases={"demo": {"path": str(knowledge_base_path), "description": "d"}})
        settings = _settings_at(settings_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.put("/api/knowledge-bases/demo/schedule", json={"every_seconds": 600})

        assert response.status_code == 200
        assert response.json() == {"name": "demo", "schedule": {"every_seconds": 600, "daily_at": None}}
        registry = json.loads(settings_path.read_text(encoding="utf-8"))["knowledge_bases"]
        assert registry["demo"]["schedule"] == {"every_seconds": 600}
        assert registry["demo"]["description"] == "d"

    async def test_clears_a_schedule(self, tmp_path: Path):
        knowledge_base_path = _knowledge_base_with_spec(tmp_path / "demo", "demo")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(
            settings_path, knowledge_bases={"demo": {"path": str(knowledge_base_path), "description": "", "schedule": {"every_seconds": 600}}}
        )
        settings = _settings_at(settings_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.delete("/api/knowledge-bases/demo/schedule")

        assert response.status_code == 200
        assert response.json() == {"name": "demo", "schedule": None}
        registry = json.loads(settings_path.read_text(encoding="utf-8"))["knowledge_bases"]
        assert "schedule" not in registry["demo"]

    async def test_rejects_both_fields_set(self, tmp_path: Path):
        settings, _knowledge_base_path, settings_path = _register_plain_knowledge_base(tmp_path, "demo")
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.put("/api/knowledge-bases/demo/schedule", json={"every_seconds": 600, "daily_at": "06:00"})

        assert response.status_code == 422
        assert set(response.json()) == {"error", "detail"}

    async def test_an_unknown_name_is_a_404_on_put(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.put("/api/knowledge-bases/ghost/schedule", json={"every_seconds": 600})

        assert response.status_code == 404
        assert response.json()["error"] == "unknown_knowledge_base"

    async def test_an_unknown_name_is_a_404_on_delete(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        container = wiring()

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.delete("/api/knowledge-bases/ghost/schedule")

        assert response.status_code == 404
        assert response.json()["error"] == "unknown_knowledge_base"

    async def test_the_detail_endpoint_surfaces_the_schedule(self, tmp_path: Path):
        knowledge_base_path = _knowledge_base_with_spec(tmp_path / "demo", "demo")
        settings_path = tmp_path / "application_settings.json"
        _write_settings_json(
            settings_path, knowledge_bases={"demo": {"path": str(knowledge_base_path), "description": "", "schedule": {"daily_at": "06:00"}}}
        )
        settings = _settings_at(settings_path)
        container = wiring()
        container.gateway.routed = {"kb.pages": SqlResult(rows=(), columns=()), "validation_store": SqlResult(rows=(), columns=())}

        async with _client(settings, container, settings_path=settings_path) as client:
            response = await client.get("/api/knowledge-bases/demo")

        assert response.status_code == 200
        assert response.json()["schedule"] == {"every_seconds": None, "daily_at": "06:00"}


class TestWorkerChildWiring:
    """The lifespan owns the worker child: it starts on boot and stops cleanly on shutdown."""

    async def test_the_lifespan_starts_and_stops_the_supervised_worker(self, tmp_path: Path):
        settings, settings_path = _knowledge_bases_app_settings(tmp_path)
        script = tmp_path / "child.py"
        script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

        async def spawn() -> Process:
            return await asyncio.create_subprocess_exec(sys.executable, str(script))

        child = SupervisedChild(spawn, name="test-worker")
        app = create_app(
            settings,
            container=wiring().container,
            settings_path=settings_path,
            jobs=MemoryJobs(),
            worker=child,
            broadcaster=_fake_broadcaster(),
        )

        async with app.router.lifespan_context(app):
            assert child.running is True

        assert child.running is False


class TestEventsEndpoint:
    async def test_a_delivered_topic_arrives_as_a_change_frame(self, tmp_path: Path) -> None:
        broadcaster = _fake_broadcaster()
        app = create_app(_settings(tmp_path), container=wiring().container, jobs=MemoryJobs(), broadcaster=broadcaster)

        async with _lifespan(app):
            start, body, task = await raw_stream(app, "/api/events")
            try:
                assert start["status"] == 200
                assert dict(start["headers"])[b"content-type"].startswith(b"text/event-stream")
                await asyncio.sleep(0.05)
                broadcaster.deliver("knowledge_base.research")
                frame = (await asyncio.wait_for(body.get(), timeout=2))["body"].decode()
                assert frame.startswith("event: change\n")
                assert "knowledge_base.research" in frame
            finally:
                await stop_stream(task)

    async def test_a_filter_keeps_only_a_matching_topic_root(self, tmp_path: Path) -> None:
        broadcaster = _fake_broadcaster()
        app = create_app(_settings(tmp_path), container=wiring().container, jobs=MemoryJobs(), broadcaster=broadcaster)

        async with _lifespan(app):
            _, body, task = await raw_stream(app, "/api/events?topics=knowledge_base")
            try:
                await asyncio.sleep(0.05)
                broadcaster.deliver("jobs")
                broadcaster.deliver("knowledge_base.research")
                frame = (await asyncio.wait_for(body.get(), timeout=2))["body"].decode()
                assert frame.startswith("event: change\n")
                assert "knowledge_base.research" in frame
            finally:
                await stop_stream(task)

    async def test_no_broadcaster_answers_503_rather_than_a_stream(self, tmp_path: Path) -> None:
        # A fake broadcaster gets the lifespan through startup (routes mount only once it runs), then
        # `app.state.broadcaster` is cleared to put the app in the state the route actually guards.
        app = create_app(_settings(tmp_path), container=wiring().container, jobs=MemoryJobs(), broadcaster=_fake_broadcaster())

        async with _lifespan(app):
            app.state.broadcaster = None
            start, body, task = await raw_stream(app, "/api/events")
            try:
                assert start["status"] == 503
                payload = json.loads((await asyncio.wait_for(body.get(), timeout=2))["body"])
                assert set(payload) == {"error", "detail"}
                assert payload["error"] == "events_unavailable"
                assert "no broadcaster" in payload["detail"]
            finally:
                await stop_stream(task)
