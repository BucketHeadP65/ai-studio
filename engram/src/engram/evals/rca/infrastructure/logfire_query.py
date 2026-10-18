"""Build the Logfire Query API client root-cause analysis reads telemetry through."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

    from core.settings import LogfireSettings

    from engram.evals.rca.internal_contracts import SupportsLogfireQuery


def build_logfire_query_client(settings: LogfireSettings, *, transport: httpx.BaseTransport | None = None) -> SupportsLogfireQuery:
    """Construct logfire's `LogfireQueryClient` from the configured Logfire settings.

    The client authenticates with the read token and targets the configured regional `base_url`.
    `logfire` loads only when this function is called.

    Args:
        settings: The `infrastructure.logfire` settings (read token + base URL).
        transport: An optional `httpx` transport, forwarded to the underlying `httpx.Client`.
            `None` uses httpx's default transport.

    Returns:
        A `SupportsLogfireQuery` ready to run SQL against the Query API.

    Raises:
        ValueError: If `settings.read_token` is empty.
    """
    if not settings.read_token:
        msg = "Logfire RCA requires infrastructure.logfire.read_token. It is empty (set APP_INFRASTRUCTURE__LOGFIRE__READ_TOKEN)."
        raise ValueError(msg)

    from logfire.experimental.query_client import LogfireQueryClient  # noqa: PLC0415 (lazy: logfire loads only on an RCA query)

    # `transport` reaches logfire's `httpx.Client` through its extra client keyword arguments.
    return LogfireQueryClient(read_token=settings.read_token, base_url=settings.base_url or None, transport=transport)
