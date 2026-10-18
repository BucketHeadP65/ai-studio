"""The reader engine speaks asyncpg: libpq-spelled URLs are translated before connecting.

The settings file writes DSNs in libpq's spelling (`?sslmode=disable`), which asyncpg's
`connect()` refuses as an unknown keyword. The reader engine must translate on the way in,
the way every other engine in the checkout does, or the first graph read answers a 500.
"""

from __future__ import annotations

from core.db import build_reader_engine


async def test_reader_engine_translates_libpq_sslmode() -> None:
    engine = build_reader_engine("postgresql+asyncpg://kb_reader:secret@localhost:5434/knowledge_bases?sslmode=disable")
    try:
        query = dict(engine.url.query)
        assert "sslmode" not in query, "asyncpg's connect() has no sslmode argument"
        assert "ssl" not in query, "asyncpg spells 'off' as the argument's absence"
    finally:
        await engine.dispose()


async def test_reader_engine_keeps_tls_when_the_dsn_requires_it() -> None:
    engine = build_reader_engine("postgresql+asyncpg://kb_reader:secret@db.example:5432/knowledge_bases?sslmode=require")
    try:
        query = dict(engine.url.query)
        assert "sslmode" not in query
        assert query.get("ssl") == "require", "dropping sslmode=require would quietly drop TLS"
    finally:
        await engine.dispose()
