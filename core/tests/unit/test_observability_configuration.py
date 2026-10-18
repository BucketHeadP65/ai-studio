"""`configure_logging` self-configures only when nobody else has.

A host that configured Logfire (a serving app with a write token) must keep its exporting
providers: a second `logfire.configure` replaces them and silently ends every span the process
ships from that moment on. The quiet local configuration exists for a process nobody configured —
the CLI, a bare library consumer.
"""

from __future__ import annotations

import logging

from typing import TYPE_CHECKING

import logfire
import pytest

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from core import observability

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def fresh_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "_configured", False)


@pytest.fixture
def recorded_configure(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(logfire, "configure", lambda **kwargs: calls.append(kwargs))
    return calls


@pytest.fixture
def handlers_restored() -> Iterator[None]:
    root = logging.getLogger()
    before = list(root.handlers)
    yield
    root.handlers[:] = before


@pytest.mark.usefixtures("fresh_flag", "handlers_restored")
def test_an_unconfigured_process_gets_the_quiet_local_configuration(
    recorded_configure: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trace, "get_tracer_provider", trace.ProxyTracerProvider)

    observability.configure_logging()

    assert recorded_configure == [{"send_to_logfire": False, "console": False, "inspect_arguments": False}]


@pytest.mark.usefixtures("fresh_flag", "handlers_restored")
def test_a_hosts_configuration_is_never_replaced(
    recorded_configure: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trace, "get_tracer_provider", TracerProvider)

    observability.configure_logging()

    assert recorded_configure == [], "re-configuring would replace the host's exporting providers"


@pytest.mark.usefixtures("fresh_flag", "handlers_restored")
def test_settings_name_the_service_and_set_the_level(recorded_configure: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trace, "get_tracer_provider", trace.ProxyTracerProvider)

    observability.configure_logging(observability.LoggingSettings(service_name="studio", environment="test", level="WARNING"))

    assert recorded_configure == [
        {"send_to_logfire": False, "inspect_arguments": False, "console": False, "service_name": "studio", "service_version": "0.0.0", "environment": "test"}
    ]
    assert logging.getLogger().level == logging.WARNING


@pytest.mark.usefixtures("fresh_flag", "handlers_restored")
def test_console_settings_print_at_the_configured_level(recorded_configure: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trace, "get_tracer_provider", trace.ProxyTracerProvider)

    observability.configure_logging(observability.LoggingSettings(service_name="studio", console=True, level="DEBUG"))

    console = recorded_configure[0]["console"]
    assert isinstance(console, logfire.ConsoleOptions)
    assert console.min_log_level == "debug"


def test_stop_tracing_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> None:
        raise RuntimeError

    monkeypatch.setattr(logfire, "shutdown", broken)

    observability.stop_tracing()
