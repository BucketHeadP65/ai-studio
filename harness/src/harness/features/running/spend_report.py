"""What something cost, asked of the telemetry that already holds every priced model call.

Six named questions, one query each, nothing stored. A scope the module does not offer is a change to
`Scope`, never a query a caller composes.
"""

from __future__ import annotations

import re

from decimal import Decimal
from typing import TYPE_CHECKING, Literal, get_args

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from harness.adapters.contracts import TelemetryUnreachable

if TYPE_CHECKING:
    from harness.adapters.contracts import Cell, Telemetry

Scope = Literal["run", "workflow", "session", "step", "model", "day"]
"""The questions a report answers. `run`, `workflow`, `session` and `step` are about one thing, named by `subject`."""

SCOPES: frozenset[str] = frozenset(get_args(Scope))
SCOPES_WITH_SUBJECT: frozenset[str] = frozenset({"run", "workflow", "session", "step"})
DEFAULT_SINCE_HOURS = 24
MAX_SINCE_HOURS = 24 * 90
SPEND_LOG_FALLBACK = "The spend log on disk still answers per run, GET /api/runs/history/{run_id} carries its spend."
"""What an unavailable answer says about where a narrower answer still lives."""

_SUBJECT = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
"""What a subject may look like before it is written into a query: run ids, workflow ids such as `wf.digest.v9`, session ids."""

_SCOPE = TypeAdapter(Scope)

_AGGREGATE = """count(*) as calls,
  coalesce(sum((attributes->>'operation.cost')::float), 0) as cost_usd,
  coalesce(sum((attributes->>'gen_ai.usage.input_tokens')::bigint), 0) as input_tokens,
  coalesce(sum((attributes->>'gen_ai.usage.output_tokens')::bigint), 0) as output_tokens,
  coalesce(sum((attributes->>'gen_ai.usage.cache_read.input_tokens')::bigint), 0) as cache_read_tokens,
  coalesce(sum((attributes->>'gen_ai.usage.cache_creation.input_tokens')::bigint), 0) as cache_write_tokens"""

_KEYS: dict[Scope, str] = {
    "run": "attributes->>'run_id'",
    "workflow": "attributes->>'run_id'",
    "session": "coalesce(attributes->>'run_id', 'chat')",
    "step": "coalesce(attributes->>'step', 'unknown')",
    "model": "concat(attributes->>'gen_ai.provider.name', '/', attributes->>'gen_ai.response.model')",
    "day": "cast(day as varchar)",
}


class SpendLine(BaseModel):
    """One row of a report: what `key` cost over the window, and the calls and tokens behind it."""

    model_config = ConfigDict(frozen=True)

    key: str
    calls: int
    cost_usd: Decimal
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


class SpendReport(BaseModel):
    """A scope's answer: its lines, their total, and what was asked."""

    model_config = ConfigDict(frozen=True)

    scope: Scope
    subject: str
    since_hours: int
    lines: tuple[SpendLine, ...]
    total: SpendLine
    source: Literal["telemetry"] = "telemetry"


class SpendUnavailable(BaseModel):
    """The telemetry could not answer, said as such, with where a narrower answer still lives."""

    model_config = ConfigDict(frozen=True)

    scope: Scope
    subject: str
    since_hours: int
    reason: str
    fallback: str = SPEND_LOG_FALLBACK


def parse_scope(value: str) -> Scope:
    """`value` as a `Scope`.

    Args:
        value: The scope's name.

    Returns:
        The scope.

    Raises:
        ValueError: Not one of the six, with the six named.
    """
    try:
        return _SCOPE.validate_python(value)
    except ValidationError as error:
        msg = f"No scope {value!r}, the scopes are {', '.join(sorted(SCOPES))}."
        raise ValueError(msg) from error


def _narrowing(scope: Scope, subject: str) -> str:
    if scope in {"run", "step"}:
        return f"and attributes->>'run_id' = '{subject}'"
    if scope == "workflow":
        return f"and attributes->>'workflow' = '{subject}'"
    if scope == "session":
        return (
            "and trace_id in (select trace_id from records where parent_span_id is null "
            f"and attributes->>'http.target' like '/api/sessions/{subject}/%')"
        )
    return ""


def sql_for(scope: Scope, subject: str, since_hours: int) -> str:
    """The one query `scope` asks: every priced model span in the window, narrowed to `subject`, grouped by the scope's key.

    Args:
        scope: The question.
        subject: The id a scope about one thing is narrowed to. The other scopes ignore it.
        since_hours: The window, in hours back from now.

    Returns:
        The SQL text.

    Raises:
        ValueError: A scope that is about one thing got no usable `subject`, or a window outside 1 to 2160 hours.
    """
    if scope in SCOPES_WITH_SUBJECT and not _SUBJECT.fullmatch(subject):
        msg = f"{scope} needs an id of letters, digits, dots, dashes, colons or underscores, up to 80 long, not {subject!r}"
        raise ValueError(msg)
    if scope == "session" and "_" in subject:
        msg = f"session needs an id of letters, digits, dots, dashes or colons, up to 80 long, not {subject!r}"
        raise ValueError(msg)
    if not 1 <= since_hours <= MAX_SINCE_HOURS:
        msg = f"since_hours must be between 1 and {MAX_SINCE_HOURS}, not {since_hours}"
        raise ValueError(msg)
    order = "key" if scope == "day" else "cost_usd desc"
    return (
        "with calls as (\n"
        "  select start_timestamp, day, trace_id, attributes from records\n"
        f"  where start_timestamp >= now() - interval '{since_hours} hours' and attributes ? 'operation.cost'"
        f" {_narrowing(scope, subject)}\n"
        ")\n"
        f"select {_KEYS[scope]} as key, {_AGGREGATE}\n"
        f"from calls group by 1 order by {order}"
    )


def _count(cell: Cell) -> int:
    return int(cell) if isinstance(cell, int | float) and not isinstance(cell, bool) else 0


def _money(cell: Cell) -> Decimal:
    return Decimal(str(cell)) if isinstance(cell, int | float) and not isinstance(cell, bool) else Decimal(0)


def _line(row: dict[str, Cell]) -> SpendLine:
    key = row.get("key")
    return SpendLine(
        key=str(key) if key is not None else "unknown",
        calls=_count(row.get("calls")),
        cost_usd=_money(row.get("cost_usd")),
        input_tokens=_count(row.get("input_tokens")),
        output_tokens=_count(row.get("output_tokens")),
        cache_read_tokens=_count(row.get("cache_read_tokens")),
        cache_write_tokens=_count(row.get("cache_write_tokens")),
    )


def _total(lines: tuple[SpendLine, ...]) -> SpendLine:
    return SpendLine(
        key="total",
        calls=sum(line.calls for line in lines),
        cost_usd=sum((line.cost_usd for line in lines), Decimal(0)),
        input_tokens=sum(line.input_tokens for line in lines),
        output_tokens=sum(line.output_tokens for line in lines),
        cache_read_tokens=sum(line.cache_read_tokens for line in lines),
        cache_write_tokens=sum(line.cache_write_tokens for line in lines),
    )


class SpendReporter:
    """Answers the six scopes from the telemetry, and says so when it cannot."""

    def __init__(self, telemetry: Telemetry) -> None:
        """A reporter that asks `telemetry`.

        Args:
            telemetry: The telemetry every report is asked of.
        """
        self._telemetry = telemetry

    async def report(self, scope: Scope, subject: str = "", since_hours: int = DEFAULT_SINCE_HOURS) -> SpendReport | SpendUnavailable:
        """What `scope` (about `subject`, where the scope is about one thing) cost over the last `since_hours`.

        Args:
            scope: The question.
            subject: The id a scope about one thing is narrowed to. The other scopes ignore it.
            since_hours: The window, in hours back from now.

        Returns:
            The report, or the unavailable answer when the telemetry could not be reached.

        Raises:
            ValueError: The subject or the window cannot be asked, see `sql_for`.
        """
        sql = sql_for(scope, subject, since_hours)
        try:
            result = await self._telemetry.query(sql)
        except TelemetryUnreachable as error:
            return SpendUnavailable(scope=scope, subject=subject, since_hours=since_hours, reason=str(error))
        lines = tuple(_line(row) for row in result.rows())
        return SpendReport(scope=scope, subject=subject, since_hours=since_hours, lines=lines, total=_total(lines))

