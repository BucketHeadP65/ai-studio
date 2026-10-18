"""The Temporal server this app uses, started here when nothing else is running one.

`ensure_server` probes the configured address. When nothing answers and the address names this
machine, it starts Temporal's own dev server over a SQLite file. When nothing answers at another
machine's address, it reports the address unreachable and starts nothing.
"""

from __future__ import annotations

import asyncio
import shutil

from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from core.observability import get_logger
from temporalio.testing import WorkflowEnvironment

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from harness.settings import RunSettings

_logger = get_logger(__name__)

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
"""The hosts this app may start a server for. Any other address belongs to somebody else."""

PROBE_SECONDS = 2.0
"""How long the probe waits before deciding nothing answers."""

UI_PORT = 8233
"""Where a server started here serves its web UI, the same port this project's Temporal compose file publishes."""

DATABASE_NAME = "temporal.db"
"""The file a started server keeps its history in, inside the state directory it was given."""


@dataclass(frozen=True)
class ServerOutcome:
    """What `ensure_server` did about the configured address."""

    address: str
    """The address durable runs are served on and started against."""
    started: bool
    """Whether this process brought the server up, and therefore takes it down again."""
    reachable: bool = True
    """False when nothing answers and this app may not start one, so nothing may be served."""


class ServerHandle(Protocol):
    """The one thing this app does with a server it started: stop it."""

    async def shutdown(self) -> None:
        """Stop the server and release its port."""


class AddressProbe(Protocol):
    """Answers whether something already serves `address`, giving up after `probe_seconds` seconds."""

    async def __call__(self, address: str, *, probe_seconds: float) -> bool:
        """Probe `address` once.

        Args:
            address: The `host:port` target to probe.
            probe_seconds: How long to wait for an answer, in seconds.

        Returns:
            True when something answers `address` inside `probe_seconds`.
        """
        ...


class Starter(Protocol):
    """Brings up a local server on `port`, keeping its history in `database_file`."""

    async def __call__(self, *, port: int, database_file: Path) -> ServerHandle:
        """Start the server.

        Args:
            port: The port the server binds.
            database_file: The file the server keeps its history in.

        Returns:
            The handle that stops the server.
        """
        ...


def split_address(address: str) -> tuple[str, int]:
    """The host and port of a `host:port` target, the bracketed IPv6 spelling included.

    Args:
        address: What `APP_RUN__TEMPORAL_ADDRESS` carries, e.g. `localhost:7233` or `[::1]:7233`.

    Returns:
        The host with any brackets stripped, and the port as a number.

    Raises:
        ValueError: `address` carries no port, so there is nothing to probe and nothing to bind.
    """
    host, separator, port = address.rpartition(":")
    if not separator or not port.isdigit():
        msg = f"temporal address {address!r} must be host:port, with a port number"
        raise ValueError(msg)
    return host.strip("[]"), int(port)


async def connect_probe(address: str, *, probe_seconds: float) -> bool:
    """Whether a TCP connection to `address` is accepted inside `probe_seconds` seconds.

    It opens a plain socket, so any listener on that port counts as an answer, Temporal or not.

    Args:
        address: A `host:port` target.
        probe_seconds: How long to wait for the connection.

    Returns:
        True when the connection was accepted in time.

    Raises:
        ValueError: `address` carries no port.
    """
    host, port = split_address(address)
    try:
        async with asyncio.timeout(probe_seconds):
            _, writer = await asyncio.open_connection(host, port)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with suppress(OSError):
        await writer.wait_closed()
    return True


async def start_dev_server(*, port: int, database_file: Path) -> ServerHandle:
    """Start Temporal's own dev server on `port`, with its history in `database_file`.

    It uses an installed `temporal` binary when the machine has one. Without one the SDK downloads
    a binary once and caches it.

    Args:
        port: The port the server listens on.
        database_file: The SQLite file the server keeps its history in. Its folder is created.

    Returns:
        The handle that stops the server.
    """
    database_file.parent.mkdir(parents=True, exist_ok=True)
    return await WorkflowEnvironment.start_local(
        port=port,
        ui=True,
        ui_port=UI_PORT,
        dev_server_existing_path=shutil.which("temporal"),
        dev_server_database_filename=str(database_file),
    )


@asynccontextmanager
async def ensure_server(
    settings: RunSettings,
    state_dir: Path,
    *,
    probe: AddressProbe = connect_probe,
    starter: Starter = start_dev_server,
) -> AsyncGenerator[ServerOutcome]:
    """Make the configured Temporal address answer for the life of this context.

    Args:
        settings: The engine settings. Only `temporal_address` is read.
        state_dir: Where a server started here keeps its history, as `temporal.db` inside it.
        probe: Answers whether the address is already served.
        starter: Brings up the local server when it is not.

    Yields:
        What happened: the address, whether this process owns the server, and whether durable runs
        can be served at all.
    """
    address = settings.temporal_address
    if await probe(address, probe_seconds=PROBE_SECONDS):
        _logger.info("temporal answers at {address}", address=address)
        yield ServerOutcome(address=address, started=False)
        return
    host, port = split_address(address)
    if host not in LOCAL_HOSTS:
        _logger.warning("nothing answers at {address}, and it is not this machine's to start", address=address)
        yield ServerOutcome(address=address, started=False, reachable=False)
        return
    try:
        server = await starter(port=port, database_file=state_dir / DATABASE_NAME)
    except Exception as exc:  # no network, no cached binary, the port taken between the probe and the bind
        _logger.warning("a local temporal server on {address} would not start ({reason})", address=address, reason=str(exc))
        yield ServerOutcome(address=address, started=False, reachable=False)
        return
    _logger.info("started a local temporal server on {address}, ui on {ui}", address=address, ui=UI_PORT)
    try:
        yield ServerOutcome(address=address, started=True)
    finally:
        await server.shutdown()
