r"""The read-only seeder that draws structural `Seed`s from the indexed knowledge base.

A seed's gold ids come from the graph's structure, never from a model.
Every draw orders by `md5(stable_key || salt)` in SQL under a `LIMIT`, so the same knowledge base, salt
and budget always give the same seeds.
A `band` filter uses `band_case`, the SQL form of `derive_difficulty`, so a seed's SQL band matches its
derived difficulty.
You must pass an owner session, since the reader role has no grant on `kb.*`. Every read is a `SELECT`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from sqlalchemy import and_, case, func, literal, or_, select, tuple_
from sqlalchemy.orm import aliased

from engram.evals.generation.domain.mirror import gold_page_bodies, passage_bodies, pinpoint_bodies
from engram.evals.generation.domain.seed_shapes import MIN_PASSAGE_CHARS, MIN_SURVEY_FANOUT, MIN_SYNTHESIS_FANOUT, TRACE_HOPS
from engram.evals.shared.contracts import DEFAULT_DIFFICULTY_THRESHOLDS, AnchorHash, Archetype, Seed, derive_difficulty
from engram.indexing.contracts import ClaimORM, EdgeORM, PageORM, SectionORM

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from sqlalchemy import ColumnElement, Result, Select
    from sqlalchemy.sql import SQLColumnExpression

    from engram.evals.shared.contracts import DifficultyThresholds, QaDifficulty
    from engram.kb.contracts import KnowledgeBaseSchema

# How many first-hop edges the trace draw samples per requested seed.
_TRACE_FIRST_HOP_BUFFER = 4

# The most second-hop edges the trace draw keeps per middle node.
_TRACE_DEGREE_CAP = 4

# The page type engram gives its own `_meta/` pages. A passage is never drawn from one.
_META_PAGE_TYPE = "meta"


class SupportsExecute(Protocol):
    """A session that runs a typed `SELECT`. An `AsyncSession` is one."""

    async def execute[T: tuple[object, ...]](self, statement: Select[T]) -> Result[T]:
        """Run a typed `SELECT`.

        Args:
            statement: The query to run.

        Returns:
            The query's result.
        """
        ...


class SeedQueries:
    """The seed reader that draws structural `Seed`s from the indexed knowledge base.

    Each seed carries one `AnchorHash` per gold page with that page's `body_hash`, so a freshness check
    can tell when a source changed under its case.
    """

    def __init__(
        self, session: SupportsExecute, spec: KnowledgeBaseSchema, thresholds: DifficultyThresholds = DEFAULT_DIFFICULTY_THRESHOLDS
    ) -> None:
        """Hold the session, the schema whose relations the draws follow, and the difficulty cutoffs.

        Args:
            session: The owner session. Only `SELECT`s are issued on it.
            spec: The knowledge base's schema. Its relations and its claims' comparative relations are
                the relations the draws follow.
            thresholds: The difficulty cutoffs a `band` filter uses.
        """
        self._session = session
        self._spec = spec
        self._thresholds = thresholds

    async def pinpoint_seeds(  # noqa: PLR0913 (each stratified-draw input is distinct, and bundling them would obscure the seeder contract)
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        """Draw pinpoint seeds from claims, in salted-hash order of the claim id.

        Args:
            collection: The knowledge base to draw claims from.
            page_ids: Restrict to claims on these pages, or `()` for the whole knowledge base.
            limit: The most seeds to return.
            salt: The salt mixed into the hash order.
            band: Restrict to this difficulty, or `None` for every difficulty.
            anchor_page_type: Restrict to claims whose page has this page type, or `None` for any.

        Returns:
            Up to `limit` pinpoint seeds, each with one gold page id. Empty when `band` asks for a
            difficulty other than `derive_difficulty(1, 0, 1)`.
        """
        if limit <= 0 or _skips_fixed_band(band, derive_difficulty(1, 0, 1, self._thresholds)):
            return []
        conditions = [PageORM.collection == collection]
        if anchor_page_type is not None:
            conditions.append(PageORM.page_type == anchor_page_type)
        statement = (
            select(ClaimORM.id, ClaimORM.page_id, ClaimORM.text, ClaimORM.quote)
            .join(PageORM, PageORM.id == ClaimORM.page_id)
            .where(*conditions)
            .order_by(_salted_hash(ClaimORM.id, salt))
            .limit(limit)
        )
        rows = (await self._session.execute(_in_pages(statement, page_ids, ClaimORM.page_id))).all()
        anchors = await self._body_hashes(tuple(row.page_id for row in rows))
        bodies = await self._page_bodies(tuple(row.page_id for row in rows))
        return [
            Seed(
                archetype=Archetype.PINPOINT,
                gold_ids=(row.page_id,),
                source_bodies=pinpoint_bodies(row.text, row.quote, bodies.get(row.page_id)),
                seed_key=f"pinpoint:{row.id}",
                n_hops=0,
                n_sections=1,
                anchors=_anchors_for((row.page_id,), anchors),
            )
            for row in rows
        ]

    async def survey_seeds(  # noqa: PLR0913 (each stratified-draw input is distinct, and bundling them would obscure the seeder contract)
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        """Draw survey seeds from hubs with at least `MIN_SURVEY_FANOUT` sources, in salted-hash order of `rel:dst`.

        Args:
            collection: The knowledge base the hub and its sources must belong to.
            page_ids: Restrict the hub to these pages, or `()` for the whole knowledge base.
            limit: The most seeds to return.
            salt: The salt mixed into the hash order.
            band: Restrict to this difficulty, or `None` for every difficulty.
            anchor_page_type: Restrict to hub pages of this page type, or `None` for any.

        Returns:
            Up to `limit` survey seeds, each over a hub's incoming fan-out.
        """
        if limit <= 0:
            return []
        relations = self._relation_slugs()
        src_page = aliased(PageORM, name="survey_src")
        dst_page = aliased(PageORM, name="survey_dst")
        n_contributors = func.count(EdgeORM.src_id.distinct())
        conditions = [src_page.collection == collection, dst_page.collection == collection, EdgeORM.rel.in_(relations)]
        if anchor_page_type is not None:
            conditions.append(dst_page.page_type == anchor_page_type)
        statement = (
            select(EdgeORM.rel, EdgeORM.dst_id)
            .join(src_page, src_page.id == EdgeORM.src_id)
            .join(dst_page, dst_page.id == EdgeORM.dst_id)
            .where(*conditions)
            .group_by(EdgeORM.rel, EdgeORM.dst_id)
            .having(n_contributors >= MIN_SURVEY_FANOUT)
        )
        statement = _in_pages(statement, page_ids, EdgeORM.dst_id)
        if band is not None:
            n_sources = n_contributors + literal(1)  # the hub itself plus its distinct contributors
            statement = statement.having(band_case(n_sources, literal(0), n_sources, self._thresholds) == band)
        statement = statement.order_by(_salted_hash(EdgeORM.rel.concat(":").concat(EdgeORM.dst_id), salt)).limit(limit)
        hubs = [(row.rel, row.dst_id) for row in (await self._session.execute(statement)).all()]
        return await self._build_survey_seeds(collection, relations, hubs)

    async def trace_seeds(  # noqa: PLR0913 (each stratified-draw input is distinct, and bundling them would obscure the seeder contract)
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        """Draw trace seeds in stages: first hops by salted hash, then a capped number of second hops per middle node.

        Args:
            collection: The knowledge base every node on the path must belong to.
            page_ids: Restrict the path's start or end to these pages, or `()` for the whole knowledge base.
            limit: The most seeds to return.
            salt: The salt mixed into the hash order.
            band: Restrict to this difficulty, or `None` for every difficulty.
            anchor_page_type: Restrict to paths whose start page has this page type, or `None` for any.

        Returns:
            Up to `limit` trace seeds, each over an acyclic `TRACE_HOPS`-hop path of three distinct pages.
            Empty when `band` asks for a difficulty other than `derive_difficulty(3, 2, 1)`.
        """
        if limit <= 0 or _skips_fixed_band(band, derive_difficulty(TRACE_HOPS + 1, TRACE_HOPS, 1, self._thresholds)):
            return []
        relations = self._relation_slugs()
        first_hops = await self._trace_first_hops(collection, relations, salt, anchor_page_type, limit)
        if not first_hops:
            return []
        second_by_mid = await self._trace_second_hops(collection, relations, salt, tuple({mid for _src, mid in first_hops}))
        paths = _assemble_trace_paths(first_hops, second_by_mid, page_ids, limit)
        nodes = tuple({node for path in paths for node in path})
        anchors = await self._body_hashes(nodes)
        bodies = await self._page_bodies(nodes)
        return [
            Seed(
                archetype=Archetype.TRACE,
                gold_ids=path,
                source_bodies=gold_page_bodies(path, bodies),
                seed_key=f"trace:{'>'.join(path)}",
                n_hops=len(path) - 1,
                n_sections=1,
                anchors=_anchors_for(path, anchors),
            )
            for path in paths
        ]

    async def synthesis_seeds(  # noqa: PLR0913 (each stratified-draw input is distinct, and bundling them would obscure the seeder contract)
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        """Draw synthesis seeds from pages reaching at least `MIN_SYNTHESIS_FANOUT` pages, in salted-hash order of `src:rel`.

        Args:
            collection: The knowledge base the source page and its targets must belong to.
            page_ids: Restrict the source page to these pages, or `()` for the whole knowledge base.
            limit: The most seeds to return.
            salt: The salt mixed into the hash order.
            band: Restrict to this difficulty, or `None` for every difficulty.
            anchor_page_type: Restrict to source pages of this page type, or `None` for any.

        Returns:
            Up to `limit` synthesis seeds, each over a source page's outgoing fan-out. Each seed's
            `n_sections` is the source page's section count, at least 1.
        """
        if limit <= 0:
            return []
        relations = self._relation_slugs()
        src_page = aliased(PageORM, name="synth_src")
        dst_page = aliased(PageORM, name="synth_dst")
        n_reached = func.count(EdgeORM.dst_id.distinct())
        conditions = [src_page.collection == collection, dst_page.collection == collection, EdgeORM.rel.in_(relations)]
        if anchor_page_type is not None:
            conditions.append(src_page.page_type == anchor_page_type)
        statement = (
            select(EdgeORM.src_id, EdgeORM.rel)
            .join(src_page, src_page.id == EdgeORM.src_id)
            .join(dst_page, dst_page.id == EdgeORM.dst_id)
            .where(*conditions)
            .group_by(EdgeORM.src_id, EdgeORM.rel)
            .having(n_reached >= MIN_SYNTHESIS_FANOUT)
        )
        statement = _in_pages(statement, page_ids, EdgeORM.src_id)
        if band is not None:
            section_count = select(func.count()).select_from(SectionORM).where(SectionORM.page_id == EdgeORM.src_id).scalar_subquery()
            n_sections = func.greatest(func.coalesce(section_count, literal(0)), literal(1))
            statement = statement.having(band_case(n_reached, literal(0), n_sections, self._thresholds) == band)
        statement = statement.order_by(_salted_hash(EdgeORM.src_id.concat(":").concat(EdgeORM.rel), salt)).limit(limit)
        sources = [(row.src_id, row.rel) for row in (await self._session.execute(statement)).all()]
        return await self._build_synthesis_seeds(collection, relations, sources)

    async def passage_seeds(  # noqa: PLR0913 (each stratified-draw input is distinct, and bundling them would obscure the seeder contract)
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        """Draw passage seeds from the sections `passage_sections` keeps, in salted-hash order of the section id.

        Args:
            collection: The knowledge base to draw sections from.
            page_ids: Restrict to sections of these pages, or `()` for the whole knowledge base.
            limit: The most seeds to return.
            salt: The salt mixed into the hash order.
            band: Restrict to this difficulty, or `None` for every difficulty.
            anchor_page_type: Restrict to sections whose page has this page type, or `None` for any.

        Returns:
            Up to `limit` passage seeds, each with its section's page as the one gold id and the section's
            body as its source text. Empty when `band` asks for a difficulty other than `derive_difficulty(1, 0, 1)`.
        """
        if limit <= 0 or _skips_fixed_band(band, derive_difficulty(1, 0, 1, self._thresholds)):
            return []
        statement = passage_sections(select(SectionORM.id, SectionORM.page_id, SectionORM.body), collection)
        if anchor_page_type is not None:
            statement = statement.where(PageORM.page_type == anchor_page_type)
        statement = statement.order_by(_salted_hash(SectionORM.id, salt)).limit(limit)
        rows = (await self._session.execute(_in_pages(statement, page_ids, SectionORM.page_id))).all()
        anchors = await self._body_hashes(tuple(row.page_id for row in rows))
        return [
            Seed(
                archetype=Archetype.PASSAGE,
                gold_ids=(row.page_id,),
                source_bodies=passage_bodies(row.body),
                seed_key=f"passage:{row.id}",
                n_hops=0,
                n_sections=1,
                anchors=_anchors_for((row.page_id,), anchors),
            )
            for row in rows
        ]

    def _relation_slugs(self) -> frozenset[str]:
        """The schema's relations plus its claims' comparative relations."""
        slugs = {relation.slug for relation in self._spec.relations}
        if self._spec.claims is not None:
            slugs.update(self._spec.claims.comparative_relations)
        return frozenset(slugs)

    async def _build_survey_seeds(self, collection: str, relations: frozenset[str], hubs: list[tuple[str, str]]) -> list[Seed]:
        """Fetch the drawn hubs' contributors in the same knowledge base and build one seed each, in draw order."""
        if not hubs:
            return []
        src_page = aliased(PageORM, name="survey_src_fetch")
        dst_page = aliased(PageORM, name="survey_dst_fetch")
        statement = (
            select(EdgeORM.rel, EdgeORM.dst_id, EdgeORM.src_id)
            .join(src_page, src_page.id == EdgeORM.src_id)
            .join(dst_page, dst_page.id == EdgeORM.dst_id)
            .where(
                src_page.collection == collection,
                dst_page.collection == collection,
                EdgeORM.rel.in_(relations),
                tuple_(EdgeORM.rel, EdgeORM.dst_id).in_(hubs),
            )
            .distinct()
            .order_by(EdgeORM.dst_id, EdgeORM.rel, EdgeORM.src_id)
        )
        contributors: dict[tuple[str, str], list[str]] = {}
        for row in (await self._session.execute(statement)).all():
            contributors.setdefault((row.rel, row.dst_id), []).append(row.src_id)
        hub_pages = tuple({dst for _rel, dst in hubs} | {src for sources in contributors.values() for src in sources})
        anchors = await self._body_hashes(hub_pages)
        bodies = await self._page_bodies(hub_pages)
        seeds: list[Seed] = []
        for rel, dst_id in hubs:
            gold = (dst_id, *contributors.get((rel, dst_id), []))
            seeds.append(
                Seed(
                    archetype=Archetype.SURVEY,
                    gold_ids=gold,
                    source_bodies=gold_page_bodies(gold, bodies),
                    seed_key=f"survey:{rel}:{dst_id}",
                    n_hops=0,
                    n_sections=len(gold),
                    anchors=_anchors_for(gold, anchors),
                )
            )
        return seeds

    async def _build_synthesis_seeds(self, collection: str, relations: frozenset[str], sources: list[tuple[str, str]]) -> list[Seed]:
        """Fetch reached pages and section counts for the drawn sources and build one seed each, in draw order."""
        if not sources:
            return []
        src_page = aliased(PageORM, name="synth_src_fetch")
        dst_page = aliased(PageORM, name="synth_dst_fetch")
        statement = (
            select(EdgeORM.src_id, EdgeORM.rel, EdgeORM.dst_id)
            .join(src_page, src_page.id == EdgeORM.src_id)
            .join(dst_page, dst_page.id == EdgeORM.dst_id)
            .where(
                src_page.collection == collection,
                dst_page.collection == collection,
                EdgeORM.rel.in_(relations),
                tuple_(EdgeORM.src_id, EdgeORM.rel).in_(sources),
            )
            .distinct()
            .order_by(EdgeORM.src_id, EdgeORM.rel, EdgeORM.dst_id)
        )
        reached: dict[tuple[str, str], list[str]] = {}
        for row in (await self._session.execute(statement)).all():
            reached.setdefault((row.src_id, row.rel), []).append(row.dst_id)
        section_counts = await self._section_counts(tuple({src for src, _rel in sources}))
        reached_pages = tuple({dst for targets in reached.values() for dst in targets})
        anchors = await self._body_hashes(reached_pages)
        bodies = await self._page_bodies(reached_pages)
        seeds: list[Seed] = []
        for src_id, rel in sources:
            gold = tuple(reached.get((src_id, rel), []))
            if not gold:
                continue
            seeds.append(
                Seed(
                    archetype=Archetype.SYNTHESIS,
                    gold_ids=gold,
                    source_bodies=gold_page_bodies(gold, bodies),
                    seed_key=f"synthesis:{rel}:{src_id}",
                    n_hops=0,
                    n_sections=max(section_counts.get(src_id, 0), 1),
                    anchors=_anchors_for(gold, anchors),
                )
            )
        return seeds

    async def _trace_first_hops(
        self, collection: str, relations: frozenset[str], salt: str, anchor_page_type: str | None, limit: int
    ) -> list[tuple[str, str]]:
        """Sample up to `limit * _TRACE_FIRST_HOP_BUFFER` first-hop `(src, mid)` edges by salted hash."""
        src_page = aliased(PageORM, name="trace_src")
        mid_page = aliased(PageORM, name="trace_mid")
        conditions = [
            src_page.collection == collection,
            mid_page.collection == collection,
            EdgeORM.rel.in_(relations),
            EdgeORM.src_id != EdgeORM.dst_id,
        ]
        if anchor_page_type is not None:
            conditions.append(src_page.page_type == anchor_page_type)
        statement = (
            select(EdgeORM.src_id, EdgeORM.dst_id)
            .join(src_page, src_page.id == EdgeORM.src_id)
            .join(mid_page, mid_page.id == EdgeORM.dst_id)
            .where(*conditions)
            .group_by(EdgeORM.src_id, EdgeORM.dst_id)
            .order_by(_salted_hash(EdgeORM.src_id.concat(">").concat(EdgeORM.dst_id), salt))
            .limit(limit * _TRACE_FIRST_HOP_BUFFER)
        )
        return [(row.src_id, row.dst_id) for row in (await self._session.execute(statement)).all()]

    async def _trace_second_hops(
        self, collection: str, relations: frozenset[str], salt: str, mids: tuple[str, ...]
    ) -> dict[str, list[str]]:
        """Extend the drawn mids to second-hop dsts, capping each mid's out-degree at `_TRACE_DEGREE_CAP`."""
        if not mids:
            return {}
        mid_page = aliased(PageORM, name="trace_mid2")
        dst_page = aliased(PageORM, name="trace_dst")
        ranked = (
            select(
                EdgeORM.src_id.label("mid"),
                EdgeORM.dst_id.label("dst"),
                func.row_number()
                .over(partition_by=EdgeORM.src_id, order_by=_salted_hash(EdgeORM.src_id.concat(">").concat(EdgeORM.dst_id), salt))
                .label("rn"),
            )
            .join(mid_page, mid_page.id == EdgeORM.src_id)
            .join(dst_page, dst_page.id == EdgeORM.dst_id)
            .where(
                mid_page.collection == collection,
                dst_page.collection == collection,
                EdgeORM.rel.in_(relations),
                EdgeORM.src_id != EdgeORM.dst_id,
                EdgeORM.src_id.in_(mids),
            )
            .subquery()
        )
        statement = select(ranked.c.mid, ranked.c.dst).where(ranked.c.rn <= _TRACE_DEGREE_CAP).order_by(ranked.c.mid, ranked.c.rn)
        second_by_mid: dict[str, list[str]] = {}
        for row in (await self._session.execute(statement)).all():
            second_by_mid.setdefault(row.mid, []).append(row.dst)
        return second_by_mid

    async def _body_hashes(self, page_ids: tuple[str, ...]) -> Mapping[str, str]:
        """Map each requested page id to its `body_hash` (the staleness anchor)."""
        if not page_ids:
            return {}
        statement = select(PageORM.id, PageORM.body_hash).where(PageORM.id.in_(page_ids))
        return {row.id: row.body_hash for row in (await self._session.execute(statement)).all()}

    async def _page_bodies(self, page_ids: tuple[str, ...]) -> Mapping[str, str]:
        """Map each requested page id to its `card_text` (the page-level context body)."""
        if not page_ids:
            return {}
        statement = select(PageORM.id, PageORM.card_text).where(PageORM.id.in_(page_ids))
        return {row.id: row.card_text for row in (await self._session.execute(statement)).all()}

    async def _section_counts(self, page_ids: tuple[str, ...]) -> Mapping[str, int]:
        """Map each requested page id to its distinct section count (absent pages omitted, read as zero)."""
        if not page_ids:
            return {}
        statement = select(SectionORM.page_id, func.count().label("n")).where(SectionORM.page_id.in_(page_ids)).group_by(SectionORM.page_id)
        return {row.page_id: row.n for row in (await self._session.execute(statement)).all()}


def _salted_hash(key: SQLColumnExpression[str], salt: str) -> ColumnElement[str]:
    """The salted draw-order expression `md5(key || salt)`, the salt bound as a parameter."""
    return func.md5(key.concat(salt))


def passage_sections[T: tuple[object, ...]](statement: Select[T], collection: str) -> Select[T]:
    """Join a `kb.sections` statement to each section's page and keep only the sections that seed a `passage`.

    A section qualifies when it belongs to `collection`, its body has at least `MIN_PASSAGE_CHARS`
    characters and its page is not a meta page. The seed draw and the census both use it, so they count
    the same sections.

    Args:
        statement: A `SELECT` whose `FROM` is `kb.sections`.
        collection: The knowledge base the sections must belong to.

    Returns:
        The statement joined to `kb.pages` on the collection and page id, narrowed to qualifying sections.
    """
    return statement.join(PageORM, and_(PageORM.collection == SectionORM.collection, PageORM.id == SectionORM.page_id)).where(
        SectionORM.collection == collection, PageORM.page_type != _META_PAGE_TYPE, func.length(SectionORM.body) >= MIN_PASSAGE_CHARS
    )


def band_case(
    n_sources: ColumnElement[int], n_hops: ColumnElement[int], n_sections: ColumnElement[int], thresholds: DifficultyThresholds
) -> ColumnElement[str]:
    """Build the SQL `CASE` form of `derive_difficulty`, its cutoffs bound as parameters.

    For the same counts and thresholds it gives the same band as `derive_difficulty`.

    Args:
        n_sources: The seed's source-count expression.
        n_hops: The seed's hop-count expression.
        n_sections: The seed's section-count expression.
        thresholds: The band cutoffs whose integer values are bound into the predicate.

    Returns:
        A `CASE` expression evaluating to `"easy"`, `"medium"` or `"hard"`.
    """
    return case(
        (and_(n_sources <= thresholds.single_source, n_hops == 0), "easy"),
        (
            or_(
                n_hops >= thresholds.deep_path_hops,
                n_sources >= thresholds.wide_fanout_sources,
                and_(n_sources >= thresholds.cross_section_sources, n_sections >= thresholds.cross_section_sections),
            ),
            "hard",
        ),
        else_="medium",
    )


def _skips_fixed_band(band: QaDifficulty | None, fixed_band: QaDifficulty) -> bool:
    """Whether a fixed-shape archetype (pinpoint, passage or trace) skips the draw because `band` asks for another band."""
    return band is not None and band != fixed_band


def _assemble_trace_paths(
    first_hops: list[tuple[str, str]], second_by_mid: dict[str, list[str]], page_ids: tuple[str, ...], limit: int
) -> list[tuple[str, str, str]]:
    """Up to `limit` distinct acyclic `src -> mid -> dst` paths in draw order, starting or ending in `page_ids` when given."""
    wanted = set(page_ids)
    paths: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for src, mid in first_hops:
        for dst in second_by_mid.get(mid, ()):
            path = (src, mid, dst)
            if len({src, mid, dst}) != TRACE_HOPS + 1 or path in seen:
                continue
            if wanted and not ({src, dst} & wanted):
                continue
            seen.add(path)
            paths.append(path)
            if len(paths) >= limit:
                return paths
    return paths


def _in_pages[T: tuple[object, ...]](statement: Select[T], page_ids: tuple[str, ...], column: SQLColumnExpression[str]) -> Select[T]:
    """Narrow `statement` to rows whose `column` is in `page_ids`, or leave it unfiltered when empty."""
    if not page_ids:
        return statement
    return statement.where(column.in_(page_ids))


def _anchors_for(page_ids: tuple[str, ...], body_hashes: Mapping[str, str]) -> tuple[AnchorHash, ...]:
    """Build one `AnchorHash` per page id that has a known body hash, in id order."""
    return tuple(AnchorHash(unit_id=page_id, body_hash=body_hashes[page_id]) for page_id in page_ids if page_id in body_hashes)


def _group_ordered(pairs: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    """Group `(key, value)` pairs into an insertion-ordered `key -> [distinct values]` mapping."""
    grouped: dict[str, list[str]] = {}
    for key, value in pairs:
        bucket = grouped.setdefault(key, [])
        if value not in bucket:
            bucket.append(value)
    return grouped
