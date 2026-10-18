r"""`EnvelopeHandler` against the real `api.envelope_*` surface — the reader-role regression guard.

The reader role (`kb_reader` — created by 0003, renamed by 0014) is granted the `api` schema only, so any `disclose` /
`GET /api/envelope` SQL naming `memory.*` directly raises `permission denied for schema memory`
through the deployed reader path. Migration `0012`
(`alembic/versions/0012_envelope_api_surface.py`) keeps those reads behind `api.envelope_*`.

`TestEnvelopeReaderRegression` drives the **real** `EnvelopeHandler` over a **real**
`ReadOnlySqlGateway` built on the **reader** role's URL (`reader_url`, exactly
`studio.knowledge_base.wiring.serve.envelope_handler_provider`'s production wiring), against a database seeded
via the admin URL, for all three ref forms. `test_migrations.py` and `test_api_contract.py` read
over the **admin** connection by design (they pin DDL shape, not the reader's grant boundary), so
neither covers reachability through the role the handler is actually wired to.

`TestEnvelopeSurfaceRoundTrip` pins both directions in one test: at head (`0012`) the
reader-backed read works; downgraded to `0011` (the `api.envelope_*` functions gone) the *same*
reader-backed call fails; re-upgraded to head it works again.

`TestFunctionBodies` pins the `api.envelope_*` bodies via `pg_get_functiondef`: each body must
reference `memory.*`, and the collection function's body must not filter by page status.

Seeding is direct admin DML, so tests stay fast. The whole module skips when Docker is unavailable
(the `pg` fixture, via `migrated`, raises `pytest.skip`).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
import pytest

from alembic import command
from alembic.config import Config
from core.ai.disclosure.contracts import ChildSummary
from engram.indexing.infrastructure import orm
from engram.retrieval.envelope import EnvelopeHandler
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.testing.infrastructure.knowledge_base_db import BED_GRAPH_BACKEND
from sqlalchemy import NullPool, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

# The bed's BM25 dialect, mirroring `tests/support/knowledge_base_db.py`'s `_BED_DIALECT`: the analog
# migrates under `pg_textsearch`, the opt-in ParadeDB bed under `pg_search`.
_BED_DIALECT = "pg_textsearch" if BED_GRAPH_BACKEND == "age" else "pg_search"

# The engram checkout, which owns the one alembic chain: `parents[5]` is the monorepo root
# (this file sits at `studio/tests/integration/knowledge_base/serve/`), whose `engram/` sibling holds
# `alembic.ini` and `alembic/`.
_ENGRAM_ROOT = Path(__file__).resolve().parents[5] / "engram"
_ALEMBIC_INI = _ENGRAM_ROOT / "alembic.ini"
_ALEMBIC_DIR = _ENGRAM_ROOT / "alembic"

# The knowledge base's Postgres schema (engram migration 0046 renamed `memory` to `kb`). No public
# constant exports this name (`engram.db.contracts.KbBase.metadata.schema` is unset; `engram.indexing.infrastructure.orm`
# keeps its own `_SCHEMA` private), so it is named once here.
_KB_SCHEMA = "kb"

_ENVELOPE_FUNCTION_SIGNATURES = (
    "api.envelope_collection(text, text[])",
    "api.envelope_page(text, text)",
    "api.envelope_section(text, text)",
    "api.envelope_collection_groups(text, text[])",
    "api.envelope_group(text, text, text[])",
    "api.envelope_collection_page_types(text, text[])",
    "api.envelope_page_type(text, text, text[])",
    "api.envelope_group_page_types(text, text, text[])",
    "api.envelope_group_page_type(text, text, text, text[])",
)


def _asyncpg_dsn(url: str) -> str:
    """SQLAlchemy `postgresql+asyncpg://` → the plain `postgresql://` raw asyncpg wants."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _titles(row: Mapping[str, object]) -> list[str]:
    """`row["sample_titles"]` (a `text[]` column) narrowed to a plain `list[str]` for assertions."""
    titles = row["sample_titles"]
    assert isinstance(titles, (list, tuple))
    return [str(title) for title in titles]


def _alembic_config(url: str) -> Config:
    """Build an alembic Config pinned to the app's ini + the throwaway DB and the bed's grammar.

    Mirrors `test_index_state_migration.py::_alembic_config`: threads the target URL and the
    bed's `dialect` / `graph_backend` through `config.attributes` (the seam `alembic/env.py`
    honours), so the down/up half migrates under the same grammar as the initial `upgrade head`.
    """
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("script_location", str(_ALEMBIC_DIR))
    config.attributes["sqlalchemy_url"] = url
    config.attributes["dialect"] = _BED_DIALECT
    config.attributes["graph_backend"] = BED_GRAPH_BACKEND
    return config


def _run_alembic(url: str, revision: str, *, downgrade: bool) -> None:
    """Run `alembic upgrade`/`downgrade` to `revision` against `url` on a loop-free thread.

    `alembic/env.py`'s `run_migrations_online` calls `asyncio.run()`; under `asyncio_mode = auto`
    this test already runs inside a pytest-asyncio loop, so the command is offloaded to a fresh
    worker thread (which has no running loop, making the nested `asyncio.run()` legal) and blocked
    on — the same guard `tests/conftest.py::run_upgrade_head` documents.
    """
    config = _alembic_config(url)
    run = command.downgrade if downgrade else command.upgrade
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(run, config, revision).result()


async def _seed(admin_url: str) -> None:
    """Seed (as admin) a page + sections exercising every summary-fallback leg.

    A page with an active indexed summary + three sections (one with an active summary, one
    with an indexed `sections.summary` but no active `unit_summaries` row — the section
    fallback-to-its-own-indexed-summary case, `0022`, and one with neither — the section
    fallback-to-`''` case), and a second page with no active summary but a frontmatter `summary`
    (the page fallback-to-frontmatter case).
    """
    engine = create_async_engine(admin_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, summary, card_text, body_hash) "
                    "VALUES ('mu.x', 'workdocs', 'memory-unit', 'x', 'X', 'active', "
                    "'Frontmatter (shadowed by the active summary).', 'x card', 'h1')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, summary, card_text, body_hash) "
                    "VALUES ('mu.y', 'workdocs', 'memory-unit', 'y', 'Y', 'active', 'Frontmatter summary of Y.', 'y card', 'h2')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO kb.unit_summaries (unit_id, unit_kind, collection, summary, status, model, source_hash) "
                    "VALUES ('mu.x', 'page', 'workdocs', 'Indexed summary of X.', 'active', 'test-model', 'hx')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO kb.sections (id, page_id, collection, heading, path, ord, body, body_hash) "
                    "VALUES ('mu.x#s1', 'mu.x', 'workdocs', 'S1', 's1', 0, 'Body of S1.', 'hs1')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO kb.sections (id, page_id, collection, heading, path, ord, body, body_hash) "
                    "VALUES ('mu.x#s2', 'mu.x', 'workdocs', 'S2', 's2', 1, 'Body of S2.', 'hs2')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO kb.sections (id, page_id, collection, heading, path, ord, body, body_hash, summary) "
                    "VALUES ('mu.x#s3', 'mu.x', 'workdocs', 'S3', 's3', 2, 'Body of S3.', 'hs3', 'Indexed (derived) summary of S3.')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO kb.unit_summaries (unit_id, unit_kind, collection, summary, status, model, source_hash) "
                    "VALUES ('mu.x#s1', 'section', 'workdocs', 'Sec1 indexed summary.', 'active', 'test-model', 'hs1sum')"
                )
            )
    finally:
        await engine.dispose()


@pytest.fixture
async def seeded(migrated: str) -> str:
    """Seed the admin (head-migrated) DB; return the admin URL (reader reads go through `reader_url`)."""
    await _seed(migrated)
    return migrated


async def _seed_grouped(admin_url: str) -> None:
    """Seed (as admin) a `repo-docs` collection with two apps' pages plus one page with no `app` at all.

    The third page's frontmatter carries no `app` key — `COALESCE(frontmatter ->> 'app', '')` must
    bucket it into the ungrouped group, exactly as an un-annotated page in the real repo-docs knowledge base does.
    """
    engine = create_async_engine(admin_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, summary, card_text, body_hash, frontmatter) "
                    "VALUES ('adr.a', 'repo-docs', 'adr', 'a', 'A', 'active', '', 'a card', 'ha', '{\"app\": \"engram\"}'::jsonb)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, summary, card_text, body_hash, frontmatter) "
                    "VALUES ('adr.b', 'repo-docs', 'adr', 'b', 'B', 'active', '', 'b card', 'hb', '{\"app\": \"studio\"}'::jsonb)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, summary, card_text, body_hash) "
                    "VALUES ('mu.c', 'repo-docs', 'memory-unit', 'c', 'C', 'active', '', 'c card', 'hc')"
                )
            )
    finally:
        await engine.dispose()


@pytest.fixture
async def grouped_seeded(migrated: str) -> str:
    """Seed the admin (head-migrated) DB with app-grouped pages; return the admin URL."""
    await _seed_grouped(migrated)
    return migrated


async def _seed_kind_grouped(admin_url: str) -> None:
    """Seed (as admin) a `repo-docs-kinds` collection with two `adr` pages and one `reference` page.

    `kind` is a first-class `NOT NULL` column (unlike frontmatter `app`), so there is no
    unannotated-page case to cover here.
    """
    engine = create_async_engine(admin_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, summary, card_text, body_hash) "
                    "VALUES ('adr.a', 'repo-docs-kinds', 'adr', 'a', 'A', 'active', '', 'a card', 'ha')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, summary, card_text, body_hash) "
                    "VALUES ('adr.b', 'repo-docs-kinds', 'adr', 'b', 'B', 'active', '', 'b card', 'hb')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, summary, card_text, body_hash) "
                    "VALUES ('ref.c', 'repo-docs-kinds', 'reference', 'c', 'C', 'active', '', 'c card', 'hc')"
                )
            )
    finally:
        await engine.dispose()


@pytest.fixture
async def kind_grouped_seeded(migrated: str) -> str:
    """Seed the admin (head-migrated) DB with kind-grouped pages; return the admin URL."""
    await _seed_kind_grouped(migrated)
    return migrated


async def _seed_app_kind_grouped(admin_url: str) -> None:
    """Seed (as admin) `repo-docs-subgroups`: app `engram` with two `adr` pages and one `reference` page, app `studio` with one `adr` page.

    Two `adr` pages under the SAME app exercises `api.envelope_group_page_types`' `GROUP BY page_type`
    (count=2, both titles as scent) and `api.envelope_group_page_type`'s own listing; the second app's
    own `adr` page proves the app+kind filter never conflates it with `engram`'s.
    """
    engine = create_async_engine(admin_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, summary, card_text, body_hash, frontmatter) "
                    "VALUES ('adr.a', 'repo-docs-subgroups', 'adr', 'a', 'Alpha', 'active', '', 'a card', 'ha', '{\"app\": \"engram\"}'::jsonb)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, summary, card_text, body_hash, frontmatter) "
                    "VALUES ('adr.b', 'repo-docs-subgroups', 'adr', 'b', 'Beta', 'active', '', 'b card', 'hb', '{\"app\": \"engram\"}'::jsonb)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, summary, card_text, body_hash, frontmatter) "
                    "VALUES ('ref.c', 'repo-docs-subgroups', 'reference', 'c', 'Gamma', 'active', '', 'c card', 'hc', '{\"app\": \"engram\"}'::jsonb)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, summary, card_text, body_hash, frontmatter) "
                    "VALUES ('adr.d', 'repo-docs-subgroups', 'adr', 'd', 'Delta', 'active', '', 'd card', 'hd', '{\"app\": \"studio\"}'::jsonb)"
                )
            )
    finally:
        await engine.dispose()


@pytest.fixture
async def app_kind_grouped_seeded(migrated: str) -> str:
    """Seed the admin (head-migrated) DB with app+kind-grouped pages; return the admin URL."""
    await _seed_app_kind_grouped(migrated)
    return migrated


async def _read_all_three_refs(reader_url: str) -> None:
    """Drive the real `EnvelopeHandler`, over the real reader role, through all three ref forms.

    Raises whatever the underlying reader connection raises (permission denied, undefined
    function, …) — callers that expect success let it propagate as a test failure; callers that
    expect failure (the round-trip test's downgraded leg) catch it themselves.
    """
    engine = create_async_engine(reader_url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        handler = EnvelopeHandler(ReadOnlySqlGateway(engine), collection="workdocs")
        await handler("collection:workdocs")
        await handler("page:mu.x")
        await handler("section:mu.x#s1")
    finally:
        await engine.dispose()


class TestEnvelopeReaderRegression:
    """The real `EnvelopeHandler`, over the real reader role, for all three ref forms."""

    async def test_collection_ref_lists_pages_with_their_indexed_summaries(self, seeded: str, reader_url: str) -> None:
        collection = "workdocs"
        engine = create_async_engine(reader_url, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            envelope = await EnvelopeHandler(ReadOnlySqlGateway(engine), collection=collection)(f"collection:{collection}")
        finally:
            await engine.dispose()

        children = {child.ref: child for child in envelope.children}
        assert envelope.kind == "collection"
        assert children[f"page:{collection}/mu.x"].summary == "Indexed summary of X."  # the active summary wins
        assert children[f"page:{collection}/mu.y"].summary == "Frontmatter summary of Y."  # no active summary -> frontmatter

    async def test_page_ref_inlines_a_small_pages_body_with_sections_ordered_by_ord(self, seeded: str, reader_url: str) -> None:
        collection = "workdocs"
        engine = create_async_engine(reader_url, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            envelope = await EnvelopeHandler(ReadOnlySqlGateway(engine), collection=collection)("page:mu.x")
        finally:
            await engine.dispose()

        assert envelope.content is not None
        assert envelope.content.index("Body of S1.") < envelope.content.index("Body of S2.")
        assert envelope.summary == "Indexed summary of X."
        assert [child.ref for child in envelope.children] == [f"section:{collection}/mu.x#s{ordinal}" for ordinal in (1, 2, 3)]
        assert envelope.children[0].summary == "Sec1 indexed summary."
        assert envelope.children[1].summary == ""  # no active summary, no indexed summary either
        assert envelope.children[2].summary == "Indexed (derived) summary of S3."  # no active summary -> its own indexed summary

    async def test_section_ref_content_is_the_body_with_no_children(self, seeded: str, reader_url: str) -> None:
        engine = create_async_engine(reader_url, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            envelope = await EnvelopeHandler(ReadOnlySqlGateway(engine), collection="workdocs")("section:mu.x#s1")
        finally:
            await engine.dispose()

        assert envelope.content == "Body of S1."
        assert envelope.summary == "Sec1 indexed summary."
        assert envelope.children == ()

    async def test_excluded_page_types_never_appear_as_collection_children(self, seeded: str, reader_url: str) -> None:
        engine = create_async_engine(seeded, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, summary, card_text, body_hash) "
                        "VALUES ('hub.h', 'workdocs', 'hub', 'h', 'H', 'active', 'A hub.', 'h card', 'h3')"
                    )
                )
        finally:
            await engine.dispose()

        collection = "workdocs"
        reader_engine = create_async_engine(reader_url, poolclass=NullPool)
        await orm.register_pgvector(reader_engine)
        try:
            gateway = ReadOnlySqlGateway(reader_engine)
            root = f"collection:{collection}"
            unfiltered = await EnvelopeHandler(gateway, collection=collection)(root)
            filtered = await EnvelopeHandler(gateway, collection=collection, excluded_page_types=frozenset({"hub"}))(root)
        finally:
            await reader_engine.dispose()

        excluded = f"page:{collection}/hub.h"
        assert excluded in {child.ref for child in unfiltered.children}
        assert excluded not in {child.ref for child in filtered.children}
        assert {child.ref for child in filtered.children} == {child.ref for child in unfiltered.children} - {excluded}

    async def test_reader_url_genuinely_differs_from_the_admin_url(self, seeded: str, reader_url: str) -> None:
        """Sanity guard: `seeded` (admin) vs `reader_url` (reader) must be different roles.

        Without this, the three reads above would prove nothing about the reader's grant boundary.
        """
        assert reader_url != seeded
        assert "kb_reader" in reader_url


class TestEnvelopeGroupSurface:
    """The real `EnvelopeHandler`, over the real reader role, for the grouped ref forms.

    Exercises `api.envelope_collection_groups` / `api.envelope_group` (migration `0019`): a
    `collection:` ref groups pages by frontmatter `app`, and a `group:` ref lists one group's pages.
    """

    async def test_collection_ref_groups_pages_by_app_with_an_ungrouped_bucket(self, grouped_seeded: str, reader_url: str) -> None:
        engine = create_async_engine(reader_url, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            envelope = await EnvelopeHandler(ReadOnlySqlGateway(engine), group_by="app")("collection:repo-docs")
        finally:
            await engine.dispose()

        children = {child.ref: child for child in envelope.children}
        assert children["group:repo-docs/engram"] == ChildSummary(
            ref="group:repo-docs/engram", kind="group", title="engram", summary="1 page · e.g. A"
        )
        assert children["group:repo-docs/studio"] == ChildSummary(
            ref="group:repo-docs/studio", kind="group", title="studio", summary="1 page · e.g. B"
        )
        assert children["group:repo-docs/"] == ChildSummary(
            ref="group:repo-docs/", kind="group", title="ungrouped", summary="1 page · e.g. C"
        )

    async def test_group_ref_lists_exactly_that_apps_pages(self, grouped_seeded: str, reader_url: str) -> None:
        collection = "repo-docs"
        engine = create_async_engine(reader_url, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            envelope = await EnvelopeHandler(ReadOnlySqlGateway(engine), group_by="app")(f"group:{collection}/engram")
        finally:
            await engine.dispose()

        assert envelope.kind == "group"
        assert envelope.title == "engram"
        assert [child.ref for child in envelope.children] == [f"page:{collection}/adr.a"]

    async def test_group_ref_for_the_ungrouped_bucket_lists_the_unannotated_page(self, grouped_seeded: str, reader_url: str) -> None:
        collection = "repo-docs"
        engine = create_async_engine(reader_url, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            envelope = await EnvelopeHandler(ReadOnlySqlGateway(engine), group_by="app")(f"group:{collection}/")
        finally:
            await engine.dispose()

        assert envelope.title == "ungrouped"
        assert [child.ref for child in envelope.children] == [f"page:{collection}/mu.c"]


class TestEnvelopeKindSurface:
    """The real `EnvelopeHandler`, over the real reader role, for the kind-grouped ref forms.

    Exercises `api.envelope_collection_page_types` / `api.envelope_page_type` (migration `0020`): a
    `collection:` ref groups pages by the first-class `kind` column, and a `group:` ref lists one
    kind's pages.
    """

    async def test_collection_ref_groups_pages_by_kind(self, kind_grouped_seeded: str, reader_url: str) -> None:
        engine = create_async_engine(reader_url, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            envelope = await EnvelopeHandler(ReadOnlySqlGateway(engine), group_by="page_type")("collection:repo-docs-kinds")
        finally:
            await engine.dispose()

        children = {child.ref: child for child in envelope.children}
        assert children["group:repo-docs-kinds/adr"] == ChildSummary(
            ref="group:repo-docs-kinds/adr", kind="group", title="adr", summary="2 pages · e.g. A; B"
        )
        assert children["group:repo-docs-kinds/reference"] == ChildSummary(
            ref="group:repo-docs-kinds/reference", kind="group", title="reference", summary="1 page · e.g. C"
        )

    async def test_group_ref_lists_exactly_that_kinds_pages(self, kind_grouped_seeded: str, reader_url: str) -> None:
        collection = "repo-docs-kinds"
        engine = create_async_engine(reader_url, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            envelope = await EnvelopeHandler(ReadOnlySqlGateway(engine), group_by="page_type")(f"group:{collection}/adr")
        finally:
            await engine.dispose()

        assert envelope.kind == "group"
        assert envelope.title == "adr"
        assert [child.ref for child in envelope.children] == [f"page:{collection}/adr.a", f"page:{collection}/adr.b"]


class TestEnvelopeAppKindSubGroupSurface:
    """The real `EnvelopeHandler`, over the real reader role, for the three-segment `group:<collection>/<app>/<kind>` ref.

    Exercises `api.envelope_group_page_types` / `api.envelope_group_page_type` (migration `0021`): counts one
    app's pages by kind (with scent), and a three-segment `group:` ref lists that app+kind's pages.
    """

    async def test_group_kinds_counts_the_apps_pages_by_kind_with_sample_titles(
        self, app_kind_grouped_seeded: str, reader_url: str
    ) -> None:
        engine = create_async_engine(reader_url, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            gateway = ReadOnlySqlGateway(engine)
            result = await gateway.rows(
                "SELECT * FROM api.envelope_group_page_types(:collection, :app, cast(:excluded_page_types as text)::text[])",
                {"collection": "repo-docs-subgroups", "app": "engram", "excluded_page_types": "{}"},
                cap=50,
            )
        finally:
            await engine.dispose()

        by_kind = {row["page_type"]: row for row in result.rows}
        assert by_kind["adr"]["pages"] == 2
        assert _titles(by_kind["adr"]) == ["Alpha", "Beta"]
        assert by_kind["reference"]["pages"] == 1
        assert _titles(by_kind["reference"]) == ["Gamma"]

    async def test_three_segment_ref_lists_exactly_that_app_and_kinds_pages(self, app_kind_grouped_seeded: str, reader_url: str) -> None:
        collection = "repo-docs-subgroups"
        engine = create_async_engine(reader_url, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            envelope = await EnvelopeHandler(ReadOnlySqlGateway(engine), group_by="app")(f"group:{collection}/engram/adr")
        finally:
            await engine.dispose()

        assert envelope.kind == "group"
        assert envelope.title == "adr"
        assert [child.ref for child in envelope.children] == [f"page:{collection}/adr.a", f"page:{collection}/adr.b"]

    async def test_a_different_app_with_the_same_kind_is_not_conflated(self, app_kind_grouped_seeded: str, reader_url: str) -> None:
        collection = "repo-docs-subgroups"
        engine = create_async_engine(reader_url, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            envelope = await EnvelopeHandler(ReadOnlySqlGateway(engine), group_by="app")(f"group:{collection}/studio/adr")
        finally:
            await engine.dispose()

        assert [child.ref for child in envelope.children] == [f"page:{collection}/adr.d"]


class TestEnvelopeSurfaceRoundTrip:
    """`0012` fails at `0011`, passes at head, and the migration chain round-trips cleanly."""

    async def test_disclose_read_fails_at_0011_and_recovers_after_reupgrade_to_head(self, seeded: str, reader_url: str) -> None:
        """The reader-backed disclose read works only at head, and the chain round-trips.

        At head (`seeded`/`migrated` already migrated the DB to `0012`) the reader-backed read
        succeeds. Downgrading to `0011` takes away both `api.envelope_*` and the `kb_reader`
        login name (`0014` restores the pre-rename one), so the *same* reader-backed call must
        fail: undefined-function or permission denied on a direct-`memory.*` read through
        SQLAlchemy, or an authorization failure before the statement is ever sent. Either way,
        never silently succeed. Re-upgrading to head must restore both, and success with them.
        """
        await _read_all_three_refs(reader_url)  # head: succeeds

        _run_alembic(seeded, "0011", downgrade=True)
        with pytest.raises((DBAPIError, asyncpg.InvalidAuthorizationSpecificationError)):
            await _read_all_three_refs(reader_url)  # 0011: no api.envelope_*, no kb_reader -> fails

        _run_alembic(seeded, "head", downgrade=False)
        await _read_all_three_refs(reader_url)  # back at head: succeeds again

    async def test_downgrade_then_reupgrade_leaves_all_three_functions_present(self, seeded: str) -> None:
        _run_alembic(seeded, "0011", downgrade=True)
        _run_alembic(seeded, "head", downgrade=False)

        connection = await asyncpg.connect(_asyncpg_dsn(seeded))
        try:
            functions = {
                row[0]
                for row in await connection.fetch(
                    "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'api'"
                )
            }
        finally:
            await connection.close()

        assert {"envelope_collection", "envelope_page", "envelope_section"} <= functions

    async def test_group_functions_vanish_at_0018_and_recover_after_reupgrade_to_head(self, seeded: str) -> None:
        """`api.envelope_collection_groups`/`api.envelope_group` exist at head, vanish at `0018`, and come back."""
        assert {"envelope_collection_groups", "envelope_group"} <= await _api_function_names(seeded)  # head: present

        _run_alembic(seeded, "0018", downgrade=True)
        assert not {"envelope_collection_groups", "envelope_group"} & await _api_function_names(seeded)  # 0018: gone

        _run_alembic(seeded, "head", downgrade=False)
        assert {"envelope_collection_groups", "envelope_group"} <= await _api_function_names(seeded)  # back at head

    async def test_kind_functions_vanish_at_0019_and_recover_after_reupgrade_to_head(self, seeded: str) -> None:
        """`api.envelope_collection_page_types`/`api.envelope_page_type` exist at head, vanish at `0019`, and come back."""
        assert {"envelope_collection_page_types", "envelope_page_type"} <= await _api_function_names(seeded)  # head: present

        _run_alembic(seeded, "0019", downgrade=True)
        assert not {"envelope_collection_page_types", "envelope_page_type"} & await _api_function_names(seeded)  # 0019: gone

        _run_alembic(seeded, "head", downgrade=False)
        assert {"envelope_collection_page_types", "envelope_page_type"} <= await _api_function_names(seeded)  # back at head

    async def test_app_kind_subgroup_functions_vanish_at_0020_and_recover_after_reupgrade_to_head(self, seeded: str) -> None:
        """`api.envelope_group_page_types`/`api.envelope_group_page_type` exist at head, vanish at `0020`, and come back."""
        assert {"envelope_group_page_types", "envelope_group_page_type"} <= await _api_function_names(seeded)  # head: present

        _run_alembic(seeded, "0020", downgrade=True)
        assert not {"envelope_group_page_types", "envelope_group_page_type"} & await _api_function_names(seeded)  # 0020: gone
        # the replaced group-counting functions survive the downgrade, restored to their pre-0021 shape,
        # under their pre-0046 names (0046's own downgrade, run first, renamed them back).
        assert {"envelope_collection_groups", "envelope_collection_kinds"} <= await _api_function_names(seeded)

        _run_alembic(seeded, "head", downgrade=False)
        assert {"envelope_group_page_types", "envelope_group_page_type"} <= await _api_function_names(seeded)  # back at head

    async def test_downgrade_to_0020_restores_the_two_column_group_counting_shape(self, seeded: str) -> None:
        """`0021` adds `sample_titles`; downgrading to `0020` must restore the plain two-column bodies, not just the names."""
        _run_alembic(seeded, "0020", downgrade=True)

        connection = await asyncpg.connect(_asyncpg_dsn(seeded))
        try:
            groups_body = await connection.fetchval(
                "SELECT pg_get_functiondef('api.envelope_collection_groups(text, text[])'::regprocedure)"
            )
            # Pre-0046 name: 0046's own downgrade (run before 0021's) renamed it back from
            # `envelope_collection_page_types`.
            kinds_body = await connection.fetchval("SELECT pg_get_functiondef('api.envelope_collection_kinds(text, text[])'::regprocedure)")
        finally:
            await connection.close()

        assert "sample_titles" not in str(groups_body)
        assert "sample_titles" not in str(kinds_body)

        _run_alembic(seeded, "head", downgrade=False)


async def _api_function_names(url: str) -> set[str]:
    """The set of function names currently defined in the `api` schema, over the admin connection."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        rows = await connection.fetch("SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'api'")
        return {row[0] for row in rows}
    finally:
        await connection.close()


class TestFunctionBodies:
    """`pg_get_functiondef` pins each `api.envelope_*` body — the 0010 review-fix verification style."""

    @pytest.fixture
    async def conn(self, migrated: str) -> AsyncIterator[asyncpg.Connection]:
        connection = await asyncpg.connect(_asyncpg_dsn(migrated))
        try:
            yield connection
        finally:
            await connection.close()

    async def _body(self, connection: asyncpg.Connection, signature: str) -> str:
        return str(await connection.fetchval(f"SELECT pg_get_functiondef('{signature}'::regprocedure)"))

    @pytest.mark.parametrize("signature", _ENVELOPE_FUNCTION_SIGNATURES)
    async def test_body_references_kb_schema(self, conn: asyncpg.Connection, signature: str) -> None:
        body = await self._body(conn, signature)

        assert f"{_KB_SCHEMA}." in body
        assert "wiki." not in body  # the pre-de-Obsidianification schema name never leaks in

    async def test_collection_body_does_not_filter_by_page_status(self, conn: asyncpg.Connection) -> None:
        body = await self._body(conn, "api.envelope_collection(text, text[])")

        assert "p.status" not in body  # the collection envelope deliberately does not filter by page status
        assert "s.status = 'active'" in body  # the summary join is still active-only
        assert "p_excluded_page_types" in body  # the schema's per-page-type exclusion is enforced in the body

    async def test_page_body_unions_the_page_row_and_its_sections(self, conn: asyncpg.Connection) -> None:
        body = await self._body(conn, "api.envelope_page(text, text)")

        assert "UNION ALL" in body
        assert "kb.pages" in body
        assert "kb.sections" in body

    async def test_all_are_security_definer(self, conn: asyncpg.Connection) -> None:
        for signature in _ENVELOPE_FUNCTION_SIGNATURES:
            body = await self._body(conn, signature)
            assert "SECURITY DEFINER" in body

    async def test_groups_body_counts_by_frontmatter_app_never_filtering_by_page_status(self, conn: asyncpg.Connection) -> None:
        body = await self._body(conn, "api.envelope_collection_groups(text, text[])")

        assert "p.status" not in body  # grouped, like flat, deliberately does not filter by page status
        assert "frontmatter ->> 'app'" in body
        assert "GROUP BY" in body
        assert "sample_titles" in body
        assert "array_agg" in body

    async def test_group_body_filters_by_collection_and_app(self, conn: asyncpg.Connection) -> None:
        body = await self._body(conn, "api.envelope_group(text, text, text[])")

        assert "frontmatter ->> 'app'" in body
        assert "s.status = 'active'" in body  # the summary join is still active-only

    async def test_kinds_body_counts_by_kind_never_filtering_by_page_status(self, conn: asyncpg.Connection) -> None:
        body = await self._body(conn, "api.envelope_collection_page_types(text, text[])")

        assert "p.status" not in body  # kind-grouped, like flat and app-grouped, does not filter by page status
        assert "GROUP BY" in body
        assert "sample_titles" in body
        assert "array_agg" in body

    async def test_kind_body_filters_by_collection_and_kind(self, conn: asyncpg.Connection) -> None:
        body = await self._body(conn, "api.envelope_page_type(text, text, text[])")

        assert "p.page_type = p_page_type" in body
        assert "s.status = 'active'" in body  # the summary join is still active-only

    async def test_group_kinds_body_counts_by_kind_within_one_app_never_filtering_by_page_status(self, conn: asyncpg.Connection) -> None:
        body = await self._body(conn, "api.envelope_group_page_types(text, text, text[])")

        assert "p.status" not in body
        assert "frontmatter ->> 'app'" in body
        assert "GROUP BY" in body
        assert "sample_titles" in body

    async def test_group_kind_body_filters_by_collection_app_and_kind(self, conn: asyncpg.Connection) -> None:
        body = await self._body(conn, "api.envelope_group_page_type(text, text, text, text[])")

        assert "frontmatter ->> 'app'" in body
        assert "p.page_type = p_page_type" in body
        assert "s.status = 'active'" in body  # the summary join is still active-only
