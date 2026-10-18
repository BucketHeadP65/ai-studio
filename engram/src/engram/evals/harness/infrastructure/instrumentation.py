"""Configure the process-wide Logfire instance so evaluation-run spans reach Logfire.

`configure_logfire` configures Logfire once per process, and only when a write token is present.
Without a token it does nothing, so no command fails for want of one.
`enable_local_span_recording` records spans locally instead, so the evaluators still read a span tree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import logfire

from core.settings import LogfireSettings
from logfire import AdvancedOptions

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from core.resolver import Resolver

type AdvancedOptionsT = AdvancedOptions
"""The Logfire advanced options, which carry the region's ingest `base_url`."""

_SERVICE_NAME = "engram-evals"
"""The OpenTelemetry `service.name` every engram evaluation-run span is tagged with in Logfire."""

_configured = False
"""Whether `configure_logfire` has already configured the process-wide Logfire instance."""


class _ConfigureFn(Protocol):
    """The part of `logfire.configure` this module calls."""

    def __call__(self, *, token: str, send_to_logfire: bool, service_name: str, advanced: AdvancedOptionsT) -> object:
        """Configure the process-wide Logfire instance."""
        ...


def reset_logfire_for_testing() -> None:
    """Forget that Logfire was configured, so a test starts with it unconfigured.

    Only tests call this.
    """
    global _configured  # noqa: PLW0603 (resetting the module guard is this helper's whole job)
    _configured = False


def configure_logfire(
    logfire_settings: LogfireSettings,
    *,
    configure: _ConfigureFn = logfire.configure,
    instrument_pydantic_ai: Callable[[], object] = logfire.instrument_pydantic_ai,
) -> bool:
    """Configure Logfire for evaluation runs when a write token is present, and otherwise do nothing.

    The first call with a non-empty `write_token` configures the process-wide Logfire instance for the
    `engram-evals` service and instruments pydantic-ai. Later calls return at once without configuring again.

    Args:
        logfire_settings: The Logfire settings. Its `write_token` decides whether anything happens and
            its `base_url` picks the region's ingest endpoint.
        configure: The Logfire configure call, `logfire.configure` unless a test replaces it.
        instrument_pydantic_ai: The pydantic-ai instrumentation call, `logfire.instrument_pydantic_ai`
            unless a test replaces it.

    Returns:
        `True` when Logfire is configured, by this call or an earlier one. `False` when the token was
        empty and nothing happened.
    """
    global _configured  # noqa: PLW0603 (the configured-once guard is intentionally process-wide)

    if _configured:
        return True

    if not logfire_settings.write_token:
        return False

    configure(
        token=logfire_settings.write_token,
        send_to_logfire=True,
        service_name=_SERVICE_NAME,
        advanced=AdvancedOptions(base_url=logfire_settings.base_url),
    )
    instrument_pydantic_ai()
    _configured = True
    return True


def enable_local_span_recording() -> None:
    """Record spans locally, with no token and no network, so the evaluators can read the span tree.

    Call it when there is no write token, because pydantic-evals records a span tree only once Logfire is configured.
    """
    logfire.configure(send_to_logfire=False, console=False)
    logfire.instrument_pydantic_ai()


class _SpanFn(Protocol):
    """The part of `logfire.span` this module calls."""

    def __call__(
        self, template: str, /, *, model: str, instructions: str, dataset: str, retrieval_strategy: str
    ) -> AbstractContextManager[object]:
        """Open the span `template` names, tagged with the variant's axes."""
        ...


EXPERIMENT_SPAN_NAME = "eval_experiment"
"""The span every experiment variant's run is tagged under."""


def experiment_span(
    *, model: str, instructions: str, dataset: str, retrieval_strategy: str, span: _SpanFn = logfire.span
) -> AbstractContextManager[object]:
    """Open a Logfire span tagged with every axis of one experiment variant.

    The retrieval strategy is tagged too, so two runs that differ only in strategy never read as the same variant.

    Args:
        model: The variant's model.
        instructions: The variant's instructions.
        dataset: The variant's dataset path.
        retrieval_strategy: The retrieval surface the variant exposes.
        span: The span opener, `logfire.span` unless a test replaces it.

    Returns:
        The open span, to be used as a context manager around the variant's run.
    """
    return span(
        EXPERIMENT_SPAN_NAME, model=model, instructions=instructions, dataset=dataset, retrieval_strategy=retrieval_strategy
    )


async def resolve_logfire_settings(container: Resolver) -> LogfireSettings:
    """Resolve the `LogfireSettings` registered on the container.

    Settings with an empty token make `configure_logfire` do nothing.

    Args:
        container: The resolver the Logfire settings are resolved from.

    Returns:
        The registered `LogfireSettings`.
    """
    return await container.get(LogfireSettings)
