"""Unit tests for the Logfire instrumentation seam (:mod:`engram.evals.harness.infrastructure.instrumentation`).

These tests assert the seam's *behaviour* without ever sending telemetry: the two Logfire
side-effecting calls (`logfire.configure` and `logfire.instrument_pydantic_ai`) are injected as
narrow-port callables, so a recorder fake captures the arguments offline. No network, no
`monkeypatch` of a logfire internal — production binds the real callables as defaults, the test
fills the seam (the "if a test must patch a name, production has a missing parameter" rule).

Covered: empty `write_token` is a no-op returning `False` (`configure` never called), a
non-empty token configures exactly once with the expected token / `service_name` / EU
`base_url` and instruments pydantic-ai, a second call is idempotent (no reconfigure), and the
experiment span tags every axis of the variant it opens for.
"""

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING

import pytest

from core.settings import LogfireSettings

from engram.evals.harness.infrastructure.instrumentation import (
    EXPERIMENT_SPAN_NAME,
    configure_logfire,
    experiment_span,
    reset_logfire_for_testing,
    resolve_logfire_settings,
)

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from engram.evals.harness.infrastructure.instrumentation import AdvancedOptionsT


class _RecordingConfigure:
    """A typed recorder standing in for `logfire.configure` — captures kwargs, sends nothing."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, *, token: str, send_to_logfire: bool, service_name: str, advanced: AdvancedOptionsT) -> None:
        self.calls.append({"token": token, "send_to_logfire": send_to_logfire, "service_name": service_name, "advanced": advanced})


class _RecordingInstrument:
    """A typed recorder standing in for `logfire.instrument_pydantic_ai` — counts invocations."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


@pytest.fixture(autouse=True)
def _reset_module_guard() -> None:
    """Clear the process-wide configured-once guard so each test starts un-configured."""
    reset_logfire_for_testing()


def test_empty_token_is_a_noop_and_returns_false() -> None:
    configure = _RecordingConfigure()
    instrument = _RecordingInstrument()

    configured = configure_logfire(LogfireSettings(write_token=""), configure=configure, instrument_pydantic_ai=instrument)

    assert configured is False
    assert configure.calls == []
    assert instrument.calls == 0


def test_non_empty_token_configures_once_with_expected_args() -> None:
    configure = _RecordingConfigure()
    instrument = _RecordingInstrument()
    resource = LogfireSettings(write_token="pylf_v1_eu_secret", base_url="https://logfire-eu.pydantic.dev")

    configured = configure_logfire(resource, configure=configure, instrument_pydantic_ai=instrument)

    assert configured is True
    assert len(configure.calls) == 1
    call = configure.calls[0]
    assert call["token"] == "pylf_v1_eu_secret"
    assert call["send_to_logfire"] is True
    assert call["service_name"] == "engram-evals"
    assert getattr(call["advanced"], "base_url", None) == "https://logfire-eu.pydantic.dev"
    assert instrument.calls == 1


def test_second_call_is_idempotent_and_does_not_reconfigure() -> None:
    configure = _RecordingConfigure()
    instrument = _RecordingInstrument()
    resource = LogfireSettings(write_token="pylf_v1_eu_secret")

    first = configure_logfire(resource, configure=configure, instrument_pydantic_ai=instrument)
    second = configure_logfire(resource, configure=configure, instrument_pydantic_ai=instrument)

    assert first is True
    assert second is True
    assert len(configure.calls) == 1
    assert instrument.calls == 1


def test_empty_token_after_a_configured_run_stays_configured() -> None:
    configure = _RecordingConfigure()
    instrument = _RecordingInstrument()

    configure_logfire(LogfireSettings(write_token="pylf_v1_eu_secret"), configure=configure, instrument_pydantic_ai=instrument)
    again = configure_logfire(LogfireSettings(write_token=""), configure=configure, instrument_pydantic_ai=instrument)

    assert again is True
    assert len(configure.calls) == 1


class _FakeContainer:
    """A minimal container yielding one scripted resource — for :func:`resolve_logfire_settings`."""

    def __init__(self, resource: object) -> None:
        self.resource = resource

    async def get[T](self, klass: type[T]) -> T:
        assert klass is LogfireSettings
        assert isinstance(self.resource, klass)
        return self.resource

    async def close(self) -> None:  # pragma: no cover — never closed in this spec
        return


async def test_resolve_logfire_settings_returns_the_registered_settings() -> None:
    # The composition root registers LogfireSettings, so the shared resolver returns it directly
    # (no try/except degrade-to-default) — proven here against a container that provides it.
    resource = LogfireSettings(write_token="pylf_v1_eu_secret")

    resolved = await resolve_logfire_settings(_FakeContainer(resource))

    assert resolved is resource


class _RecordingSpan:
    """A typed recorder standing in for `logfire.span`, capturing the tags and opening nothing."""

    def __init__(self) -> None:
        self.template = ""
        self.attributes: dict[str, str] = {}

    def __call__(
        self, template: str, /, *, model: str, instructions: str, dataset: str, retrieval_strategy: str
    ) -> AbstractContextManager[object]:
        self.template = template
        self.attributes = {"model": model, "instructions": instructions, "dataset": dataset, "retrieval_strategy": retrieval_strategy}
        return nullcontext()


def test_the_experiment_span_tags_every_axis_of_the_variant() -> None:
    # The retrieval surface is one of the axes, so two runs differing only in their strategy must
    # not land on the same span tags and read as one variant run twice.
    span = _RecordingSpan()

    with experiment_span(
        model="test-model", instructions="answer briefly", dataset="evals/seed.yaml", retrieval_strategy="search-hybrid", span=span
    ):
        pass

    assert span.template == EXPERIMENT_SPAN_NAME
    assert span.attributes == {
        "model": "test-model",
        "instructions": "answer briefly",
        "dataset": "evals/seed.yaml",
        "retrieval_strategy": "search-hybrid",
    }
