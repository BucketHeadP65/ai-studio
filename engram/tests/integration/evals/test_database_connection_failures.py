"""A database that refuses the connection fails the case transiently, through the reader engine an evaluation run opens."""

from __future__ import annotations

import socket

import pytest

from core.db import build_reader_engine

from engram.evals.harness.transient_failures import is_transient_error_text, is_transient_failure


def _refused_port() -> int:
    """A local port nothing listens on, so a connection to it is refused."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _connection_failure(host: str) -> OSError:
    """What opening a reader connection raises when nothing listens on the database's port."""
    engine = build_reader_engine(f"postgresql://reader:reader@{host}:{_refused_port()}/kb")
    try:
        with pytest.raises(OSError, match="Connect call failed") as raised:
            async with engine.connect():
                pass
    finally:
        await engine.dispose()
    return raised.value


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
async def test_a_database_that_refuses_the_connection_fails_the_case_transiently(host: str) -> None:
    error = await _connection_failure(host)

    assert is_transient_failure(error) is True
    assert is_transient_error_text(f"{type(error).__name__}: {error}") is True
