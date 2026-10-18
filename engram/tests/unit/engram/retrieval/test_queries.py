r"""Unit specs for the serve-slice query handlers (`src/engram/retrieval/queries.py`).

Driven entirely through fakes — :class:`FakeSqlGateway` (records the SQL + bound
params) and :class:`FakeEmbedding` (records every text embedded) — so there is no
database and no network. They pin the read-side contract:

* `SearchHandler` embeds the query **exactly once** (one `embed_batch` call,
  one text) and binds that vector into the `api.search` params when hybrid;
* `hybrid=False` → **no** embed call and a NULL query vector (the BM25-only leg);
* `k` / `level` / `facets` / `collection` are plumbed into the bound
  params (`facets` serialized to a JSON string), and `k` rides the gateway cap;
* the gateway's truncation message (`"… N more rows — narrow your filters"`)
  surfaces on the handler result unchanged;
* `FetchHandler` calls `api.fetch` with the **`unit_id`** param name the 0002
  migration declares (not `id`);
* `LookupValueHandler` passes `facet` + `term` to `api.lookup_value`;
* `DescribeSchemaHandler` renders the COMMENT M-Schema text (object + column
  comments) from `gateway.api_objects()`;
* `StatusHandler` reads `kb.*` row counts through the **admin** gateway, not the
  reader (`kb.*` is not granted to `kb_reader`).
* `KnowledgeBasesHandler` reads `kb.pages` grouped by `collection` (page count, page type mix) left-joined
  to `index_fingerprint` (last index time), also through the **admin** gateway.
"""

from __future__ import annotations

import importlib.util
import json
import re

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from core.ai.retrieval.contracts import (
    CteGraphTraversalBuilder,
    GraphPathsResult,
    GraphReach,
    KnowledgeBaseStamp,
    ParadeDBHybridSearchBuilder,
    ResultWindow,
)
from core.ai.retrieval.names import FIND_PATHS, GRAPH_EXPAND
from pydantic import ValidationError

from engram.retrieval.domain import (
    ColumnInfo,
    DescribeSchema,
    Fetch,
    Graph,
    GraphEdge,
    GraphExpand,
    GraphNode,
    GraphPath,
    GraphPaths,
    LookupValue,
    NextSteps,
    ObjectInfo,
    SchemaDescription,
    Search,
    SqlResult,
    Status,
)
from engram.retrieval.exceptions import DescribeSchemaRefusedError
from engram.retrieval.queries import (
    _GRAPH_ROW_CAP,
    DESCRIBE_SCHEMA_CATALOG_INSTRUCTION,
    GRAPH_EXPAND_SQL,
    GRAPH_PATHS_SQL,
    GRAPH_RESULT_CHARS,
    SCHEMA_OBJECT_LIMIT,
    SCHEMA_RESULT_CHARS,
    SEARCH_SQL,
    DescribeSchemaHandler,
    FetchHandler,
    GraphExpandHandler,
    GraphHandler,
    GraphPathsHandler,
    KnowledgeBasesHandler,
    KnowledgeBaseValidationHandler,
    LookupValueHandler,
    SearchHandler,
    StatusHandler,
    bound_graph_paths,
    bound_graph_result,
    graph_next_steps,
)
from engram.testing.fakes.embedding import FakeEmbedding, _vector
from engram.testing.fakes.gateway import FakeSqlGateway, RecordedQuery

if TYPE_CHECKING:
    from types import ModuleType

_HIT_COLUMNS = ("id", "hit_level", "page_id", "page_title", "snippet", "score")

# `engram/alembic/versions` — four levels up from `tests/unit/engram/retrieval/`, alongside the
# migration file `TestBakedOverrideConstantsMatchTheMigration` loads directly (no DB, no Docker).
_ALEMBIC_VERSIONS_DIR = Path(__file__).resolve().parents[4] / "alembic" / "versions"


def _load_migration_module(revision: str) -> ModuleType:
    """Import an alembic version file as a plain module, found by its revision-number filename prefix.

    Migration files aren't a normal importable package, so this execs the one `<revision>_*.py`
    under `alembic/versions` directly via `importlib`. Pure Python, no DB, no Docker — mirrors
    `tests/integration/test_migrations.py`'s `_load_migration_module`, kept local so the unit tier
    never imports from the integration tree.
    """
    matches = sorted(_ALEMBIC_VERSIONS_DIR.glob(f"{revision}_*.py"))
    assert len(matches) == 1, f"expected exactly one {revision}_*.py under {_ALEMBIC_VERSIONS_DIR}, found {matches}"
    spec = importlib.util.spec_from_file_location(f"_migration_{revision}", matches[0])
    assert spec is not None, f"no import spec for {matches[0]}"
    assert spec.loader is not None, f"import spec for {matches[0]} carries no loader"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _search_result() -> SqlResult:
    return SqlResult(rows=({"id": "paper.x#c1", "hit_level": "claim", "score": 0.5},), columns=_HIT_COLUMNS)


@dataclass(frozen=True)
class _FakePageType:
    """One page type, the slice `DescribeSchemaHandler`'s spec protocol needs: `page_type` + `id_prefix` + `decision_declared`."""

    page_type: str
    id_prefix: str = ""
    decision_declared: bool = False


@dataclass(frozen=True)
class _FakeSchemaSpec:
    """The `SchemaSpec` stand-in `DescribeSchemaHandler` reads, carrying the declared page types and the slug."""

    page_types: tuple[_FakePageType, ...]
    slug: str = "vault-x"


def _spec(page_types: tuple[str, ...]) -> _FakeSchemaSpec:
    """A `_FakeSchemaSpec` from plain page_type names, slugged `vault-x` for the knowledge-base-identity tests."""
    return _FakeSchemaSpec(page_types=tuple(_FakePageType(page_type=page_type) for page_type in page_types))


def _cards_schema() -> SchemaDescription:
    """`api.cards`, `api.edges`, `api.claims`, and `api.log_session` carrying the neutral comment text migration 0053 writes."""
    return SchemaDescription(
        objects=(
            ObjectInfo(
                name="api.cards",
                kind="view",
                comment=(
                    "L1 card scan (~50-150 tokens/row): one compact row per page across every page type. Filter by type, "
                    "exclude stubs/superseded, order by updated. Example: SELECT id, title, summary FROM "
                    "api.cards WHERE type = '<page-type>' AND NOT superseded LIMIT 20;"
                ),
                columns=(
                    ColumnInfo(name="id", data_type="text", comment="Page id, unique only paired with src_collection. Example value: '<page-id>'."),
                    ColumnInfo(name="type", data_type="text", comment="One of this knowledge base's page types. api.page_types lists them."),
                ),
            ),
            ObjectInfo(
                name="api.edges",
                kind="view",
                comment=(
                    "Typed relations between pages/claims, both endpoints named. Example: SELECT src_title, rel, dst_title "
                    "FROM api.edges WHERE rel = '<relation>' AND dst_id = '<page-id>' LIMIT 20;"
                ),
                columns=(),
            ),
            ObjectInfo(
                name="api.claims",
                kind="view",
                comment="Extracted claims, one row per claim.",
                columns=(
                    ColumnInfo(
                        name="fields",
                        data_type="jsonb",
                        comment=(
                            "jsonb: the claim's text fields, each declared by the knowledge base's claim grammar, keyed by field "
                            "name with the value as written. '{}' when the claim carries none."
                        ),
                    ),
                    ColumnInfo(
                        name="links",
                        data_type="jsonb",
                        comment=(
                            "jsonb: the claim's link fields, each declared by the knowledge base's claim grammar, keyed by field "
                            "name, each an array of its target pages' titles in written order."
                        ),
                    ),
                ),
            ),
            ObjectInfo(
                name="api.log_session",
                kind="function",
                comment=(
                    "Session telemetry: the only write the read-only role may perform (SECURITY DEFINER insert into "
                    "kb.query_log). Example: SELECT api.log_session('q?', 'compare', ARRAY['api.search'], "
                    "ARRAY['<page-id>'], 1200);"
                ),
                columns=(),
            ),
        )
    )


# The named-object tuples the tests below pass as `DescribeSchema(objects=...)` to reach FULL
# detail for a fixture's objects — catalog mode (no `objects`) only ever shows one-line purposes,
# so a test asserting grounded comment/column text must name every object its fixture carries.
_CARDS_OBJECTS = ("api.cards", "api.edges", "api.claims", "api.log_session")


class TestSearchHandler:
    async def test_the_date_range_binds_as_dates_and_an_undated_search_binds_nulls(self):
        gateway, embedder = FakeSqlGateway(result=_search_result()), FakeEmbedding(dimensions=4)
        handler = SearchHandler(gateway, embedder)

        await handler(Search(query="brine", collection="research", hybrid=False, since=date(2026, 9, 1), until=date(2026, 9, 30)))
        dated = gateway.last.params
        await handler(Search(query="brine", collection="research", hybrid=False))
        undated = gateway.last.params

        assert (dated["since"], dated["until"]) == (date(2026, 9, 1), date(2026, 9, 30))
        assert (undated["since"], undated["until"]) == (None, None)
        assert "since => CAST(:since AS date), until => CAST(:until AS date)" in gateway.last.sql

    async def test_hybrid_embeds_the_query_exactly_once_and_binds_the_vector(self):
        gateway, embedder = FakeSqlGateway(result=_search_result()), FakeEmbedding(dimensions=4)
        handler = SearchHandler(gateway, embedder)

        await handler(Search(query="graph rag token cost", k=8, collection="research"))

        assert embedder.batches == [["graph rag token cost"]]  # one batch, one text → embedded once
        params = gateway.last.params
        expected = _vector("graph rag token cost", 4)  # the deterministic fake vector
        assert params["qvec"] == _halfvec_literal(expected)
        assert "api.search" in gateway.last.sql
        assert "include_meta => false" in gateway.last.sql  # meta.* scaffolding is left out before k is applied

    async def test_hybrid_names_the_embedders_model_and_keyword_only_names_none(self):
        gateway, embedder = FakeSqlGateway(result=_search_result()), FakeEmbedding(dimensions=4, model="embed-large")
        handler = SearchHandler(gateway, embedder)

        await handler(Search(query="graph rag", collection="research"))
        hybrid = gateway.last.params
        await handler(Search(query="graph rag", collection="research", hybrid=False))
        keyword = gateway.last.params

        assert hybrid["query_model"] == "embed-large"
        assert keyword["query_model"] is None
        assert "query_model => CAST(:query_model AS text)" in gateway.last.sql

    async def test_an_embedder_with_no_model_names_none(self):
        gateway, embedder = FakeSqlGateway(result=_search_result()), FakeEmbedding(dimensions=4, model="")
        handler = SearchHandler(gateway, embedder)

        await handler(Search(query="graph rag", collection="research"))

        assert gateway.last.params["query_model"] is None

    async def test_hybrid_false_skips_embedding_and_passes_null_vector(self):
        gateway, embedder = FakeSqlGateway(result=_search_result()), FakeEmbedding(dimensions=4)
        handler = SearchHandler(gateway, embedder)

        await handler(Search(query="graph rag", hybrid=False, collection="research"))

        assert embedder.batches == []  # BM25-only → never embedded
        assert gateway.last.params["qvec"] is None

    async def test_k_and_facets_are_plumbed_into_params_and_cap(self):
        gateway, embedder = FakeSqlGateway(result=_search_result()), FakeEmbedding(dimensions=4)
        handler = SearchHandler(gateway, embedder)

        await handler(
            Search(
                query="q",
                k=20,
                level="claim",
                facets={"task": ["long-doc-qa"], "stage": ["retrieval"], "domain": ["biomedical"]},
                collection="research",
            )
        )

        params = gateway.last.params
        assert params["q"] == "q"
        assert params["k"] == 20
        assert params["level"] == "claim"
        assert params["facets"] == json.dumps({"task": ["long-doc-qa"], "stage": ["retrieval"], "domain": ["biomedical"]})
        assert params["collection"] == "research"
        assert gateway.last.cap == 20  # k rides the row cap

    async def test_facets_default_to_an_empty_jsonb_object(self):
        gateway, embedder = FakeSqlGateway(result=_search_result()), FakeEmbedding(dimensions=4)
        handler = SearchHandler(gateway, embedder)

        await handler(Search(query="q", collection="research"))

        assert gateway.last.params["facets"] == "{}"

    async def test_truncation_message_surfaces_on_the_result(self):
        truncated = SqlResult(rows=(), columns=_HIT_COLUMNS, truncated_message="3 more rows — narrow your filters")
        gateway, embedder = FakeSqlGateway(result=truncated), FakeEmbedding(dimensions=4)
        handler = SearchHandler(gateway, embedder)

        result = await handler(Search(query="q", collection="research"))

        assert result.truncated_message == "3 more rows — narrow your filters"

    async def test_the_per_leg_facts_reach_the_caller_unnarrowed(self):
        """`api.search` publishes each leg's own rank beside the fused score; the read path drops none of it."""
        row = {
            "id": "paper.x#c1",
            "hit_level": "claim",
            "page_id": "paper.x",
            "page_title": "X",
            "snippet": "an excerpt",
            "score": 0.5,
            "lexical_rank": 2,
            "vector_rank": None,
            "vector_distance": None,
            "snippet_truncated": True,
        }
        gateway = FakeSqlGateway(result=SqlResult(rows=(row,), columns=tuple(row)))
        handler = SearchHandler(gateway, FakeEmbedding(dimensions=4))

        result = await handler(Search(query="q", collection="research"))

        assert result.rows == (row,)
        assert result.columns == tuple(row)
        assert gateway.last.sql.lstrip().startswith("SELECT * FROM api.search(")  # no narrower projection in the way

    def test_the_search_query_gates_on_no_score_rank_or_distance_threshold(self):
        """Which legs selected a row is a fact the caller reads, never a cutoff this query applies."""
        lowered = SEARCH_SQL.lower()
        for gate in ("rrf", "score >", "score <", "lexical_rank >", "vector_rank >", "vector_distance", "similarity"):
            assert gate not in lowered


class TestFetchHandler:
    async def test_fetch_calls_api_fetch_with_the_collection_and_unit_id_params(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=("id", "level", "title", "body")))
        handler = FetchHandler(gateway, "research")

        await handler(Fetch(id="paper.lightrag-2025#c1"))

        assert "api.fetch" in gateway.last.sql
        assert "not like 'meta.%'" in gateway.last.sql.lower()  # a meta id is not fetchable as content
        # 0025 leads with the served collection; unit_id keeps the 0002 arg name
        assert gateway.last.params == {"collection": "research", "unit_id": "paper.lightrag-2025#c1"}

    async def test_binds_the_handlers_own_served_collection_not_the_query(self):
        """The served collection comes from the handler, never from the `Fetch` message (it carries none)."""
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = FetchHandler(gateway, "field-notes")

        await handler(Fetch(id="mu.x"))

        assert gateway.last.params["collection"] == "field-notes"


class TestLookupValueHandler:
    async def test_lookup_passes_collection_facet_and_term(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=("value", "definition", "n_pages")))
        handler = LookupValueHandler(gateway, "research")

        await handler(LookupValue(facet="tasks", term="qa"))

        assert "api.lookup_value" in gateway.last.sql
        assert gateway.last.params == {"collection": "research", "facet": "tasks", "term": "qa"}


class TestGraphHandler:
    """`GraphHandler` scopes both `api.cards` and `api.edges` reads to its served collection."""

    async def test_binds_the_served_collection_on_both_node_and_edge_reads(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = GraphHandler(gateway, "field-notes")

        await handler(Graph())

        assert len(gateway.rows_calls) == 2  # nodes, then edges
        assert all(call.params["collection"] == "field-notes" for call in gateway.rows_calls)

    async def test_page_type_and_rel_still_narrow_alongside_the_collection(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = GraphHandler(gateway, "research")

        await handler(Graph(page_type="paper", rel="cites"))

        nodes_call, edges_call = gateway.rows_calls
        assert nodes_call.params == {"collection": "research", "page_type": "paper", "rel": "cites"}
        assert edges_call.params == {"collection": "research", "page_type": "paper", "rel": "cites"}

    async def test_a_query_collection_overrides_the_wired_default(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = GraphHandler(gateway, "research")

        await handler(Graph(collection="field-notes"))

        assert all(call.params["collection"] == "field-notes" for call in gateway.rows_calls)

    async def test_no_query_collection_falls_back_to_the_wired_default(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = GraphHandler(gateway, "research")

        await handler(Graph(collection=None))

        assert all(call.params["collection"] == "research" for call in gateway.rows_calls)


class TestDescribeSchemaHandler:
    async def test_renders_object_and_column_comments(self):
        schema = SchemaDescription(
            objects=(
                ObjectInfo(
                    name="api.cards",
                    kind="view",
                    comment="L1 card scan — one row per page.",
                    columns=(
                        ColumnInfo(name="id", data_type="text", comment="Page id."),
                        ColumnInfo(name="title", data_type="text", comment="Human-readable title."),
                    ),
                ),
            )
        )
        handler = DescribeSchemaHandler(FakeSqlGateway(schema=schema))

        rendered = await handler(DescribeSchema(objects=("api.cards",)))

        assert "api.cards" in rendered
        assert "L1 card scan" in rendered
        assert "id" in rendered
        assert "Page id." in rendered
        assert "Human-readable title." in rendered

    async def test_pulls_from_api_objects(self):
        gateway = FakeSqlGateway()
        handler = DescribeSchemaHandler(gateway)

        await handler(DescribeSchema())

        assert gateway.api_objects_calls == 1

    async def test_with_a_spec_the_render_opens_with_the_knowledge_base_slug_and_root(self):
        handler = DescribeSchemaHandler(FakeSqlGateway(schema=_cards_schema()), (_spec(page_types=("note",)),), root=Path("/x/vault"))

        rendered = await handler(DescribeSchema(objects=_CARDS_OBJECTS))

        assert rendered.splitlines()[0] == "knowledge_base vault-x at /x/vault"

    async def test_with_a_spec_the_comments_render_unchanged(self):
        handler = DescribeSchemaHandler(FakeSqlGateway(schema=_cards_schema()), (_spec(page_types=("recipe",)),))

        rendered = await handler(DescribeSchema(objects=_CARDS_OBJECTS))

        assert "one compact row per page across every page type" in rendered
        assert "WHERE type = '<page-type>'" in rendered
        assert "ARRAY['<page-id>']" in rendered


class TestDescribeSchemaHandlerOverSeveralSpecs:
    """A door speaking for several knowledge bases leaves the comments as written and names each knowledge base in its own block."""

    def _two_specs(self) -> tuple[_FakeSchemaSpec, _FakeSchemaSpec]:
        recipes = _FakeSchemaSpec(
            page_types=(_FakePageType(page_type="recipe", id_prefix="rcp."), _FakePageType(page_type="ingredient", id_prefix="ing.")),
            slug="kitchen",
        )
        rulings = _FakeSchemaSpec(page_types=(_FakePageType(page_type="ruling", decision_declared=True),), slug="legal")
        return recipes, rulings

    async def test_the_comments_render_unchanged(self):
        handler = DescribeSchemaHandler(FakeSqlGateway(schema=_cards_schema()), self._two_specs())

        rendered = await handler(DescribeSchema(objects=_CARDS_OBJECTS))

        assert "one compact row per page across every page type" in rendered
        assert "Example value: '<page-id>'." in rendered

    async def test_one_grounding_block_per_spec_in_the_order_given(self):
        handler = DescribeSchemaHandler(FakeSqlGateway(schema=_cards_schema()), self._two_specs())

        rendered = await handler(DescribeSchema(objects=_CARDS_OBJECTS))

        assert "knowledge_base kitchen" in rendered
        assert "knowledge_base legal" in rendered
        assert rendered.index("knowledge_base kitchen") < rendered.index("knowledge_base legal")
        assert rendered.index("L1 card scan") < rendered.index("knowledge_base kitchen"), "the schema comes first, the blocks after"

    async def test_each_block_states_its_own_kinds_example_ids_and_decision_page_types(self):
        handler = DescribeSchemaHandler(FakeSqlGateway(schema=_cards_schema()), self._two_specs())

        rendered = await handler(DescribeSchema(objects=_CARDS_OBJECTS))

        assert "- page types: recipe|ingredient" in rendered
        assert "- example ids: rcp.example, ing.example" in rendered
        assert "- decision page types: none, this knowledge base declares no decision page type" in rendered
        assert "- page types: ruling" in rendered
        assert "- example ids: ruling.example" in rendered
        assert "- decision page types: ruling" in rendered

    async def test_a_spec_with_no_page_kinds_says_so(self):
        empty = _FakeSchemaSpec(page_types=(), slug="fresh")
        handler = DescribeSchemaHandler(FakeSqlGateway(schema=_cards_schema()), (empty, _FakeSchemaSpec(page_types=(), slug="also")))

        rendered = await handler(DescribeSchema(objects=_CARDS_OBJECTS))

        assert "- page types: none declared" in rendered
        assert "- example ids: example" in rendered

    async def test_no_spec_at_all_renders_the_comments_alone(self):
        handler = DescribeSchemaHandler(FakeSqlGateway(schema=_cards_schema()))

        rendered = await handler(DescribeSchema(objects=_CARDS_OBJECTS))

        assert "one compact row per page across every page type" in rendered
        assert "knowledge_base " not in rendered


_FUNCTION_NAMES = frozenset({"api.search", "api.fetch", "api.lookup_value", "api.log_session", "api.graph_expand", "api.graph_paths", "api.envelope_page"})


def _twenty_objects_schema() -> SchemaDescription:
    """20 `ObjectInfo` records — a realistic surface size, well past `SCHEMA_OBJECT_LIMIT`.

    Every comment carries a short first sentence (what a catalog line shows) and a longer second
    sentence that only a detail render should ever surface, so a test can tell the two modes apart
    by content, not just by length.
    """
    names = (
        "api.about", "api.cards", "api.claims", "api.edges", "api.facets", "api.authoring_rules",
        "api.graph_paths", "api.sections", "api.contradictions", "api.stale", "api.envelope_page", "api.raw_chunks",
        "api.schema_guide", "api.page_types", "api.search", "api.fetch", "api.lookup_value", "api.log_session",
        "api.graph_expand",
    )  # fmt: skip
    return SchemaDescription(
        objects=tuple(
            ObjectInfo(
                name=name,
                kind="function" if name in _FUNCTION_NAMES else "view",
                comment=f"One-line purpose of {name}. A longer second sentence that only detail mode should show.",
                columns=(ColumnInfo(name="id", data_type="text", comment=f"{name}'s id column."),),
            )
            for name in names
        )
    )


class TestDescribeSchemaHandlerCatalogAndDetail:
    """The catalog-first split: no `objects` lists everything compactly, named `objects` answers detail."""

    async def test_the_default_catalog_lists_every_object_by_name_kind_and_one_line_purpose(self):
        schema = _twenty_objects_schema()
        handler = DescribeSchemaHandler(FakeSqlGateway(schema=schema))

        rendered = await handler(DescribeSchema())

        for obj in schema.objects:
            assert f"{obj.name} ({obj.kind}): One-line purpose of {obj.name}." in rendered
        assert "A longer second sentence" not in rendered  # detail-only text stays out of the catalog
        assert "id column" not in rendered  # so do columns
        assert DESCRIBE_SCHEMA_CATALOG_INSTRUCTION in rendered
        assert len(rendered) <= SCHEMA_RESULT_CHARS

    async def test_the_catalog_asks_the_gateway_for_everything_once(self):
        gateway = FakeSqlGateway(schema=_twenty_objects_schema())
        handler = DescribeSchemaHandler(gateway)

        await handler(DescribeSchema())

        assert gateway.api_objects_calls == 1
        assert gateway.api_objects_names_calls == [()]

    async def test_selected_objects_answer_only_their_own_full_detail(self):
        handler = DescribeSchemaHandler(FakeSqlGateway(schema=_twenty_objects_schema()))

        rendered = await handler(DescribeSchema(objects=("api.search", "api.fetch")))

        assert "A longer second sentence that only detail mode should show." in rendered
        assert "api.search's id column." in rendered
        assert "api.fetch's id column." in rendered
        assert "api.cards" not in rendered  # only the two requested objects render
        assert DESCRIBE_SCHEMA_CATALOG_INSTRUCTION not in rendered  # detail answers columns, not the catalog
        assert len(rendered) <= SCHEMA_RESULT_CHARS

    async def test_bare_names_normalize_to_the_api_qualified_form(self):
        handler = DescribeSchemaHandler(FakeSqlGateway(schema=_twenty_objects_schema()))

        rendered = await handler(DescribeSchema(objects=("search",)))

        assert "api.search's id column." in rendered

    async def test_more_than_the_limit_is_refused_in_one_sentence_before_any_gateway_call(self):
        gateway = FakeSqlGateway(schema=_twenty_objects_schema())
        handler = DescribeSchemaHandler(gateway)
        nine = tuple(f"api.obj{i}" for i in range(SCHEMA_OBJECT_LIMIT + 1))

        with pytest.raises(DescribeSchemaRefusedError) as excinfo:
            await handler(DescribeSchema(objects=nine))

        message = str(excinfo.value)
        assert message.count(". ") == 0  # one sentence
        assert message.endswith(".")
        assert str(SCHEMA_OBJECT_LIMIT) in message
        assert gateway.api_objects_calls == 0  # refused before any DB round trip

    async def test_an_unknown_name_lists_close_and_valid_names(self):
        handler = DescribeSchemaHandler(FakeSqlGateway(schema=_twenty_objects_schema()))

        with pytest.raises(DescribeSchemaRefusedError) as excinfo:
            await handler(DescribeSchema(objects=("api.serach",)))

        message = str(excinfo.value)
        assert "api.serach" in message
        assert "api.search" in message  # the close match
        assert "api.cards" in message  # the full valid-name list

    async def test_an_oversized_comment_is_cut_with_a_narrowing_instruction(self):
        huge = SchemaDescription(objects=(ObjectInfo(name="api.huge", kind="view", comment="x" * 50_000, columns=()),))
        handler = DescribeSchemaHandler(FakeSqlGateway(schema=huge))

        rendered = await handler(DescribeSchema(objects=("api.huge",)))

        assert len(rendered) <= SCHEMA_RESULT_CHARS
        assert "shortened" in rendered.lower()

    async def test_many_large_comments_together_still_stay_within_budget(self):
        many = SchemaDescription(
            objects=tuple(
                ObjectInfo(name=f"api.wide{i}", kind="view", comment="y" * 2_000, columns=()) for i in range(SCHEMA_OBJECT_LIMIT)
            )
        )
        handler = DescribeSchemaHandler(FakeSqlGateway(schema=many))

        rendered = await handler(DescribeSchema(objects=tuple(obj.name for obj in many.objects)))

        assert len(rendered) <= SCHEMA_RESULT_CHARS


class TestPublishedSignaturesCarryEveryBoundArgument:
    """The `api` signatures at head must accept the calls `queries.py` actually makes.

    Every read here is one call against a generated function whose argument list a migration
    publishes: `SEARCH_SQL` binds by name, the two traversal reads bind by position. Neither side
    can see the other, so a parameter renamed in the descriptor, or an argument added to the SQL
    without the matching revision, only shows up when a database is in the room. These load
    `0066_search_page_date.py` (the latest `api.search`) and `0048_focused_retrieval_results.py`
    (the latest traversal) directly and render their own frozen descriptors through the real
    builders, with no DB and no Docker, so the mismatch fails at the unit tier instead.
    """

    def _rendered(self, method: str, *, current: bool = True) -> str:
        """One of 0048's frozen descriptors rendered to its parameter declarations by the real builder."""
        module = _load_migration_module("0048")
        builder = CteGraphTraversalBuilder()
        schema = module._graph_schema(edge_table="kb.edges", current=current)
        rendered: str = getattr(builder, method)(schema)
        return rendered

    def test_every_named_search_argument_is_a_parameter_of_the_published_function(self) -> None:
        module = _load_migration_module("0066")
        bound = {match.group(1) for match in re.finditer(r"(\w+) =>", SEARCH_SQL)}
        # One declaration per rendered line: a facet default may itself carry a comma (the session
        # collection setting does), so the line break is the separator, never the comma.
        declared = {
            declaration.strip().split()[0]
            for declaration in ParadeDBHybridSearchBuilder()._signature(module._search_schema(page_dated=True)).split(",\n")
            if declaration.strip()
        }

        assert bound, "SEARCH_SQL binds nothing by name — the guard would pass vacuously"
        assert bound <= declared, f"SEARCH_SQL binds {sorted(bound - declared)}, which api.search does not declare"

    def test_the_traversal_reads_pass_exactly_as_many_arguments_as_the_published_functions_take(self) -> None:
        expand = len(self._rendered("_expand_signature").split(",\n"))
        paths = len(self._rendered("_paths_signature").split(",\n"))

        assert GRAPH_EXPAND_SQL.count("api.graph_expand(") == 1, "one call per statement, or the bind count below is a sum"
        assert GRAPH_PATHS_SQL.count("api.graph_paths(") == 1, "one call per statement, or the bind count below is a sum"
        assert GRAPH_EXPAND_SQL[GRAPH_EXPAND_SQL.index("api.graph_expand(") :].count(":") == expand
        assert GRAPH_PATHS_SQL[GRAPH_PATHS_SQL.index("api.graph_paths(") :].count(":") == paths

    def test_the_currency_argument_is_last_on_both_published_functions(self) -> None:
        # `GRAPH_EXPAND_SQL` / `GRAPH_PATHS_SQL` pass `include_superseded` positionally, as the final
        # argument, so its place in the signature is part of the contract, not only its presence.
        for method in ("_expand_signature", "_paths_signature"):
            assert self._rendered(method).rstrip().endswith("include_superseded boolean DEFAULT false")
            assert "include_superseded" not in self._rendered(method, current=False)


class TestStatusHandler:
    async def test_status_reads_row_counts_through_the_admin_gateway(self):
        admin = FakeSqlGateway(result=SqlResult(rows=({"table": "pages", "n": 3},), columns=("table", "n")))
        handler = StatusHandler(admin_gateway=admin)

        await handler(Status())

        assert len(admin.rows_calls) >= 1  # kb.* counts go through admin (not granted to the reader role)
        assert any("memory." in call.sql for call in admin.rows_calls)


class TestKnowledgeBasesHandler:
    async def test_reads_through_the_admin_gateway_grouped_by_collection(self):
        admin = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = KnowledgeBasesHandler(admin_gateway=admin)

        await handler()

        assert "kb.pages" in admin.last.sql
        assert "index_fingerprint" in admin.last.sql
        assert admin.last.params == {}  # no filters — every collection is listed

    async def test_two_compiled_collections_are_both_listed(self):
        rows = (
            {"collection": "research", "pages": 42, "page_types": ["paper", "entity"], "last_indexed": datetime(2026, 8, 19, tzinfo=UTC)},
            {"collection": "notebook", "pages": 7, "page_types": ["page"], "last_indexed": datetime(2026, 8, 18, tzinfo=UTC)},
        )
        admin = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        handler = KnowledgeBasesHandler(admin_gateway=admin)

        result = await handler()

        assert {info.name for info in result.collections} == {"research", "notebook"}
        research = next(info for info in result.collections if info.name == "research")
        assert (research.pages, research.page_types) == (42, ("paper", "entity"))
        assert research.last_indexed == datetime(2026, 8, 19, tzinfo=UTC)

    async def test_no_fingerprint_row_leaves_last_compiled_none(self):
        rows = ({"collection": "fresh", "pages": 3, "page_types": ["paper"], "last_indexed": None},)
        admin = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        handler = KnowledgeBasesHandler(admin_gateway=admin)

        result = await handler()

        (info,) = result.collections
        assert info.last_indexed is None  # the left join's miss case

    async def test_empty_knowledge_base_yields_no_collections(self):
        handler = KnowledgeBasesHandler(admin_gateway=FakeSqlGateway(result=SqlResult(rows=(), columns=())))

        result = await handler()

        assert result.collections == ()


class TestKnowledgeBaseValidationHandler:
    async def test_reads_validation_store_filtered_to_the_collection(self):
        admin = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = KnowledgeBaseValidationHandler(admin_gateway=admin)

        await handler("field-notes")

        assert "kb.validation_store" in admin.last.sql
        assert admin.last.params == {"collection": "field-notes"}

    async def test_rows_project_into_lint_issues_with_optional_anchor(self):
        rows = (
            {"page_id": "doc.a", "severity": "error", "rule": "dangling-code-path", "message": "source_path missing", "anchor": None},
            {"page_id": "doc.b", "severity": "warning", "rule": "spec-vocabulary-drift", "message": "seeded but missing", "anchor": "c17"},
        )
        admin = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        handler = KnowledgeBaseValidationHandler(admin_gateway=admin)

        result = await handler("field-notes")

        first, second = result.issues
        assert (first.page_id, first.severity, first.rule, first.anchor) == ("doc.a", "error", "dangling-code-path", None)
        assert (second.message, second.anchor) == ("seeded but missing", "c17")

    async def test_an_unrecognized_severity_value_raises_rather_than_rendering_silently(self):
        rows = ({"page_id": "doc.a", "severity": "critical", "rule": "dangling-code-path", "message": "source_path missing", "anchor": None},)
        admin = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        handler = KnowledgeBaseValidationHandler(admin_gateway=admin)

        with pytest.raises(ValidationError):
            await handler("field-notes")

    async def test_no_rows_yields_an_empty_report(self):
        handler = KnowledgeBaseValidationHandler(admin_gateway=FakeSqlGateway(result=SqlResult(rows=(), columns=())))

        result = await handler("fresh")

        assert result.issues == ()


def _node_cells(
    node_id: str, *, type_: str = "entity", title: str = "T", status: str = "verified", superseded: bool = False
) -> dict[str, object]:
    """The node-shaped columns `_graph_node` reads (api.graph_expand/paths return these per row)."""
    return {"id": node_id, "type": type_, "title": title, "status": status, "superseded": superseded}


def _edge_cells(src_id: str | None, dst_id: str | None, rel: str | None, provenance: str | None = None) -> dict[str, object]:
    """The edge-shaped columns `_graph_edge` reads; a row reaching no node leaves `rel` NULL."""
    return {"src_id": src_id, "dst_id": dst_id, "rel": rel, "provenance_claim": provenance}


_SEED_ROW = (_node_cells("paper.graph") | _edge_cells(None, None, None),)
"""A walk that reached only its seed."""


def _graph_gateway(result: SqlResult | None = None, *, reach: GraphReach | None = None) -> FakeSqlGateway:
    """A gateway answering a walk with `result` and `api.graph_reach` with `reach`, or with no row when it is `None`."""
    reach_rows = () if reach is None else (reach.model_dump(),)
    return FakeSqlGateway(
        result=result or SqlResult(rows=(), columns=()), routed={"api.graph_reach": SqlResult(rows=reach_rows, columns=())}
    )


def _walk_call(gateway: FakeSqlGateway) -> RecordedQuery:
    """The latest call that ran a walk rather than `api.graph_reach`."""
    return next(call for call in reversed(gateway.rows_calls) if "api.graph_reach" not in call.sql)


class TestGraphExpandHandler:
    async def test_binds_collection_seed_ids_rel_types_and_k_with_null_guarded_rel_types(self):
        gateway = _graph_gateway()
        handler = GraphExpandHandler(gateway, "research")

        await handler(GraphExpand(seed_ids=("paper.graph", "ent.bm25"), hops=3, rel_types=("cites", "evaluates"), k=15))

        assert "api.graph_expand" in _walk_call(gateway).sql
        params = _walk_call(gateway).params
        assert params["collection"] == "research"  # 0025 leads the call with the served collection
        assert params["seed_ids"] == ["paper.graph", "ent.bm25"]  # tuple → list (text[] bind)
        assert params["hops"] == 3
        assert params["rel_types"] == ["cites", "evaluates"]
        assert params["k"] == 15
        assert _walk_call(gateway).cap == _GRAPH_ROW_CAP  # k caps the NODES; the rows a node's assertions fill ride the graph cap

    async def test_a_walk_with_nodes_past_its_last_hop_says_how_far_they_lie(self):
        far = GraphReach(reached_nodes=12, deepest_hop=4, complete=True)
        handler = GraphExpandHandler(_graph_gateway(result=SqlResult(rows=_SEED_ROW, columns=()), reach=far), "research")

        result = await handler(GraphExpand(seed_ids=("paper.graph",), hops=2))
        assert result.next_steps is not None

        assert "Nodes lie past hop 2: the seeds reach 12 node(s), the farthest 4 hops out." in result.next_steps.summary
        assert "graph_expand(seed_ids=['paper.graph'], hops=4) to reach further out." in result.next_steps.options
        assert result.reach == far

    async def test_a_walk_at_its_hop_limit_says_so_and_offers_no_wider_walk(self):
        far = GraphReach(reached_nodes=40, deepest_hop=9, complete=True)
        handler = GraphExpandHandler(_graph_gateway(result=SqlResult(rows=_SEED_ROW, columns=()), reach=far), "research")

        result = await handler(GraphExpand(seed_ids=("paper.graph",), hops=9))
        assert result.next_steps is not None

        assert "the farthest 9 hops out. An expansion walks at most 6 hops." in result.next_steps.summary
        assert not any(option.startswith("graph_expand(") for option in result.next_steps.options)

    async def test_a_walk_that_reached_every_node_says_so(self):
        reach = GraphReach(reached_nodes=1, deepest_hop=0, complete=True)
        handler = GraphExpandHandler(_graph_gateway(result=SqlResult(rows=_SEED_ROW, columns=()), reach=reach), "research")

        result = await handler(GraphExpand(seed_ids=("paper.graph",), hops=2))
        assert result.next_steps is not None

        assert result.next_steps.summary.endswith("These are all the nodes the seeds reach.")

    async def test_a_reach_stopped_at_its_node_limit_says_nodes_may_lie_further(self):
        reach = GraphReach(reached_nodes=10_000, deepest_hop=2, complete=False)
        handler = GraphExpandHandler(_graph_gateway(result=SqlResult(rows=_SEED_ROW, columns=()), reach=reach), "research")

        result = await handler(GraphExpand(seed_ids=("paper.graph",), hops=2))
        assert result.next_steps is not None

        assert "The seeds reach more than 10,000 nodes, so nodes may lie past hop 2." in result.next_steps.summary
        assert "graph_expand(seed_ids=['paper.graph'], hops=6) to reach further out." in result.next_steps.options

    async def test_the_reach_reads_the_walks_own_seeds_and_filters(self):
        gateway = _graph_gateway(result=SqlResult(rows=_SEED_ROW, columns=()))
        handler = GraphExpandHandler(gateway, "research")

        await handler(GraphExpand(seed_ids=("paper.graph",), rel_types=("cites",), include_superseded=True))

        assert gateway.last.params == {
            "collection": "research",
            "seed_ids": ["paper.graph"],
            "rel_types": ["cites"],
            "target_id": None,
            "include_superseded": True,
        }
        assert "api.graph_reach" in gateway.last.sql

    async def test_a_cut_walk_reads_no_reach(self):
        gateway = _graph_gateway(result=SqlResult(rows=_expand_rows(21), columns=()))
        handler = GraphExpandHandler(gateway, "research")

        result = await handler(GraphExpand(seed_ids=("paper.graph",), k=20))

        assert len(gateway.rows_calls) == 1
        assert result.reach is None

    async def test_null_rel_types_binds_none(self):
        gateway = _graph_gateway()
        handler = GraphExpandHandler(gateway, "research")

        await handler(GraphExpand(seed_ids=("paper.graph",)))

        assert _walk_call(gateway).params["rel_types"] is None  # None → NULL text[] (CAST guard runs every relation)

    async def test_binds_the_handlers_own_served_collection(self):
        gateway = _graph_gateway()
        handler = GraphExpandHandler(gateway, "field-notes")

        await handler(GraphExpand(seed_ids=("mu.x",)))

        assert _walk_call(gateway).params["collection"] == "field-notes"

    async def test_projects_rows_to_graph_result_with_edges_only_where_rel_present(self):
        rows = (
            _node_cells("paper.graph", type_="paper", title="Graph Indexing") | _edge_cells(None, None, None),  # seed: no in-edge
            _node_cells("ent.graphrag", title="GraphRAG") | _edge_cells("paper.graph", "ent.graphrag", "evaluates", "paper.graph#c1"),
        )
        gateway = _graph_gateway(result=SqlResult(rows=rows, columns=()))
        handler = GraphExpandHandler(gateway, "research")

        result = await handler(GraphExpand(seed_ids=("paper.graph",)))

        assert {node.id for node in result.nodes} == {"paper.graph", "ent.graphrag"}
        seed = next(node for node in result.nodes if node.id == "paper.graph")
        assert (seed.type, seed.title, seed.status, seed.superseded) == ("paper", "Graph Indexing", "verified", False)
        assert len(result.edges) == 1  # the seed row's NULL rel yields no edge
        (edge,) = result.edges
        assert (edge.src_id, edge.dst_id, edge.rel, edge.provenance_claim) == ("paper.graph", "ent.graphrag", "evaluates", "paper.graph#c1")

    async def test_every_node_and_edge_carries_a_handle_qualified_by_the_served_collection(self):
        """A graph handle is fetchable: a node is its page, an edge's provenance is the claim unit asserting it."""
        rows = (
            _node_cells("paper.graph", type_="paper") | _edge_cells(None, None, None),
            _node_cells("ent.graphrag") | _edge_cells("paper.graph", "ent.graphrag", "evaluates", "paper.graph#c1"),
        )
        gateway = _graph_gateway(result=SqlResult(rows=rows, columns=()))
        handler = GraphExpandHandler(gateway, "research")

        result = await handler(GraphExpand(seed_ids=("paper.graph",)))

        assert {node.ref for node in result.nodes} == {"page:research/paper.graph", "page:research/ent.graphrag"}
        (edge,) = result.edges
        assert edge.provenance_ref == "unit:research/paper.graph#c1"

    async def test_an_edge_that_asserts_no_claim_carries_no_provenance_handle(self):
        rows = (
            _node_cells("paper.a") | _edge_cells(None, None, None),
            _node_cells("ent.bm25") | _edge_cells("paper.a", "ent.bm25", "cites"),
        )
        gateway = _graph_gateway(result=SqlResult(rows=rows, columns=()))
        handler = GraphExpandHandler(gateway, "research")

        result = await handler(GraphExpand(seed_ids=("paper.a",)))

        (edge,) = result.edges
        assert (edge.provenance_claim, edge.provenance_ref) == (None, None)

    async def test_deduplicates_nodes_reached_by_more_than_one_edge(self):
        rows = (
            _node_cells("paper.a") | _edge_cells(None, None, None),
            _node_cells("paper.b") | _edge_cells(None, None, None),
            _node_cells("ent.bm25") | _edge_cells("paper.a", "ent.bm25", "cites"),
            _node_cells("ent.bm25") | _edge_cells("paper.b", "ent.bm25", "builds_on"),
        )
        gateway = _graph_gateway(result=SqlResult(rows=rows, columns=()))
        handler = GraphExpandHandler(gateway, "research")

        result = await handler(GraphExpand(seed_ids=("paper.a", "paper.b")))

        assert [node.id for node in result.nodes] == ["paper.a", "paper.b", "ent.bm25"]  # one node per id, two edges into the last
        assert len(result.edges) == 2


class TestGraphPathsHandler:
    async def test_binds_collection_from_to_max_hops_rel_types_and_k(self):
        gateway = _graph_gateway()
        handler = GraphPathsHandler(gateway, "research")

        await handler(GraphPaths(from_id="paper.graph", to_id="ent.bm25", max_hops=5, rel_types=("cites",), k=7))

        assert "api.graph_paths" in _walk_call(gateway).sql
        params = _walk_call(gateway).params
        assert params["collection"] == "research"  # 0025 leads the call with the served collection
        assert params["from_id"] == "paper.graph"
        assert params["to_id"] == "ent.bm25"
        assert params["max_hops"] == 5
        assert params["rel_types"] == ["cites"]
        assert params["k"] == 7
        assert _walk_call(gateway).cap == 8 * 6  # (k + 1) routes, each at most (max_hops + 1) nodes

    async def test_null_rel_types_binds_none(self):
        gateway = _graph_gateway()
        handler = GraphPathsHandler(gateway, "research")

        await handler(GraphPaths(from_id="a", to_id="b"))

        assert _walk_call(gateway).params["rel_types"] is None

    async def test_binds_the_handlers_own_served_collection(self):
        gateway = _graph_gateway()
        handler = GraphPathsHandler(gateway, "field-notes")

        await handler(GraphPaths(from_id="a", to_id="b"))

        assert _walk_call(gateway).params["collection"] == "field-notes"

    async def test_groups_rows_by_path_rank_into_ordered_paths(self):
        rows = (
            {"path_rank": 1, "score": 0.9} | _node_cells("paper.graph", type_="paper") | _edge_cells(None, None, None),
            {"path_rank": 1, "score": 0.9} | _node_cells("ent.bm25") | _edge_cells("paper.graph", "ent.bm25", "cites", "paper.graph#c2"),
            {"path_rank": 2, "score": 0.4} | _node_cells("paper.graph", type_="paper") | _edge_cells(None, None, None),
            {"path_rank": 2, "score": 0.4} | _node_cells("ent.x") | _edge_cells("paper.graph", "ent.x", "evaluates"),
            {"path_rank": 2, "score": 0.4} | _node_cells("ent.bm25") | _edge_cells("ent.x", "ent.bm25", "builds_on"),
        )
        gateway = _graph_gateway(result=SqlResult(rows=rows, columns=()))
        handler = GraphPathsHandler(gateway, "research")

        result = await handler(GraphPaths(from_id="paper.graph", to_id="ent.bm25"))

        assert len(result.paths) == 2  # two path_rank groups
        first, second = result.paths
        assert [node.id for node in first.nodes] == ["paper.graph", "ent.bm25"]  # order preserved within a rank
        assert [edge.rel for edge in first.edges] == ["cites"]  # the NULL-rel first row yields no edge
        assert first.score == 0.9
        assert [node.id for node in second.nodes] == ["paper.graph", "ent.x", "ent.bm25"]
        assert [edge.rel for edge in second.edges] == ["evaluates", "builds_on"]
        assert second.score == 0.4

    async def test_empty_result_yields_no_paths(self):
        handler = GraphPathsHandler(_graph_gateway(result=SqlResult(rows=(), columns=())), "research")

        result = await handler(GraphPaths(from_id="a", to_id="b"))

        assert result.paths == ()

    async def test_no_route_says_none_exists_when_the_start_reached_every_node_it_could(self):
        reach = GraphReach(reached_nodes=7, deepest_hop=3, complete=True)
        handler = GraphPathsHandler(_graph_gateway(reach=reach), "research")

        result = await handler(GraphPaths(from_id="a", to_id="b"))

        assert result.next_steps.summary == "No route from 'a' to 'b' exists: 'b' is not among the 7 node(s) 'a' reaches."
        assert not any(option.startswith("find_paths(from_id='a'") for option in result.next_steps.options)
        assert result.reach == reach

    async def test_no_route_within_max_hops_names_the_shortest_route_and_the_search_that_finds_it(self):
        reach = GraphReach(reached_nodes=30, deepest_hop=7, target_hop=6, complete=False)
        handler = GraphPathsHandler(_graph_gateway(reach=reach), "research")

        result = await handler(GraphPaths(from_id="a", to_id="b", max_hops=4))

        assert result.next_steps.summary == "No route from 'a' to 'b' within 4 hops. The shortest route takes 6 hops."
        assert result.next_steps.options[0] == "find_paths(from_id='a', to_id='b', max_hops=6) to look for a longer route."

    async def test_a_shortest_route_past_the_search_limit_is_named_as_out_of_reach(self):
        reach = GraphReach(reached_nodes=30, deepest_hop=11, target_hop=11, complete=False)
        handler = GraphPathsHandler(_graph_gateway(reach=reach), "research")

        result = await handler(GraphPaths(from_id="a", to_id="b", max_hops=8))

        assert result.next_steps.summary.endswith("The shortest route takes 11 hops, past the 8 hops a route search looks.")
        assert not any(option.startswith("find_paths(from_id='a'") for option in result.next_steps.options)

    async def test_a_reach_stopped_at_its_node_limit_leaves_a_longer_route_open(self):
        reach = GraphReach(reached_nodes=10_000, deepest_hop=3, complete=False)
        handler = GraphPathsHandler(_graph_gateway(reach=reach), "research")

        result = await handler(GraphPaths(from_id="a", to_id="b", max_hops=4))

        assert result.next_steps.summary == (
            "No route from 'a' to 'b' within 4 hops. 'a' reaches more than 10,000 nodes without 'b' among them, "
            "so a longer route is not ruled out."
        )
        assert result.next_steps.options[0] == "find_paths(from_id='a', to_id='b', max_hops=8) to look for a longer route."

    async def test_the_reach_reads_the_routes_own_start_end_and_filters(self):
        gateway = _graph_gateway()
        handler = GraphPathsHandler(gateway, "research")

        await handler(GraphPaths(from_id="a", to_id="b", rel_types=("cites",)))

        assert "api.graph_reach" in gateway.last.sql
        assert gateway.last.params == {
            "collection": "research",
            "seed_ids": ["a"],
            "rel_types": ["cites"],
            "target_id": "b",
            "include_superseded": False,
        }

    async def test_found_routes_read_no_reach(self):
        gateway = _graph_gateway(result=SqlResult(rows=_path_rows(1), columns=()))
        handler = GraphPathsHandler(gateway, "research")

        result = await handler(GraphPaths(from_id="paper.graph", to_id="ent.n001"))

        assert len(gateway.rows_calls) == 1
        assert result.reach is None


def _halfvec_literal(vector: list[float]) -> str:
    """The expected `halfvec` text-literal the handler binds: `[f1,f2,…]`."""
    return "[" + ",".join(repr(component) for component in vector) + "]"


def _expand_rows(count: int, *, title: str = "T", hop_src: str = "paper.graph", with_seed: bool = False) -> tuple[dict[str, object], ...]:
    """`count` reached-node rows, each with the edge that reached it, in the order the traversal returns them.

    `with_seed` prepends the seed's own row (hop 0, no reaching edge), the way the traversal returns it.
    """
    seed = (_node_cells(hop_src, title=title) | _edge_cells(None, None, None),) if with_seed else ()
    return seed + tuple(
        _node_cells(f"ent.n{index:03d}", title=title) | _edge_cells(hop_src, f"ent.n{index:03d}", "cites", f"{hop_src}#c{index}")
        for index in range(count)
    )


def _path_rows(count: int, *, title: str = "T") -> tuple[dict[str, object], ...]:
    """`count` two-node routes, one `path_rank` group each, in rank order."""
    rows: list[dict[str, object]] = []
    for rank in range(1, count + 1):
        rows.append({"path_rank": rank, "score": 0.5} | _node_cells("paper.graph", title=title) | _edge_cells(None, None, None))
        rows.append(
            {"path_rank": rank, "score": 0.5}
            | _node_cells(f"ent.n{rank:03d}", title=title)
            | _edge_cells("paper.graph", f"ent.n{rank:03d}", "cites", f"paper.graph#c{rank}")
        )
    return tuple(rows)


class TestGraphCurrency:
    async def test_expansion_binds_the_current_only_default(self):
        gateway = _graph_gateway()
        handler = GraphExpandHandler(gateway, "research")

        await handler(GraphExpand(seed_ids=("paper.graph",)))

        assert _walk_call(gateway).params["include_superseded"] is False

    async def test_expansion_binds_the_diagnostic_opt_in(self):
        gateway = _graph_gateway()
        handler = GraphExpandHandler(gateway, "research")

        await handler(GraphExpand(seed_ids=("paper.graph",), include_superseded=True))

        assert _walk_call(gateway).params["include_superseded"] is True

    async def test_paths_bind_the_currency_flag_both_ways(self):
        gateway = _graph_gateway()
        handler = GraphPathsHandler(gateway, "research")

        await handler(GraphPaths(from_id="a", to_id="b"))
        assert _walk_call(gateway).params["include_superseded"] is False

        await handler(GraphPaths(from_id="a", to_id="b", include_superseded=True))
        assert _walk_call(gateway).params["include_superseded"] is True


class TestGraphRowCaps:
    async def test_expansion_reads_under_the_graph_row_cap_not_the_node_cap(self):
        # One node can be reached by several authored assertions, so a row cap set to `k` would drop
        # assertions the result is supposed to carry.
        gateway = _graph_gateway()
        handler = GraphExpandHandler(gateway, "research")

        await handler(GraphExpand(seed_ids=("paper.graph",), k=20))

        assert _walk_call(gateway).cap == _GRAPH_ROW_CAP

    async def test_paths_read_under_the_rows_a_capped_route_set_can_occupy(self):
        gateway = _graph_gateway()
        handler = GraphPathsHandler(gateway, "research")

        await handler(GraphPaths(from_id="a", to_id="b", max_hops=4, k=10))

        assert _walk_call(gateway).cap == 11 * 5  # (k + 1) routes, each at most (max_hops + 1) nodes

    async def test_a_path_row_cap_never_runs_past_the_graph_row_cap(self):
        gateway = _graph_gateway()
        handler = GraphPathsHandler(gateway, "research")

        await handler(GraphPaths(from_id="a", to_id="b", max_hops=10_000, k=10_000))

        assert _walk_call(gateway).cap == _GRAPH_ROW_CAP


class TestGraphTruncationIsHonest:
    async def test_k_nodes_come_back_with_the_probe_reported_as_an_uncounted_remainder(self):
        gateway = _graph_gateway(result=SqlResult(rows=_expand_rows(21), columns=()))
        handler = GraphExpandHandler(gateway, "research")

        result = await handler(GraphExpand(seed_ids=("paper.graph",), k=20))

        assert len(result.nodes) == 20
        assert result.window.returned_items == 20
        assert result.window.truncated is True
        assert result.window.omitted_items is None  # the probe proves more exist; nothing counted them
        assert result.window.reason

    async def test_a_complete_neighbourhood_reports_nothing_omitted(self):
        gateway = _graph_gateway(result=SqlResult(rows=_expand_rows(3), columns=()))
        handler = GraphExpandHandler(gateway, "research")

        result = await handler(GraphExpand(seed_ids=("paper.graph",), k=20))

        assert result.window == ResultWindow(returned_items=3, omitted_items=0, truncated=False)

    async def test_an_edge_of_a_dropped_node_is_dropped_with_it(self):
        gateway = _graph_gateway(result=SqlResult(rows=_expand_rows(3, with_seed=True), columns=()))
        handler = GraphExpandHandler(gateway, "research")

        result = await handler(GraphExpand(seed_ids=("paper.graph",), k=2))

        assert [node.id for node in result.nodes] == ["paper.graph", "ent.n000"]
        assert {edge.dst_id for edge in result.edges} == {"ent.n000"}

    async def test_parallel_assertions_into_one_node_both_survive_the_cut(self):
        rows = (
            _node_cells("paper.a") | _edge_cells(None, None, None),
            _node_cells("ent.bm25") | _edge_cells("paper.a", "ent.bm25", "cites", "paper.a#c1"),
            _node_cells("ent.bm25") | _edge_cells("paper.a", "ent.bm25", "cites", "paper.a#c2"),
        )
        gateway = _graph_gateway(result=SqlResult(rows=rows, columns=()))
        handler = GraphExpandHandler(gateway, "research")

        result = await handler(GraphExpand(seed_ids=("paper.a",), k=2))

        assert [node.id for node in result.nodes] == ["paper.a", "ent.bm25"]
        assert [edge.provenance_claim for edge in result.edges] == ["paper.a#c1", "paper.a#c2"]
        assert result.window.truncated is False  # two nodes asked for, two nodes found

    async def test_k_routes_come_back_with_the_probe_reported_as_an_uncounted_remainder(self):
        gateway = _graph_gateway(result=SqlResult(rows=_path_rows(11), columns=()))
        handler = GraphPathsHandler(gateway, "research")

        result = await handler(GraphPaths(from_id="a", to_id="b", k=10))

        assert len(result.paths) == 10
        assert result.window.truncated is True
        assert result.window.omitted_items is None


class TestGraphResultsAreTypedAndActionable:
    async def test_find_paths_answers_a_typed_result_not_a_bare_tuple(self):
        gateway = _graph_gateway(result=SqlResult(rows=_path_rows(1), columns=()))
        handler = GraphPathsHandler(gateway, "research")

        result = await handler(GraphPaths(from_id="paper.graph", to_id="ent.n001"))

        assert isinstance(result, GraphPathsResult)
        assert [node.id for node in result.paths[0].nodes] == ["paper.graph", "ent.n001"]
        assert result.next_steps.summary

    async def test_an_expansion_names_a_fetchable_node_and_its_provenance(self):
        gateway = _graph_gateway(result=SqlResult(rows=_expand_rows(1, with_seed=True), columns=()))
        handler = GraphExpandHandler(gateway, "research")

        result = await handler(GraphExpand(seed_ids=("paper.graph",)))

        assert result.next_steps is not None
        options = " ".join(result.next_steps.options)
        assert "page:research/paper.graph" in options
        assert "unit:research/paper.graph#c0" in options

    async def test_an_empty_expansion_offers_the_currency_opt_in(self):
        gateway = _graph_gateway(result=SqlResult(rows=(), columns=()))
        handler = GraphExpandHandler(gateway, "research")

        result = await handler(GraphExpand(seed_ids=("paper.gone",)))

        assert result.next_steps is not None
        assert "include_superseded" in " ".join(result.next_steps.options)

    async def test_next_steps_name_only_the_tools_the_profile_exposes(self):
        gateway = _graph_gateway(result=SqlResult(rows=(), columns=()))
        handler = GraphExpandHandler(gateway, "research")

        result = await handler(GraphExpand(seed_ids=("paper.gone",)))
        graph_only = graph_next_steps((), (), truncated=False, tools=frozenset({GRAPH_EXPAND, FIND_PATHS}), seed_ids=("paper.gone",))

        assert result.next_steps is not None
        assert "search(" in " ".join(result.next_steps.options)  # the default profile has every tool
        assert "search(" not in " ".join(graph_only.options)
        assert graph_only.summary


class TestTheGraphResultBudget:
    def _steps(self) -> NextSteps:
        return NextSteps(summary="A neighbourhood.", options=("fetch(unit_id='page:research/ent.n000')",))

    def test_a_result_that_already_fits_is_untouched(self):
        nodes = tuple(GraphNode(id=f"ent.n{i}", ref=f"page:research/ent.n{i}", type="entity", title="T", status="verified", superseded=False) for i in range(3))

        result = bound_graph_result(nodes, (), knowledge_base=None, next_steps=self._steps())

        assert result.nodes == nodes
        assert result.window == ResultWindow(returned_items=3, omitted_items=0, truncated=False)

    def test_a_giant_display_title_shortens_while_its_id_and_ref_do_not(self):
        nodes = (
            GraphNode(id="ent.big", ref="page:research/ent.big", type="entity", title="é" * 20_000, status="verified", superseded=False),
        )

        result = bound_graph_result(nodes, (), knowledge_base=None, next_steps=self._steps())

        (node,) = result.nodes
        assert node.id == "ent.big"
        assert node.ref == "page:research/ent.big"
        assert len(node.title) < 20_000
        assert node.title.endswith("…")
        assert len(result.model_dump_json()) <= GRAPH_RESULT_CHARS

    def test_a_giant_mandatory_identity_returns_an_empty_refusal_that_never_echoes_it(self):
        giant = "x" * 20_000
        nodes = (GraphNode(id=giant, ref=f"page:research/{giant}", type="entity", title="T", status="verified", superseded=False),)

        result = bound_graph_result(nodes, (), knowledge_base=None, next_steps=self._steps())

        assert result.nodes == ()
        assert result.window.refused is True
        assert result.window.returned_items == 0
        assert result.window.reason
        assert giant not in result.model_dump_json()
        assert result.next_steps is not None
        assert result.next_steps.options

    def test_the_stamp_and_the_window_are_measured_with_the_nodes(self):
        # A node that fits without a stamp can stop fitting with one, so the budget is checked over
        # the complete answer, never over the nodes alone.
        nodes = tuple(
            GraphNode(id=f"ent.n{i:04d}", ref=f"page:research/ent.n{i:04d}", type="entity", title="T" * 60, status="verified", superseded=False)
            for i in range(200)
        )
        stamp = KnowledgeBaseStamp(slug="research", changed_since=0)

        plain = bound_graph_result(nodes, (), knowledge_base=None, next_steps=self._steps())
        stamped = bound_graph_result(nodes, (), knowledge_base=stamp, next_steps=self._steps())

        assert len(stamped.model_dump_json()) <= GRAPH_RESULT_CHARS
        assert len(stamped.nodes) <= len(plain.nodes)
        assert stamped.knowledge_base == stamp

    def test_a_result_of_exactly_the_budget_is_kept_whole(self):
        node = GraphNode(id="ent.n0", ref="page:research/ent.n0", type="entity", title="T", status="verified", superseded=False)
        steps = self._steps()
        base = len(bound_graph_result((node,), (), knowledge_base=None, next_steps=steps).model_dump_json())

        exact = bound_graph_result((node,), (), knowledge_base=None, next_steps=steps, max_chars=base)
        over = bound_graph_result((node,), (), knowledge_base=None, next_steps=steps, max_chars=base - 1)

        assert exact.nodes == (node,)
        assert exact.window.truncated is False
        assert over.nodes == () or over.nodes[0].title != node.title

    def test_a_dropped_node_counts_exactly_when_nothing_else_was_left_out(self):
        nodes = tuple(
            GraphNode(id=f"ent.n{i:04d}", ref=f"page:research/ent.n{i:04d}", type="entity", title="T" * 200, status="verified", superseded=False)
            for i in range(200)
        )

        result = bound_graph_result(nodes, (), knowledge_base=None, next_steps=self._steps())

        assert result.window.truncated is True
        assert result.window.omitted_items == 200 - len(result.nodes)

    def test_a_dropped_node_is_an_uncounted_remainder_once_the_probe_found_more(self):
        nodes = tuple(
            GraphNode(id=f"ent.n{i:04d}", ref=f"page:research/ent.n{i:04d}", type="entity", title="T" * 200, status="verified", superseded=False)
            for i in range(200)
        )

        result = bound_graph_result(nodes, (), knowledge_base=None, next_steps=self._steps(), omitted_items=None)

        assert result.window.omitted_items is None

    def test_paths_are_kept_or_dropped_whole_with_every_assertion_they_carry(self):
        def route(index: int) -> GraphPath:
            nodes = tuple(
                GraphNode(id=f"ent.{index}.{step}", ref=f"page:research/ent.{index}.{step}", type="entity", title="T" * 300, status="verified", superseded=False)
                for step in range(3)
            )
            edges = tuple(
                GraphEdge(src_id=nodes[step].id, dst_id=nodes[step + 1].id, rel="cites", provenance_claim=f"c{step}", provenance_ref=f"unit:research/c{step}")
                for step in range(2)
            )
            return GraphPath(nodes=nodes, edges=edges, score=1.0 / (index + 1))

        paths = tuple(route(index) for index in range(40))

        result = bound_graph_paths(paths, knowledge_base=None, next_steps=self._steps())

        assert len(result.model_dump_json()) <= GRAPH_RESULT_CHARS
        assert result.window.truncated is True
        assert all(len(path.edges) == 2 for path in result.paths)  # no route came back half-asserted

    def test_a_route_whose_identity_cannot_fit_refuses_rather_than_corrupting_it(self):
        giant = "y" * 20_000
        node = GraphNode(id=giant, ref=f"page:research/{giant}", type="entity", title="T", status="verified", superseded=False)
        paths = (GraphPath(nodes=(node,), edges=(), score=1.0),)

        result = bound_graph_paths(paths, knowledge_base=None, next_steps=self._steps())

        assert result.paths == ()
        assert result.window.refused is True
        assert giant not in result.model_dump_json()
