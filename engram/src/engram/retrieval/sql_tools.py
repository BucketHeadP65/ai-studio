r"""The SQL tools, `describe_schema` and `run_sql`, with their descriptions and usage instructions.

Every server and capability that offers the SQL tools builds them from the factories here.
`run_sql` checks the statement with the guard, then sends it through the `kb_reader` role inside
`SELECT * FROM (<statement>) AS q LIMIT n`, so the database refuses anything that is not a query and
stops at the row cap.

* `make_run_sql` returns `{"rows": [...], "note": ...}` kept within `SQL_RESULT_CHARS`, and raises
  `ToolError` for a statement the guard refused or that did not run, or `ModelRetry` when asked to retry.
* `make_sandbox_run_sql` returns the same bounded rows as a bare `list[dict]` for code mode, and raises
  `ModelRetry` for a statement the guard refused or that did not run.
* `make_describe_schema` answers a compact catalog with no `objects` named, or full column detail for up
  to eight named objects, and raises `ToolError` for a refused request, or `ModelRetry` when asked to retry.

No string in this module names what a knowledge base is about. The caller says that.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING, cast

from core.ai.embedding import query_model_of
from core.ai.retrieval.contracts import RefusedStatementError, VectorQueryRewrite, single_statement
from core.connection_failures import is_database_connection_failure
from core.observability import get_logger
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import JsonValue
from pydantic_ai.exceptions import ModelRetry
from pydantic_core import to_jsonable_python

from .budget import clip_text, fits_budget
from .domain import DescribeSchema
from .exceptions import DescribeSchemaRefusedError, QueryNotEmbeddedError
from .infrastructure.gateway import STATEMENT_ERRORS
from .query_embedding import EMBED_DEADLINE_SECONDS, NO_EMBEDDER_REASON, embed_within, keyword_only_note

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from core.ai.embedding import QueryEmbedder

    from .domain import Row, SqlGateway, SqlResult
    from .queries import DescribeSchemaHandler

    RunSqlTool = Callable[[str, int], Awaitable[dict[str, JsonValue]]]
    """The top-level `run_sql` tool callable, `(sql, row_limit) -> {"rows": [...], "note": ...}`."""

    SandboxRunSqlTool = Callable[[str, int], Awaitable[list[dict[str, JsonValue]]]]
    """The code-mode sandbox `run_sql` callable, `(sql, row_limit) -> list[dict]`, one dict per row."""

    DescribeSchemaTool = Callable[[list[str] | None], Awaitable[str]]
    """The `describe_schema` tool callable, `(objects=None) -> str`.

    It answers a compact catalog with no `objects` named, or full detail for the named ones.
    """

_logger = get_logger(__name__)

# The hard row cap: run_sql never returns more than this, whatever the agent asks for.
MAX_ROWS = 50

# The describe_schema description. It says how to query, never what the knowledge base is about, and it
# must not steer the agent to read api.about's body as orientation.
DESCRIBE_SCHEMA_DESCRIPTION = (
    "Render the read-only `api` surface's schema: every view/function's name, kind and one-line "
    "purpose. Start with no arguments, then request named objects (`objects=['api.search']`, up to "
    "eight at a time) for their COMMENT, columns and types (units + example values live in the "
    "comments). Call this FIRST to learn what you can query and how to enter the knowledge base. "
    "This surface describes HOW to query the knowledge base, not what it is about. Use it to plan "
    "your queries, then answer the question from the units you actually retrieve, not from this "
    "schema text. A rough cheapest-first order for most questions: scan the compact cards "
    "(`api.cards`), run hybrid search (`api.search`), read the canonical sections behind a hit "
    "(`api.sections`), bring in typed graph relations when the question needs them (`api.edges`), "
    "and check a specific claim against its raw source span "
    "when it matters (`api.claims`, `api.raw_chunks`)."
)

# The run_sql description, including the `vector_query` argument and what the guard refuses.
RUN_SQL_DESCRIPTION = (
    "Execute one read-only SQL statement against the `api` surface and return "
    '`{"rows": [{column: value}, ...], "note": ...}` (capped at 50 rows and at 2,000 characters per '
    "text cell, and `note` says when rows or cells were shortened. Narrow your filters or select fewer "
    "columns). Call `describe_schema()` first for the object catalog, then `describe_schema(objects=[...])` "
    "for a named object's exact columns, and query only those. Never guess names. For semantic / "
    "hybrid search, call `api.search` and pass `vector_query => '<text>'`: the server embeds that "
    "text for you and runs hybrid (BM25 + vector). The text may be a richer or longer description "
    "of intent than the lexical `query` argument, one plain quoted text of at most 2,000 characters, at most "
    "four per statement. Omit `vector_query` for BM25-only keyword search. "
    "Example: `SELECT * FROM api.search('<keywords>', vector_query => '<a fuller description of what "
    "you are looking for>', k => 8)`. Ground facet values with "
    "`api.lookup_value` before filtering, and fetch full text by id with `api.fetch`. The connection "
    "is read-only. Send one query: WITH RECURSIVE and functions that change the session, take locks or "
    "sleep are refused, so walk links with `api.graph_expand` or `api.graph_paths`."
)

# The description of the code mode run_sql, which returns one dict per row.
SANDBOX_RUN_SQL_DESCRIPTION = (
    "Execute one read-only SQL statement against the `api` surface and return the rows as a list of "
    "dicts (one per row, column => value, capped at 50 rows and at 2,000 characters per text cell). "
    "For semantic / hybrid search, call "
    "`api.search` and pass `vector_query => '<text>'`: the server embeds that text and runs hybrid "
    "(BM25 + vector). Omit it for BM25-only. Call it as "
    "`rows = await run_sql(sql=<your SELECT over api.*>)`, then read a cell by column, e.g. "
    "`rows[0]['id']`. Ground facet values with `api.lookup_value` before filtering. The connection "
    "is read-only (`kb_reader` role), and a rejected statement raises. Send one query: WITH RECURSIVE "
    "and functions that change the session, take locks or sleep are refused, so walk links with "
    "`api.graph_expand` or `api.graph_paths`."
)

# The hint run_sql adds when the read-only role rejects a statement.
READ_ONLY_HINT = "the knowledge base connection is read-only. Query the api.* views and functions."

# The guidance an agent reads on how to use the SQL tools. The slots `scope`, `pairing`,
# `statement_tool`, `schema_tool` and `knowledge_base` take the caller's tool names or a phrase that
# stands for them. Every sentence must hold for any knowledge base, so no subject word goes in.
TOOLING_INSTRUCTIONS_TEMPLATE = (
    "# Read-only SQL over {scope}{pairing}\n"
    "{statement_tool} sends ONE read-only statement to that knowledge base's `api` surface, which exposes hybrid "
    "search, full text by id and typed graph walks as `api.*` callables, so a single statement can "
    "search, join and count where four tool calls could not. Call {schema_tool} with no arguments "
    "for the object catalog, then with the ones you need named for their exact columns, before "
    "writing SQL. Never guess a name. Searching means "
    "`api.search`, never a hand-rolled `ILIKE`/regex scan over titles or bodies. Substring matching "
    "finds only what you already knew to name, and padding the pattern with names from your own "
    "training imports the exact parametric bias the probe exists to test: search with the question's "
    "own words, and widen only with terms the knowledge base itself returned. For semantic "
    "search pass `vector_query => '<text>'` to `api.search` and the server embeds it for you, and omit it "
    "for keyword-only. Results are capped at 50 rows: when the note says rows were truncated, narrow "
    "the filters rather than asking again. The connection is read-only, so a statement that writes "
    "comes back as a failure with the reason.\n"
    "- This reaches that knowledge base and nothing else. Anything else you can do here answers through its "
    "own tools, not through SQL.\n"
    "- You do not know what {knowledge_base} holds until you look. Before answering a substantive question "
    "from your own general knowledge, spend one probe on the question's key terms and let the hits "
    "decide: empty hits earn you the general answer, and real hits mean the answer is BUILT from them. "
    "Lead with what the knowledge base says, put the id beside each claim you take from it ('a page says "
    "X' without that page's id is an unsourced claim wearing a sourced one's clothes), and say plainly "
    "where its evidence is thin or contested. A probe whose hits changed nothing in your answer was "
    "theater, not retrieval: if the hits truly are beside the point, say so and why, id by id. The "
    "one thing you may not do is probe, receive evidence, and then write the answer you already had. "
    "General knowledge is the fallback and must be labeled as such, because an unsourced answer dressed as a "
    "sourced one is worse than none."
)

def tooling_instructions(run_sql: str, describe_schema: str) -> str:
    """Render the SQL tools guidance for one knowledge base, naming the tools as the caller registered them.

    Args:
        run_sql: The name the one-statement read tool is registered under.
        describe_schema: The name the M-Schema tool is registered under.

    Returns:
        The rendered guidance, which says how to query and never what the knowledge base is about.
    """
    return TOOLING_INSTRUCTIONS_TEMPLATE.format(
        scope=f"one knowledge base (`{run_sql}` / `{describe_schema}`)",
        pairing="",
        statement_tool=f"`{run_sql}`",
        schema_tool=f"`{describe_schema}`",
        knowledge_base="this knowledge base",
    )




def make_describe_schema(describe_handler: DescribeSchemaHandler, *, retry_refusals: bool = False) -> DescribeSchemaTool:
    """Build the `describe_schema` tool callable over `describe_handler`.

    With no `objects` (or an empty list) the tool answers the compact catalog. Naming objects answers
    their full detail. Too many named objects, or an unknown one, raises `ToolError` from the tool, or
    `ModelRetry` with the same text when `retry_refusals` is set.

    Args:
        describe_handler: The handler that renders the schema for a `DescribeSchema` request.
        retry_refusals: Whether a refused request goes back to the model as a retry rather than failing the call.

    Returns:
        The `describe_schema` tool callable.
    """

    async def describe_schema(objects: list[str] | None = None) -> str:
        try:
            return await describe_handler(DescribeSchema(objects=tuple(objects or ())))
        except DescribeSchemaRefusedError as exc:
            raise _refusal(str(exc), retry=retry_refusals) from exc

    return describe_schema


def _refusal(text: str, *, retry: bool) -> ToolError | ModelRetry:
    """The error a SQL tool raises for a refused request: a retry for the model, or a failed call."""
    return ModelRetry(text) if retry else ToolError(text)


SQL_CELL_CHARS = 2_000
"""The longest a string inside one `run_sql` row cell may run before it is shortened."""

SQL_RESULT_CHARS = 16_000
"""The longest complete `run_sql` envelope (`{"rows": [...], "note": ...}`), in characters, `note` included."""

_SQL_CELLS_SHORTENED_NOTE = "Text cells were shortened to fit this answer's size budget."
"""Folded into `note` whenever any string in a cell ran past `SQL_CELL_CHARS`."""

_SQL_ROWS_OMITTED_NOTE = "More rows were omitted. Select fewer columns or narrow filters."
"""Folded into `note` whenever the row cap of `SQL_RESULT_CHARS` left rows out."""

_SQL_NO_ROW_FITS_NOTE = "No row fit this answer's size budget. Select fewer columns."
"""The fixed `note` when not even one shortened row's own keys fit `SQL_RESULT_CHARS`."""


def _shorten_leaf(value: JsonValue, limit: int) -> tuple[JsonValue, bool]:
    """`value` with every nested string clipped to `limit` characters, and whether anything was cut."""
    if isinstance(value, str):
        clipped = clip_text(value, limit)
        return clipped, clipped != value
    if isinstance(value, list):
        items = [_shorten_leaf(item, limit) for item in value]
        return [item for item, _cut in items], any(cut for _item, cut in items)
    if isinstance(value, dict):
        items = {key: _shorten_leaf(item, limit) for key, item in value.items()}
        return {key: item for key, (item, _cut) in items.items()}, any(cut for _item, cut in items.values())
    return value, False


def _jsonable_cells(row: Row) -> dict[str, JsonValue]:
    """One row with every cell converted to its JSON value."""
    cells: dict[str, JsonValue] = to_jsonable_python(dict(row))
    return cells


def _shorten_row(row: dict[str, JsonValue], limit: int) -> tuple[dict[str, JsonValue], bool]:
    """One converted row with every nested string clipped to `limit`, and whether anything was cut."""
    items = {key: _shorten_leaf(value, limit) for key, value in row.items()}
    return {key: item for key, (item, _cut) in items.items()}, any(cut for _item, cut in items.values())


def _sql_note(note: str | None, *, shortened: bool, omitted: bool) -> str | None:
    """`note` with the cells-shortened / rows-omitted fragments folded in, whichever apply."""
    parts = (note, _SQL_CELLS_SHORTENED_NOTE if shortened else None, _SQL_ROWS_OMITTED_NOTE if omitted else None)
    joined = " ".join(part for part in parts if part)
    return joined or None


def _bound_sql_response(result: SqlResult, *, note: str | None, max_chars: int = SQL_RESULT_CHARS) -> dict[str, JsonValue]:
    """The `{"rows": [...], "note": ...}` envelope with shortened cells and whole rows, kept within `max_chars`."""
    shortened_rows: list[JsonValue] = []
    any_shortened = False
    for row in result.rows:
        cells, cut = _shorten_row(_jsonable_cells(row), SQL_CELL_CHARS)
        shortened_rows.append(cells)
        any_shortened = any_shortened or cut

    complete: dict[str, JsonValue] = {"rows": shortened_rows, "note": _sql_note(note, shortened=any_shortened, omitted=False)}
    if fits_budget(complete, max_chars):
        return complete

    kept: list[JsonValue] = []
    for row in shortened_rows:
        candidate: list[JsonValue] = [*kept, row]
        trial: dict[str, JsonValue] = {"rows": candidate, "note": _sql_note(note, shortened=any_shortened, omitted=True)}
        if fits_budget(trial, max_chars):
            kept = candidate
        else:
            break
    envelope: dict[str, JsonValue] = {"rows": kept, "note": _sql_note(note, shortened=any_shortened, omitted=True)}
    if fits_budget(envelope, max_chars):
        return envelope
    return {"rows": [], "note": _SQL_NO_ROW_FITS_NOTE}


def make_run_sql(
    gateway: SqlGateway, embed_query: QueryEmbedder | None, *, embed_deadline: float = EMBED_DEADLINE_SECONDS, retry_refusals: bool = False
) -> RunSqlTool:
    """Build the top-level `run_sql` tool callable, which returns `{"rows": [...], "note": ...}`.

    The rows are capped at `MAX_ROWS` and the envelope is kept within `SQL_RESULT_CHARS`. The statement
    runs as written, so an `api.search` or `api.fetch` call through it returns the `meta.*` pages that
    the `search` and `fetch` tools hide. A statement the guard refuses raises `ToolError` from the tool,
    carrying the guard's reason, and a statement that did not run raises it carrying `read_only_error`'s text.
    With `retry_refusals` set, both raise `ModelRetry` with the same text instead. Without it, a statement that
    did not run because the database connection failed raises that error as it is, for the server to name.

    Args:
        gateway: The read-only SQL gateway the statement runs through.
        embed_query: The embedder for `vector_query` text, or None to run such searches BM25-only.
        embed_deadline: The longest one statement's texts may take to embed, in seconds, before the search runs BM25-only.
        retry_refusals: Whether a refused statement goes back to the model as a retry rather than failing the call.

    Returns:
        The `run_sql` tool callable.
    """

    async def run_sql(sql: str, row_limit: int = 50) -> dict[str, JsonValue]:
        try:
            result, note = await _run_sql_result(gateway, embed_query, sql, row_limit, embed_deadline)
        except RefusedStatementError as exc:
            raise _refusal(str(exc), retry=retry_refusals) from exc
        except STATEMENT_ERRORS as exc:
            if not retry_refusals and is_database_connection_failure(exc):
                raise
            _logger.info("run_sql rejected (read-only role): {problem}", problem=exc.__class__.__name__)
            raise _refusal(read_only_error(exc), retry=retry_refusals) from exc
        aside = " ".join(part for part in (note, result.truncated_message) if part)
        return _bound_sql_response(result, note=aside or None)

    return run_sql


def make_sandbox_run_sql(
    gateway: SqlGateway, embed_query: QueryEmbedder | None, *, embed_deadline: float = EMBED_DEADLINE_SECONDS
) -> SandboxRunSqlTool:
    """Build the code mode `run_sql` callable, which returns one dict per row in the gateway's column order.

    The rows are the same bounded rows the top-level form returns, without the `note`. The statement
    runs as written, so an `api.search` or `api.fetch` call through it returns the `meta.*` pages that
    the `search` and `fetch` tools hide. A statement the guard refuses raises `ModelRetry` from the
    callable, carrying the guard's reason, and a statement that did not run raises it carrying
    `read_only_error`'s text.

    Args:
        gateway: The read-only SQL gateway the statement runs through.
        embed_query: The embedder for `vector_query` text, or None to run such searches BM25-only.
        embed_deadline: The longest one statement's texts may take to embed, in seconds, before the search runs BM25-only.

    Returns:
        The sandbox `run_sql` callable.
    """

    async def run_sql(sql: str, row_limit: int = 50) -> list[dict[str, JsonValue]]:
        try:
            result, note = await _run_sql_result(gateway, embed_query, sql, row_limit, embed_deadline)
        except RefusedStatementError as exc:
            raise ModelRetry(str(exc)) from exc
        except STATEMENT_ERRORS as exc:
            _logger.info("sandbox run_sql rejected (read-only role): {problem}", problem=exc.__class__.__name__)
            raise ModelRetry(read_only_error(exc)) from exc
        bounded = _bound_sql_response(result, note=note)
        return cast("list[dict[str, JsonValue]]", bounded["rows"])

    return run_sql


async def _run_sql_result(
    gateway: SqlGateway, embed_query: QueryEmbedder | None, sql: str, row_limit: int, embed_deadline: float
) -> tuple[SqlResult, str | None]:
    """Check one statement, run it as a subquery stopped one row past the cap, and return its result plus any embed-fallback note."""
    statement, note = await _expand_vector_query(single_statement(sql), embed_query, embed_deadline)
    cap = max(1, min(row_limit, MAX_ROWS))
    result = await gateway.rows(f"SELECT * FROM ({statement}) AS q LIMIT {cap + 1}", cap=cap)  # noqa: S608 (the statement passed the guard and runs as the read-only role)
    return result, note


async def _expand_vector_query(sql: str, embed_query: QueryEmbedder | None, deadline: float) -> tuple[str, str | None]:
    """Embed each distinct `vector_query` text once within `deadline`, and write it in as `query_vec` with its model, or NULL with a note."""
    rewrite = VectorQueryRewrite.of(sql)
    if not rewrite.texts:
        return sql, None
    if embed_query is None:
        return rewrite.render(None), keyword_only_note(NO_EMBEDDER_REASON)
    try:
        vectors = await embed_within(lambda: asyncio.gather(*(embed_query(text) for text in rewrite.texts)), deadline=deadline)
    except QueryNotEmbeddedError as exc:
        return rewrite.render(None), keyword_only_note(exc.reason)
    return rewrite.render(vectors, model=query_model_of(embed_query)), None


def read_only_error(exc: Exception) -> str:
    """The message a SQL tool returns for a statement that did not run, with the read-only hint.

    Args:
        exc: The database error the statement raised.

    Returns:
        The database error's text followed by `READ_ONLY_HINT`.
    """
    detail = str(getattr(exc, "orig", exc)).strip() or exc.__class__.__name__
    return f"SQL error: {detail}\n\nHint: {READ_ONLY_HINT}"
