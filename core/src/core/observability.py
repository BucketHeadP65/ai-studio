"""Application logging and tracing, configured in one place.

Modules ask for a logger with `get_logger` or a traced span with `span`, and never import a logging backend
directly. A log call takes a plain message template and named fields: `log.info("indexed {pages} pages", pages=12)`.
The template reads as the message with the fields filled in, and every field is also kept as its own queryable
attribute. The backend is Logfire, set up once by `configure_logging` at process start or by the first `span`. By
default logs are recorded locally, shipped nowhere, and nothing is written to the console, so stdout stays free for
a stdio protocol. A process that ships its own spans calls `start_tracing`, and `stop_tracing` on its way out. The
price table refresh behind `start_price_updates` runs through the same backend.
"""

from __future__ import annotations

import logging
import re
import sys
import urllib.error
import urllib.request

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from logfire import ConsoleOptions, LevelName

    from core.settings import LogfireSettings

type FieldValue = str | bool | int | float | Sequence[str] | Sequence[bool] | Sequence[int] | Sequence[float] | None
"""A value a log call or a span may carry. Turn anything else, an identifier or a path, into text first."""

type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
"""The lowest level that is recorded."""


class LoggingSettings(BaseModel):
    """How a process names itself in its telemetry, how much it records, and whether it prints to the console.

    Each application declares this section with its own `service_name`. `console` stays off unless asked, because
    a process speaking a stdio protocol must keep stdout to itself.
    """

    service_name: str
    """The service every log record and span reports itself as."""
    service_version: str = "0.0.0"
    """The deployed version, supplied by the deployment."""
    environment: str = "local"
    """The deployment environment, such as `local` or `production`."""
    level: LogLevel = "INFO"
    console: bool = False
    """Whether records are also printed to the console."""


class Logger(Protocol):
    """The logging surface modules use, independent of the backend behind it.

    Every method takes a message template and named fields. A `{name}` in the template is filled with that field's
    value for the message, and each field is also kept as an attribute. A field left `None` is dropped. Logging
    never raises.
    """

    def debug(self, message: str, /, **fields: FieldValue) -> None:
        """Record detailed output useful only when diagnosing a problem.

        Args:
            message: The message template.
            **fields: The values the message is about.
        """

    def info(self, message: str, /, **fields: FieldValue) -> None:
        """Record a normal, expected event.

        Args:
            message: The message template.
            **fields: The values the message is about.
        """

    def warning(self, message: str, /, *, error: BaseException | None = None, **fields: FieldValue) -> None:
        """Record something unexpected that did not stop the operation.

        Args:
            message: The message template.
            error: An exception the warning is about, recorded with its traceback.
            **fields: The values the message is about.
        """

    def error(self, message: str, /, *, error: BaseException | None = None, **fields: FieldValue) -> None:
        """Record a failure in the current operation.

        Args:
            message: The message template.
            error: The exception that failed it, recorded with its traceback.
            **fields: The values the message is about.
        """

    def exception(self, message: str, /, *, error: BaseException | None = None, **fields: FieldValue) -> None:
        """Record a failure at error level with a traceback, the active exception's when `error` is not given.

        Args:
            message: The message template.
            error: The exception to record, or `None` for the one being handled.
            **fields: The values the message is about.
        """


class Span(Protocol):
    """The slice of a traced span a caller may use: attaching facts discovered mid-operation."""

    def set_attribute(self, key: str, value: object) -> None:
        """Attach one attribute to the span. Callable any time before the span exits.

        Args:
            key: The attribute name.
            value: The attribute value.
        """


_configured = False

# The attribute Logfire reads a record's message template from, so records of one template group together.
_TEMPLATE_ATTRIBUTE = "logfire.msg_template"

# A log record's own attribute names. A field with one of these names is stored with a trailing underscore,
# because `logging` refuses an extra that would overwrite them.
_RECORD_ATTRIBUTES = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}

# Past `_FieldLogger._emit` and the public method that called it, to the caller's own line.
_CALLER = 3

_PLACEHOLDER = re.compile(r"\{(\w+)(![rs])?(:[^{}]*)?\}")
_SCALARS = (bool, str, int, float)


class _FieldLogger:
    """A `Logger` over one standard library logger."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def debug(self, message: str, /, **fields: FieldValue) -> None:
        self._emit(logging.DEBUG, message, None, fields)

    def info(self, message: str, /, **fields: FieldValue) -> None:
        self._emit(logging.INFO, message, None, fields)

    def warning(self, message: str, /, *, error: BaseException | None = None, **fields: FieldValue) -> None:
        self._emit(logging.WARNING, message, error, fields)

    def error(self, message: str, /, *, error: BaseException | None = None, **fields: FieldValue) -> None:
        self._emit(logging.ERROR, message, error, fields)

    def exception(self, message: str, /, *, error: BaseException | None = None, **fields: FieldValue) -> None:
        self._emit(logging.ERROR, message, error if error is not None else sys.exception(), fields)

    def _emit(self, level: int, template: str, error: BaseException | None, fields: Mapping[str, FieldValue]) -> None:
        if not self._logger.isEnabledFor(level):
            return
        present = {name: value for name, value in fields.items() if value is not None}
        extra: dict[str, object] = {_stored_name(name): _carried(value) for name, value in present.items()}
        extra[_TEMPLATE_ATTRIBUTE] = template
        self._logger.log(level, _rendered(template, present), exc_info=error, extra=extra, stacklevel=_CALLER)


def _stored_name(name: str) -> str:
    """The attribute name a field is kept under, clear of the log record's own attributes."""
    return f"{name}_" if name in _RECORD_ATTRIBUTES else name


def _carried(value: FieldValue) -> object:
    """The value as an attribute: a scalar or a sequence of one scalar type as it is, anything else as text."""
    if isinstance(value, _SCALARS):
        return value
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        kinds = {next((kind for kind in _SCALARS if isinstance(item, kind)), None) for item in value}
        if len(kinds) <= 1 and None not in kinds:
            return tuple(value)
    return str(value)


def _rendered(template: str, fields: Mapping[str, FieldValue]) -> str:
    """The template with each `{name}`, `{name!r}` or `{name:spec}` it names filled in. Any other text stays as written."""

    def fill(match: re.Match[str]) -> str:
        name, conversion, spec = match.group(1), match.group(2), match.group(3)
        if name not in fields:
            return match.group(0)
        value = fields[name]
        if conversion == "!r":
            return repr(value)
        if spec:
            try:
                return format(value, spec[1:])
            except (TypeError, ValueError):
                return str(value)
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            return ", ".join(str(item) for item in value)
        return str(value)

    return _PLACEHOLDER.sub(fill, template)


def configure_logging(settings: LoggingSettings | None = None) -> None:
    """Route logging through Logfire. Call once at process start, later calls do nothing.

    It is safe to call from more than one entry point. A host that already configured Logfire keeps its
    configuration. Only a process nobody configured gets the quiet local one: records kept in process, shipped
    nowhere, printed only when `settings.console` asks.

    Args:
        settings: The process's logging settings. `None` records at `INFO` under no service name and prints nothing.
    """
    global _configured  # noqa: PLW0603 (a configured-once flag is genuine process state)
    if _configured:
        return
    import logfire  # noqa: PLC0415 (the one place that names the backend, loaded only when logging is set up)

    from opentelemetry import trace  # noqa: PLC0415 (loaded with logfire, only when logging is set up)

    if isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
        if settings is None:
            logfire.configure(send_to_logfire=False, console=False, inspect_arguments=False)
        else:
            logfire.configure(
                send_to_logfire=False,
                inspect_arguments=False,
                console=_console(settings),
                service_name=settings.service_name,
                service_version=settings.service_version,
                environment=settings.environment,
            )
    root = logging.getLogger()
    root.setLevel(settings.level if settings is not None else "INFO")
    root.addHandler(logfire.LogfireLoggingHandler())
    _configured = True


def get_logger(name: str) -> Logger:
    """The logger for a module, usually called with `__name__`.

    Args:
        name: The logger name.

    Returns:
        The logger.
    """
    return _FieldLogger(logging.getLogger(name))


def span(name: str, **fields: FieldValue) -> AbstractContextManager[Span]:
    """A traced span for one logical operation, through the same Logfire backend as `get_logger`.

    It calls `configure_logging` first, so a span opened in a process that never configured logging still records.
    `name` may carry `{field}` placeholders, filled from `fields` the way a log message is.

    Args:
        name: The span name, a template.
        **fields: The attributes the span starts with. A field left `None` is dropped.

    Returns:
        The span, to use as a context manager.
    """
    configure_logging()
    import logfire  # noqa: PLC0415 (the one place that names the backend, loaded only when logging is set up)

    present = {field: value for field, value in fields.items() if value is not None}
    message = _rendered(name, present).replace("{", "{{").replace("}", "}}")
    active = logfire.span(message, _span_name=name)
    active.set_attributes({field: _carried(value) for field, value in present.items()})
    return active


def baggage(**values: str) -> AbstractContextManager[None]:
    """Attach `values` to every span and record opened while the returned context is entered.

    Args:
        **values: The names and values to attach, such as a run id.

    Returns:
        The context manager that holds them.
    """
    import logfire  # noqa: PLC0415 (the one place that names the backend, loaded only when baggage is set)

    return logfire.set_baggage(**values)


class Tracing(Protocol):
    """How a process starts shipping its own spans.

    A test that composes an application hands it one that ships nothing.
    """

    def start(self) -> None:
        """Begin shipping this process's spans."""
        ...


def start_tracing(telemetry: LogfireSettings, settings: LoggingSettings) -> None:
    """Ship this process's spans to Logfire under `settings.service_name`, and say whether managed prompts are reachable.

    It replaces the quiet local configuration `configure_logging` makes, so a later `configure_logging` call keeps
    this one. Call it from a composition root, never at import.

    Args:
        telemetry: Where the spans go, and the write token they go with.
        settings: The service identity, level and console choice the spans carry.
    """
    import logfire  # noqa: PLC0415 (the one place that names the backend, loaded only when tracing starts)

    logfire.configure(
        token=telemetry.write_token,
        advanced=logfire.AdvancedOptions(base_url=telemetry.base_url),
        inspect_arguments=False,
        console=_console(settings),
        service_name=settings.service_name,
        service_version=settings.service_version,
        environment=settings.environment,
    )
    logging.getLogger().setLevel(settings.level)
    logfire.instrument_pydantic_ai()
    check_managed_prompts(telemetry.base_url, telemetry.write_token)


def stop_tracing() -> None:
    """Flush every span and record still buffered, then close the exporters. Call it once on the way out.

    A process that exits normally flushes on its own. A server stopped from its lifespan calls this so a signal
    loses nothing. Log records stop going to Logfire first, so a record logged afterwards reaches only the root
    logger's other handlers. It never raises.
    """
    import logfire  # noqa: PLC0415 (the one place that names the backend, loaded only when tracing stops)

    # A closed span processor logs a warning for every span it is handed, and Logfire's handler would turn that
    # warning into another span for the same processor, without end.
    root = logging.getLogger()
    for handler in [handler for handler in root.handlers if isinstance(handler, logfire.LogfireLoggingHandler)]:
        root.removeHandler(handler)
    try:
        logfire.shutdown()
    except Exception as exc:  # noqa: BLE001 (shutting telemetry down must never fail the process that is stopping)
        _logger.debug("telemetry did not shut down cleanly ({error})", error=type(exc).__name__)


_CONSOLE_LEVELS: dict[LogLevel, LevelName] = {"DEBUG": "debug", "INFO": "info", "WARNING": "warning", "ERROR": "error"}


def _console(settings: LoggingSettings) -> ConsoleOptions | Literal[False]:
    """Logfire's console option for `settings`: printed at the configured level, or off."""
    import logfire  # noqa: PLC0415 (the one place that names the backend, loaded only when logging is set up)

    return logfire.ConsoleOptions(min_log_level=_CONSOLE_LEVELS[settings.level]) if settings.console else False


_logger = get_logger(__name__)


def check_managed_prompts(base_url: str, token: str) -> None:
    """Log once whether the managed prompts at `base_url` can be read with `token`.

    It makes one request and never raises. A refusal or a network failure is logged as a warning.

    Args:
        base_url: The Logfire endpoint.
        token: The token the managed prompts are read with.
    """
    url = f"{base_url.rstrip('/')}/v1/variables/"
    if not url.startswith(("http://", "https://")):
        _logger.warning("managed prompts unreachable: {url} is not an HTTP endpoint", url=url)
        return
    request = urllib.request.Request(url, headers={"Authorization": f"bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            _logger.info("managed prompts reachable (HTTP {status})", status=response.status)
    except urllib.error.HTTPError as exc:
        _logger.warning(
            "managed prompts NOT reachable (HTTP {status}: {reason}). The agent is running on the "
            "in-repo instructions, and edits made in Logfire will have no effect until this token "
            "can read /v1/variables/",
            status=exc.code,
            reason=exc.reason,
        )
    except OSError as exc:  # offline, DNS or timeout, and the default still serves
        _logger.warning("managed prompts unreachable ({reason}), using the in-repo instructions", reason=str(exc)[:120])


class PriceUpdater(Protocol):
    """The slice of genai-prices' updater a process drives: start polling, stop polling."""

    def start(self) -> None:
        """Begin refreshing in the background."""
        ...

    def stop(self) -> None:
        """Stop refreshing."""
        ...


def start_price_updates(updater: PriceUpdater) -> Callable[[], None]:
    """Refresh the price table through `updater`, and hand back the stop.

    The returned stop never raises. An error the updater kept is logged as a warning.

    Args:
        updater: The price table updater to start.

    Returns:
        The function that stops the refresh.
    """
    updater.start()

    def stop() -> None:
        try:
            updater.stop()
        except Exception as exc:  # noqa: BLE001 (the updater kept its last snapshot, so this is a warning, not a failure)
            _logger.warning("the price refresh stopped with an error it had kept ({kind})", kind=type(exc).__name__)

    return stop
