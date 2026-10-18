"""Generate a knowledge base's link-graph traversal functions from a `GraphSchema`.

Every backend generates the same `graph_expand`, `graph_paths` and `graph_reach` signatures, the
node-plus-edge result columns derived from the schema, the hop, top-k and reach caps, and the
trailing projection that joins each reached node to its display view. Every walk keeps the set of
nodes it has reached and takes one hop at a time from the newest of them, so its work grows with the
nodes and edges it reaches, never with the number of paths between them. `BaseGraphTraversalBuilder`
generates the walks and a backend subclass supplies the one hop. `CteGraphTraversalBuilder` reads the
edge relation. `AgeGraphTraversalBuilder` matches one hop in a projected Apache AGE graph and maps
its `agtype` results back to the same columns.

A builder belongs to a database engine, not a knowledge base, so either one serves any knowledge
base from its `GraphSchema`. The Apache AGE graph name is a builder argument, not a schema field.
"""

from __future__ import annotations

import abc

from typing import TYPE_CHECKING, ClassVar, Protocol

from .schema import PATH_RANK_COLUMN, PATH_SCORE_COLUMN
from .search_builder import pinned_search_path

if TYPE_CHECKING:
    from .schema import GraphSchema

GRAPH_EXPAND_HOP_LIMIT = 6
"""The most hops an expansion walks out from its seeds, whatever `hops` asks for. Both backends apply it."""

GRAPH_PATHS_HOP_LIMIT = 8
"""The most hops a route may take, whatever `max_hops` asks for. Both backends apply it."""

GRAPH_REACH_NODE_LIMIT = 10_000
"""The most nodes a reach walk takes in before it stops and says it stopped. Both backends apply it."""

GRAPH_TOP_K_CAP = 50
"""The most reached nodes or paths a traversal selects, whatever `k` asks for.

The selection takes one item past the cap, so a caller can tell a complete answer from a cut one.
The cap counts nodes or paths, never the edges that reached a selected node. A handler reading the
rows applies the same cap.
"""
_K_CAP = f"least(greatest(k, 1), {GRAPH_TOP_K_CAP})"

# The trailing argument a currency-aware traversal takes: false walks only current nodes, true walks superseded ones too.
# Present only when `GraphSchema.superseded_col` is set.
_CURRENCY_PARAM = "include_superseded boolean DEFAULT false"

# The default names of the three query functions, and the alias the node view is joined under.
_EXPAND_FUNCTION = "api.graph_expand"
_PATHS_FUNCTION = "api.graph_paths"
_REACH_FUNCTION = "api.graph_reach"
_NODE_ALIAS = "c"

# The name the walk and reached-set CTEs carry a reached node's id under, whatever the schema's id column is called.
_REACHED_ID = "id"

# The one row a reach walk returns.
_REACH_RETURNS = "reached_nodes int, deepest_hop int, target_hop int, complete boolean"

# A step's edges travel between statements as jsonb objects under these keys, the payload columns as p0, p1 and so on.
_STEP_KEYS = ("src", "dst", "rel")


def _node_col_types(schema: GraphSchema) -> tuple[str, ...]:
    """The SQL types of `schema.node_cols`, all `text` when the schema declares none."""
    if schema.node_col_types:
        return schema.node_col_types
    return ("text",) * len(schema.node_cols)


def _payload_keys(schema: GraphSchema) -> tuple[str, ...]:
    """The jsonb keys a step's edge carries its payload columns under, in `edge_payload_cols` order."""
    return tuple(f"p{index}" for index in range(len(schema.edge_payload_cols)))


def _step_record(schema: GraphSchema) -> str:
    """The column list `jsonb_to_recordset` reads a step's edges with."""
    return ", ".join(f"{key} text" for key in (*_STEP_KEYS, *_payload_keys(schema)))


def _step_object(schema: GraphSchema, alias: str) -> str:
    """The `jsonb_build_object` arguments that turn one step edge row of `alias` into its jsonb object."""
    return ", ".join(f"'{key}', {alias}.{key}" for key in (*_STEP_KEYS, *_payload_keys(schema)))


def _refresh_function(schema: GraphSchema) -> str:
    """The projection-refresh function name, in the edge relation's schema when it has one."""
    if "." in schema.edge_table:
        return f"{schema.edge_table.split('.', 1)[0]}.refresh_graph"
    return "refresh_graph"


def _dst_partition_col(partition_col: str) -> str:
    """The destination-side partition column paired with a source-side one, `src_` swapped for `dst_`."""
    return partition_col.replace("src_", "dst_", 1)


class GraphTraversalBuilder(Protocol):
    """The contract for generating a knowledge base's link-graph traversal SQL from a `GraphSchema`.

    `BaseGraphTraversalBuilder` implements it over a set of backend hooks. Every method generates a
    function body or DDL only, never a `COMMENT ON FUNCTION`, which the caller writes.
    """

    def expand_body(self, schema: GraphSchema, *, function_name: str = _EXPAND_FUNCTION) -> str:
        """Return the `CREATE FUNCTION` statement for the knowledge base's neighbourhood expansion.

        Args:
            schema: The graph schema the expansion walks.
            function_name: The schema-qualified name to define the expansion function under (defaults
                to `api.graph_expand`).

        Returns:
            The complete `CREATE FUNCTION` DDL statement in this backend's grammar.
        """
        ...

    def paths_body(self, schema: GraphSchema, *, function_name: str = _PATHS_FUNCTION) -> str:
        """Return the `CREATE FUNCTION` statement for the knowledge base's path finding.

        Args:
            schema: The graph schema the paths traverse.
            function_name: The schema-qualified name to define the path-finding function under
                (defaults to `api.graph_paths`).

        Returns:
            The complete `CREATE FUNCTION` DDL statement in this backend's grammar.
        """
        ...

    def reach_body(self, schema: GraphSchema, *, function_name: str = _REACH_FUNCTION) -> str:
        """Return the `CREATE FUNCTION` statement for the walk that says how far a seed set reaches.

        Args:
            schema: The graph schema the walk traverses.
            function_name: The schema-qualified name to define the reach function under (defaults to
                `api.graph_reach`).

        Returns:
            The complete `CREATE FUNCTION` DDL statement in this backend's grammar.
        """
        ...

    def create_graph_ddl(self, schema: GraphSchema) -> list[str]:
        """Return the one-time graph-projection DDL this backend needs before the functions.

        Args:
            schema: The graph schema whose edge relation the projection reads.

        Returns:
            One single SQL command per element, or `[]` when the backend needs no projection.
        """
        ...

    def refresh_body(self, schema: GraphSchema) -> str:
        """Return the `CREATE FUNCTION` statement for the writer-only projection refresh.

        Args:
            schema: The graph schema whose edge relation the refresh re-projects.

        Returns:
            The complete `CREATE FUNCTION` DDL statement for the refresh routine.
        """
        ...


class BaseGraphTraversalBuilder(abc.ABC):
    """Generates link-graph traversal functions from a `GraphSchema`, deferring the one hop to a backend.

    The base generates the three walks, their signatures, the RETURNS clauses derived from the
    schema's node and edge columns, the hop, top-k and reach caps, and the trailing projection that
    joins each reached node to its display view. A backend subclass supplies the one-hop step, the
    function volatility, the search path, the projection DDL and the refresh routine its engine needs.

    The function signatures are the same for every knowledge base and backend. Expansion takes a seed
    set, a hop bound, an optional relation-type filter and a top-k, and returns one row per reached
    node carrying the edge that reached it. Path finding takes two node ids, a hop bound, the same
    relation filter and a top-k, and returns one row per node of each ranked path. Reach takes a seed
    set, the same relation filter and an optional target, and returns one row: how many nodes the
    seeds reach, the hops to the farthest of them and to the target, and whether the walk took in
    every node it could reach.

    Expansion rows are ordered by hop, node, source, relation and payload, so two runs of the same
    query return identical rows. Every shortest-arrival edge of a selected node survives the top-k cut.
    Routes are ranked by length, then by their nodes, relations and payloads.
    """

    _volatility: ClassVar[str]
    """The volatility every generated query function declares."""

    def create_graph_ddl(self, schema: GraphSchema) -> list[str]:
        """Return the one-time graph-projection DDL this backend needs before the functions.

        Args:
            schema: The graph schema whose edge relation the projection reads.

        Returns:
            One single SQL command per element, or `[]` when the backend needs no projection.
        """
        del schema
        return []

    def expand_body(self, schema: GraphSchema, *, function_name: str = _EXPAND_FUNCTION) -> str:
        """Return the `graph_expand` definition: a hop-by-hop walk over the set of reached nodes.

        Each hop follows the edges that leave the nodes the last hop reached and keeps the ones that
        arrive at a node not yet reached. The walk stops at the hop bound, when nothing new is reached,
        or once more nodes are reached than `k` selects, since a later hop could never be selected.

        Args:
            schema: The graph schema the walk traverses.
            function_name: The schema-qualified name to define the function under (defaults to
                `api.graph_expand`).

        Returns:
            The `CREATE FUNCTION` statement for a relation-filtered, top-k-capped neighbourhood walk
            joined to the node view, seeds always emitted as rows.
        """
        carry = self._edge_carry_cols(schema)
        payload_keys = _payload_keys(schema)
        arrival = ", ".join([f"'{_REACHED_ID}', s.dst", "'hop', walked_hops", _step_object(schema, "s")])
        arrival_record = ", ".join([f"{_REACHED_ID} text", "hop int", _step_record(schema)])
        arrival_cols = ", ".join(
            [
                f"a.{_REACHED_ID}",
                "a.hop",
                f"a.src AS {schema.src_col}",
                f"a.rel AS {schema.rel_col}",
                *(f"a.{key} AS {col}" for key, col in zip(payload_keys, schema.edge_payload_cols, strict=True)),
            ],
        )
        seed_cols = ", ".join([f"s.seed AS {_REACHED_ID}", "0 AS hop", *(f"NULL::text AS {col}" for col in carry)])
        return (
            f"        CREATE FUNCTION {function_name}(\n"
            f"          {self._expand_signature(schema)}\n"
            f"        ) RETURNS TABLE ({self._expand_returns(schema)})\n"
            f"        LANGUAGE plpgsql {self._volatility} SECURITY DEFINER SET search_path = {self._search_path(schema)} AS $fn$\n"
            f"        DECLARE\n"
            f"          capped_k      int := {_K_CAP};\n"
            f"          capped_hops   int := {self._hop_cap('hops', GRAPH_EXPAND_HOP_LIMIT)};\n"
            f"{self._walk_declarations()}"
            f"          fresh_edges   jsonb;\n"
            f"          arrival_edges jsonb := '[]'::jsonb;\n"
            f"        BEGIN\n"
            f"{self._walk_start()}"
            f"          WHILE walked_hops < capped_hops AND cardinality(frontier_ids) > 0 AND seen_count <= capped_k LOOP\n"
            f"            walked_hops := walked_hops + 1;\n"
            f"{self._step(schema, frontier='frontier_ids', backward=False)}"
            f"            SELECT coalesce(jsonb_agg(jsonb_build_object({arrival})), '[]'::jsonb),\n"
            f"                   coalesce(array_agg(DISTINCT s.dst), ARRAY[]::text[])\n"
            f"              INTO fresh_edges, frontier_ids\n"
            f"              FROM jsonb_to_recordset(step_edges) AS s({_step_record(schema)})\n"
            f"              WHERE NOT seen ? s.dst;\n"
            f"            arrival_edges := arrival_edges || fresh_edges;\n"
            f"{self._walk_admit()}"
            f"          END LOOP;\n"
            f"          RETURN QUERY\n"
            f"          WITH walk AS (\n"
            f"            SELECT {seed_cols}\n"
            f"            FROM unnest(seed_ids) AS s(seed)\n"
            f"            UNION ALL\n"
            f"            SELECT {arrival_cols}\n"
            f"            FROM jsonb_to_recordset(arrival_edges) AS a({arrival_record})\n"
            f"          ),\n"
            f"{self._expand_selection(schema, k_cap='capped_k')};\n"
            f"        END;\n"
            f"        $fn$"
        )

    def paths_body(self, schema: GraphSchema, *, function_name: str = _PATHS_FUNCTION) -> str:
        """Return the `graph_paths` definition: routes found by deepening a length bound one hop at a time.

        A walk back from `to_id` over the set of reached nodes gives each node its hops to `to_id`.
        Each bound, from the hops between the two nodes up to `max_hops`, then lists every route of at
        most that many hops, extending a route only to a node it has not visited from which `to_id`
        is still within the bound. Deepening stops once more routes are found than `k` selects, since
        a longer route ranks after every shorter one.

        Args:
            schema: The graph schema the paths traverse.
            function_name: The schema-qualified name to define the function under (defaults to
                `api.graph_paths`).

        Returns:
            The `CREATE FUNCTION` statement for a relation-filtered, top-k-capped route search that
            returns one row per node of each ranked route, joined to the node view.
        """
        accumulators = self._path_accumulators(schema)
        payload_keys = _payload_keys(schema)
        prefix_record = ", ".join(["path text[]", *(f"{name} text[]" for name in accumulators)])
        start = ", ".join(["'path', jsonb_build_array(from_id)", *(f"'{name}', jsonb_build_array(NULL::text)" for name in accumulators)])
        extended = ", ".join(
            [
                "'path', p.path || s.dst",
                "'rels', p.rels || s.rel",
                *(f"'{name}', p.{name} || s.{key}" for name, key in zip(accumulators[1:], payload_keys, strict=True)),
            ],
        )
        route_cols = ", ".join(["r.path", *(f"r.{name}" for name in accumulators), "r.hop"])
        return (
            f"        CREATE FUNCTION {function_name}(\n"
            f"          {self._paths_signature(schema)}\n"
            f"        ) RETURNS TABLE ({self._paths_returns(schema)})\n"
            f"        LANGUAGE plpgsql {self._volatility} SECURITY DEFINER SET search_path = {self._search_path(schema)} AS $fn$\n"
            f"        DECLARE\n"
            f"          capped_k        int := {_K_CAP};\n"
            f"          capped_hops     int := {self._hop_cap('max_hops', GRAPH_PATHS_HOP_LIMIT)};\n"
            f"          distances       jsonb;\n"
            f"          target_frontier text[];\n"
            f"          back_hops       int := 0;\n"
            f"          frontier_ids    text[];\n"
            f"          step_edges      jsonb;\n"
            f"          bound           int := 0;\n"
            f"          depth           int;\n"
            f"          prefixes        jsonb;\n"
            f"          extended        jsonb;\n"
            f"          routes_found    jsonb := '[]'::jsonb;\n"
            f"        BEGIN\n"
            f"          IF from_id IS NULL OR to_id IS NULL OR from_id = to_id THEN\n"
            f"            RETURN;\n"
            f"          END IF;\n"
            f"          distances := jsonb_build_object(to_id, 0);\n"
            f"          target_frontier := ARRAY[to_id];\n"
            f"          WHILE bound < capped_hops LOOP\n"
            f"            bound := bound + 1;\n"
            f"            WHILE back_hops < bound AND cardinality(target_frontier) > 0 LOOP\n"
            f"              back_hops := back_hops + 1;\n"
            f"{self._step(schema, frontier='target_frontier', backward=True)}"
            f"              SELECT coalesce(array_agg(DISTINCT s.src), ARRAY[]::text[]) INTO target_frontier\n"
            f"                FROM jsonb_to_recordset(step_edges) AS s(src text)\n"
            f"                WHERE NOT distances ? s.src;\n"
            f"              distances := distances || (\n"
            f"                SELECT coalesce(jsonb_object_agg(f.node, back_hops), jsonb_build_object()) FROM unnest(target_frontier) AS f(node)\n"
            f"              );\n"
            f"            END LOOP;\n"
            f"            EXIT WHEN NOT distances ? from_id AND cardinality(target_frontier) = 0;\n"
            f"            CONTINUE WHEN NOT distances ? from_id;\n"
            f"            prefixes := jsonb_build_array(jsonb_build_object({start}));\n"
            f"            routes_found := '[]'::jsonb;\n"
            f"            depth := 0;\n"
            f"            WHILE depth < bound AND jsonb_array_length(prefixes) > 0 LOOP\n"
            f"              depth := depth + 1;\n"
            f"              frontier_ids := ARRAY(SELECT DISTINCT p.path[cardinality(p.path)] FROM jsonb_to_recordset(prefixes) AS p(path text[]));\n"
            f"{self._step(schema, frontier='frontier_ids', backward=False)}"
            f"              SELECT coalesce(jsonb_agg(jsonb_build_object({extended})), '[]'::jsonb) INTO extended\n"
            f"                FROM jsonb_to_recordset(prefixes) AS p({prefix_record})\n"
            f"                JOIN jsonb_to_recordset(step_edges) AS s({_step_record(schema)}) ON s.src = p.path[cardinality(p.path)]\n"
            f"                WHERE NOT s.dst = ANY(p.path)\n"
            f"                  AND depth + coalesce((distances ->> s.dst)::int, bound + 1) <= bound;\n"
            f"              routes_found := routes_found || (\n"
            f"                SELECT coalesce(jsonb_agg(x.route || jsonb_build_object('hop', depth)), '[]'::jsonb)\n"
            f"                FROM jsonb_array_elements(extended) AS x(route) WHERE x.route -> 'path' ->> depth = to_id\n"
            f"              );\n"
            f"              prefixes := (\n"
            f"                SELECT coalesce(jsonb_agg(x.route), '[]'::jsonb)\n"
            f"                FROM jsonb_array_elements(extended) AS x(route) WHERE x.route -> 'path' ->> depth <> to_id\n"
            f"              );\n"
            f"            END LOOP;\n"
            f"            EXIT WHEN jsonb_array_length(routes_found) > capped_k;\n"
            f"          END LOOP;\n"
            f"          RETURN QUERY\n"
            f"          WITH routes AS (\n"
            f"            SELECT {route_cols}\n"
            f"            FROM jsonb_to_recordset(routes_found) AS r({prefix_record}, hop int)\n"
            f"          ),\n"
            f"{self._paths_selection(schema, k_cap='capped_k')};\n"
            f"        END;\n"
            f"        $fn$"
        )

    def reach_body(self, schema: GraphSchema, *, function_name: str = _REACH_FUNCTION) -> str:
        """Return the `graph_reach` definition: a hop-by-hop walk with no hop bound that counts what it reaches.

        The walk stops at `target_id` once it reaches it, when nothing new is reached, or once it has
        taken in `GRAPH_REACH_NODE_LIMIT` nodes. `complete` is true only when it stopped because nothing
        new was reached.

        Args:
            schema: The graph schema the walk traverses.
            function_name: The schema-qualified name to define the function under (defaults to
                `api.graph_reach`).

        Returns:
            The `CREATE FUNCTION` statement for a relation-filtered walk returning one row of
            `reached_nodes`, `deepest_hop`, `target_hop` (NULL when the target was not reached) and
            `complete`.
        """
        return (
            f"        CREATE FUNCTION {function_name}(\n"
            f"          {self._reach_signature(schema)}\n"
            f"        ) RETURNS TABLE ({_REACH_RETURNS})\n"
            f"        LANGUAGE plpgsql {self._volatility} SECURITY DEFINER SET search_path = {self._search_path(schema)} AS $fn$\n"
            f"        DECLARE\n"
            f"{self._walk_declarations()}"
            f"        BEGIN\n"
            f"{self._walk_start()}"
            f"          LOOP\n"
            f"            IF target_id IS NOT NULL AND seen ? target_id THEN\n"
            f"              RETURN QUERY SELECT seen_count, walked_hops, (seen ->> target_id)::int, false;\n"
            f"              RETURN;\n"
            f"            END IF;\n"
            f"            IF cardinality(frontier_ids) = 0 THEN\n"
            f"              RETURN QUERY SELECT seen_count, greatest(walked_hops - 1, 0), NULL::int, true;\n"
            f"              RETURN;\n"
            f"            END IF;\n"
            f"            IF seen_count >= {GRAPH_REACH_NODE_LIMIT} THEN\n"
            f"              RETURN QUERY SELECT seen_count, walked_hops, NULL::int, false;\n"
            f"              RETURN;\n"
            f"            END IF;\n"
            f"            walked_hops := walked_hops + 1;\n"
            f"{self._step(schema, frontier='frontier_ids', backward=False)}"
            f"            SELECT coalesce(array_agg(DISTINCT s.dst), ARRAY[]::text[]) INTO frontier_ids\n"
            f"              FROM jsonb_to_recordset(step_edges) AS s(dst text)\n"
            f"              WHERE NOT seen ? s.dst;\n"
            f"{self._walk_admit()}"
            f"          END LOOP;\n"
            f"        END;\n"
            f"        $fn$"
        )

    @abc.abstractmethod
    def refresh_body(self, schema: GraphSchema) -> str:
        """Return the full `CREATE FUNCTION` for the writer-only projection refresh.

        Args:
            schema: The graph schema whose edge relation the refresh re-projects.

        Returns:
            The complete `CREATE FUNCTION` DDL statement for the refresh routine.
        """

    @abc.abstractmethod
    def _step(self, schema: GraphSchema, *, frontier: str, backward: bool) -> str:
        """The plpgsql statement that sets `step_edges` to the edges one hop from `frontier`, forward or backward."""

    @abc.abstractmethod
    def _search_path(self, schema: GraphSchema) -> str:
        """The `search_path` every generated function pins."""

    def _walk_declarations(self) -> str:
        """Declare the reached set, its size, the newest nodes and the hop count a forward walk keeps."""
        return (
            "          frontier_ids  text[];\n"
            "          seen          jsonb;\n"
            "          seen_count    int;\n"
            "          step_edges    jsonb;\n"
            "          walked_hops   int := 0;\n"
        )

    def _walk_start(self) -> str:
        """Start a forward walk at the distinct seeds, each reached at hop 0."""
        return (
            "          frontier_ids := ARRAY(SELECT DISTINCT s.seed FROM unnest(seed_ids) AS s(seed) WHERE s.seed IS NOT NULL);\n"
            "          seen := (SELECT coalesce(jsonb_object_agg(f.node, 0), jsonb_build_object()) FROM unnest(frontier_ids) AS f(node));\n"
            "          seen_count := cardinality(frontier_ids);\n"
        )

    def _walk_admit(self) -> str:
        """Add the nodes the last hop reached to the reached set, at that hop."""
        return (
            "            seen := seen || (\n"
            "              SELECT coalesce(jsonb_object_agg(f.node, walked_hops), jsonb_build_object()) FROM unnest(frontier_ids) AS f(node)\n"
            "            );\n"
            "            seen_count := seen_count + cardinality(frontier_ids);\n"
        )

    def _expand_signature(self, schema: GraphSchema) -> str:
        """Build the `graph_expand` parameter list, gaining a leading collection parameter when partitioned."""
        base = "seed_ids text[],\n          hops int DEFAULT 2,\n          rel_types text[] DEFAULT NULL,\n          k int DEFAULT 20"
        return self._signature(schema, base)

    def _paths_signature(self, schema: GraphSchema) -> str:
        """Build the `graph_paths` parameter list, gaining a leading collection parameter when partitioned."""
        base = (
            "from_id text,\n          to_id text,\n          max_hops int DEFAULT 4,"
            "\n          rel_types text[] DEFAULT NULL,\n          k int DEFAULT 10"
        )
        return self._signature(schema, base)

    def _reach_signature(self, schema: GraphSchema) -> str:
        """Build the `graph_reach` parameter list, gaining a leading collection parameter when partitioned."""
        base = "seed_ids text[],\n          rel_types text[] DEFAULT NULL,\n          target_id text DEFAULT NULL"
        return self._signature(schema, base)

    def _signature(self, schema: GraphSchema, base: str) -> str:
        """Wrap one function's own parameters in the optional collection and currency parameters the schema asks for."""
        parameters = base if not schema.superseded_col else f"{base},\n          {_CURRENCY_PARAM}"
        if schema.partition_col:
            return f"p_collection text,\n          {parameters}"
        return parameters

    def _expand_returns(self, schema: GraphSchema) -> str:
        """Build the `graph_expand` RETURNS TABLE column set from the schema."""
        node = [f"{col} {sql_type}" for col, sql_type in zip(schema.node_cols, _node_col_types(schema), strict=True)]
        edge = [f"{schema.src_col} text", f"{schema.dst_col} text", f"{schema.rel_col} text"]
        payload = [f"{col} text" for col in schema.edge_payload_cols]
        return ", ".join([*node, *edge, *payload])

    def _paths_returns(self, schema: GraphSchema) -> str:
        """Build the `graph_paths` RETURNS TABLE column set: the expand columns plus rank and score."""
        return f"{self._expand_returns(schema)}, {PATH_RANK_COLUMN} int, {PATH_SCORE_COLUMN} float8"

    def _hop_cap(self, param: str, limit: int) -> str:
        """Build the clamped hop-bound expression for a hop parameter."""
        return f"least(greatest({param}, 1), {limit})"

    def _edge_carry_cols(self, schema: GraphSchema) -> tuple[str, ...]:
        """The edge columns a reached node carries: the source, the relation, then any payload columns."""
        return (schema.src_col, schema.rel_col, *schema.edge_payload_cols)

    def _partition_predicate(self, schema: GraphSchema, *, alias: str, collection_param: str) -> str:
        """Build the trailing `AND <alias>.<partition_col> = <collection_param>` clause, or nothing when unpartitioned."""
        if not schema.partition_col:
            return ""
        return f" AND {alias}.{schema.partition_col} = {collection_param}"

    def _no_relation_filter(self, param: str) -> str:
        """Build the test that the caller named no relations, where NULL and an empty list both mean no filter."""
        return f"{param} IS NULL OR cardinality({param}) = 0"

    def _expand_selection(self, schema: GraphSchema, *, k_cap: str, collection_param: str = "p_collection") -> str:
        """Build the selection and projection every expansion core shares over its `walk` CTE, taking one node past the clamp."""
        carry = self._edge_carry_cols(schema)
        reached_carry = ", ".join(f"w.{col}" for col in carry)
        shortest_carry = ", ".join(f"r.{col}" for col in carry)
        columns = [
            f"f.{_REACHED_ID}",
            *(f"{_NODE_ALIAS}.{col}" for col in schema.node_cols[1:]),
            f"f.{schema.src_col}",
            f"f.{_REACHED_ID} AS {schema.dst_col}",
            f"f.{schema.rel_col}",
            *(f"f.{col}" for col in schema.edge_payload_cols),
        ]
        select = ",\n                 ".join(columns)
        order = ", ".join(f"f.{col}" for col in (_REACHED_ID, *carry))
        partition = self._partition_predicate(schema, alias=_NODE_ALIAS, collection_param=collection_param)
        return (
            f"          reached AS (\n"
            f"            SELECT DISTINCT w.{_REACHED_ID}, w.hop, {reached_carry}\n"
            f"            FROM walk w\n"
            f"          ),\n"
            f"          arrivals AS (\n"
            f"            SELECT r.{_REACHED_ID}, min(r.hop) AS hop\n"
            f"            FROM reached r\n"
            f"            GROUP BY r.{_REACHED_ID}\n"
            f"          ),\n"
            f"          selected_nodes AS (\n"
            f"            SELECT a.{_REACHED_ID}, a.hop\n"
            f"            FROM arrivals a\n"
            f"            ORDER BY a.hop, a.{_REACHED_ID}\n"
            f"            LIMIT {k_cap} + 1\n"
            f"          ),\n"
            f"          shortest AS (\n"
            f"            SELECT r.{_REACHED_ID}, r.hop, {shortest_carry}\n"
            f"            FROM reached r\n"
            f"            JOIN selected_nodes s ON s.{_REACHED_ID} = r.{_REACHED_ID} AND s.hop = r.hop\n"
            f"          )\n"
            f"          SELECT {select}\n"
            f"          FROM shortest f\n"
            f"          LEFT JOIN {schema.node_view} {_NODE_ALIAS} ON {_NODE_ALIAS}.{schema.node_cols[0]} = f.{_REACHED_ID}{partition}\n"
            f"          ORDER BY f.hop, {order}"
        )

    def _paths_selection(self, schema: GraphSchema, *, k_cap: str, collection_param: str = "p_collection") -> str:
        """Build the ranking and projection every path core shares over its `routes` CTE, taking one route past the clamp."""
        accumulators = self._path_accumulators(schema)
        route_cols = ", ".join(f"r.{name}" for name in ("path", *accumulators))
        ranked_cols = ", ".join(f"cand.{name}" for name in ("path", *accumulators))
        key = ", ".join(f"cand.{name}" for name in ("hop", "path", *accumulators))
        columns = [
            f"step.node_id AS {schema.node_cols[0]}",
            *(f"{_NODE_ALIAS}.{col}" for col in schema.node_cols[1:]),
            f"cp.path[step.ord - 1] AS {schema.src_col}",
            f"step.node_id AS {schema.dst_col}",
            f"cp.rels[step.ord] AS {schema.rel_col}",
            *(f"cp.{col}s[step.ord] AS {col}" for col in schema.edge_payload_cols),
            f"cp.{PATH_RANK_COLUMN}::int",
            f"cp.{PATH_SCORE_COLUMN}",
        ]
        select = ",\n                 ".join(columns)
        partition = self._partition_predicate(schema, alias=_NODE_ALIAS, collection_param=collection_param)
        return (
            f"          complete AS (\n"
            f"            SELECT DISTINCT {route_cols}, r.hop\n"
            f"            FROM routes r\n"
            f"          ),\n"
            f"          selected_paths AS (\n"
            f"            SELECT {ranked_cols}, cand.hop,\n"
            f"                   row_number() OVER (ORDER BY {key}) AS {PATH_RANK_COLUMN},\n"
            f"                   (1.0 / (1 + cand.hop))::float8 AS {PATH_SCORE_COLUMN}\n"
            f"            FROM complete cand\n"
            f"            ORDER BY {key}\n"
            f"            LIMIT {k_cap} + 1\n"
            f"          )\n"
            f"          SELECT {select}\n"
            f"          FROM selected_paths cp\n"
            f"          CROSS JOIN LATERAL unnest(cp.path) WITH ORDINALITY AS step(node_id, ord)\n"
            f"          LEFT JOIN {schema.node_view} {_NODE_ALIAS} ON {_NODE_ALIAS}.{schema.node_cols[0]} = step.node_id{partition}\n"
            f"          ORDER BY cp.{PATH_RANK_COLUMN}, step.ord"
        )

    def _path_accumulators(self, schema: GraphSchema) -> tuple[str, ...]:
        """The per-route arrays a path carries beside its node array: the relations, then each payload."""
        return ("rels", *(f"{col}s" for col in schema.edge_payload_cols))


class CteGraphTraversalBuilder(BaseGraphTraversalBuilder):
    """Generates the portable traversal that reads the edge relation directly.

    Every function is a `LANGUAGE plpgsql STABLE` body. Each hop reads the edges whose source (or,
    walking back, whose destination) is among the newest reached nodes, joining the node view to
    read whether the node an edge arrives at is current. The backend needs no projection, so
    `create_graph_ddl` returns `[]` and the refresh routine does nothing.
    """

    _volatility: ClassVar[str] = "STABLE"

    def _step(self, schema: GraphSchema, *, frontier: str, backward: bool) -> str:
        """Read the step's edges from the edge relation, deduplicated, with the relation, partition and currency filters."""
        near = schema.dst_col if backward else schema.src_col
        columns = ", ".join(
            [
                f"e.{schema.src_col} AS src",
                f"e.{schema.dst_col} AS dst",
                f"e.{schema.rel_col} AS rel",
                *(f"e.{col} AS {key}" for col, key in zip(schema.edge_payload_cols, _payload_keys(schema), strict=True)),
            ],
        )
        partition = self._partition_predicate(schema, alias="e", collection_param="p_collection")
        return (
            f"            SELECT coalesce(jsonb_agg(jsonb_build_object({_step_object(schema, 'e')})), '[]'::jsonb)\n"
            f"              INTO step_edges\n"
            f"              FROM (\n"
            f"                SELECT DISTINCT {columns}\n"
            f"                FROM {schema.edge_table} e\n"
            f"{self._currency_join(schema)}"
            f"                WHERE e.{near} = ANY({frontier})\n"
            f"                  AND ({self._no_relation_filter('rel_types')} OR e.{schema.rel_col} = ANY(rel_types)){partition}"
            f"{self._currency_predicate(schema, alias='n')}\n"
            f"              ) AS e;\n"
        )

    def _currency_predicate(self, schema: GraphSchema, *, alias: str) -> str:
        """Build the step's currency clause, where a node with no node-view row counts as current."""
        if not schema.superseded_col:
            return ""
        return f"\n                  AND (include_superseded OR NOT coalesce({alias}.{schema.superseded_col}, false))"

    def _currency_join(self, schema: GraphSchema) -> str:
        """Build the step's node-view `LEFT JOIN` that reads whether the node an edge arrives at is current."""
        if not schema.superseded_col:
            return ""
        partition = self._partition_predicate(schema, alias="n", collection_param="p_collection")
        return f"                LEFT JOIN {schema.node_view} n ON n.{schema.node_cols[0]} = e.{schema.dst_col}{partition}\n"

    def refresh_body(self, schema: GraphSchema) -> str:
        """Return the no-op refresh routine.

        This backend walks the edge relation live, so there is no projected graph to rebuild. The
        function exists so a load step can call a refresh on any backend.

        Args:
            schema: The graph schema naming the schema the routine lives in.

        Returns:
            The `CREATE FUNCTION` statement for an empty-body refresh returning void.
        """
        return (
            f"        CREATE FUNCTION {_refresh_function(schema)}() RETURNS void\n"
            f"        LANGUAGE plpgsql AS $$\n"
            f"        BEGIN\n"
            f"          -- No projected graph under the recursive-CTE backend (the traversal walks the edge relation live).\n"
            f"          RETURN;\n"
            f"        END;\n"
            f"        $$"
        )

    def _search_path(self, schema: GraphSchema) -> str:
        """Build the recursive-CTE `search_path`: the edge relation's schema between the pinned defaults."""
        head = [schema.edge_table.split(".", 1)[0]] if "." in schema.edge_table else []
        return pinned_search_path(head)



class AgeGraphTraversalBuilder(BaseGraphTraversalBuilder):
    """Generates the Apache AGE traversal over a projected openCypher property graph.

    Every function is a `LANGUAGE plpgsql VOLATILE` body. Each hop runs one single-hop
    `cypher(...)` match from the newest reached nodes through `EXECUTE`, with the cypher parameter
    map bound as the first positional parameter, and maps its `agtype` result back to text. The
    query functions only read what the last refresh projected, so a read-only role can execute them.
    `create_graph_ddl` projects the edge relation into the graph once, and the refresh routine
    re-projects it on every load with plain DELETE and INSERT.

    The node label, edge label and node id property are fixed names, so a reached node's id is the
    `id` property whatever the knowledge base calls its node id column.
    """

    _volatility: ClassVar[str] = "VOLATILE"
    _NODE_LABEL = "Node"
    _EDGE_LABEL = "REL"
    _VERTEX_ID = "id"
    _COLLECTION_PROPERTY = "collection"

    def __init__(self, graph_name: str) -> None:
        """Bind the builder to one Apache AGE property graph.

        Args:
            graph_name: The name of the AGE graph the traversal walks and the projection builds.
        """
        self._graph = graph_name

    def create_graph_ddl(self, schema: GraphSchema) -> list[str]:
        """Return the one-time extension, graph, and edge-relation projection DDL.

        Each element is a single SQL command. The list creates the AGE extension, pins the
        `search_path`, creates the graph and its node and edge labels, registers the node label's
        catalog shape, then projects one vertex per distinct edge endpoint and one relationship per
        edge row carrying the relation and payload columns.

        Args:
            schema: The graph schema whose edge relation the projection reads.

        Returns:
            The projection commands, one single SQL command per element.
        """
        return [
            "CREATE EXTENSION IF NOT EXISTS age",
            f"SET search_path = {self._search_path(schema)}",
            f"SELECT create_graph('{self._graph}')",
            f"SELECT create_vlabel('{self._graph}', '{self._NODE_LABEL}')",
            f"SELECT create_elabel('{self._graph}', '{self._EDGE_LABEL}')",
            f"SELECT * FROM cypher('{self._graph}', $$ CREATE (:{self._NODE_LABEL}) $$) AS (v agtype) WHERE false",
            self._node_projection(schema),
            self._edge_projection(schema),
        ]

    def _step(self, schema: GraphSchema, *, frontier: str, backward: bool) -> str:
        """Match one hop in the graph, keeping both ends in `p_collection`, the named relations and a current arrival node."""
        near = "b" if backward else "a"
        payload_keys = _payload_keys(schema)
        conditions = [f"{near}.{self._VERTEX_ID} IN $frontier"]
        if schema.partition_col:
            conditions += [f"a.{self._COLLECTION_PROPERTY} = $p_collection", f"b.{self._COLLECTION_PROPERTY} = $p_collection"]
        conditions.append(f"(size($rel_types) = 0 OR r.{schema.rel_col} IN $rel_types)")
        if schema.superseded_col:
            conditions.append(f"(NOT b.{schema.superseded_col} OR $include_superseded)")
        where = "\n                    AND ".join(conditions)
        returns = ", ".join(
            [
                f"a.{self._VERTEX_ID} AS src",
                f"b.{self._VERTEX_ID} AS dst",
                f"r.{schema.rel_col} AS rel",
                *(f"r.{col} AS {key}" for col, key in zip(schema.edge_payload_cols, payload_keys, strict=True)),
            ],
        )
        agtype_cols = ", ".join(f"{key} agtype" for key in (*_STEP_KEYS, *payload_keys))
        casts = ", ".join(f"(g.{key})::text AS {key}" for key in (*_STEP_KEYS, *payload_keys))
        return (
            f"            EXECUTE $exec$\n"
            f"              SELECT coalesce(jsonb_agg(jsonb_build_object({_step_object(schema, 'e')})), '[]'::jsonb)\n"
            f"              FROM (\n"
            f"                SELECT DISTINCT {casts}\n"
            f"                FROM cypher('{self._graph}', $cy$\n"
            f"                    MATCH (a:{self._NODE_LABEL})-[r:{self._EDGE_LABEL}]->(b:{self._NODE_LABEL})\n"
            f"                    WHERE {where}\n"
            f"                    RETURN {returns}\n"
            f"                $cy$, $1) AS g({agtype_cols})\n"
            f"              ) AS e\n"
            f"            $exec$ INTO step_edges USING {self._step_map(schema, frontier)};\n"
        )

    def _step_map(self, schema: GraphSchema, frontier: str) -> str:
        """Build the cypher parameter map one hop binds as its first positional parameter."""
        entries = [("frontier", frontier), ("rel_types", "coalesce(rel_types, ARRAY[]::text[])")]
        if schema.partition_col:
            entries.append(("p_collection", "p_collection"))
        if schema.superseded_col:
            entries.append(("include_superseded", "include_superseded"))
        pairs = ", ".join(f"'{name}', to_jsonb({value})::text::agtype" for name, value in entries)
        return f"ag_catalog.agtype_build_map({pairs})"

    def refresh_body(self, schema: GraphSchema) -> str:
        """Return the writer-only projection refresh.

        The refresh clears the graph's label tables and re-projects them from the edge relation with
        plain DELETE and INSERT. A load step runs it as the writer.

        Args:
            schema: The graph schema whose edge relation is re-projected. A set `partition_col`
                keys each vertex by `(id, collection)` and carries the collection onto every
                vertex and edge.

        Returns:
            The `CREATE FUNCTION` statement for the refresh routine.
        """
        edge_map = self._edge_property_map(schema)
        node_map = self._vertex_property_map(schema)
        if schema.partition_col:
            dst_collection = _dst_partition_col(schema.partition_col)
            node_from = (
                f"(SELECT e.{schema.src_col} AS {self._VERTEX_ID}, e.{schema.partition_col} AS {self._COLLECTION_PROPERTY}"
                f" FROM {schema.edge_table} e"
                f" UNION SELECT e2.{schema.dst_col} AS {self._VERTEX_ID}, e2.{dst_collection} AS {self._COLLECTION_PROPERTY}"
                f" FROM {schema.edge_table} e2) AS n{self._vertex_currency_join(schema)}"
            )
            edge_join = (
                f'            JOIN {self._graph}."{self._NODE_LABEL}" va'
                f" ON ag_catalog.agtype_access_operator(va.properties, '\"{self._VERTEX_ID}\"'::agtype) = to_jsonb(e.{schema.src_col})::text::agtype"
                f" AND ag_catalog.agtype_access_operator(va.properties, '\"{self._COLLECTION_PROPERTY}\"'::agtype)"
                f" = to_jsonb(e.{schema.partition_col})::text::agtype\n"
                f'            JOIN {self._graph}."{self._NODE_LABEL}" vb'
                f" ON ag_catalog.agtype_access_operator(vb.properties, '\"{self._VERTEX_ID}\"'::agtype) = to_jsonb(e.{schema.dst_col})::text::agtype"
                f" AND ag_catalog.agtype_access_operator(vb.properties, '\"{self._COLLECTION_PROPERTY}\"'::agtype)"
                f" = to_jsonb(e.{dst_collection})::text::agtype;\n"
            )
        else:
            node_from = (
                f"(SELECT e.{schema.src_col} AS {self._VERTEX_ID} FROM {schema.edge_table} e"
                f" UNION SELECT e2.{schema.dst_col} FROM {schema.edge_table} e2) AS n{self._vertex_currency_join(schema)}"
            )
            edge_join = (
                f'            JOIN {self._graph}."{self._NODE_LABEL}" va'
                f" ON ag_catalog.agtype_access_operator(va.properties, '\"{self._VERTEX_ID}\"'::agtype) = to_jsonb(e.{schema.src_col})::text::agtype\n"
                f'            JOIN {self._graph}."{self._NODE_LABEL}" vb'
                f" ON ag_catalog.agtype_access_operator(vb.properties, '\"{self._VERTEX_ID}\"'::agtype) = to_jsonb(e.{schema.dst_col})::text::agtype;\n"
            )
        return (
            f"        CREATE FUNCTION {_refresh_function(schema)}() RETURNS void\n"
            f"        LANGUAGE plpgsql SECURITY DEFINER SET search_path = {self._search_path(schema)} AS $fn$\n"
            f"        BEGIN\n"
            f'          DELETE FROM {self._graph}."{self._EDGE_LABEL}";\n'
            f'          DELETE FROM {self._graph}."{self._NODE_LABEL}";\n'
            f'          INSERT INTO {self._graph}."{self._NODE_LABEL}" (properties)\n'
            f"            SELECT {node_map}\n"
            f"            FROM {node_from};\n"
            f'          INSERT INTO {self._graph}."{self._EDGE_LABEL}" (start_id, end_id, properties)\n'
            f"            SELECT va.{self._VERTEX_ID}, vb.{self._VERTEX_ID}, {edge_map}\n"
            f"            FROM {schema.edge_table} e\n"
            f"{edge_join}"
            f"        END;\n"
            f"        $fn$"
        )

    def _node_projection(self, schema: GraphSchema) -> str:
        """Build the one-time vertex projection: one `Node` per distinct edge endpoint."""
        if schema.partition_col:
            dst_collection = _dst_partition_col(schema.partition_col)
            return (
                f'INSERT INTO {self._graph}."{self._NODE_LABEL}" (properties)\n'
                f"        SELECT {self._vertex_property_map(schema)}\n"
                f"        FROM (SELECT {schema.src_col} AS {self._VERTEX_ID}, {schema.partition_col} AS {self._COLLECTION_PROPERTY}"
                f" FROM {schema.edge_table}"
                f" UNION SELECT {schema.dst_col} AS {self._VERTEX_ID}, {dst_collection} AS {self._COLLECTION_PROPERTY}"
                f" FROM {schema.edge_table}) AS n{self._vertex_currency_join(schema)}"
            )
        return (
            f'INSERT INTO {self._graph}."{self._NODE_LABEL}" (properties)\n'
            f"        SELECT {self._vertex_property_map(schema)}\n"
            f"        FROM (SELECT {schema.src_col} AS {self._VERTEX_ID} FROM {schema.edge_table}"
            f" UNION SELECT {schema.dst_col} FROM {schema.edge_table}) AS n{self._vertex_currency_join(schema)}"
        )

    def _vertex_property_map(self, schema: GraphSchema) -> str:
        """Build the `agtype_build_map` carrying a vertex's id, its partition, and its currency."""
        pairs = [f"'{self._VERTEX_ID}', n.{self._VERTEX_ID}"]
        if schema.partition_col:
            pairs.append(f"'{self._COLLECTION_PROPERTY}', n.{self._COLLECTION_PROPERTY}")
        if schema.superseded_col:
            pairs.append(f"'{schema.superseded_col}', coalesce(v.{schema.superseded_col}, false)")
        return f"ag_catalog.agtype_build_map({', '.join(pairs)})"

    def _vertex_currency_join(self, schema: GraphSchema) -> str:
        """Build the join the vertex projection reads currency from, `currency_source` when set, else `node_view`."""
        if not schema.superseded_col:
            return ""
        partition = f" AND v.{schema.partition_col} = n.{self._COLLECTION_PROPERTY}" if schema.partition_col else ""
        source = schema.currency_source or schema.node_view
        return f" LEFT JOIN {source} v ON v.{schema.node_cols[0]} = n.{self._VERTEX_ID}{partition}"

    def _edge_projection(self, schema: GraphSchema) -> str:
        """Build the one-time relationship projection: one `REL` per edge row."""
        edge_map = self._edge_property_map(schema)
        if schema.partition_col:
            dst_collection = _dst_partition_col(schema.partition_col)
            return (
                f'INSERT INTO {self._graph}."{self._EDGE_LABEL}" (start_id, end_id, properties)\n'
                f"        SELECT a.{self._VERTEX_ID}, b.{self._VERTEX_ID}, {edge_map}\n"
                f"        FROM {schema.edge_table} e\n"
                f'        JOIN {self._graph}."{self._NODE_LABEL}" a'
                f" ON ag_catalog.agtype_access_operator(a.properties, '\"{self._VERTEX_ID}\"'::agtype) = to_jsonb(e.{schema.src_col})::text::agtype"
                f" AND ag_catalog.agtype_access_operator(a.properties, '\"{self._COLLECTION_PROPERTY}\"'::agtype)"
                f" = to_jsonb(e.{schema.partition_col})::text::agtype\n"
                f'        JOIN {self._graph}."{self._NODE_LABEL}" b'
                f" ON ag_catalog.agtype_access_operator(b.properties, '\"{self._VERTEX_ID}\"'::agtype) = to_jsonb(e.{schema.dst_col})::text::agtype"
                f" AND ag_catalog.agtype_access_operator(b.properties, '\"{self._COLLECTION_PROPERTY}\"'::agtype)"
                f" = to_jsonb(e.{dst_collection})::text::agtype"
            )
        return (
            f'INSERT INTO {self._graph}."{self._EDGE_LABEL}" (start_id, end_id, properties)\n'
            f"        SELECT a.{self._VERTEX_ID}, b.{self._VERTEX_ID}, {edge_map}\n"
            f"        FROM {schema.edge_table} e\n"
            f'        JOIN {self._graph}."{self._NODE_LABEL}" a'
            f" ON ag_catalog.agtype_access_operator(a.properties, '\"{self._VERTEX_ID}\"'::agtype) = to_jsonb(e.{schema.src_col})::text::agtype\n"
            f'        JOIN {self._graph}."{self._NODE_LABEL}" b'
            f" ON ag_catalog.agtype_access_operator(b.properties, '\"{self._VERTEX_ID}\"'::agtype) = to_jsonb(e.{schema.dst_col})::text::agtype"
        )

    def _edge_property_map(self, schema: GraphSchema) -> str:
        """Build the `agtype_build_map` carrying an edge's relation, payload, and collection properties."""
        pairs = [f"'{schema.rel_col}', e.{schema.rel_col}", *(f"'{col}', e.{col}" for col in schema.edge_payload_cols)]
        if schema.partition_col:
            pairs.append(f"'{self._COLLECTION_PROPERTY}', e.{schema.partition_col}")
        return f"ag_catalog.agtype_build_map({', '.join(pairs)})"

    def _search_path(self, schema: GraphSchema) -> str:
        """Build the Apache AGE `search_path`: `ag_catalog` and the edge schema between the pinned defaults."""
        middle = [schema.edge_table.split(".", 1)[0]] if "." in schema.edge_table else []
        return pinned_search_path(["ag_catalog", *middle])
