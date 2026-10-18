r"""Unit specs for the link-graph backend seam (`engram.retrieval.graph_backend`).

No database and no network: every backend method returns **SQL statement lists**
(golden SQL) that the alembic migration executes one-by-one later — these tests
assert their *shape*, never run them. The vocabulary itself is pure
plumbing — a frozen :class:`GraphBackends` dataclass wrapping
`Mapping[GraphBackend, GraphBackendSql]` that dispatches by enum member (mirrors
:class:`SearchDialects` in the BM25 seam).

A hand-written :class:`FakeGraphBackend` structurally implements the
:class:`GraphBackendSql` Protocol so it is accepted by the vocabulary **without**
`cast(...)` — proving the port is structural (the real CTE / AGE backends need
no inheritance either). The two concrete backends are pinned for: the recursive
CTE + cycle guard + relation filter (CTE), the `cypher(` variable-length
traversal + relation filter (AGE), the `create_graph` projection DDL (AGE) vs
`[]` (CTE), and — the load-bearing contract — that BOTH backends' expand /
paths statement lists project the SAME ordered column set from the API contract.

The single-statement invariant: alembic runs over asyncpg, where one `op.execute`
may carry only ONE SQL command. So every element of every
returned list must be a single top-level command (the CREATE FUNCTION, then its
COMMENT, as separate elements) — asserted via :func:`_top_level_statement_count`.
"""

from __future__ import annotations

import re

from enum import StrEnum
from typing import TYPE_CHECKING, Literal

import pytest

from engram.retrieval.graph_backend import GraphBackend, GraphBackends
from engram.retrieval.graph_backend.age import AgeGraphBackend
from engram.retrieval.graph_backend.cte import CteGraphBackend

if TYPE_CHECKING:
    from engram.retrieval.graph_backend.backend import GraphBackendSql

# The API contract's RETURNS column order, reconciled with the queries.py consumer.
# Both backends must project exactly these, in order, from each
# function body — the parity guard. `api.graph_expand` returns one row per reached node
# carrying the edge that reached it (`GraphExpandHandler` hydrates a GraphNode from the
# node columns + a GraphEdge from the edge columns, skipping a seed row whose `rel` is
# NULL); `api.graph_paths` adds the `path_rank` group and per-path `score` the
# handler groups + ranks on. The names/order match `_graph_node` / `_graph_edge` /
# `_graph_path` in `engram.retrieval.queries` exactly.
_EXPAND_COLUMNS: tuple[str, ...] = ("id", "type", "title", "status", "superseded", "src_id", "dst_id", "rel", "provenance_claim")
_PATHS_COLUMNS: tuple[str, ...] = (*_EXPAND_COLUMNS, "path_rank", "score")


def _returns_table_columns(body: str) -> list[str]:
    """Extract the bare column names from a `RETURNS TABLE (...)` clause.

    Args:
        body: A `CREATE FUNCTION` DDL string with a `RETURNS TABLE (...)` clause.

    Returns:
        The column names in declaration order (the leading token of each
        `name type` pair), so the test can assert ordered parity against the
        API contract without coupling to the SQL types.
    """
    match = re.search(r"RETURNS TABLE\s*\(([^)]*)\)", body, re.DOTALL)
    assert match is not None, "no RETURNS TABLE (...) clause found"
    return [segment.split()[0].strip() for segment in match.group(1).split(",") if segment.strip()]


def _top_level_statement_count(statement: str) -> int:
    """Count the top-level SQL commands in `statement`, ignoring quoted spans.

    asyncpg rejects a prepared statement carrying more than one command, so the
    migration must hand each `op.execute` exactly one. A `;` *inside* a span the
    parser would not split on — a dollar-quoted `$tag$ ... $tag$` function body or a
    single-quoted `'...'` string literal (e.g. a COMMENT's prose, which embeds
    `;`) — is part of that one command, not a separator; both span page_types are
    stripped before counting. The dollar-quote tag is matched generically (`$$` and a
    named tag like `$fn$` are both valid Postgres syntax), and the AGE backend nests
    several distinct tags (`$fn$` wrapping `$exec$` wrapping the cypher `$cy$`): a
    properly-nested outermost `$fn$ ... $fn$` span is stripped whole, taking the inner
    tags and their `;` with it. A single trailing `;` is fine (it terminates the one
    command, leaving no second command after it).

    Args:
        statement: One SQL string as it would be passed to a single `op.execute`.

    Returns:
        The number of non-empty top-level commands separated by `;` outside any
        dollar-quoted body or string literal — `1` for a well-formed single statement.
    """
    # A dollar-quoted span opens with $tag$ and closes with the SAME tag ($$ when tag is empty);
    # the backreference pins the close to the open so a properly-nested outermost span (e.g. the
    # AGE $fn$ body wrapping $exec$/$cy$) is stripped in one match, inner tags and all.
    without_bodies = re.sub(r"\$(\w*)\$.*?\$\1\$", "", statement, flags=re.DOTALL)
    # SQL escapes a single quote by doubling it (''), so '(?:[^']|'')*' matches a whole literal.
    without_literals = re.sub(r"'(?:[^']|'')*'", "", without_bodies)
    return len([part for part in without_literals.split(";") if part.strip()])


def _assert_each_is_single_statement(statements: list[str]) -> None:
    """Assert every element of `statements` is exactly one top-level SQL command."""
    for statement in statements:
        count = _top_level_statement_count(statement)
        assert count == 1, f"expected one top-level command, found {count}: {statement[:80]!r}"


class FakeGraphBackend:
    """Structural stand-in for a real `GraphBackendSql`.

    `name` is a public attribute and the emitters return canned single-statement
    lists so the vocabulary can be exercised without a database.
    """

    def __init__(self, name: Literal["cte", "age"]) -> None:
        self.name: Literal["cte", "age"] = name

    def create_graph_ddl(self) -> list[str]:
        return [] if self.name == "cte" else [f"-- create graph for {self.name}"]

    def expand_statements(self) -> list[str]:
        return [f"-- api.graph_expand body for {self.name}"]

    def paths_statements(self) -> list[str]:
        return [f"-- api.graph_paths body for {self.name}"]

    def refresh_statements(self) -> list[str]:
        return [f"-- wiki.refresh_graph for {self.name}"]


class TestGraphBackendEnum:
    def test_has_exactly_cte_and_age(self):
        assert {member.value for member in GraphBackend} == {"cte", "age"}

    def test_is_a_strenum(self):
        assert issubclass(GraphBackend, StrEnum)
        assert GraphBackend.CTE == "cte"
        assert GraphBackend.AGE == "age"


class TestGraphBackends:
    def test_select_returns_the_registered_strategy_per_enum_member(self):
        cte, age = FakeGraphBackend("cte"), FakeGraphBackend("age")
        vocabulary = GraphBackends(by_key={GraphBackend.CTE: cte, GraphBackend.AGE: age})

        assert vocabulary.select(GraphBackend.CTE) is cte
        assert vocabulary.select(GraphBackend.AGE) is age

    def test_accepts_a_structural_fake_without_cast(self):
        # The fake satisfies the Protocol by shape alone.
        fake: GraphBackendSql = FakeGraphBackend("cte")
        vocabulary = GraphBackends(by_key={GraphBackend.CTE: fake})

        selected = vocabulary.select(GraphBackend.CTE)

        assert selected.name == "cte"
        assert selected.create_graph_ddl() == []
        assert selected.expand_statements() == ["-- api.graph_expand body for cte"]

    def test_select_propagates_keyerror_for_an_unregistered_backend(self):
        vocabulary = GraphBackends(by_key={GraphBackend.CTE: FakeGraphBackend("cte")})

        with pytest.raises(KeyError):
            vocabulary.select(GraphBackend.AGE)


class TestBuildGraphBackends:
    def test_maps_each_enum_member_to_its_concrete_backend(self):
        from engram.retrieval.graph_backend import build_graph_backends

        registry = build_graph_backends()

        assert isinstance(registry.select(GraphBackend.CTE), CteGraphBackend)
        assert isinstance(registry.select(GraphBackend.AGE), AgeGraphBackend)


class TestGraphBackendProtocol:
    def test_both_backends_satisfy_the_runtime_checkable_protocol_without_cast(self):
        from engram.retrieval.graph_backend.backend import GraphBackendSql

        assert isinstance(CteGraphBackend(), GraphBackendSql)
        assert isinstance(AgeGraphBackend(), GraphBackendSql)

    def test_names_are_the_backend_literals(self):
        assert CteGraphBackend().name == "cte"
        assert AgeGraphBackend().name == "age"


class TestSingleStatementInvariant:
    """asyncpg-safety: every emitted element is exactly ONE top-level SQL command.

    A multi-command ``op.execute`` (``CREATE FUNCTION ...; COMMENT ON FUNCTION ...;``) raises
    ``cannot insert multiple commands into a prepared statement`` on asyncpg, so each backend
    hands the migration lists whose every element is a single command — the CREATE FUNCTION and
    its COMMENT as separate elements — verified for both concrete backends.
    """

    def test_cte_emits_only_single_statements(self):
        backend = CteGraphBackend()

        _assert_each_is_single_statement(backend.create_graph_ddl())
        _assert_each_is_single_statement(backend.expand_statements())
        _assert_each_is_single_statement(backend.paths_statements())
        _assert_each_is_single_statement(backend.refresh_statements())

    def test_age_emits_only_single_statements(self):
        backend = AgeGraphBackend()

        _assert_each_is_single_statement(backend.create_graph_ddl())
        _assert_each_is_single_statement(backend.expand_statements())
        _assert_each_is_single_statement(backend.paths_statements())
        _assert_each_is_single_statement(backend.refresh_statements())

    def test_each_statement_list_separates_create_from_comment(self):
        # The CREATE FUNCTION and its COMMENT are distinct elements (≥2 each), so a
        # single op.execute never carries both — the asyncpg multi-command failure mode.
        for backend in (CteGraphBackend(), AgeGraphBackend()):
            expand, paths = backend.expand_statements(), backend.paths_statements()

            assert len(expand) >= 2
            assert len(paths) >= 2
            assert any("CREATE FUNCTION api.graph_expand(" in stmt for stmt in expand)
            assert any("COMMENT ON FUNCTION api.graph_expand" in stmt for stmt in expand)
            assert any("CREATE FUNCTION api.graph_paths(" in stmt for stmt in paths)
            assert any("COMMENT ON FUNCTION api.graph_paths" in stmt for stmt in paths)


class TestResultColumnParity:
    """The load-bearing contract: both backends project the SAME ordered columns."""

    def _expand_create(self, statements: list[str]) -> str:
        """Return the single `CREATE FUNCTION api.graph_expand` element of a list."""
        return next(stmt for stmt in statements if "CREATE FUNCTION api.graph_expand(" in stmt)

    def _paths_create(self, statements: list[str]) -> str:
        """Return the single `CREATE FUNCTION api.graph_paths` element of a list."""
        return next(stmt for stmt in statements if "CREATE FUNCTION api.graph_paths(" in stmt)

    def test_expand_bodies_share_the_api_contract_column_set(self):
        cte_cols = _returns_table_columns(self._expand_create(CteGraphBackend().expand_statements()))
        age_cols = _returns_table_columns(self._expand_create(AgeGraphBackend().expand_statements()))

        assert cte_cols == list(_EXPAND_COLUMNS)
        assert age_cols == list(_EXPAND_COLUMNS)
        assert cte_cols == age_cols

    def test_paths_bodies_share_the_api_contract_column_set(self):
        cte_cols = _returns_table_columns(self._paths_create(CteGraphBackend().paths_statements()))
        age_cols = _returns_table_columns(self._paths_create(AgeGraphBackend().paths_statements()))

        assert cte_cols == list(_PATHS_COLUMNS)
        assert age_cols == list(_PATHS_COLUMNS)
        assert cte_cols == age_cols

    def test_backend_contract_constants_match_the_consumer_column_set(self):
        # The documented source-of-truth tuples in backend.py must equal the consumer's
        # ordered column set (the same tuples the RETURNS clauses are asserted against),
        # so the contract constant cannot silently drift from what the bodies emit.
        from engram.retrieval.graph_backend.backend import EXPAND_COLUMNS, PATHS_COLUMNS

        assert EXPAND_COLUMNS == _EXPAND_COLUMNS
        assert PATHS_COLUMNS == _PATHS_COLUMNS


class TestCteGraphBackend:
    """The portable backend must emit a recursive CTE over `wiki.edges`."""

    def _expand(self) -> str:
        return next(stmt for stmt in CteGraphBackend().expand_statements() if "CREATE FUNCTION" in stmt)

    def _paths(self) -> str:
        return next(stmt for stmt in CteGraphBackend().paths_statements() if "CREATE FUNCTION" in stmt)

    def _refresh(self) -> str:
        return next(stmt for stmt in CteGraphBackend().refresh_statements() if "CREATE FUNCTION" in stmt)

    def test_create_graph_ddl_is_empty(self):
        # The CTE backend needs no graph projection — it walks wiki.edges directly.
        assert CteGraphBackend().create_graph_ddl() == []

    def test_expand_body_declares_graph_expand_with_the_contract_signature(self):
        body = self._expand()

        assert "CREATE FUNCTION api.graph_expand(" in body
        assert "seed_ids text[]" in body
        assert "hops int DEFAULT 2" in body
        assert "rel_types text[] DEFAULT NULL" in body
        assert "k int DEFAULT 20" in body

    def test_expand_body_walks_the_edges_one_hop_at_a_time_over_the_reached_set(self):
        body = self._expand()

        assert "RECURSIVE" not in body
        assert "FROM wiki.edges e" in body
        # A hop keeps only the edges into a node the walk has not reached yet.
        assert "WHERE NOT seen ? s.dst;" in body

    def test_expand_body_filters_by_optional_rel_types(self):
        body = self._expand()

        # NULL rel_types → every relation; non-NULL → membership filter.
        assert "rel_types IS NULL OR" in body
        assert "= ANY(rel_types)" in body

    def test_expand_body_joins_provenance_and_titles(self):
        # Titles + provenance come from the api.cards / api.claims surfaces.
        body = self._expand()

        assert "api.cards" in body
        assert "provenance_claim" in body

    def test_paths_body_declares_graph_paths_with_the_contract_signature(self):
        body = self._paths()

        assert "CREATE FUNCTION api.graph_paths(" in body
        assert "from_id text" in body
        assert "to_id text" in body
        assert "max_hops int DEFAULT 4" in body
        assert "rel_types text[] DEFAULT NULL" in body
        assert "k int DEFAULT 10" in body

    def test_paths_body_never_visits_a_node_twice_on_one_route(self):
        body = self._paths()

        assert "RECURSIVE" not in body
        assert "WHERE NOT s.dst = ANY(p.path)" in body

    def test_both_bodies_are_security_definer_and_k_capped(self):
        for body in (self._expand(), self._paths()):
            assert "SECURITY DEFINER" in body
            assert "least(greatest(k, 1), 50)" in body

    def test_bodies_comment_the_functions_for_the_m_schema(self):
        # The functions carry COMMENTs (the M-Schema agents read) even though the
        # GRANT is emitted by the migration, not here.
        expand_comments = [stmt for stmt in CteGraphBackend().expand_statements() if "COMMENT ON FUNCTION" in stmt]
        paths_comments = [stmt for stmt in CteGraphBackend().paths_statements() if "COMMENT ON FUNCTION" in stmt]

        assert any("COMMENT ON FUNCTION api.graph_expand" in stmt for stmt in expand_comments)
        assert any("COMMENT ON FUNCTION api.graph_paths" in stmt for stmt in paths_comments)

    def test_excludes_the_age_cypher_grammar(self):
        # The CTE backend is pure SQL — never AGE's cypher / agtype.
        for body in (self._expand(), self._paths()):
            assert "cypher(" not in body
            assert "agtype" not in body

    def test_refresh_graph_is_a_noop(self):
        # The CTE backend walks wiki.edges live, so refresh_graph() exists only so the index
        # load step can call it backend-agnostically — its body is an empty RETURN, no projection.
        body = self._refresh()

        assert "CREATE FUNCTION wiki.refresh_graph()" in body
        assert "RETURNS void" in body
        assert "RETURN;" in body
        # No projection writes under the CTE backend.
        assert "DELETE FROM" not in body
        assert "INSERT INTO" not in body


class TestAgeGraphBackend:
    """The AGE backend wraps openCypher variable-length traversal."""

    def _expand(self) -> str:
        return next(stmt for stmt in AgeGraphBackend().expand_statements() if "CREATE FUNCTION" in stmt)

    def _paths(self) -> str:
        return next(stmt for stmt in AgeGraphBackend().paths_statements() if "CREATE FUNCTION" in stmt)

    def _refresh(self) -> str:
        return next(stmt for stmt in AgeGraphBackend().refresh_statements() if "CREATE FUNCTION" in stmt)

    def test_create_graph_ddl_creates_the_age_extension_and_graph(self):
        ddl = AgeGraphBackend().create_graph_ddl()

        assert ddl != []
        # The extension, create_graph and projection are individual single statements.
        assert any("CREATE EXTENSION IF NOT EXISTS age" in stmt for stmt in ddl)
        assert any("create_graph('wiki_kg')" in stmt for stmt in ddl)
        # Projects nodes/edges from the relational source of truth.
        assert any("wiki.edges" in stmt for stmt in ddl)

    def test_expand_body_declares_graph_expand_with_the_contract_signature(self):
        body = self._expand()

        assert "CREATE FUNCTION api.graph_expand(" in body
        assert "seed_ids text[]" in body
        assert "hops int DEFAULT 2" in body
        assert "rel_types text[] DEFAULT NULL" in body
        assert "k int DEFAULT 20" in body

    def test_expand_body_matches_one_hop_in_cypher_at_a_time(self):
        body = self._expand()

        assert "cypher('wiki_kg'" in body
        assert "MATCH (a:Node)-[r:REL]->(b:Node)" in body
        assert "[*" not in body

    def test_expand_body_filters_relations(self):
        # The relation filter narrows the traversal to rel_types when supplied.
        body = self._expand()

        assert "rel_types" in body

    def test_paths_body_declares_graph_paths_with_the_contract_signature(self):
        body = self._paths()

        assert "CREATE FUNCTION api.graph_paths(" in body
        assert "from_id text" in body
        assert "to_id text" in body
        assert "max_hops int DEFAULT 4" in body
        assert "rel_types text[] DEFAULT NULL" in body
        assert "k int DEFAULT 10" in body

    def test_paths_body_matches_one_hop_in_cypher_at_a_time(self):
        body = self._paths()

        assert "cypher('wiki_kg'" in body
        assert "MATCH (a:Node)-[r:REL]->(b:Node)" in body
        assert "[*" not in body

    def test_both_bodies_are_security_definer_and_k_capped(self):
        for body in (self._expand(), self._paths()):
            assert "SECURITY DEFINER" in body
            assert "least(greatest(k, 1), 50)" in body

    def test_bodies_comment_the_functions_for_the_m_schema(self):
        expand_comments = [stmt for stmt in AgeGraphBackend().expand_statements() if "COMMENT ON FUNCTION" in stmt]
        paths_comments = [stmt for stmt in AgeGraphBackend().paths_statements() if "COMMENT ON FUNCTION" in stmt]

        assert any("COMMENT ON FUNCTION api.graph_expand" in stmt for stmt in expand_comments)
        assert any("COMMENT ON FUNCTION api.graph_paths" in stmt for stmt in paths_comments)

    def test_refresh_graph_projects_wiki_kg_from_edges(self):
        # The writer-only projection the index load step runs: clear the graph, then re-insert one
        # Node per distinct edge endpoint and one REL per wiki.edges row. SECURITY DEFINER with a
        # pinned search_path (it writes the ag_catalog-backed label tables as the index writer).
        body = self._refresh()

        assert "CREATE FUNCTION wiki.refresh_graph()" in body
        assert "RETURNS void" in body
        assert "SECURITY DEFINER" in body
        assert "SET search_path = pg_catalog, ag_catalog, wiki, public, pg_temp" in body
        # Clears then re-projects the graph from the relational source of truth.
        assert 'DELETE FROM wiki_kg."REL"' in body
        assert 'DELETE FROM wiki_kg."Node"' in body
        assert 'INSERT INTO wiki_kg."Node"' in body
        assert 'INSERT INTO wiki_kg."REL"' in body
        assert "wiki.edges" in body
