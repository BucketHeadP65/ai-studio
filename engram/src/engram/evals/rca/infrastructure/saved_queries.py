"""Read a saved-SQL file into named queries.

The file format::

    -- name: slowest_tool_spans
    SELECT span_name, ... FROM records WHERE ... ORDER BY duration DESC LIMIT 20

    -- name: lowest_recall_cases
    SELECT case_name, ... FROM records WHERE recall_at_5 = 0

A `-- name: <key>` comment line opens a block. Every line after it, up to the next `-- name:` or
end-of-file, is that query's SQL. Surrounding whitespace and blank lines are stripped. Comment and
blank lines before the first `-- name:` (a file header) are ignored. A `-- name:` marker with no
key, and a named block whose body is empty, are both skipped, and neither yields an entry.
"""

from __future__ import annotations

from pathlib import Path

_NAME_MARKER = "-- name:"
"""The line prefix that opens a named-query block in a saved-SQL file."""


def load_named_queries(source: str | Path) -> dict[str, str]:
    """Parse a saved-SQL file into `{name: sql}`, in the format this module describes.

    Args:
        source: Either the SQL text itself, or a `Path` to read it from (UTF-8).

    Returns:
        A mapping of query name to its SQL, in the order the blocks appear in the file. Empty when
        the file has no named blocks.
    """
    text = source.read_text(encoding="utf-8") if isinstance(source, Path) else source

    queries: dict[str, str] = {}
    current_name: str | None = None
    body: list[str] = []

    def _flush() -> None:
        if current_name is None:
            return
        sql = "\n".join(body).strip()
        if sql:  # a named block with no SQL body is not a usable query, skip it
            queries[current_name] = sql

    for line in text.splitlines():
        if line.lstrip().startswith(_NAME_MARKER):
            _flush()
            key = line.lstrip().removeprefix(_NAME_MARKER).strip()
            current_name = key or None  # a `-- name:` with no key opens nothing
            body = []
        elif current_name is not None:
            body.append(line)

    _flush()
    return queries
