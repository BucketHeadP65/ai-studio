"""Engine / session-factory smoke tests against in-memory sqlite+aiosqlite."""

from __future__ import annotations

import pytest

from sqlalchemy import Column, Integer, MetaData, String, Table, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core.db import async_dsn, asyncpg_dsn, build_engine, build_session_factory, create_tables


def test_build_engine_returns_async_engine():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    assert isinstance(engine, AsyncEngine)


class TestAsyncDsn:
    @pytest.mark.parametrize("scheme", ["postgresql", "postgres"])
    def test_a_bare_postgres_scheme_gains_the_asyncpg_driver(self, scheme: str):
        assert async_dsn(f"{scheme}://u:p@host:5432/db") == "postgresql+asyncpg://u:p@host:5432/db"

    def test_sslmode_becomes_asyncpg_ssl_word(self):
        assert async_dsn("postgresql://u:p@host/db?sslmode=require") == "postgresql+asyncpg://u:p@host/db?ssl=require"

    def test_sslmode_disable_becomes_absence(self):
        assert async_dsn("postgresql://u:p@host/db?sslmode=disable") == "postgresql+asyncpg://u:p@host/db"

    def test_other_query_keys_survive_the_translation(self):
        assert async_dsn("postgresql://host/db?application_name=app&sslmode=require") == (
            "postgresql+asyncpg://host/db?application_name=app&ssl=require"
        )

    def test_an_already_async_dsn_still_gets_its_sslmode_translated(self):
        assert async_dsn("postgresql+asyncpg://host/db?sslmode=verify-full") == "postgresql+asyncpg://host/db?ssl=verify-full"

    def test_an_already_async_dsn_without_sslmode_passes_unchanged(self):
        assert async_dsn("postgresql+asyncpg://u:p@host:5432/db") == "postgresql+asyncpg://u:p@host:5432/db"

    @pytest.mark.parametrize("dsn", ["postgresql+psycopg://host/db?sslmode=require", "sqlite+aiosqlite:///:memory:", "mysql://host/db"])
    def test_other_drivers_and_schemes_pass_untouched(self, dsn: str):
        assert async_dsn(dsn) == dsn


def test_build_session_factory_returns_sessionmaker():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    factory = build_session_factory(engine)
    assert isinstance(factory, async_sessionmaker)


async def test_session_factory_opens_a_working_session():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    factory = build_session_factory(engine)
    async with factory() as session:
        assert isinstance(session, AsyncSession)
        result = await session.execute(text("SELECT 1"))
        assert result.scalar_one() == 1
    await engine.dispose()


async def test_create_tables_creates_the_passed_metadata_tables():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    metadata = MetaData()
    Table("widgets", metadata, Column("id", Integer, primary_key=True), Column("name", String))

    await create_tables(engine, metadata)

    async with engine.begin() as conn:
        rows = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='widgets'"))
        assert rows.scalar_one() == "widgets"
    await engine.dispose()


def test_asyncpg_dsn_strips_the_driver_and_restores_sslmode():
    assert asyncpg_dsn("postgresql+asyncpg://u:p@h:5432/db?ssl=require") == "postgresql://u:p@h:5432/db?sslmode=require"


def test_asyncpg_dsn_passes_a_libpq_dsn_through():
    assert asyncpg_dsn("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


def test_asyncpg_dsn_leaves_other_schemes_alone():
    assert asyncpg_dsn("sqlite+aiosqlite:///:memory:") == "sqlite+aiosqlite:///:memory:"
