"""The root-cause-analysis report: its sections, its Markdown rendering and the instruction text for the model.

`RcaReport` holds one `RcaSection` per saved query. `render_rca_report_markdown` renders a report as
Markdown. `build_rca_prompt` returns the brief a model runs over the Logfire MCP to explain which tool
calls dominate latency and which cases have `recall@5 == 0`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Sequence

# A JSON cell value as the Logfire Query API returns it in a row. It must stay a `type` alias so
# pydantic builds a named recursive schema. `QueryRow` is one record.
type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
type QueryRow = dict[str, JsonValue]

_DEFAULT_RCA_QUERY_NAMES: tuple[str, ...] = ("slowest_tool_spans", "lowest_recall_cases", "token_outliers")
"""The RCA queries the report runs by default, the latency / recall / token-outlier triad.

A name absent from the loaded file becomes an annotated empty section. These are the names
`build_rca_prompt` points the model at when given none.
"""


class RcaSection(BaseModel):
    """One named RCA query's result within a `RcaReport`.

    Attributes:
        name: The saved-query name this section reports (e.g. `"slowest_tool_spans"`).
        sql: The SQL that was run, or `None` when the name was absent from the loaded file.
        rows: The validated Query-API rows (empty when the query was absent or returned nothing).
        error: A human-readable reason the section is empty (a missing name, or a query failure),
            or `None` when the query ran and returned rows normally.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    sql: str | None
    rows: list[QueryRow]
    error: str | None = None


class RcaReport(BaseModel):
    """A structured root-cause-analysis report: one `RcaSection` per requested query.

    `render_rca_report_markdown` turns it into Markdown for a report file or a prompt.

    Attributes:
        sections: The query results, in the order the queries were requested.
    """

    model_config = ConfigDict(frozen=True)

    sections: list[RcaSection]


def render_rca_report_markdown(report: RcaReport) -> str:
    """Render an `RcaReport` as Markdown, one section per query, with its rows.

    Each section becomes a `## <name>` heading carrying the row count, then either the rows (as a
    fenced JSON-ish line per row) or the section's `error` note. Suitable for an on-disk RCA
    report or for grounding the model's free-text analysis.

    Args:
        report: The report to render.

    Returns:
        A Markdown string. Always non-empty given at least one section, lists sections in order.
    """
    lines: list[str] = ["# RCA report", ""]
    for section in report.sections:
        lines.append(f"## {section.name} ({len(section.rows)} rows)")
        if section.error is not None:
            lines.append(f"_{section.error}_")
        elif not section.rows:
            lines.append("_no rows_")
        else:
            lines.extend(f"- {row}" for row in section.rows)
        lines.append("")
    return "\n".join(lines)


def build_rca_prompt(*, focus: str | None = None, query_names: Sequence[str] | None = None) -> str:
    """Build the RCA instruction text for the powerful model on the Logfire-MCP path.

    A pure function: the same inputs give identical text. The brief asks a model to query the
    evaluation-run traces over the Logfire MCP and answer two questions, which tool calls dominate
    latency, and which cases have `recall@5 == 0` and why, grounding every claim in concrete spans.

    Args:
        focus: An optional scope to anchor the analysis on (e.g. an experiment or run name). Woven
            into the brief when given.
        query_names: Optional saved-query names to point the model at as a starting set (e.g. those
            from `load_named_queries`), defaults to
            (`slowest_tool_spans` / `lowest_recall_cases` / `token_outliers`).

    Returns:
        The instruction string for the model. Deterministic for a given `focus` / `query_names`.
    """
    names = list(query_names) if query_names is not None else list(_DEFAULT_RCA_QUERY_NAMES)
    scope = f" Focus the analysis on {focus}." if focus else ""
    names_line = ", ".join(names)

    return (
        "You are a root-cause-analysis assistant for an LLM retrieval eval suite (engram). The deep "
        "store for evaluation-run telemetry is Pydantic Logfire: every model request, tool call, token "
        "count, and latency for each case is recorded as an OpenTelemetry span tree. Use the Logfire "
        "MCP tools to query those traces directly." + scope + "\n\n"
        "Answer two questions, and only from evidence in the traces:\n"
        "1. Latency: which tool calls dominate end-to-end latency? Rank tool spans by their "
        "contribution (p95 duration x call count), and for the worst offenders identify which cases "
        "and queries triggered them.\n"
        "2. Recall failures: which cases have recall@5 == 0 (the agent cited none of the expected "
        "source ids), and why? For each, inspect that case's span tree (the search/fetch tool "
        "inputs and outputs) and explain whether the retrieval missed the relevant pages, the agent "
        "ignored retrieved pages, or the query was malformed.\n\n"
        f"Useful saved queries to start from: {names_line}. Adapt them as needed.\n\n"
        "Ground every claim in a specific span, trace id, or case name: cite the evidence. If the "
        "traces do not support a conclusion, say so explicitly rather than guessing. Finish with a "
        "ranked, actionable list of the most likely root causes."
    )
