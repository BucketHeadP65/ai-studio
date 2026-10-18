"""Structural tests for the link-graph traversal builders.

The builders emit deterministic SQL text from a `GraphSchema`, so these assert the generated body's
structure (the invariant scaffold, each backend's traversal grammar, the schema-derived RETURNS, and
that no placeholder leaks) rather than executing it. The knowledge base fixture is a generic graph, not any
consumer's, so the library stays knowledge-base-agnostic. Behavioral parity against a real database is proven
by the consumer's own integration suite.
"""

from __future__ import annotations

import dataclasses

from typing import TYPE_CHECKING

import pytest

from core.ai.retrieval.contracts import AgeGraphTraversalBuilder, CteGraphTraversalBuilder, GraphSchema

if TYPE_CHECKING:
    from core.ai.retrieval.graph_builder import BaseGraphTraversalBuilder

# The fixed signatures, RETURNS column set, and caps every backend shares (invariant across knowledge bases).
_EXPAND_SIGNATURE = ("seed_ids text[]", "hops int DEFAULT 2", "rel_types text[] DEFAULT NULL", "k int DEFAULT 20")
_REACH_SIGNATURE = ("seed_ids text[]", "rel_types text[] DEFAULT NULL", "target_id text DEFAULT NULL")
_PATHS_SIGNATURE = ("from_id text", "to_id text", "max_hops int DEFAULT 4", "rel_types text[] DEFAULT NULL", "k int DEFAULT 10")
_EXPAND_RETURNS = (
    "node_id text, label text, name text, state text, archived boolean, from_node text, to_node text, kind text, weight text"
)
_PATHS_RETURNS = f"{_EXPAND_RETURNS}, path_rank int, score float8"

# Golden pins of the bodies a descriptor that declares neither a partition nor a currency column renders. That is the
# shape every already-applied migration's own frozen descriptor asks for, so these pin what a replay of the existing
# chain still builds. Drift here is a deliberate change to re-pin, never an accident to ship.
_GOLDEN_CTE_EXPAND = "        CREATE FUNCTION api.graph_expand(\n          seed_ids text[],\n          hops int DEFAULT 2,\n          rel_types text[] DEFAULT NULL,\n          k int DEFAULT 20\n        ) RETURNS TABLE (node_id text, label text, name text, state text, archived boolean, from_node text, to_node text, kind text, weight text)\n        LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, graph, public, pg_temp AS $fn$\n        DECLARE\n          capped_k      int := least(greatest(k, 1), 50);\n          capped_hops   int := least(greatest(hops, 1), 6);\n          frontier_ids  text[];\n          seen          jsonb;\n          seen_count    int;\n          step_edges    jsonb;\n          walked_hops   int := 0;\n          fresh_edges   jsonb;\n          arrival_edges jsonb := '[]'::jsonb;\n        BEGIN\n          frontier_ids := ARRAY(SELECT DISTINCT s.seed FROM unnest(seed_ids) AS s(seed) WHERE s.seed IS NOT NULL);\n          seen := (SELECT coalesce(jsonb_object_agg(f.node, 0), jsonb_build_object()) FROM unnest(frontier_ids) AS f(node));\n          seen_count := cardinality(frontier_ids);\n          WHILE walked_hops < capped_hops AND cardinality(frontier_ids) > 0 AND seen_count <= capped_k LOOP\n            walked_hops := walked_hops + 1;\n            SELECT coalesce(jsonb_agg(jsonb_build_object('src', e.src, 'dst', e.dst, 'rel', e.rel, 'p0', e.p0)), '[]'::jsonb)\n              INTO step_edges\n              FROM (\n                SELECT DISTINCT e.from_node AS src, e.to_node AS dst, e.kind AS rel, e.weight AS p0\n                FROM graph.links e\n                WHERE e.from_node = ANY(frontier_ids)\n                  AND (rel_types IS NULL OR cardinality(rel_types) = 0 OR e.kind = ANY(rel_types))\n              ) AS e;\n            SELECT coalesce(jsonb_agg(jsonb_build_object('id', s.dst, 'hop', walked_hops, 'src', s.src, 'dst', s.dst, 'rel', s.rel, 'p0', s.p0)), '[]'::jsonb),\n                   coalesce(array_agg(DISTINCT s.dst), ARRAY[]::text[])\n              INTO fresh_edges, frontier_ids\n              FROM jsonb_to_recordset(step_edges) AS s(src text, dst text, rel text, p0 text)\n              WHERE NOT seen ? s.dst;\n            arrival_edges := arrival_edges || fresh_edges;\n            seen := seen || (\n              SELECT coalesce(jsonb_object_agg(f.node, walked_hops), jsonb_build_object()) FROM unnest(frontier_ids) AS f(node)\n            );\n            seen_count := seen_count + cardinality(frontier_ids);\n          END LOOP;\n          RETURN QUERY\n          WITH walk AS (\n            SELECT s.seed AS id, 0 AS hop, NULL::text AS from_node, NULL::text AS kind, NULL::text AS weight\n            FROM unnest(seed_ids) AS s(seed)\n            UNION ALL\n            SELECT a.id, a.hop, a.src AS from_node, a.rel AS kind, a.p0 AS weight\n            FROM jsonb_to_recordset(arrival_edges) AS a(id text, hop int, src text, dst text, rel text, p0 text)\n          ),\n          reached AS (\n            SELECT DISTINCT w.id, w.hop, w.from_node, w.kind, w.weight\n            FROM walk w\n          ),\n          arrivals AS (\n            SELECT r.id, min(r.hop) AS hop\n            FROM reached r\n            GROUP BY r.id\n          ),\n          selected_nodes AS (\n            SELECT a.id, a.hop\n            FROM arrivals a\n            ORDER BY a.hop, a.id\n            LIMIT capped_k + 1\n          ),\n          shortest AS (\n            SELECT r.id, r.hop, r.from_node, r.kind, r.weight\n            FROM reached r\n            JOIN selected_nodes s ON s.id = r.id AND s.hop = r.hop\n          )\n          SELECT f.id,\n                 c.label,\n                 c.name,\n                 c.state,\n                 c.archived,\n                 f.from_node,\n                 f.id AS to_node,\n                 f.kind,\n                 f.weight\n          FROM shortest f\n          LEFT JOIN graph.entities c ON c.node_id = f.id\n          ORDER BY f.hop, f.id, f.from_node, f.kind, f.weight;\n        END;\n        $fn$"
_GOLDEN_CTE_PATHS = "        CREATE FUNCTION api.graph_paths(\n          from_id text,\n          to_id text,\n          max_hops int DEFAULT 4,\n          rel_types text[] DEFAULT NULL,\n          k int DEFAULT 10\n        ) RETURNS TABLE (node_id text, label text, name text, state text, archived boolean, from_node text, to_node text, kind text, weight text, path_rank int, score float8)\n        LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, graph, public, pg_temp AS $fn$\n        DECLARE\n          capped_k        int := least(greatest(k, 1), 50);\n          capped_hops     int := least(greatest(max_hops, 1), 8);\n          distances       jsonb;\n          target_frontier text[];\n          back_hops       int := 0;\n          frontier_ids    text[];\n          step_edges      jsonb;\n          bound           int := 0;\n          depth           int;\n          prefixes        jsonb;\n          extended        jsonb;\n          routes_found    jsonb := '[]'::jsonb;\n        BEGIN\n          IF from_id IS NULL OR to_id IS NULL OR from_id = to_id THEN\n            RETURN;\n          END IF;\n          distances := jsonb_build_object(to_id, 0);\n          target_frontier := ARRAY[to_id];\n          WHILE bound < capped_hops LOOP\n            bound := bound + 1;\n            WHILE back_hops < bound AND cardinality(target_frontier) > 0 LOOP\n              back_hops := back_hops + 1;\n            SELECT coalesce(jsonb_agg(jsonb_build_object('src', e.src, 'dst', e.dst, 'rel', e.rel, 'p0', e.p0)), '[]'::jsonb)\n              INTO step_edges\n              FROM (\n                SELECT DISTINCT e.from_node AS src, e.to_node AS dst, e.kind AS rel, e.weight AS p0\n                FROM graph.links e\n                WHERE e.to_node = ANY(target_frontier)\n                  AND (rel_types IS NULL OR cardinality(rel_types) = 0 OR e.kind = ANY(rel_types))\n              ) AS e;\n              SELECT coalesce(array_agg(DISTINCT s.src), ARRAY[]::text[]) INTO target_frontier\n                FROM jsonb_to_recordset(step_edges) AS s(src text)\n                WHERE NOT distances ? s.src;\n              distances := distances || (\n                SELECT coalesce(jsonb_object_agg(f.node, back_hops), jsonb_build_object()) FROM unnest(target_frontier) AS f(node)\n              );\n            END LOOP;\n            EXIT WHEN NOT distances ? from_id AND cardinality(target_frontier) = 0;\n            CONTINUE WHEN NOT distances ? from_id;\n            prefixes := jsonb_build_array(jsonb_build_object('path', jsonb_build_array(from_id), 'rels', jsonb_build_array(NULL::text), 'weights', jsonb_build_array(NULL::text)));\n            routes_found := '[]'::jsonb;\n            depth := 0;\n            WHILE depth < bound AND jsonb_array_length(prefixes) > 0 LOOP\n              depth := depth + 1;\n              frontier_ids := ARRAY(SELECT DISTINCT p.path[cardinality(p.path)] FROM jsonb_to_recordset(prefixes) AS p(path text[]));\n            SELECT coalesce(jsonb_agg(jsonb_build_object('src', e.src, 'dst', e.dst, 'rel', e.rel, 'p0', e.p0)), '[]'::jsonb)\n              INTO step_edges\n              FROM (\n                SELECT DISTINCT e.from_node AS src, e.to_node AS dst, e.kind AS rel, e.weight AS p0\n                FROM graph.links e\n                WHERE e.from_node = ANY(frontier_ids)\n                  AND (rel_types IS NULL OR cardinality(rel_types) = 0 OR e.kind = ANY(rel_types))\n              ) AS e;\n              SELECT coalesce(jsonb_agg(jsonb_build_object('path', p.path || s.dst, 'rels', p.rels || s.rel, 'weights', p.weights || s.p0)), '[]'::jsonb) INTO extended\n                FROM jsonb_to_recordset(prefixes) AS p(path text[], rels text[], weights text[])\n                JOIN jsonb_to_recordset(step_edges) AS s(src text, dst text, rel text, p0 text) ON s.src = p.path[cardinality(p.path)]\n                WHERE NOT s.dst = ANY(p.path)\n                  AND depth + coalesce((distances ->> s.dst)::int, bound + 1) <= bound;\n              routes_found := routes_found || (\n                SELECT coalesce(jsonb_agg(x.route || jsonb_build_object('hop', depth)), '[]'::jsonb)\n                FROM jsonb_array_elements(extended) AS x(route) WHERE x.route -> 'path' ->> depth = to_id\n              );\n              prefixes := (\n                SELECT coalesce(jsonb_agg(x.route), '[]'::jsonb)\n                FROM jsonb_array_elements(extended) AS x(route) WHERE x.route -> 'path' ->> depth <> to_id\n              );\n            END LOOP;\n            EXIT WHEN jsonb_array_length(routes_found) > capped_k;\n          END LOOP;\n          RETURN QUERY\n          WITH routes AS (\n            SELECT r.path, r.rels, r.weights, r.hop\n            FROM jsonb_to_recordset(routes_found) AS r(path text[], rels text[], weights text[], hop int)\n          ),\n          complete AS (\n            SELECT DISTINCT r.path, r.rels, r.weights, r.hop\n            FROM routes r\n          ),\n          selected_paths AS (\n            SELECT cand.path, cand.rels, cand.weights, cand.hop,\n                   row_number() OVER (ORDER BY cand.hop, cand.path, cand.rels, cand.weights) AS path_rank,\n                   (1.0 / (1 + cand.hop))::float8 AS score\n            FROM complete cand\n            ORDER BY cand.hop, cand.path, cand.rels, cand.weights\n            LIMIT capped_k + 1\n          )\n          SELECT step.node_id AS node_id,\n                 c.label,\n                 c.name,\n                 c.state,\n                 c.archived,\n                 cp.path[step.ord - 1] AS from_node,\n                 step.node_id AS to_node,\n                 cp.rels[step.ord] AS kind,\n                 cp.weights[step.ord] AS weight,\n                 cp.path_rank::int,\n                 cp.score\n          FROM selected_paths cp\n          CROSS JOIN LATERAL unnest(cp.path) WITH ORDINALITY AS step(node_id, ord)\n          LEFT JOIN graph.entities c ON c.node_id = step.node_id\n          ORDER BY cp.path_rank, step.ord;\n        END;\n        $fn$"
_GOLDEN_AGE_EXPAND = "        CREATE FUNCTION api.graph_expand(\n          seed_ids text[],\n          hops int DEFAULT 2,\n          rel_types text[] DEFAULT NULL,\n          k int DEFAULT 20\n        ) RETURNS TABLE (node_id text, label text, name text, state text, archived boolean, from_node text, to_node text, kind text, weight text)\n        LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, ag_catalog, graph, public, pg_temp AS $fn$\n        DECLARE\n          capped_k      int := least(greatest(k, 1), 50);\n          capped_hops   int := least(greatest(hops, 1), 6);\n          frontier_ids  text[];\n          seen          jsonb;\n          seen_count    int;\n          step_edges    jsonb;\n          walked_hops   int := 0;\n          fresh_edges   jsonb;\n          arrival_edges jsonb := '[]'::jsonb;\n        BEGIN\n          frontier_ids := ARRAY(SELECT DISTINCT s.seed FROM unnest(seed_ids) AS s(seed) WHERE s.seed IS NOT NULL);\n          seen := (SELECT coalesce(jsonb_object_agg(f.node, 0), jsonb_build_object()) FROM unnest(frontier_ids) AS f(node));\n          seen_count := cardinality(frontier_ids);\n          WHILE walked_hops < capped_hops AND cardinality(frontier_ids) > 0 AND seen_count <= capped_k LOOP\n            walked_hops := walked_hops + 1;\n            EXECUTE $exec$\n              SELECT coalesce(jsonb_agg(jsonb_build_object('src', e.src, 'dst', e.dst, 'rel', e.rel, 'p0', e.p0)), '[]'::jsonb)\n              FROM (\n                SELECT DISTINCT (g.src)::text AS src, (g.dst)::text AS dst, (g.rel)::text AS rel, (g.p0)::text AS p0\n                FROM cypher('demo_kg', $cy$\n                    MATCH (a:Node)-[r:REL]->(b:Node)\n                    WHERE a.id IN $frontier\n                    AND (size($rel_types) = 0 OR r.kind IN $rel_types)\n                    RETURN a.id AS src, b.id AS dst, r.kind AS rel, r.weight AS p0\n                $cy$, $1) AS g(src agtype, dst agtype, rel agtype, p0 agtype)\n              ) AS e\n            $exec$ INTO step_edges USING ag_catalog.agtype_build_map('frontier', to_jsonb(frontier_ids)::text::agtype, 'rel_types', to_jsonb(coalesce(rel_types, ARRAY[]::text[]))::text::agtype);\n            SELECT coalesce(jsonb_agg(jsonb_build_object('id', s.dst, 'hop', walked_hops, 'src', s.src, 'dst', s.dst, 'rel', s.rel, 'p0', s.p0)), '[]'::jsonb),\n                   coalesce(array_agg(DISTINCT s.dst), ARRAY[]::text[])\n              INTO fresh_edges, frontier_ids\n              FROM jsonb_to_recordset(step_edges) AS s(src text, dst text, rel text, p0 text)\n              WHERE NOT seen ? s.dst;\n            arrival_edges := arrival_edges || fresh_edges;\n            seen := seen || (\n              SELECT coalesce(jsonb_object_agg(f.node, walked_hops), jsonb_build_object()) FROM unnest(frontier_ids) AS f(node)\n            );\n            seen_count := seen_count + cardinality(frontier_ids);\n          END LOOP;\n          RETURN QUERY\n          WITH walk AS (\n            SELECT s.seed AS id, 0 AS hop, NULL::text AS from_node, NULL::text AS kind, NULL::text AS weight\n            FROM unnest(seed_ids) AS s(seed)\n            UNION ALL\n            SELECT a.id, a.hop, a.src AS from_node, a.rel AS kind, a.p0 AS weight\n            FROM jsonb_to_recordset(arrival_edges) AS a(id text, hop int, src text, dst text, rel text, p0 text)\n          ),\n          reached AS (\n            SELECT DISTINCT w.id, w.hop, w.from_node, w.kind, w.weight\n            FROM walk w\n          ),\n          arrivals AS (\n            SELECT r.id, min(r.hop) AS hop\n            FROM reached r\n            GROUP BY r.id\n          ),\n          selected_nodes AS (\n            SELECT a.id, a.hop\n            FROM arrivals a\n            ORDER BY a.hop, a.id\n            LIMIT capped_k + 1\n          ),\n          shortest AS (\n            SELECT r.id, r.hop, r.from_node, r.kind, r.weight\n            FROM reached r\n            JOIN selected_nodes s ON s.id = r.id AND s.hop = r.hop\n          )\n          SELECT f.id,\n                 c.label,\n                 c.name,\n                 c.state,\n                 c.archived,\n                 f.from_node,\n                 f.id AS to_node,\n                 f.kind,\n                 f.weight\n          FROM shortest f\n          LEFT JOIN graph.entities c ON c.node_id = f.id\n          ORDER BY f.hop, f.id, f.from_node, f.kind, f.weight;\n        END;\n        $fn$"
_GOLDEN_AGE_PATHS = "        CREATE FUNCTION api.graph_paths(\n          from_id text,\n          to_id text,\n          max_hops int DEFAULT 4,\n          rel_types text[] DEFAULT NULL,\n          k int DEFAULT 10\n        ) RETURNS TABLE (node_id text, label text, name text, state text, archived boolean, from_node text, to_node text, kind text, weight text, path_rank int, score float8)\n        LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, ag_catalog, graph, public, pg_temp AS $fn$\n        DECLARE\n          capped_k        int := least(greatest(k, 1), 50);\n          capped_hops     int := least(greatest(max_hops, 1), 8);\n          distances       jsonb;\n          target_frontier text[];\n          back_hops       int := 0;\n          frontier_ids    text[];\n          step_edges      jsonb;\n          bound           int := 0;\n          depth           int;\n          prefixes        jsonb;\n          extended        jsonb;\n          routes_found    jsonb := '[]'::jsonb;\n        BEGIN\n          IF from_id IS NULL OR to_id IS NULL OR from_id = to_id THEN\n            RETURN;\n          END IF;\n          distances := jsonb_build_object(to_id, 0);\n          target_frontier := ARRAY[to_id];\n          WHILE bound < capped_hops LOOP\n            bound := bound + 1;\n            WHILE back_hops < bound AND cardinality(target_frontier) > 0 LOOP\n              back_hops := back_hops + 1;\n            EXECUTE $exec$\n              SELECT coalesce(jsonb_agg(jsonb_build_object('src', e.src, 'dst', e.dst, 'rel', e.rel, 'p0', e.p0)), '[]'::jsonb)\n              FROM (\n                SELECT DISTINCT (g.src)::text AS src, (g.dst)::text AS dst, (g.rel)::text AS rel, (g.p0)::text AS p0\n                FROM cypher('demo_kg', $cy$\n                    MATCH (a:Node)-[r:REL]->(b:Node)\n                    WHERE b.id IN $frontier\n                    AND (size($rel_types) = 0 OR r.kind IN $rel_types)\n                    RETURN a.id AS src, b.id AS dst, r.kind AS rel, r.weight AS p0\n                $cy$, $1) AS g(src agtype, dst agtype, rel agtype, p0 agtype)\n              ) AS e\n            $exec$ INTO step_edges USING ag_catalog.agtype_build_map('frontier', to_jsonb(target_frontier)::text::agtype, 'rel_types', to_jsonb(coalesce(rel_types, ARRAY[]::text[]))::text::agtype);\n              SELECT coalesce(array_agg(DISTINCT s.src), ARRAY[]::text[]) INTO target_frontier\n                FROM jsonb_to_recordset(step_edges) AS s(src text)\n                WHERE NOT distances ? s.src;\n              distances := distances || (\n                SELECT coalesce(jsonb_object_agg(f.node, back_hops), jsonb_build_object()) FROM unnest(target_frontier) AS f(node)\n              );\n            END LOOP;\n            EXIT WHEN NOT distances ? from_id AND cardinality(target_frontier) = 0;\n            CONTINUE WHEN NOT distances ? from_id;\n            prefixes := jsonb_build_array(jsonb_build_object('path', jsonb_build_array(from_id), 'rels', jsonb_build_array(NULL::text), 'weights', jsonb_build_array(NULL::text)));\n            routes_found := '[]'::jsonb;\n            depth := 0;\n            WHILE depth < bound AND jsonb_array_length(prefixes) > 0 LOOP\n              depth := depth + 1;\n              frontier_ids := ARRAY(SELECT DISTINCT p.path[cardinality(p.path)] FROM jsonb_to_recordset(prefixes) AS p(path text[]));\n            EXECUTE $exec$\n              SELECT coalesce(jsonb_agg(jsonb_build_object('src', e.src, 'dst', e.dst, 'rel', e.rel, 'p0', e.p0)), '[]'::jsonb)\n              FROM (\n                SELECT DISTINCT (g.src)::text AS src, (g.dst)::text AS dst, (g.rel)::text AS rel, (g.p0)::text AS p0\n                FROM cypher('demo_kg', $cy$\n                    MATCH (a:Node)-[r:REL]->(b:Node)\n                    WHERE a.id IN $frontier\n                    AND (size($rel_types) = 0 OR r.kind IN $rel_types)\n                    RETURN a.id AS src, b.id AS dst, r.kind AS rel, r.weight AS p0\n                $cy$, $1) AS g(src agtype, dst agtype, rel agtype, p0 agtype)\n              ) AS e\n            $exec$ INTO step_edges USING ag_catalog.agtype_build_map('frontier', to_jsonb(frontier_ids)::text::agtype, 'rel_types', to_jsonb(coalesce(rel_types, ARRAY[]::text[]))::text::agtype);\n              SELECT coalesce(jsonb_agg(jsonb_build_object('path', p.path || s.dst, 'rels', p.rels || s.rel, 'weights', p.weights || s.p0)), '[]'::jsonb) INTO extended\n                FROM jsonb_to_recordset(prefixes) AS p(path text[], rels text[], weights text[])\n                JOIN jsonb_to_recordset(step_edges) AS s(src text, dst text, rel text, p0 text) ON s.src = p.path[cardinality(p.path)]\n                WHERE NOT s.dst = ANY(p.path)\n                  AND depth + coalesce((distances ->> s.dst)::int, bound + 1) <= bound;\n              routes_found := routes_found || (\n                SELECT coalesce(jsonb_agg(x.route || jsonb_build_object('hop', depth)), '[]'::jsonb)\n                FROM jsonb_array_elements(extended) AS x(route) WHERE x.route -> 'path' ->> depth = to_id\n              );\n              prefixes := (\n                SELECT coalesce(jsonb_agg(x.route), '[]'::jsonb)\n                FROM jsonb_array_elements(extended) AS x(route) WHERE x.route -> 'path' ->> depth <> to_id\n              );\n            END LOOP;\n            EXIT WHEN jsonb_array_length(routes_found) > capped_k;\n          END LOOP;\n          RETURN QUERY\n          WITH routes AS (\n            SELECT r.path, r.rels, r.weights, r.hop\n            FROM jsonb_to_recordset(routes_found) AS r(path text[], rels text[], weights text[], hop int)\n          ),\n          complete AS (\n            SELECT DISTINCT r.path, r.rels, r.weights, r.hop\n            FROM routes r\n          ),\n          selected_paths AS (\n            SELECT cand.path, cand.rels, cand.weights, cand.hop,\n                   row_number() OVER (ORDER BY cand.hop, cand.path, cand.rels, cand.weights) AS path_rank,\n                   (1.0 / (1 + cand.hop))::float8 AS score\n            FROM complete cand\n            ORDER BY cand.hop, cand.path, cand.rels, cand.weights\n            LIMIT capped_k + 1\n          )\n          SELECT step.node_id AS node_id,\n                 c.label,\n                 c.name,\n                 c.state,\n                 c.archived,\n                 cp.path[step.ord - 1] AS from_node,\n                 step.node_id AS to_node,\n                 cp.rels[step.ord] AS kind,\n                 cp.weights[step.ord] AS weight,\n                 cp.path_rank::int,\n                 cp.score\n          FROM selected_paths cp\n          CROSS JOIN LATERAL unnest(cp.path) WITH ORDINALITY AS step(node_id, ord)\n          LEFT JOIN graph.entities c ON c.node_id = step.node_id\n          ORDER BY cp.path_rank, step.ord;\n        END;\n        $fn$"


def _graph() -> GraphSchema:
    """A generic directed graph: `graph.links` edges walked, `graph.entities` joined for node detail."""
    return GraphSchema(
        edge_table="graph.links",
        src_col="from_node",
        dst_col="to_node",
        rel_col="kind",
        edge_payload_cols=("weight",),
        node_view="graph.entities",
        node_cols=("node_id", "label", "name", "state", "archived"),
        node_col_types=("text", "text", "text", "text", "boolean"),
    )


def _partitioned_graph() -> GraphSchema:
    """The generic graph fixture with a partition column set on the edge table's source-collection column."""
    return dataclasses.replace(_graph(), partition_col="src_collection")


_BUILDERS: list[BaseGraphTraversalBuilder] = [CteGraphTraversalBuilder(), AgeGraphTraversalBuilder("demo_kg")]


@pytest.mark.parametrize("builder", _BUILDERS)
def test_expand_generates_the_invariant_scaffold(builder: BaseGraphTraversalBuilder) -> None:
    body = builder.expand_body(_graph())
    assert body.startswith("        CREATE FUNCTION api.graph_expand(")
    for parameter in _EXPAND_SIGNATURE:
        assert parameter in body
    assert f"RETURNS TABLE ({_EXPAND_RETURNS})" in body
    assert "SECURITY DEFINER" in body
    assert "least(greatest(k, 1), 50)" in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_paths_generates_the_invariant_scaffold(builder: BaseGraphTraversalBuilder) -> None:
    body = builder.paths_body(_graph())
    assert body.startswith("        CREATE FUNCTION api.graph_paths(")
    for parameter in _PATHS_SIGNATURE:
        assert parameter in body
    assert f"RETURNS TABLE ({_PATHS_RETURNS})" in body
    assert "SECURITY DEFINER" in body
    assert "least(greatest(k, 1), 50)" in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_reach_generates_the_invariant_scaffold(builder: BaseGraphTraversalBuilder) -> None:
    body = builder.reach_body(_graph())
    assert body.startswith("        CREATE FUNCTION api.graph_reach(")
    for parameter in _REACH_SIGNATURE:
        assert parameter in body
    assert "RETURNS TABLE (reached_nodes int, deepest_hop int, target_hop int, complete boolean)" in body
    assert "SECURITY DEFINER" in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_reach_stops_at_the_target_at_its_node_limit_and_when_nothing_new_is_reached(builder: BaseGraphTraversalBuilder) -> None:
    body = builder.reach_body(_graph())
    assert "IF target_id IS NOT NULL AND seen ? target_id THEN\n              RETURN QUERY SELECT seen_count, walked_hops, (seen ->> target_id)::int, false;" in body
    assert "IF cardinality(frontier_ids) = 0 THEN\n              RETURN QUERY SELECT seen_count, greatest(walked_hops - 1, 0), NULL::int, true;" in body
    assert "IF seen_count >= 10000 THEN\n              RETURN QUERY SELECT seen_count, walked_hops, NULL::int, false;" in body
    assert "hops" not in body.split("BEGIN", 1)[0].replace("walked_hops", "")  # no hop bound


@pytest.mark.parametrize("builder", _BUILDERS)
def test_expand_and_paths_share_the_node_and_edge_returns(builder: BaseGraphTraversalBuilder) -> None:
    # The paths RETURNS is exactly the expand RETURNS plus the path-rank group and score.
    graph = _graph()
    assert f"RETURNS TABLE ({_EXPAND_RETURNS})" in builder.expand_body(graph)
    assert _EXPAND_RETURNS in builder.paths_body(graph)


@pytest.mark.parametrize("builder", _BUILDERS)
def test_no_placeholder_leaks_into_the_generated_bodies(builder: BaseGraphTraversalBuilder) -> None:
    graph = _graph()
    for body in (builder.expand_body(graph), builder.paths_body(graph), builder.reach_body(graph), builder.refresh_body(graph)):
        assert "{" not in body
        assert "}" not in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_generation_is_deterministic(builder: BaseGraphTraversalBuilder) -> None:
    graph = _graph()
    assert builder.expand_body(graph) == builder.expand_body(graph)
    assert builder.paths_body(graph) == builder.paths_body(graph)
    assert builder.reach_body(graph) == builder.reach_body(graph)
    assert builder.create_graph_ddl(graph) == builder.create_graph_ddl(graph)


@pytest.mark.parametrize("builder", _BUILDERS)
def test_function_name_defaults_to_the_conventional_api_but_is_overridable(builder: BaseGraphTraversalBuilder) -> None:
    # The function name defaults to the conventional api.graph_* names a consumer's handlers bind to,
    # but is overridable per function (mirroring the hybrid-search builder's function_name argument).
    graph = _graph()
    assert builder.expand_body(graph).startswith("        CREATE FUNCTION api.graph_expand(")
    assert builder.paths_body(graph).startswith("        CREATE FUNCTION api.graph_paths(")
    assert builder.expand_body(graph, function_name="reader.expand").startswith("        CREATE FUNCTION reader.expand(")
    assert builder.paths_body(graph, function_name="reader.paths").startswith("        CREATE FUNCTION reader.paths(")
    assert builder.reach_body(graph).startswith("        CREATE FUNCTION api.graph_reach(")
    assert builder.reach_body(graph, function_name="reader.reach").startswith("        CREATE FUNCTION reader.reach(")


@pytest.mark.parametrize("builder", _BUILDERS)
def test_node_col_types_render_the_returns_column_types(builder: BaseGraphTraversalBuilder) -> None:
    # The last node column is boolean per the schema; the rest are text.
    body = builder.expand_body(_graph())
    assert "state text, archived boolean," in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_omitting_node_col_types_defaults_every_node_column_to_text(builder: BaseGraphTraversalBuilder) -> None:
    graph = dataclasses.replace(_graph(), node_col_types=())
    body = builder.expand_body(graph)
    assert "state text, archived text," in body  # the boolean is no longer declared; all node cols are text


@pytest.mark.parametrize("builder", _BUILDERS)
def test_both_bodies_join_the_node_view_for_detail(builder: BaseGraphTraversalBuilder) -> None:
    # Every query body joins the node view for a reached node's display columns, on both backends
    # (the CTE walks the edge relation directly, the AGE body traverses the projected graph).
    graph = _graph()
    for body in (builder.expand_body(graph), builder.paths_body(graph)):
        assert "graph.entities" in body


# What each generated body tests for "the caller named no relations", per backend. The AGE step states the rule in
# cypher over a list that is never NULL, the CTE step in SQL over an argument that may be.
_NO_RELATION_FILTER = {
    "CteGraphTraversalBuilder": "(rel_types IS NULL OR cardinality(rel_types) = 0 OR e.kind = ANY(rel_types))",
    "AgeGraphTraversalBuilder": "(size($rel_types) = 0 OR r.kind IN $rel_types)",
}

_WALKS = ("expand_body", "paths_body", "reach_body")


@pytest.mark.parametrize("builder", _BUILDERS)
def test_an_empty_relation_list_reads_as_no_filter_on_every_body(builder: BaseGraphTraversalBuilder) -> None:
    # An unset argument and an empty list mean the same thing on both backends and in every walk,
    # so a raw SQL caller passing ARRAY[]::text[] reaches exactly what one passing NULL reaches.
    graph = _graph()
    for method in _WALKS:
        body = getattr(builder, method)(graph)
        assert _NO_RELATION_FILTER[type(builder).__name__] in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_no_walk_lists_paths_through_a_recursive_query_or_a_variable_length_match(builder: BaseGraphTraversalBuilder) -> None:
    for method in _WALKS:
        body = getattr(builder, method)(_graph())
        assert "WITH RECURSIVE" not in body
        assert "[*" not in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_every_forward_walk_keeps_only_edges_into_a_node_not_yet_reached(builder: BaseGraphTraversalBuilder) -> None:
    for method in ("expand_body", "reach_body"):
        body = getattr(builder, method)(_graph())
        assert "WHERE NOT seen ? s.dst;" in body
        assert "seen := seen || (" in body


def test_cte_steps_read_the_edge_relation_from_the_newest_nodes() -> None:
    builder = CteGraphTraversalBuilder()
    for method in ("expand_body", "reach_body"):
        body = getattr(builder, method)(_graph())
        assert "FROM graph.links e" in body
        assert "WHERE e.from_node = ANY(frontier_ids)" in body
        # The CTE backend is pure SQL, never AGE's cypher / agtype grammar.
        assert "cypher(" not in body
        assert "agtype" not in body


def test_paths_walk_back_from_the_target_then_deepen_a_bound() -> None:
    body = CteGraphTraversalBuilder().paths_body(_graph())
    assert "WHERE e.to_node = ANY(target_frontier)" in body  # the walk back reads edges by their destination
    assert "WHERE NOT distances ? s.src;" in body
    assert "WHERE NOT s.dst = ANY(p.path)" in body  # a route never visits a node twice
    assert "AND depth + coalesce((distances ->> s.dst)::int, bound + 1) <= bound;" in body  # the target stays within the bound
    assert "EXIT WHEN jsonb_array_length(routes_found) > capped_k;" in body  # a longer route ranks after every shorter one
    assert "CROSS JOIN LATERAL unnest" in body  # each ranked path unnested to one row per node


def test_cte_needs_no_projection_and_refresh_is_a_noop() -> None:
    builder = CteGraphTraversalBuilder()
    graph = _graph()
    assert builder.create_graph_ddl(graph) == []
    refresh = builder.refresh_body(graph)
    assert "CREATE FUNCTION graph.refresh_graph()" in refresh  # placed in the edge relation's schema
    assert "RETURNS void" in refresh
    assert "RETURN;" in refresh
    assert "DELETE FROM" not in refresh
    assert "INSERT INTO" not in refresh


def test_age_steps_match_one_hop_with_a_bound_param() -> None:
    builder = AgeGraphTraversalBuilder("demo_kg")
    for method in _WALKS:
        body = getattr(builder, method)(_graph())
        assert "cypher('demo_kg'" in body
        assert "MATCH (a:Node)-[r:REL]->(b:Node)" in body
        assert "agtype" in body
        # The nested dollar-quote tags stay distinct, and the cypher 3rd arg is a bound positional param.
        assert "$fn$" in body
        assert "            EXECUTE $exec$\n" in body
        assert "$cy$, $1)" in body
        assert "'frontier', to_jsonb(frontier_ids)::text::agtype" in body or "'frontier', to_jsonb(target_frontier)" in body


def test_age_paths_walk_back_by_the_destination() -> None:
    body = AgeGraphTraversalBuilder("demo_kg").paths_body(_graph())
    assert "WHERE b.id IN $frontier" in body
    assert "'frontier', to_jsonb(target_frontier)::text::agtype" in body
    assert "WHERE a.id IN $frontier" in body


def test_age_projects_the_graph_and_refresh_rebuilds_it() -> None:
    builder = AgeGraphTraversalBuilder("demo_kg")
    graph = _graph()
    ddl = builder.create_graph_ddl(graph)
    assert any("CREATE EXTENSION IF NOT EXISTS age" in stmt for stmt in ddl)
    assert any("create_graph('demo_kg')" in stmt for stmt in ddl)
    assert any("graph.links" in stmt for stmt in ddl)  # projected from the relational edge source
    refresh = builder.refresh_body(graph)
    assert "CREATE FUNCTION graph.refresh_graph()" in refresh
    assert 'DELETE FROM demo_kg."REL"' in refresh
    assert 'INSERT INTO demo_kg."Node"' in refresh
    assert "graph.links" in refresh


def test_age_graph_name_scopes_the_traversal_and_projection() -> None:
    # The graph name is a builder argument (the CTE backend has no analogue), threaded everywhere.
    builder = AgeGraphTraversalBuilder("other_kg")
    graph = _graph()
    assert "cypher('other_kg'" in builder.expand_body(graph)
    assert any("create_graph('other_kg')" in stmt for stmt in builder.create_graph_ddl(graph))


# -- Partitioning (unset path stays byte-identical; set path filters by collection) -----------------------------------


def test_cte_expand_unset_partition_col_is_byte_identical_to_the_golden_body() -> None:
    assert CteGraphTraversalBuilder().expand_body(_graph()) == _GOLDEN_CTE_EXPAND


def test_cte_paths_unset_partition_col_is_byte_identical_to_the_golden_body() -> None:
    assert CteGraphTraversalBuilder().paths_body(_graph()) == _GOLDEN_CTE_PATHS


def test_age_expand_unset_partition_col_is_byte_identical_to_the_golden_body() -> None:
    assert AgeGraphTraversalBuilder("demo_kg").expand_body(_graph()) == _GOLDEN_AGE_EXPAND


def test_age_paths_unset_partition_col_is_byte_identical_to_the_golden_body() -> None:
    assert AgeGraphTraversalBuilder("demo_kg").paths_body(_graph()) == _GOLDEN_AGE_PATHS


@pytest.mark.parametrize("builder", _BUILDERS)
def test_unset_partition_col_emits_no_collection_parameter_or_predicate(builder: BaseGraphTraversalBuilder) -> None:
    graph = _graph()
    for body in (builder.expand_body(graph), builder.paths_body(graph)):
        assert "p_collection" not in body
        assert "src_collection" not in body


def test_cte_expand_partitioned_gains_leading_collection_parameter_and_filters() -> None:
    body = CteGraphTraversalBuilder().expand_body(_partitioned_graph())
    assert body.startswith("        CREATE FUNCTION api.graph_expand(\n          p_collection text,\n          seed_ids text[],")
    assert "AND e.src_collection = p_collection" in body  # the edge walk stays inside the caller's collection
    assert "LEFT JOIN graph.entities c ON c.node_id = f.id AND c.src_collection = p_collection" in body  # seed/node reads too


def test_cte_paths_partitioned_gains_leading_collection_parameter_and_filters() -> None:
    body = CteGraphTraversalBuilder().paths_body(_partitioned_graph())
    assert body.startswith("        CREATE FUNCTION api.graph_paths(\n          p_collection text,\n          from_id text,")
    assert "AND e.src_collection = p_collection" in body
    assert "LEFT JOIN graph.entities c ON c.node_id = step.node_id AND c.src_collection = p_collection" in body


def test_cte_partitioned_generation_stays_deterministic() -> None:
    graph = _partitioned_graph()
    builder = CteGraphTraversalBuilder()
    assert builder.expand_body(graph) == builder.expand_body(graph)
    assert builder.paths_body(graph) == builder.paths_body(graph)


# -- AGE partitioning: composite vertex identity + the UNWIND/aggregate per-hop filter ----------------------------------


def test_age_node_projection_keys_vertices_by_id_and_collection_when_partitioned() -> None:
    ddl = AgeGraphTraversalBuilder("demo_kg").create_graph_ddl(_partitioned_graph())
    node_insert = next(stmt for stmt in ddl if 'INSERT INTO demo_kg."Node"' in stmt)
    assert "'id', n.id, 'collection', n.collection" in node_insert
    assert "SELECT from_node AS id, src_collection AS collection FROM graph.links" in node_insert
    assert "SELECT to_node AS id, dst_collection AS collection FROM graph.links" in node_insert


def test_age_node_projection_stays_bare_id_when_unpartitioned() -> None:
    ddl = AgeGraphTraversalBuilder("demo_kg").create_graph_ddl(_graph())
    node_insert = next(stmt for stmt in ddl if 'INSERT INTO demo_kg."Node"' in stmt)
    assert "collection" not in node_insert
    assert "'id', n.id)" in node_insert


def test_age_edge_projection_carries_collection_and_joins_the_composite_key_when_partitioned() -> None:
    ddl = AgeGraphTraversalBuilder("demo_kg").create_graph_ddl(_partitioned_graph())
    edge_insert = next(stmt for stmt in ddl if 'INSERT INTO demo_kg."REL"' in stmt)
    assert "'collection', e.src_collection" in edge_insert
    assert (
        "agtype_access_operator(a.properties, '\"collection\"'::agtype) = to_jsonb(e.src_collection)::text::agtype" in edge_insert
    )
    assert (
        "agtype_access_operator(b.properties, '\"collection\"'::agtype) = to_jsonb(e.dst_collection)::text::agtype" in edge_insert
    )


def test_age_edge_projection_has_no_collection_property_or_join_when_unpartitioned() -> None:
    ddl = AgeGraphTraversalBuilder("demo_kg").create_graph_ddl(_graph())
    edge_insert = next(stmt for stmt in ddl if 'INSERT INTO demo_kg."REL"' in stmt)
    assert "collection" not in edge_insert


def test_age_refresh_rebuilds_composite_identity_when_partitioned() -> None:
    refresh = AgeGraphTraversalBuilder("demo_kg").refresh_body(_partitioned_graph())
    assert "'id', n.id, 'collection', n.collection" in refresh
    assert "e.from_node AS id, e.src_collection AS collection FROM graph.links e" in refresh
    assert "e2.to_node AS id, e2.dst_collection AS collection FROM graph.links e2" in refresh
    assert "'collection', e.src_collection" in refresh  # the edge's own property map
    assert (
        "agtype_access_operator(va.properties, '\"collection\"'::agtype) = to_jsonb(e.src_collection)::text::agtype" in refresh
    )
    assert (
        "agtype_access_operator(vb.properties, '\"collection\"'::agtype) = to_jsonb(e.dst_collection)::text::agtype" in refresh
    )


def test_age_refresh_stays_bare_id_when_unpartitioned() -> None:
    refresh = AgeGraphTraversalBuilder("demo_kg").refresh_body(_graph())
    assert "collection" not in refresh


def test_age_partitioned_walks_gain_a_leading_collection_parameter_and_keep_both_ends_of_every_hop_inside_it() -> None:
    builder = AgeGraphTraversalBuilder("demo_kg")
    graph = _partitioned_graph()
    for method, head in (
        ("expand_body", "api.graph_expand(\n          p_collection text,\n          seed_ids text[],"),
        ("paths_body", "api.graph_paths(\n          p_collection text,\n          from_id text,"),
        ("reach_body", "api.graph_reach(\n          p_collection text,\n          seed_ids text[],"),
    ):
        body = getattr(builder, method)(graph)
        assert body.startswith(f"        CREATE FUNCTION {head}")
        # Each hop keeps both of its ends in the caller's collection, so no walk passes through another collection's node.
        assert "AND a.collection = $p_collection\n                    AND b.collection = $p_collection\n" in body
        assert "'p_collection', to_jsonb(p_collection)::text::agtype" in body
        assert "'rel_types', to_jsonb(coalesce(rel_types, ARRAY[]::text[]))::text::agtype" in body
        # AGE 1.6.0 rejects these forms outright. The filter must never regress to them.
        assert "ALL(" not in body
        assert "ANY(n" not in body
    assert "ON c.node_id = f.id AND c.src_collection = p_collection" in builder.expand_body(graph)
    assert "ON c.node_id = step.node_id AND c.src_collection = p_collection" in builder.paths_body(graph)


def test_age_unset_partition_col_bodies_stay_byte_identical_to_the_golden_bodies() -> None:
    # Pinned above the guard-removal: the un-partitioned AGE bodies must render exactly as before.
    assert AgeGraphTraversalBuilder("demo_kg").expand_body(_graph()) == _GOLDEN_AGE_EXPAND
    assert AgeGraphTraversalBuilder("demo_kg").paths_body(_graph()) == _GOLDEN_AGE_PATHS


def test_age_partitioned_generation_stays_deterministic() -> None:
    graph = _partitioned_graph()
    builder = AgeGraphTraversalBuilder("demo_kg")
    assert builder.expand_body(graph) == builder.expand_body(graph)
    assert builder.paths_body(graph) == builder.paths_body(graph)
    assert builder.create_graph_ddl(graph) == builder.create_graph_ddl(graph)
    assert builder.refresh_body(graph) == builder.refresh_body(graph)


# -- Currency: the opt-in flag and the per-hop predicate both backends apply ------------------------------------------


def _current_graph() -> GraphSchema:
    """The generic graph fixture with the node view's currency column declared."""
    return dataclasses.replace(_graph(), superseded_col="archived")


def _current_partitioned_graph() -> GraphSchema:
    """The generic graph fixture with both a partition column and a currency column declared."""
    return dataclasses.replace(_partitioned_graph(), superseded_col="archived")


@pytest.mark.parametrize("builder", _BUILDERS)
def test_a_declared_currency_column_adds_the_opt_in_flag_to_both_signatures(builder: BaseGraphTraversalBuilder) -> None:
    graph = _current_graph()
    for body in (builder.expand_body(graph), builder.paths_body(graph)):
        assert "include_superseded boolean DEFAULT false" in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_no_currency_column_means_no_flag_and_no_predicate(builder: BaseGraphTraversalBuilder) -> None:
    graph = _graph()
    for body in (builder.expand_body(graph), builder.paths_body(graph)):
        assert "include_superseded" not in body
        assert "coalesce(n.archived" not in body
        assert "stale_ct" not in body


def test_cte_applies_the_currency_predicate_at_every_reached_node() -> None:
    builder = CteGraphTraversalBuilder()
    for body in (builder.expand_body(_current_graph()), builder.paths_body(_current_graph())):
        # The walk joins the node view on the edge it is about to take, so a stale node is never
        # stepped onto and nothing behind it is reached either.
        assert "LEFT JOIN graph.entities n ON n.node_id = e.to_node" in body
        assert "AND (include_superseded OR NOT coalesce(n.archived, false))" in body


def test_age_applies_the_currency_predicate_at_every_reached_node() -> None:
    builder = AgeGraphTraversalBuilder("demo_kg")
    for method in _WALKS:
        body = getattr(builder, method)(_current_graph())
        # A hop never arrives at a stale node, so a descendant behind one is never reached. The seed is never judged.
        assert "AND (NOT b.archived OR $include_superseded)\n" in body
        assert "'include_superseded', to_jsonb(include_superseded)::text::agtype" in body


def test_age_projects_the_currency_property_onto_every_vertex() -> None:
    builder = AgeGraphTraversalBuilder("demo_kg")
    graph = _current_graph()
    node_insert = next(stmt for stmt in builder.create_graph_ddl(graph) if 'INSERT INTO demo_kg."Node"' in stmt)
    for statement in (node_insert, builder.refresh_body(graph)):
        assert "'archived', coalesce(v.archived, false)" in statement
        assert "LEFT JOIN graph.entities v ON v.node_id = n.id" in statement


def _sourced_graph() -> GraphSchema:
    """The currency-aware partitioned fixture reading currency from a relation of its own."""
    return dataclasses.replace(_current_partitioned_graph(), currency_source="graph.all_entities")


def test_age_projects_currency_from_the_currency_source_when_one_is_named() -> None:
    builder = AgeGraphTraversalBuilder("demo_kg")
    graph = _sourced_graph()
    node_insert = next(stmt for stmt in builder.create_graph_ddl(graph) if 'INSERT INTO demo_kg."Node"' in stmt)
    for statement in (node_insert, builder.refresh_body(graph)):
        # The projection is written once for every partition and read back by every caller, so it
        # reads currency from the named relation rather than the node view.
        assert "LEFT JOIN graph.all_entities v ON v.node_id = n.id AND v.src_collection = n.collection" in statement
        assert "LEFT JOIN graph.entities v" not in statement


def test_the_currency_source_never_reaches_a_read_side_join() -> None:
    graph = _sourced_graph()
    bodies = [
        AgeGraphTraversalBuilder("demo_kg").expand_body(graph),
        AgeGraphTraversalBuilder("demo_kg").paths_body(graph),
        CteGraphTraversalBuilder().expand_body(graph),
        CteGraphTraversalBuilder().paths_body(graph),
    ]
    for body in bodies:
        assert "graph.all_entities" not in body, "a read joins the node view, which answers the session asking"


@pytest.mark.parametrize("builder", _BUILDERS)
def test_no_currency_source_renders_exactly_what_the_node_view_rendered(builder: BaseGraphTraversalBuilder) -> None:
    # Every descriptor written before the field existed leaves it unset, so its generated SQL — the
    # bodies a frozen migration replays — must not move by a byte.
    graph = _current_partitioned_graph()
    explicit = dataclasses.replace(graph, currency_source=graph.node_view)
    for method in ("expand_body", "paths_body", "refresh_body"):
        assert getattr(builder, method)(graph) == getattr(builder, method)(explicit)


# -- Deterministic selection: k+1 logical items, then every shortest arrival ------------------------------------------

@pytest.mark.parametrize("builder", _BUILDERS)
def test_expansion_selects_one_more_logical_node_than_the_cap(builder: BaseGraphTraversalBuilder) -> None:
    body = builder.expand_body(_graph())
    cap = "capped_k"
    assert "          capped_k      int := least(greatest(k, 1), 50);\n" in body
    # A hop past the one that reached more nodes than the cap selects could never be selected, so the walk stops there.
    assert "AND seen_count <= capped_k LOOP" in body
    assert "min(r.hop) AS hop" in body  # every node's shortest arrival, before anything is dropped
    selection = "          selected_nodes AS (\n            SELECT a.id, a.hop\n            FROM arrivals a\n"
    assert selection + f"            ORDER BY a.hop, a.id\n            LIMIT {cap} + 1\n          )," in body
    # The extra node is the probe that proves more exist; a bare row cap would hide it.
    assert f"LIMIT {cap}\n" not in body
    assert body.count("LIMIT ") == 1


@pytest.mark.parametrize("builder", _BUILDERS)
def test_expansion_joins_every_shortest_arrival_row_for_a_selected_node(builder: BaseGraphTraversalBuilder) -> None:
    body = builder.expand_body(_graph())
    # Selection picks nodes; the join then brings back every assertion that reached one at that hop,
    # so two parallel edges into the same node both survive.
    assert "          shortest AS (\n            SELECT r.id, r.hop, r.from_node, r.kind, r.weight\n            FROM reached r\n            JOIN selected_nodes s ON s.id = r.id AND s.hop = r.hop\n          )" in body
    assert "DISTINCT ON" not in body


@pytest.mark.parametrize("builder", _BUILDERS)
def test_expansion_orders_by_hop_node_source_relation_and_provenance(builder: BaseGraphTraversalBuilder) -> None:
    assert "          ORDER BY f.hop, f.id, f.from_node, f.kind, f.weight" in builder.expand_body(_graph())


@pytest.mark.parametrize("builder", _BUILDERS)
def test_paths_rank_by_hop_then_the_node_relation_and_provenance_sequences(builder: BaseGraphTraversalBuilder) -> None:
    body = builder.paths_body(_graph())
    cap = "capped_k"
    assert "row_number() OVER (ORDER BY cand.hop, cand.path, cand.rels, cand.weights) AS path_rank" in body
    assert f"            ORDER BY cand.hop, cand.path, cand.rels, cand.weights\n            LIMIT {cap} + 1\n" in body
    assert f"LIMIT {cap}\n" not in body
    assert body.count("LIMIT ") == 1


@pytest.mark.parametrize("builder", _BUILDERS)
def test_paths_keep_two_routes_with_the_same_nodes_but_different_assertions_apart(builder: BaseGraphTraversalBuilder) -> None:
    body = builder.paths_body(_graph())
    # The path key is the node sequence PLUS the relation and provenance sequences, so two authored
    # assertions between the same pair rank as two routes rather than collapsing into one.
    assert "          complete AS (\n            SELECT DISTINCT r.path, r.rels, r.weights, r.hop\n            FROM routes r\n          )," in body


def test_both_backends_render_byte_identical_bodies_but_for_the_one_hop() -> None:
    # The backends differ only in the statement that reads one hop, so a reviewer can diff the two generated functions
    # line by line.
    graph = _current_partitioned_graph()
    cte, age = CteGraphTraversalBuilder(), AgeGraphTraversalBuilder("demo_kg")
    for method in _WALKS:
        cte_body, age_body = getattr(cte, method)(graph), getattr(age, method)(graph)
        for frontier, backward in (("frontier_ids", False), ("target_frontier", True)):
            cte_body = cte_body.replace(cte._step(graph, frontier=frontier, backward=backward), "<step>\n")
            age_body = age_body.replace(age._step(graph, frontier=frontier, backward=backward), "<step>\n")
        assert cte_body.replace(cte._search_path(graph), "").replace("STABLE", "") == age_body.replace(
            age._search_path(graph), ""
        ).replace("VOLATILE", "")


@pytest.mark.parametrize("builder", _BUILDERS)
def test_a_descriptor_without_a_currency_column_keeps_its_argument_list(builder: BaseGraphTraversalBuilder) -> None:
    # An applied migration grants and drops its functions by their exact argument list, so a descriptor
    # that asks for no currency column must keep rendering the argument list those statements name.
    graph = _graph()
    assert builder.expand_body(graph).startswith(
        "        CREATE FUNCTION api.graph_expand(\n          seed_ids text[],\n          hops int DEFAULT 2,"
        "\n          rel_types text[] DEFAULT NULL,\n          k int DEFAULT 20\n        ) RETURNS TABLE ("
    )
    assert builder.paths_body(graph).startswith(
        "        CREATE FUNCTION api.graph_paths(\n          from_id text,\n          to_id text,\n          max_hops int DEFAULT 4,"
        "\n          rel_types text[] DEFAULT NULL,\n          k int DEFAULT 10\n        ) RETURNS TABLE ("
    )


@pytest.mark.parametrize("builder", _BUILDERS)
def test_the_currency_flag_is_the_last_argument_so_existing_calls_still_resolve(builder: BaseGraphTraversalBuilder) -> None:
    graph = _current_partitioned_graph()
    assert builder.expand_body(graph).startswith(
        "        CREATE FUNCTION api.graph_expand(\n          p_collection text,\n          seed_ids text[],\n          hops int DEFAULT 2,"
        "\n          rel_types text[] DEFAULT NULL,\n          k int DEFAULT 20,\n          include_superseded boolean DEFAULT false\n        )"
    )
    assert builder.paths_body(graph).startswith(
        "        CREATE FUNCTION api.graph_paths(\n          p_collection text,\n          from_id text,\n          to_id text,"
        "\n          max_hops int DEFAULT 4,\n          rel_types text[] DEFAULT NULL,\n          k int DEFAULT 10,"
        "\n          include_superseded boolean DEFAULT false\n        )"
    )
