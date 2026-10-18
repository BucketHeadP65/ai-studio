"""The harness's Logfire tracing, and the endpoints that must never reach a trace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.observability import configure_logging, get_logger, start_tracing

if TYPE_CHECKING:
    from core.observability import LoggingSettings
    from core.settings import LogfireSettings

POLLED = ("/api/pending", "/api/run/state", "/api/events")
"""Endpoints a browser asks over and over, and which must never reach the trace.

Each entry is matched as a regular expression against the request URL, so a path carrying an id
is excluded by pattern rather than one line per resource.
"""


@dataclass(frozen=True)
class LogfireTracing:
    """Ships this process's spans and records to Logfire under the logging section's service name, when a write token is on file.

    Without a write token, records stay in the process and nothing is shipped. Either way, logging is configured from
    the logging section, so every module's records reach the same place.
    """

    logfire: LogfireSettings
    logging: LoggingSettings

    def start(self) -> None:
        """Start shipping when a write token is on file, then route every module's records through the same backend."""
        if self.logfire.write_token:
            start_tracing(self.logfire, self.logging)
        configure_logging(self.logging)
        if not self.logfire.write_token:
            _logger.info("no infrastructure.logfire.write_token on file, so spans and records stay in this process")


_logger = get_logger(__name__)
