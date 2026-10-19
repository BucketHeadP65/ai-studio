"""Declarative knowledge base descriptors the hybrid-search and graph SQL is generated from.

A knowledge base describes itself with one `SearchSchema` and one `GraphSchema`. They say which
tables are searchable, which column holds each surface's text and embedding, which facets narrow a
query and how the link graph is shaped. The builders generate the rest of the SQL.

Identifier fields (`table`, `key_col`, `bm25_index`) name relations and columns. Expression fields
carry SQL fragments. Structural fragments (`parent_id_expr`, `title_expr`, `join`, `select_cols`) are
interpolated verbatim. Fragments that narrow on a facet (`FacetPool.predicates`,
`SearchSurface.membership`, `SearchSurface.filters`) name it through a `{facet}` placeholder that the
builder renders per dialect. A fragment must be trusted configuration written in code, never runtime
or user-supplied input.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")

PATH_RANK_COLUMN = "path_rank"
"""The `api.graph_paths` column naming the group a row's path belongs to."""
PATH_SCORE_COLUMN = "score"
"""The `api.graph_paths` column holding the path's score, repeated on every row of its group."""


def _require_non_empty(value: str, label: str) -> None:
    """Raise `ValueError` when `value` is empty or blank."""
    if not value.strip():
        msg = f"{label} must be a non-empty string"
        raise ValueError(msg)


def _require_unique(values: Iterable[str], label: str) -> None:
    """Raise `ValueError` when `values` contains a duplicate."""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        msg = f"{label} must be unique, found duplicate(s): {', '.join(sorted(duplicates))}"
        raise ValueError(msg)


@dataclass(frozen=True)
class SearchSurface:
    """One searchable surface: a relation contributing a BM25 leg and a vector leg to hybrid search.

    A surface maps a relation onto the shared result columns a search returns (`id`, `hit_level`,
    `page_id`, `page_title`, `snippet`). The identifier fields name the relation and its columns. The
    expression fields carry SQL fragments the builder interpolates verbatim, so a surface can reach
    into a facet pool or project a computed value.

    Attributes:
        name: A short handle for the surface, used to stem its leg names (e.g. `section` gives
            `section_bm25` / `section_vec`). Must be unique within a `SearchSchema`.
        table: The relation the surface scans, schema-qualified (e.g. `docs.sections`).
        alias: The `FROM` alias for `table`, used to qualify `key_col` / `text_col` /
            `embedding_col` in the generated legs.
        key_col: The primary-key column identifying a row (projected as the result `id`).
        text_col: The column the BM25 leg matches against.
        embedding_col: The column the vector leg compares against the query vector.
        hit_level: The literal tagging every hit from this surface (projected as `hit_level` and
            matched by the level facet). Must be unique within a `SearchSchema`.
        parent_id_expr: The SQL expression projecting a hit's parent-document id (self-referential
            for a top-level surface, a foreign key for a nested one, or a NULL literal when none
            applies). Projected as the result `page_id` column.
        title_expr: The SQL expression projecting a hit's display title (often reached through a
            joined facet pool).
        bm25_index: The name of the BM25 index the surface's keyword leg uses. Must be unique within
            a `SearchSchema`.
        join: An optional `JOIN` clause tying the surface to a facet pool (empty when the surface is
            itself the pool). Interpolated verbatim.
        membership: An optional predicate restricting the surface to a facet pool's row set (e.g. a
            semi-join against the pool's keys), empty when `join` already scopes it. May reference a
            facet through a `{facet}` placeholder.
        filters: Extra per-surface predicates ANDed into both legs (e.g. a per-surface currency
            check the shared facet pool cannot express). Each may reference a facet through a
            `{facet}` placeholder.
        partition_col: The column naming the partition a row belongs to, which together with
            `key_col` identifies the row. Set on every surface of a partitioned schema and on none
            of an unpartitioned one. `join` and `membership` must then match on it too.
        vector_filters: Extra predicates ANDed into the vector leg alone (e.g. keeping only rows
            embedded by the query's model). Each may reference a facet through a `{facet}`
            placeholder.
        title_pin: Whether a hit of this surface whose `title_expr` equals the query, ignoring case
            and surrounding spaces, goes first when no other candidate holds that title. Read only
            by a tuned schema.
        group_expr: The group a hit counts against for the tuned cap on rows per group, an
            expression over the surface. Empty groups a hit by `parent_id_expr`, and a NULL group
            by the hit's own key.
        document_expr: The document a hit counts against for the tuned cap on hits per document,
            an expression over the surface. Empty counts a hit against its group as its document,
            and a NULL document against the hit's own key. Read only when the tuning caps hits per
            document.
    """

    name: str
    table: str
    alias: str
    key_col: str
    text_col: str
    embedding_col: str
    hit_level: str
    parent_id_expr: str
    title_expr: str
    bm25_index: str
    join: str = ""
    membership: str = ""
    filters: tuple[str, ...] = ()
    partition_col: str = ""
    vector_filters: tuple[str, ...] = ()
    title_pin: bool = False
    group_expr: str = ""
    document_expr: str = ""

    def __post_init__(self) -> None:
        """Validate the identifier fields are present and `hit_level` is quote-safe.

        Raises:
            ValueError: When any of the required identifier fields is empty, or `hit_level` contains
                a single-quote character.
        """
        for label, value in (
            ("SearchSurface.name", self.name),
            ("SearchSurface.table", self.table),
            ("SearchSurface.alias", self.alias),
            ("SearchSurface.key_col", self.key_col),
            ("SearchSurface.text_col", self.text_col),
            ("SearchSurface.embedding_col", self.embedding_col),
            ("SearchSurface.hit_level", self.hit_level),
            ("SearchSurface.bm25_index", self.bm25_index),
        ):
            _require_non_empty(value, label)
        # `hit_level` is interpolated into single-quoted SQL literals, so a single quote would break out of them.
        if "'" in self.hit_level:
            msg = "SearchSurface.hit_level must not contain a single-quote character"
            raise ValueError(msg)


@dataclass(frozen=True)
class FacetPool:
    """A pre-filter CTE the facets are applied in once, then shared across surfaces.

    A surface either scans the pool directly or joins to it. A schema declares one pool per distinct
    facet-filtered relation.

    Attributes:
        name: The CTE name surfaces reference (e.g. `doc_pool`). Must be unique within a
            `SearchSchema`.
        table: The relation the pool scans, schema-qualified.
        alias: The `FROM` alias for `table`, used to qualify the `select_cols` and `predicates`.
        select_cols: The columns the pool projects (qualified SQL expressions), the surfaces read
            titles and keys from these. Interpolated verbatim.
        predicates: The facet WHERE fragments applied inside the pool (one per facet the pool
            filters on), ANDed together. Each references a facet through a `{facet}` placeholder.
    """

    name: str
    table: str
    alias: str
    select_cols: tuple[str, ...]
    predicates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate the pool names a relation and projects at least one column.

        Raises:
            ValueError: When `name` / `table` / `alias` is empty or `select_cols` is empty.
        """
        _require_non_empty(self.name, "FacetPool.name")
        _require_non_empty(self.table, "FacetPool.table")
        _require_non_empty(self.alias, "FacetPool.alias")
        if not self.select_cols:
            msg = "FacetPool.select_cols must project at least one column"
            raise ValueError(msg)


@dataclass(frozen=True)
class Facet:
    """One narrowing parameter of a search: a function argument the pools and legs filter on.

    A facet declares the shape of a parameter in the generated function signature. How it filters is
    set by the `FacetPool.predicates` and `SearchSurface.filters` that reference it.

    Attributes:
        name: The parameter name (e.g. `collection`). Must be unique within a `SearchSchema`.
        sql_type: The parameter's SQL type (e.g. `text`, `boolean`).
        default: The parameter's SQL default literal (e.g. `'general'`, `true`), or `None` for a
            `NULL` default.
    """

    name: str
    sql_type: str
    default: str | None = None

    def __post_init__(self) -> None:
        """Validate the facet names a parameter and a type.

        Raises:
            ValueError: When `name` or `sql_type` is empty.
        """
        _require_non_empty(self.name, "Facet.name")
        _require_non_empty(self.sql_type, "Facet.sql_type")


SEARCH_LEGS = ("lexical", "semantic")
"""The two legs every surface ranks with, as a weight's key names them after its surface."""


@dataclass(frozen=True)
class SearchTuning:
    """The numbers a generated search reads when it runs, each clamped by a bound generated into the function.

    The defaults below are rendered into the function. A row of `relation` overrides any of them by key, so a
    measurement changes a number with an `UPDATE` and no new function. The bounds hold whatever the row says.

    Attributes:
        relation: A relation whose jsonb column `tuning` overrides the defaults by key, or empty to read the defaults
            alone. It holds one row, `id = 1`, unless `partition_col` is set. The function reads it as its owner, so a
            reader needs no grant on it.
        budget: The most rows each leg of each surface ranks and hands to the fusion. Bound to 1..200, and never below
            the search's `k`.
        rrf_constant: The constant of reciprocal rank fusion. Bound to 1..1000.
        k_ceiling: The most rows one search returns. Bound to 1..50.
        snippet_chars: The longest snippet, in characters. Bound to 80..2000.
        rows_per_group: The most hits one group keeps, a pinned title outside the count, so one long
            page's parts never fill the result. Bound to 1..50.
        weights: A weight per surface and leg, keyed `<surface>:<lexical|semantic>`, 1 when not named. Each is bound
            to 0..10. A row's `weights` replaces these whole, so it names every weight it wants other than 1.
        max_hits_per_document: The most hits one document keeps after the cap on rows per group, its best first and a
            pinned title its best. Its next `FOLDED_UNITS_PER_HIT` hits are listed under its best hit as folded units,
            in a `folded_units` jsonb column returned last. Bound to 0..50, and 0 or a null in the row keeps every hit.
            `None` generates neither the cap nor the column.
        folded_snippet_chars: The longest snippet a folded unit carries, in characters. It is the unit's own snippet,
            whole when it fits, else cut at the last whole word inside this length, or mid-word when no word ends
            inside it, with an ellipsis. So it is never longer than `snippet_chars`. Bound to 20..2000. `None` lists
            folded units without a snippet. It needs `max_hits_per_document`.
        partition_col: The column of `relation` naming the partition a row tunes. Set, the row whose column is null
            holds the numbers for every partition, and a partition's own row may set `max_hits_per_document` and
            `folded_snippet_chars` for that partition's hits. Empty, `relation` holds one row, `id = 1`. It needs
            `relation` and `max_hits_per_document`, and a schema with a partition and a collection facet.
        partition_visible: A boolean SQL expression saying whether the reading session may see a partition, with
            `{partition}` standing for it. The cap function reads a partition's own row only when it holds, and the
            default row otherwise. Empty reads any partition's row. It needs `partition_col`.
    """

    relation: str = ""
    budget: int = 50
    rrf_constant: float = 60.0
    k_ceiling: int = 50
    snippet_chars: int = 480
    rows_per_group: int = 3
    weights: tuple[tuple[str, float], ...] = ()
    max_hits_per_document: int | None = None
    folded_snippet_chars: int | None = None
    partition_col: str = ""
    partition_visible: str = ""

    def __post_init__(self) -> None:
        """Validate the folded snippet length and the partition column against the cap and the relation.

        Raises:
            ValueError: When `folded_snippet_chars` is set without `max_hits_per_document`, or `partition_col` is set
                without `relation` or `max_hits_per_document`, or is not an identifier, or `partition_visible` is set
                without `partition_col` or without naming `{partition}`.
        """
        if self.folded_snippet_chars is not None and self.max_hits_per_document is None:
            msg = "SearchTuning.folded_snippet_chars needs max_hits_per_document, since only a capped search folds units"
            raise ValueError(msg)
        if self.partition_visible and "{partition}" not in self.partition_visible:
            msg = "SearchTuning.partition_visible must name the partition it judges as {partition}"
            raise ValueError(msg)
        if not self.partition_col:
            if self.partition_visible:
                msg = "SearchTuning.partition_visible needs partition_col, since it judges a partition's own row"
                raise ValueError(msg)
            return
        if not _IDENTIFIER.fullmatch(self.partition_col):
            msg = f"SearchTuning.partition_col {self.partition_col!r} must be an identifier"
            raise ValueError(msg)
        if not self.relation:
            msg = "SearchTuning.partition_col needs a relation, since its rows are what a partition tunes"
            raise ValueError(msg)
        if self.max_hits_per_document is None:
            msg = "SearchTuning.partition_col needs max_hits_per_document, the one number a partition's row sets beside the folded snippet length"
            raise ValueError(msg)


@dataclass(frozen=True)
class SearchSchema:
    """The searchable shape of a knowledge base: its surfaces, facet pools, and facets.

    A `SearchSchema` is the data a hybrid-search builder needs to generate a knowledge base's search
    function. The surfaces' legs are unioned in their order. The pools are the shared facet
    pre-filters the surfaces reference by name. The facets are the function's narrowing parameters.

    Attributes:
        surfaces: The searchable surfaces, in the order their legs are unioned. Must be non-empty
            with unique names, hit levels, and BM25 index names.
        pools: The facet pre-filter CTEs the surfaces join to. Must have unique names.
        facets: The narrowing parameters the pools and surfaces filter on. Must have unique names.
        embedding_sql_type: The SQL type of the query-vector parameter and the embedding columns
            (e.g. `halfvec(1024)`), fixing the knowledge base's embedding dimension.
        level_facet: The name of the facet that gates a surface by its `hit_level`. When set, every
            generated leg carries a `(<level_facet> IS NULL OR <level_facet> = '<hit_level>')`
            predicate so a query can pin one surface. Empty when the knowledge base has no such facet.
            Must name a declared facet.
        collection_facet: The name of the facet that scopes a query to one caller session, if any.
            When set, the search builder renders that facet's `DEFAULT` as the session's own setting
            (`current_setting('app.collection', true)`) regardless of the facet's own declared
            default, so an omitted argument never falls back to a fixed knowledge base name. Empty when the
            knowledge base has no such facet. Must name a declared facet.
        partition_result_col: The result column holding each hit's partition, returned right after
            `id`. Set, the schema is partitioned: every surface must name its `partition_col`, and
            the fusion keys a hit by its partition and its id together, so two partitions' rows that
            share an id stay two hits. It must not share a facet's name, because a result column and
            an argument cannot share one. Empty for an unpartitioned schema, whose hits are keyed by
            id alone.
        tuning: The numbers the search reads when it runs. Set, every leg stops at a budget, the semantic leg
            ranks its nearest rows by exact distance, the fusion weighs each leg, a lone exact title goes first,
            each group keeps at most `rows_per_group` hits and, when the tuning sets `max_hits_per_document`, each
            document keeps at most that many and folds the rest under its best hit, each folded unit with a snippet
            when the tuning sets `folded_snippet_chars`. A tuning with `partition_col` reads both of those numbers
            for each hit's own partition. Unset, the search renders
            its fixed constants. Every surface name must then be a plain identifier, since it names a weight.
    """

    surfaces: tuple[SearchSurface, ...]
    pools: tuple[FacetPool, ...] = ()
    facets: tuple[Facet, ...] = ()
    embedding_sql_type: str = "halfvec(1024)"
    level_facet: str = ""
    collection_facet: str = ""
    partition_result_col: str = ""
    tuning: SearchTuning | None = None

    def __post_init__(self) -> None:
        """Validate the schema declares at least one surface and no colliding names.

        Raises:
            ValueError: When there are no surfaces, or a surface name / hit level / BM25 index, a
                pool name, or a facet name is not unique, or `level_facet` / `collection_facet` names
                no declared facet, or the surfaces' partition columns disagree with
                `partition_result_col`, or a tuning read per partition sits on a schema with no partition or no
                collection facet.
        """
        if not self.surfaces:
            msg = "SearchSchema.surfaces must declare at least one surface"
            raise ValueError(msg)
        _require_non_empty(self.embedding_sql_type, "SearchSchema.embedding_sql_type")
        _require_unique((surface.name for surface in self.surfaces), "SearchSchema surface names")
        _require_unique((surface.hit_level for surface in self.surfaces), "SearchSchema surface hit levels")
        _require_unique((surface.bm25_index for surface in self.surfaces), "SearchSchema surface BM25 index names")
        _require_unique((pool.name for pool in self.pools), "SearchSchema pool names")
        _require_unique((facet.name for facet in self.facets), "SearchSchema facet names")
        if self.level_facet and self.level_facet not in {facet.name for facet in self.facets}:
            msg = f"SearchSchema.level_facet {self.level_facet!r} names no declared facet"
            raise ValueError(msg)
        if self.collection_facet and self.collection_facet not in {facet.name for facet in self.facets}:
            msg = f"SearchSchema.collection_facet {self.collection_facet!r} names no declared facet"
            raise ValueError(msg)
        self._check_partition()
        self._check_tuning()

    def _check_tuning(self) -> None:
        """Raise `ValueError` when a tuned schema names a surface that is no identifier, or weighs no surface's leg."""
        if self.tuning is None:
            return
        unnamed = [surface.name for surface in self.surfaces if not _IDENTIFIER.fullmatch(surface.name)]
        if unnamed:
            msg = f"SearchSchema.tuning names a weight after each surface, so these surface names must be identifiers: {', '.join(unnamed)}"
            raise ValueError(msg)
        known = {f"{surface.name}:{leg}" for surface in self.surfaces for leg in SEARCH_LEGS}
        unknown = [key for key, _weight in self.tuning.weights if key not in known]
        if unknown:
            msg = f"SearchSchema.tuning weighs no surface and leg of this schema: {', '.join(unknown)}"
            raise ValueError(msg)
        self._check_tuning_per_partition()
        declared = {"tuning", "capped_k", "leg_budget", "rrf_constant", "snippet_chars", "rows_per_group"}
        if self.tuning.max_hits_per_document is not None:
            declared.add("max_hits_per_document")
        if self.tuning.folded_snippet_chars is not None:
            declared.add("folded_snippet_chars")
        declared |= {f"weight_{surface.name}_{leg}" for surface in self.surfaces for leg in SEARCH_LEGS}
        taken = sorted(facet.name for facet in self.facets if facet.name in declared)
        if taken:
            msg = f"SearchSchema.tuning declares a variable these facets are named after: {', '.join(taken)}"
            raise ValueError(msg)

    def _check_tuning_per_partition(self) -> None:
        """Raise `ValueError` when a tuning read per partition sits on a schema with no partition or no collection facet."""
        if self.tuning is None or not self.tuning.partition_col:
            return
        if not self.partition_result_col:
            msg = "SearchTuning.partition_col reads a row per partition, so the schema must set partition_result_col"
            raise ValueError(msg)
        if not self.collection_facet:
            msg = "SearchTuning.partition_col needs the schema's collection_facet, which names the partition a cap is read for"
            raise ValueError(msg)

    def _check_partition(self) -> None:
        """Raise `ValueError` when the surfaces' partition columns disagree with `partition_result_col`."""
        if not self.partition_result_col:
            partitioned = [surface.name for surface in self.surfaces if surface.partition_col]
            if partitioned:
                msg = f"SearchSchema surfaces {', '.join(partitioned)} name a partition_col, so partition_result_col must be set"
                raise ValueError(msg)
            return
        unpartitioned = [surface.name for surface in self.surfaces if not surface.partition_col.strip()]
        if unpartitioned:
            msg = f"SearchSchema.partition_result_col is set, so every surface needs a partition_col: {', '.join(unpartitioned)}"
            raise ValueError(msg)
        if self.partition_result_col in {facet.name for facet in self.facets}:
            msg = f"SearchSchema.partition_result_col {self.partition_result_col!r} is also a facet's name"
            raise ValueError(msg)

    def surface(self, name: str) -> SearchSurface:
        """Return the surface with the given handle.

        Args:
            name: The `SearchSurface.name` to look up.

        Returns:
            The matching surface.

        Raises:
            KeyError: When no surface has that name.
        """
        for surface in self.surfaces:
            if surface.name == name:
                return surface
        msg = f"no surface named {name!r}"
        raise KeyError(msg)


@dataclass(frozen=True)
class GraphSchema:
    """The link-graph shape of a knowledge base: the edge relation walked and the node view joined for detail.

    A `GraphSchema` is the data a graph-traversal builder needs to generate a knowledge base's neighbourhood
    expansion and path-finding functions. The traversal walks the edge relation, and each reached node
    is joined to the node view for its display columns. The two result column sets (`expand_columns` /
    `paths_columns`) are derived from these fields, and both generated functions return them.

    Attributes:
        edge_table: The edge relation the traversal walks, schema-qualified (e.g. `graph.edges`).
        src_col: The edge column holding the source node id.
        dst_col: The edge column holding the destination node id.
        rel_col: The edge column holding the relation type.
        edge_payload_cols: Extra edge columns carried through onto each reached node (e.g. a
            provenance reference), in projection order.
        node_view: The relation joined for a node's display columns, schema-qualified (e.g.
            `api.nodes`).
        node_cols: The node columns projected for each reached node, in projection order. Must be
            non-empty. The first column is the node id joined on.
        node_col_types: The SQL types of `node_cols`, in the same order, naming each node column's
            type in the generated RETURNS clause. Empty defaults every node column to `text`. When
            non-empty it must have one type per node column.
        partition_col: The column that scopes a traversal to one logical partition (e.g. a knowledge base's
            collection), present under this same name on both `edge_table` and `node_view`. `None`
            (the default) generates an unpartitioned traversal on every backend. Set, both backends
            gain a leading `p_collection` parameter on both functions.
            `CteGraphTraversalBuilder` filters every edge walk and node read by this column during
            expansion, and `AgeGraphTraversalBuilder` projects the column onto every vertex and edge
            and discards any matched path containing a node from another partition.
        superseded_col: The boolean `node_view` column that is true when a newer node replaces this
            one. `None` (the default) generates a traversal that walks every node regardless of
            currency and takes no currency argument. Set, both functions gain a trailing
            `include_superseded boolean DEFAULT false` argument and skip a superseded node at every
            hop they reach, so nothing reachable only through one comes back either.
            `CteGraphTraversalBuilder` reads the column off the node view as it takes each edge, and
            `AgeGraphTraversalBuilder` projects it onto every vertex and drops any matched path
            carrying a superseded node past the caller's own seed.
        currency_source: The relation `AgeGraphTraversalBuilder`'s projection reads `superseded_col`
            from, `None` (the default) reading it from `node_view`. When `node_view` narrows to what
            the reading session may see, name the unnarrowed relation here so the projection reads
            currency for every node. It is a relation expression (a
            table, a view, or a parenthesized subquery) projecting `node_cols[0]`, `partition_col`
            and `superseded_col` under exactly those names, interpolated verbatim. Unused by
            `CteGraphTraversalBuilder`, which reads currency inside the caller's own query.
    """

    edge_table: str
    src_col: str
    dst_col: str
    rel_col: str
    node_view: str
    node_cols: tuple[str, ...]
    edge_payload_cols: tuple[str, ...] = field(default_factory=tuple)
    node_col_types: tuple[str, ...] = field(default_factory=tuple)
    partition_col: str | None = None
    superseded_col: str | None = None
    currency_source: str | None = None

    def __post_init__(self) -> None:
        """Validate the identifier fields are present and the node projection is non-empty.

        Raises:
            ValueError: When any identifier field is empty, `node_cols` is empty, `node_col_types` is
                non-empty and does not have one type per node column, or `partition_col` /
                `superseded_col` / `currency_source` is set but blank.
        """
        _require_non_empty(self.edge_table, "GraphSchema.edge_table")
        _require_non_empty(self.src_col, "GraphSchema.src_col")
        _require_non_empty(self.dst_col, "GraphSchema.dst_col")
        _require_non_empty(self.rel_col, "GraphSchema.rel_col")
        _require_non_empty(self.node_view, "GraphSchema.node_view")
        if not self.node_cols:
            msg = "GraphSchema.node_cols must project at least one column"
            raise ValueError(msg)
        if self.node_col_types and len(self.node_col_types) != len(self.node_cols):
            msg = f"GraphSchema.node_col_types must have one type per node column ({len(self.node_cols)}), got {len(self.node_col_types)}"
            raise ValueError(msg)
        if self.partition_col is not None:
            _require_non_empty(self.partition_col, "GraphSchema.partition_col")
        if self.superseded_col is not None:
            _require_non_empty(self.superseded_col, "GraphSchema.superseded_col")
        if self.currency_source is not None:
            _require_non_empty(self.currency_source, "GraphSchema.currency_source")

    @property
    def expand_columns(self) -> tuple[str, ...]:
        """The ordered `graph_expand` result columns: each node's columns plus the edge that reached it.

        The node columns come first, then the source, destination and relation columns and any edge
        payload columns.
        """
        return (*self.node_cols, self.src_col, self.dst_col, self.rel_col, *self.edge_payload_cols)

    @property
    def paths_columns(self) -> tuple[str, ...]:
        """The ordered `graph_paths` result columns: the `expand_columns` followed by the path rank and score."""
        return (*self.expand_columns, PATH_RANK_COLUMN, PATH_SCORE_COLUMN)
