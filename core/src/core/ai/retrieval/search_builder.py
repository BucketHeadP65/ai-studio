"""Generate a knowledge base's hybrid-search function from a `SearchSchema`.

Every generated body has the same shape: a facet-filtered pool per relation, a keyword leg and a
vector leg per searchable surface, Reciprocal Rank Fusion (`sum(1.0 / (60 + rank))`) over the unioned
legs with each leg's own rank kept beside the fused score, and a hard top-k clamp.
`BaseHybridSearchBuilder` generates that shape. A backend subclass supplies its BM25 grammar and
body style through a small set of hooks.

A builder belongs to a database engine, not a knowledge base, so `ParadeDBHybridSearchBuilder` and
`PgTextSearchHybridSearchBuilder` serve any knowledge base from its `SearchSchema`. The builder renders
each `{facet}` placeholder as a bare name where parameters bind by name and as a positional marker
where they bind by position.
"""

from __future__ import annotations

import abc
import json

from typing import TYPE_CHECKING, Protocol

from .schema import SEARCH_LEGS

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from .schema import Facet, FacetPool, SearchSchema, SearchSurface, SearchTuning

# Placeholders for the query text, the query vector and the clamped top-k, rendered per body style.
_QUERY = "{query}"
_QUERY_VEC = "{query_vec}"
_CAPPED_K = "{capped_k}"

# The numbers a tuned search reads when it runs, each a variable the body declares and a placeholder its query uses.
_TUNED_NUMBERS = ("leg_budget", "rrf_constant", "snippet_chars", "rows_per_group")
_BUDGET = "{leg_budget}"
_RRF_CONSTANT = "{rrf_constant}"
_SNIPPET_CHARS = "{snippet_chars}"
_ROWS_PER_GROUP = "{rows_per_group}"
_MAX_HITS = "{max_hits_per_document}"

# The bounds generated into a tuned search, which hold whatever its tuning row says.
_BUDGET_BOUND = 200
_K_BOUND = 50
_RRF_BOUNDS = (1, 1000)
_SNIPPET_BOUNDS = (80, 2000)
_WEIGHT_BOUNDS = (0, 10)
_ROWS_PER_GROUP_BOUND = 50
_MAX_HITS_BOUND = 50

FOLDED_UNITS_PER_HIT = 3
"""The most folded units a capped search lists under one hit, best first."""

SEARCH_SNIPPET_CHARS = 480
"""The longest snippet a generated body returns per hit, in characters.

A longer match is cut to this length, ending in an ellipsis, and the row says so in `snippet_truncated`.
"""

# The columns every generated body returns after `id` and any partition column, whatever the knowledge base or backend.
# A NULL rank means that leg did not select the row. No generated predicate compares a rank against a threshold.
_RETURNS_COLUMNS = (
    "hit_level text, page_id text, page_title text, snippet text, score float8, "
    "lexical_rank int, vector_rank int, vector_distance float8, snippet_truncated boolean"
)

# The name each leg projects a row's partition under, before the fusion returns it as the schema's result column.
_PARTITION_KEY = "partition_key"

# The two leg tags the fusion filters its rank aggregates on. Both backends emit the same pair.
_LEXICAL_LEG = "lexical"
_SEMANTIC_LEG = "semantic"


def _snippet_case(source_text: str, *, tuned: bool) -> str:
    """Bound `source_text` to the snippet length, ending a cut excerpt with an ellipsis."""
    limit, cut = (_SNIPPET_CHARS, f"{_SNIPPET_CHARS} - 1") if tuned else (str(SEARCH_SNIPPET_CHARS), str(SEARCH_SNIPPET_CHARS - 1))
    return (
        f"CASE WHEN char_length({source_text}) > {limit}\n"
        f"     THEN left({source_text}, {cut}) || '\u2026'\n"
        f"     ELSE {source_text}\n"
        "END"
    )


def _weight(surface: SearchSurface, leg: str) -> str:
    """The variable a tuned search reads one surface's leg weight from."""
    return f"weight_{surface.name}_{leg}"


def _capped(schema: SearchSchema) -> bool:
    """Whether the search caps the hits of one document and returns the folded units."""
    return schema.tuning is not None and schema.tuning.max_hits_per_document is not None


def _tuned_names(schema: SearchSchema) -> tuple[str, ...]:
    """Every variable a tuned search declares for its query, in binding order, or none for an untuned one."""
    if schema.tuning is None:
        return ()
    capped = ("max_hits_per_document",) if _capped(schema) else ()
    return (*_TUNED_NUMBERS, *(_weight(surface, leg) for surface in schema.surfaces for leg in SEARCH_LEGS), *capped)


def _declarations(schema: SearchSchema) -> str:
    """The function's `DECLARE` lines: the clamped `k` alone untuned, or every number read from the tuning row."""
    tuning = schema.tuning
    if tuning is None:
        return "  capped_k int := least(greatest(k, 1), 50);"
    lines = [
        f"  tuning jsonb := {_tuning_source(tuning)};",
        f"  capped_k int := least(greatest(k, 1), least(greatest((tuning ->> 'k_ceiling')::numeric::int, 1), {_K_BOUND}));",
        f"  leg_budget int := greatest(least(greatest((tuning ->> 'budget')::numeric::int, 1), {_BUDGET_BOUND}), capped_k);",
        f"  rrf_constant float8 := least(greatest((tuning ->> 'rrf_constant')::float8, {_RRF_BOUNDS[0]}), {_RRF_BOUNDS[1]});",
        f"  snippet_chars int := least(greatest((tuning ->> 'snippet_chars')::numeric::int, {_SNIPPET_BOUNDS[0]}), {_SNIPPET_BOUNDS[1]});",
        f"  rows_per_group int := least(greatest((tuning ->> 'rows_per_group')::numeric::int, 1), {_ROWS_PER_GROUP_BOUND});",
    ]
    lines.extend(
        f"  {_weight(surface, leg)} float8 := least(greatest(coalesce((tuning -> 'weights' ->> '{surface.name}:{leg}')::float8, 1), "
        f"{_WEIGHT_BOUNDS[0]}), {_WEIGHT_BOUNDS[1]});"
        for surface in schema.surfaces
        for leg in SEARCH_LEGS
    )
    if _capped(schema):
        lines.append(
            f"  max_hits_per_document int := least(greatest(coalesce((tuning ->> 'max_hits_per_document')::numeric::int, 0), 0), {_MAX_HITS_BOUND});"
        )
    return "\n".join(lines)


def _tuning_source(tuning: SearchTuning) -> str:
    """The defaults as a jsonb literal, overridden by key by the tuning row when the tuning names one."""
    defaults: dict[str, int | float | dict[str, float]] = {
        "budget": tuning.budget,
        "rrf_constant": float(tuning.rrf_constant),
        "k_ceiling": tuning.k_ceiling,
        "snippet_chars": tuning.snippet_chars,
        "rows_per_group": tuning.rows_per_group,
        "weights": dict(tuning.weights),
    }
    if tuning.max_hits_per_document is not None:
        defaults["max_hits_per_document"] = tuning.max_hits_per_document
    literal = "'" + json.dumps(defaults).replace("'", "''") + "'::jsonb"
    if not tuning.relation:
        return literal
    return f"{literal} || coalesce((SELECT t.tuning FROM {tuning.relation} t WHERE t.id = 1), '{{}}'::jsonb)"


# The DEFAULT of the schema's `collection_facet`: the reader session's own collection.
# It replaces that facet's declared `Facet.default`, which is never rendered.
_SESSION_COLLECTION_DEFAULT = "current_setting('app.collection', true)"


def _substitute(sql: str, tokens: Mapping[str, str]) -> str:
    """Replace each `{name}` placeholder in `sql` with its rendered token."""
    for name, token in tokens.items():
        sql = sql.replace("{" + name + "}", token)
    return sql


def pinned_search_path(schemas: Iterable[str]) -> str:
    """The `search_path` a generated `SECURITY DEFINER` function pins: `pg_catalog`, `schemas`, `public`, then `pg_temp`.

    No other schema can shadow a built-in, and no temporary object a caller created can shadow anything.

    Args:
        schemas: The schemas the function reads, in the order names resolve through them.

    Returns:
        The comma-separated list.
    """
    return ", ".join(["pg_catalog", *schemas, "public", "pg_temp"])


def _search_path_schemas(schema: SearchSchema) -> str:
    """The pinned `search_path` over each table's schema in first-seen order."""
    seen: list[str] = []
    tables = [*(surface.table for surface in schema.surfaces), *(pool.table for pool in schema.pools)]
    for table in tables:
        if "." in table:
            qualifier = table.split(".", 1)[0]
            if qualifier not in seen:
                seen.append(qualifier)
    return pinned_search_path(seen)


def _returns_columns(schema: SearchSchema) -> str:
    """The `RETURNS TABLE` column list: `id`, any partition column, the shared columns, then any folded units."""
    partition = f"{schema.partition_result_col} text, " if schema.partition_result_col else ""
    folded = ", folded_units jsonb" if _capped(schema) else ""
    return f"id text, {partition}{_RETURNS_COLUMNS}{folded}"


def _level_predicate(schema: SearchSchema, surface: SearchSurface) -> str | None:
    """The per-leg level filter for `surface`, or `None` when the knowledge base has no level facet."""
    if not schema.level_facet:
        return None
    reference = "{" + schema.level_facet + "}"
    return f"({reference} IS NULL OR {reference} = '{surface.hit_level}')"


class HybridSearchBuilder(Protocol):
    """The contract for generating a knowledge base's hybrid-search function body.

    `BaseHybridSearchBuilder` implements it over a set of backend hooks.
    """

    def api_search_body(self, schema: SearchSchema, *, function_name: str = "api.search") -> str:
        """Return the full `CREATE FUNCTION` statement for the knowledge base's hybrid search.

        Args:
            schema: The knowledge base schema the body searches over.
            function_name: The schema-qualified function name to create.

        Returns:
            The complete `CREATE FUNCTION` DDL statement.
        """
        ...


class BaseHybridSearchBuilder(abc.ABC):
    """Generates a hybrid-search function from a `SearchSchema`, deferring the backend grammar to hooks.

    `api_search_body` generates the function signature and result columns, the top-k clamp, the facet
    pools, a keyword and a vector leg per surface, the fusion over the unioned legs and the security
    settings. A backend subclass overrides the six hooks for its BM25 grammar and body style.

    Each leg tags itself `lexical` or `semantic`, and the fusion keeps each leg's own rank and the
    semantic leg's raw distance beside the fused score. No generated predicate compares a rank, the
    fused score or a distance against a threshold.

    The two legs of a surface share one `FROM` and one set of base predicates (the level filter, the
    pool membership and any per-surface filters). Leg projections and semi-join subqueries are always
    relation-qualified.

    A partitioned schema keys every hit by its partition and its id, and returns the partition right
    after `id`. An unpartitioned schema renders exactly as it did before partitions existed.
    """

    def api_search_body(self, schema: SearchSchema, *, function_name: str = "api.search") -> str:
        """Return the full `CREATE FUNCTION` statement for the knowledge base's hybrid search.

        Args:
            schema: The knowledge base schema the body searches over.
            function_name: The schema-qualified function name to create.

        Returns:
            The complete `CREATE FUNCTION` DDL statement in this backend's grammar.
        """
        referands = ("query", "capped_k", "query_vec", *(facet.name for facet in schema.facets), *_tuned_names(schema))
        return_query = self._render_return_query(self._inner_query(schema), referands)
        preamble = self._preamble()
        preamble_block = f"{preamble}\n" if preamble else ""
        return (
            f"CREATE FUNCTION {function_name}(\n  {self._signature(schema)}\n)"
            f" RETURNS TABLE ({_returns_columns(schema)})\n"
            f"LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = {_search_path_schemas(schema)} AS $$\n"
            f"{preamble_block}"
            f"DECLARE\n{_declarations(schema)}\n"
            f"BEGIN\n{return_query}\nEND;\n$$"
        )

    def _signature(self, schema: SearchSchema) -> str:
        """Build the function's parameter list: the fixed search inputs then one parameter per facet."""
        lines = ["query text", "k int DEFAULT 12", f"query_vec {schema.embedding_sql_type} DEFAULT NULL"]
        lines.extend(f"{facet.name} {facet.sql_type} DEFAULT {self._facet_default(facet, schema)}" for facet in schema.facets)
        return ",\n  ".join(lines)

    def _facet_default(self, facet: Facet, schema: SearchSchema) -> str:
        """The SQL `DEFAULT` for one facet, the session's collection for the schema's `collection_facet`."""
        if schema.collection_facet and facet.name == schema.collection_facet:
            return _SESSION_COLLECTION_DEFAULT
        return "NULL" if facet.default is None else facet.default

    def _inner_query(self, schema: SearchSchema) -> str:
        """Build the inner `WITH ... SELECT` the body runs: pools, per-surface legs, the fusion select."""
        ctes = [self._pool_cte(pool) for pool in schema.pools]
        for surface in schema.surfaces:
            ctes.append(self._bm25_leg(schema, surface))
            ctes.append(self._vector_leg(schema, surface))
        ctes.append(self._legs_cte(schema))
        if schema.tuning is not None:
            return "WITH\n" + ",\n".join([*ctes, *self._policy_ctes(schema)]) + "\n" + self._policy_select(schema)
        partition = f"       legs.{_PARTITION_KEY},\n" if schema.partition_result_col else ""
        group_key = f"legs.{_PARTITION_KEY}, legs.id" if schema.partition_result_col else "legs.id"
        fusion = (
            "SELECT legs.id,\n"
            f"{partition}"
            "       max(legs.lvl),\n"
            "       max(legs.page_id),\n"
            "       max(legs.page_title),\n"
            "       max(legs.snippet),\n"
            f"       {self._fused_score(schema)},\n"
            f"       (min(legs.r) FILTER (WHERE legs.leg = '{_LEXICAL_LEG}'))::int,\n"
            f"       (min(legs.r) FILTER (WHERE legs.leg = '{_SEMANTIC_LEG}'))::int,\n"
            "       min(legs.vector_distance)::float8,\n"
            "       bool_or(legs.snippet_truncated)\n"
            "FROM legs\n"
            f"GROUP BY {group_key}\n"
            "ORDER BY rrf DESC\n"
            f"LIMIT {_CAPPED_K}"
        )
        return "WITH\n" + ",\n".join(ctes) + "\n" + fusion

    def _policy_ctes(self, schema: SearchSchema) -> list[str]:
        """The fused candidates, whether a lone exact title pins one, and each candidate's place within its group."""
        partitioned = bool(schema.partition_result_col)
        partition = f"legs.{_PARTITION_KEY}, " if partitioned else ""
        group_key = f"legs.{_PARTITION_KEY}, legs.id" if partitioned else "legs.id"
        fused = (
            "legs_fused AS (\n"
            f"  SELECT legs.id, {partition}max(legs.lvl) AS lvl, max(legs.page_id) AS page_id, max(legs.page_title) AS page_title,\n"
            f"         max(legs.snippet) AS snippet, {self._fused_score(schema)},\n"
            f"         (min(legs.r) FILTER (WHERE legs.leg = '{_LEXICAL_LEG}'))::int AS lexical_rank,\n"
            f"         (min(legs.r) FILTER (WHERE legs.leg = '{_SEMANTIC_LEG}'))::int AS vector_rank,\n"
            "         min(legs.vector_distance)::float8 AS vector_distance, bool_or(legs.snippet_truncated) AS snippet_truncated,\n"
            f"         max(legs.grp) AS grp, bool_or(legs.pin) AS pin{', max(legs.document_key) AS document_key' if _capped(schema) else ''}\n"
            "  FROM legs\n"
            f"  GROUP BY {group_key}\n"
            ")"
        )
        alone = (
            "fused AS (\n"
            "  SELECT legs_fused.*, legs_fused.pin AND count(*) FILTER (WHERE legs_fused.pin) OVER () = 1 AS pin_alone\n"
            "  FROM legs_fused\n"
            ")"
        )
        groups = f"fused.{_PARTITION_KEY}, fused.grp, fused.pin_alone" if partitioned else "fused.grp, fused.pin_alone"
        ruled = (
            "ruled AS (\n"
            f"  SELECT fused.*, row_number() OVER (PARTITION BY {groups} ORDER BY fused.rrf DESC, fused.id) AS in_group\n"
            "  FROM fused\n"
            ")"
        )
        if not _capped(schema):
            return [fused, alone, ruled]
        return [fused, alone, ruled, *self._document_ctes(schema)]

    def _document_ctes(self, schema: SearchSchema) -> list[str]:
        """Each kept candidate's place within its document, and the hits past the cap folded per document."""
        partitioned = bool(schema.partition_result_col)
        documents = f"ruled.{_PARTITION_KEY}, ruled.document_key" if partitioned else "ruled.document_key"
        documented = (
            "documented AS (\n"
            f"  SELECT ruled.*, row_number() OVER (PARTITION BY {documents} ORDER BY ruled.pin_alone DESC, ruled.rrf DESC, ruled.id) AS in_document\n"
            "  FROM ruled\n"
            f"  WHERE ruled.pin_alone OR ruled.in_group <= {_ROWS_PER_GROUP}\n"
            ")"
        )
        keys = f"documented.{_PARTITION_KEY}, documented.document_key" if partitioned else "documented.document_key"
        folded = (
            "folded AS (\n"
            f"  SELECT {keys},\n"
            "         jsonb_agg(jsonb_build_object('id', documented.id, 'hit_level', documented.lvl) ORDER BY documented.in_document) AS units\n"
            "  FROM documented\n"
            f"  WHERE {_MAX_HITS} > 0 AND documented.in_document > {_MAX_HITS}\n"
            f"    AND documented.in_document <= {_MAX_HITS} + {FOLDED_UNITS_PER_HIT}\n"
            f"  GROUP BY {keys}\n"
            ")"
        )
        return [documented, folded]

    def _policy_select(self, schema: SearchSchema) -> str:
        """The result after the policies: a lone exact title first, at most `rows_per_group` hits a group, cut to `k`."""
        if _capped(schema):
            return self._capped_select(schema)
        partition = f"ruled.{_PARTITION_KEY}, " if schema.partition_result_col else ""
        return (
            f"SELECT ruled.id, {partition}ruled.lvl, ruled.page_id, ruled.page_title, ruled.snippet, ruled.rrf, ruled.lexical_rank,\n"
            "       ruled.vector_rank, ruled.vector_distance, ruled.snippet_truncated\n"
            "FROM ruled\n"
            f"WHERE ruled.pin_alone OR ruled.in_group <= {_ROWS_PER_GROUP}\n"
            "ORDER BY ruled.pin_alone DESC, ruled.rrf DESC, ruled.id\n"
            f"LIMIT {_CAPPED_K}"
        )

    def _capped_select(self, schema: SearchSchema) -> str:
        """The result of a capped search: at most `max_hits_per_document` hits a document, the next ones folded under its best."""
        partitioned = bool(schema.partition_result_col)
        partition = f"documented.{_PARTITION_KEY}, " if partitioned else ""
        same_partition = f"folded.{_PARTITION_KEY} = documented.{_PARTITION_KEY} AND " if partitioned else ""
        return (
            f"SELECT documented.id, {partition}documented.lvl, documented.page_id, documented.page_title, documented.snippet, documented.rrf,\n"
            "       documented.lexical_rank, documented.vector_rank, documented.vector_distance, documented.snippet_truncated,\n"
            "       coalesce(folded.units, '[]'::jsonb)\n"
            "FROM documented\n"
            f"LEFT JOIN folded ON {same_partition}folded.document_key = documented.document_key AND documented.in_document = 1\n"
            f"WHERE {_MAX_HITS} = 0 OR documented.in_document <= {_MAX_HITS}\n"
            "ORDER BY documented.pin_alone DESC, documented.rrf DESC, documented.id\n"
            f"LIMIT {_CAPPED_K}"
        )

    def _policy_columns(self, schema: SearchSchema, surface: SearchSurface) -> list[str]:
        """The columns a tuned leg adds for the policies: whether its title pins, its group and, when capped, its document."""
        title = f"lower(btrim({surface.title_expr})) = lower(btrim({_QUERY}))" if surface.title_pin else "false"
        group = surface.group_expr or surface.parent_id_expr
        columns = [f"{title} AS pin", f"coalesce({group}, {surface.alias}.{surface.key_col})::text AS grp"]
        if _capped(schema):
            document = surface.document_expr or group
            columns.append(f"coalesce({document}, {surface.alias}.{surface.key_col})::text AS document_key")
        return columns

    def _pool_cte(self, pool: FacetPool) -> str:
        """Build one facet-pre-filter CTE from a `FacetPool`."""
        lines = [f"{pool.name} AS {self._materialized()}(", f"  SELECT {', '.join(pool.select_cols)}", f"  FROM {pool.table} {pool.alias}"]
        if pool.predicates:
            lines.append(f"  WHERE {' AND '.join(pool.predicates)}")
        lines.append(")")
        return "\n".join(lines)

    def _bm25_leg(self, schema: SearchSchema, surface: SearchSurface) -> str:
        """Build a surface's keyword leg: the shared projection, tagged `lexical`, ranked by the backend's BM25 order."""
        where = " AND ".join([*self._base_predicates(schema, surface), self._bm25_match_predicate(surface)])
        tuned = schema.tuning is not None
        weight = (
            [f"         {{{_weight(surface, _LEXICAL_LEG)}}}::float8 AS w,", *(f"         {column}," for column in self._policy_columns(schema, surface))]
            if tuned
            else []
        )
        lines = [
            f"{surface.name}_bm25 AS {self._materialized()}(",
            f"  SELECT {self._projection(surface, tuned=tuned)},",
            f"         '{_LEXICAL_LEG}'::text AS leg,",
            *weight,
            "         NULL::float8 AS vector_distance,",
            f"         row_number() OVER (ORDER BY {self._bm25_rank_expr(surface)}) AS r",
            f"  FROM {self._from_clause(surface)}",
            f"  WHERE {where}",
        ]
        tail = f"ORDER BY {self._bm25_rank_expr(surface)}\n    LIMIT {_BUDGET}" if tuned else self._bm25_leg_tail(surface)
        if tail:
            lines.append(f"  {tail}")
        lines.append(")")
        return "\n".join(lines)

    def _vector_leg(self, schema: SearchSchema, surface: SearchSurface) -> str:
        """Build a surface's vector leg: the shared projection, tagged `semantic`, ranked by cosine distance to the query."""
        embedding = f"{surface.alias}.{surface.embedding_col}"
        where = " AND ".join(
            [f"{_QUERY_VEC} IS NOT NULL", *self._base_predicates(schema, surface), f"{embedding} IS NOT NULL", *surface.vector_filters],
        )
        if schema.tuning is not None:
            return self._budgeted_vector_leg(schema, surface, embedding, where)
        lines = [
            f"{surface.name}_vec AS {self._materialized()}(",
            f"  SELECT {self._projection(surface, tuned=False)},",
            f"         '{_SEMANTIC_LEG}'::text AS leg,",
            f"         ({embedding} <=> {_QUERY_VEC})::float8 AS vector_distance,",
            f"         row_number() OVER (ORDER BY {embedding} <=> {_QUERY_VEC}) AS r",
            f"  FROM {self._from_clause(surface)}",
            f"  WHERE {where}",
            ")",
        ]
        return "\n".join(lines)

    def _budgeted_vector_leg(self, schema: SearchSchema, surface: SearchSurface, embedding: str, where: str) -> str:
        """A tuned vector leg: the budget's nearest rows by exact distance, never by an approximate index, ranked by it."""
        lines = [
            f"{surface.name}_vec AS {self._materialized()}(",
            "  SELECT near.*, row_number() OVER (ORDER BY near.vector_distance, near.id) AS r",
            "  FROM (",
            f"    SELECT {self._projection(surface, tuned=True)},",
            f"           '{_SEMANTIC_LEG}'::text AS leg,",
            f"           {{{_weight(surface, _SEMANTIC_LEG)}}}::float8 AS w,",
            *(f"           {column}," for column in self._policy_columns(schema, surface)),
            f"           ({embedding} <=> {_QUERY_VEC})::float8 AS vector_distance",
            f"    FROM {self._from_clause(surface)}",
            f"    WHERE {where}",
            # Exact by construction: an approximate index under a pool filter can stop before it has the budget's rows.
            f"    ORDER BY ({embedding} <=> {_QUERY_VEC}) + 0",
            f"    LIMIT {_BUDGET}",
            "  ) AS near",
            ")",
        ]
        return "\n".join(lines)

    def _fused_score(self, schema: SearchSchema) -> str:
        """The fusion's score column: plain reciprocal rank fusion, or each leg weighed with the tuned constant."""
        if schema.tuning is None:
            return "sum(1.0 / (60 + legs.r))::float8 AS rrf"
        return f"sum(legs.w / ({_RRF_CONSTANT} + legs.r))::float8 AS rrf"

    def _legs_cte(self, schema: SearchSchema) -> str:
        """Union every surface's keyword and vector legs into the `legs` CTE the fusion aggregates."""
        names = [f"{surface.name}_{leg}" for surface in schema.surfaces for leg in ("bm25", "vec")]
        weight = schema.tuning is not None
        capped = _capped(schema)
        selects = [
            f"  {'' if index == 0 else 'UNION ALL '}SELECT {name}.id, {self._legs_partition(schema, name)}{name}.lvl, "
            f"{name}.page_id, {name}.page_title, {name}.snippet, {name}.snippet_truncated, {name}.leg, "
            f"{f'{name}.w, {name}.pin, {name}.grp, ' if weight else ''}{f'{name}.document_key, ' if capped else ''}"
            f"{name}.vector_distance, {name}.r FROM {name}"
            for index, name in enumerate(names)
        ]
        return "legs AS (\n" + "\n".join(selects) + "\n)"

    def _legs_partition(self, schema: SearchSchema, leg: str) -> str:
        """The partition column one leg contributes to the union, or nothing for an unpartitioned schema."""
        return f"{leg}.{_PARTITION_KEY}, " if schema.partition_result_col else ""

    def _projection(self, surface: SearchSurface, *, tuned: bool) -> str:
        """Build the result columns both legs of a surface project, before each leg's tag and rank."""
        text = f"{surface.alias}.{surface.text_col}"
        partition = f"{surface.alias}.{surface.partition_col} AS {_PARTITION_KEY}, " if surface.partition_col else ""
        limit = _SNIPPET_CHARS if tuned else str(SEARCH_SNIPPET_CHARS)
        return (
            f"{surface.alias}.{surface.key_col} AS id, {partition}'{surface.hit_level}'::text AS lvl, "
            f"{surface.parent_id_expr} AS page_id, {surface.title_expr} AS page_title, "
            f"{_snippet_case(text, tuned=tuned)} AS snippet, "
            f"char_length({text}) > {limit} AS snippet_truncated"
        )

    def _from_clause(self, surface: SearchSurface) -> str:
        """Build a surface's `FROM` clause: its aliased relation plus any pool join."""
        return f"{surface.table} {surface.alias} {surface.join}".rstrip()

    def _base_predicates(self, schema: SearchSchema, surface: SearchSurface) -> list[str]:
        """The predicates both legs of a surface share: the level filter, pool membership, and filters."""
        predicates: list[str] = []
        level = _level_predicate(schema, surface)
        if level:
            predicates.append(level)
        if surface.membership:
            predicates.append(surface.membership)
        predicates.extend(surface.filters)
        return predicates

    @abc.abstractmethod
    def _preamble(self) -> str:
        """Return the plpgsql body preamble emitted after `AS $$`, or an empty string for none."""

    @abc.abstractmethod
    def _materialized(self) -> str:
        """Return the CTE materialization keyword (with a trailing space) for pools and scoring legs."""

    @abc.abstractmethod
    def _render_return_query(self, inner: str, referands: tuple[str, ...]) -> str:
        """Render the parameter placeholders in `inner` and wrap it as this body's terminated `RETURN QUERY`."""

    @abc.abstractmethod
    def _bm25_rank_expr(self, surface: SearchSurface) -> str:
        """Return the expression a keyword leg ranks by inside its `row_number()` window."""

    @abc.abstractmethod
    def _bm25_match_predicate(self, surface: SearchSurface) -> str:
        """Return the `WHERE` predicate a keyword leg matches the query text with."""

    @abc.abstractmethod
    def _bm25_leg_tail(self, surface: SearchSurface) -> str:
        """Return a keyword leg's trailing clause (a per-leg top-k order and limit), or an empty string."""


class ParadeDBHybridSearchBuilder(BaseHybridSearchBuilder):
    """Generates the hybrid-search body in ParadeDB `pg_search` grammar.

    The body runs dynamically (`RETURN QUERY EXECUTE ... USING`, parameters bound positionally) with
    each scoring leg in its own `MATERIALIZED` CTE. Keyword legs match with `@@@ paradedb.parse` and
    rank by `paradedb.score`, with no per-leg limit.
    """

    def _preamble(self) -> str:
        """No preamble, since a dynamic body binds parameters positionally."""
        return ""

    def _materialized(self) -> str:
        """Materialize each scoring leg so its BM25 scan runs standalone before the union."""
        return "MATERIALIZED "

    def _render_return_query(self, inner: str, referands: tuple[str, ...]) -> str:
        """Bind the placeholders to positional markers and wrap `inner` in a dynamic `RETURN QUERY EXECUTE`."""
        tokens = {name: f"${position}" for position, name in enumerate(referands, start=1)}
        return f"RETURN QUERY EXECUTE $q$\n{_substitute(inner, tokens)}\n$q$ USING {', '.join(referands)};"

    def _bm25_rank_expr(self, surface: SearchSurface) -> str:
        """Rank by the BM25 relevance score of the matched row, best first."""
        return f"paradedb.score({surface.alias}.{surface.key_col}) DESC"

    def _bm25_match_predicate(self, surface: SearchSurface) -> str:
        """Match the surface's text column against the parsed query with the `@@@` operator."""
        return f"{surface.alias}.{surface.text_col} @@@ paradedb.parse({_QUERY})"

    def _bm25_leg_tail(self, surface: SearchSurface) -> str:
        """No leg tail, since the fusion clamp bounds the result."""
        del surface
        return ""


class PgTextSearchHybridSearchBuilder(BaseHybridSearchBuilder):
    """Generates the hybrid-search body in `pg_textsearch` grammar.

    The body is a static `RETURN QUERY` binding parameters by name, with plain CTEs. It sets
    `#variable_conflict use_variable`, so a bare parameter name that collides with a column resolves
    to the parameter. A keyword leg keeps rows whose `<@>` score is below `0` and takes its own top-k
    slice.
    """

    def _preamble(self) -> str:
        """Resolve bare parameter names to the parameter, so a name that also names a column is unambiguous."""
        return "#variable_conflict use_variable"

    def _materialized(self) -> str:
        """No materialization: the `<@>` legs are ordinary CTEs that inline into the union."""
        return ""

    def _render_return_query(self, inner: str, referands: tuple[str, ...]) -> str:
        """Bind the placeholders to their names and wrap `inner` in a static `RETURN QUERY`."""
        return f"RETURN QUERY\n{_substitute(inner, {name: name for name in referands})};"

    def _bm25_rank_expr(self, surface: SearchSurface) -> str:
        """Rank by the `<@>` distance to the query (lower is better, so the top rank is the best match)."""
        return self._score_expr(surface)

    def _bm25_match_predicate(self, surface: SearchSurface) -> str:
        """Keep only rows the query actually matches: `<@>` scores a match below `0`, a non-match at `0`."""
        return f"{self._score_expr(surface)} < 0"

    def _bm25_leg_tail(self, surface: SearchSurface) -> str:
        """Take each leg's own top-k slice, since `<@>` does not filter on its own."""
        return f"ORDER BY {self._score_expr(surface)}\n    LIMIT {_CAPPED_K}"

    def _score_expr(self, surface: SearchSurface) -> str:
        """The shared `<@>` score expression a keyword leg ranks, filters, and slices by."""
        return f"{surface.alias}.{surface.text_col} <@> to_bm25query({_QUERY}, '{surface.bm25_index}')"
