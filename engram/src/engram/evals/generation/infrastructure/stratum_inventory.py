"""The census adapter that counts the seed pool per stratum over the indexed knowledge base."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, literal, select
from sqlalchemy.orm import aliased

from engram.evals.generation.domain.coverage import DIFFICULTY_ORDER, Stratum, StratumCount, StratumInventory
from engram.evals.generation.domain.seed_shapes import MIN_SURVEY_FANOUT, MIN_SYNTHESIS_FANOUT, TRACE_HOPS
from engram.evals.generation.infrastructure.seeds import band_case, passage_sections
from engram.evals.shared.contracts import Archetype, QaDifficulty, derive_difficulty
from engram.indexing.contracts import ClaimORM, EdgeORM, IndexFingerprintORM, PageORM, SectionORM

if TYPE_CHECKING:
    from engram.evals.generation.infrastructure.seeds import SupportsExecute
    from engram.evals.shared.contracts import DifficultyThresholds
    from engram.kb.contracts import KnowledgeBaseSchema


_NOT_INDEXED = "not indexed"
"""The index build of a knowledge base that holds no stored indexer version."""


def _as_difficulty(value: str) -> QaDifficulty:
    """Narrow a band string from the SQL `CASE` to `QaDifficulty`, raising `ValueError` on any other value."""
    match value:
        case "easy" | "medium" | "hard":
            return value
        case _:
            msg = f"unexpected difficulty band from SQL: {value!r}"
            raise ValueError(msg)


class StratumInventoryQueries:
    """The census that counts a knowledge base's seed pool per stratum, one grouped aggregate per archetype.

    You must pass an owner session, since the reader role has no grant on the `kb.*` tables. Every read
    is a `SELECT`. Each count applies the same structural thresholds and difficulty bands as the seed draws.
    """

    def __init__(self, session: SupportsExecute, spec: KnowledgeBaseSchema, thresholds: DifficultyThresholds) -> None:
        """Hold the session, the schema whose relations the counts follow, and the difficulty cutoffs.

        Args:
            session: The owner session. Only `SELECT`s are issued on it.
            spec: The knowledge base's schema. Its relations and its claims' comparative relations are
                the relations the survey, trace and synthesis counts follow.
            thresholds: The difficulty cutoffs the SQL bands use.
        """
        self._session = session
        self._spec = spec
        self._thresholds = thresholds

    async def inventory(self, *, collection: str) -> StratumInventory:
        """Count the seed pool of a knowledge base per stratum.

        Args:
            collection: The knowledge base to count.

        Returns:
            The inventory, its counts ordered by archetype, difficulty and anchor page type.
        """
        relations = self._relation_slugs()
        counts: list[StratumCount] = []
        counts.extend(await self._pinpoint_counts(collection))
        counts.extend(await self._survey_counts(collection, relations))
        counts.extend(await self._trace_counts(collection, relations))
        counts.extend(await self._synthesis_counts(collection, relations))
        counts.extend(await self._passage_counts(collection))
        counts.sort(
            key=lambda count: (count.stratum.archetype.value, DIFFICULTY_ORDER[count.stratum.difficulty], count.stratum.anchor_page_type)
        )
        return StratumInventory(collection=collection, counts=tuple(counts))

    async def knowledge_base_fingerprint(self, *, collection: str) -> str:
        """The knowledge base's identity for a dataset stamp: its `kb.index_fingerprint` `spec_hash` and its name.

        A knowledge base that was never indexed has an empty `spec_hash`, so its fingerprint is `:{collection}`.

        Args:
            collection: The knowledge base whose fingerprint is read.

        Returns:
            The `"{spec_hash}:{collection}"` string.
        """
        statement = select(IndexFingerprintORM.spec_hash).where(IndexFingerprintORM.collection == collection)
        spec_hash = (await self._session.execute(statement)).scalar_one_or_none()
        return f"{spec_hash or ''}:{collection}"

    async def index_build(self, *, collection: str) -> str:
        """The index build in `kb.index_fingerprint`: its `indexer_version`, which carries the chunk sizes, then its `embed_model`.

        Args:
            collection: The knowledge base whose index build is read.

        Returns:
            The two separated by a space, or `not indexed` when the knowledge base has no stored indexer version.
        """
        statement = select(IndexFingerprintORM.indexer_version, IndexFingerprintORM.embed_model).where(IndexFingerprintORM.collection == collection)
        row = (await self._session.execute(statement)).one_or_none()
        if row is None or not row.indexer_version:
            return _NOT_INDEXED
        return f"{row.indexer_version} {row.embed_model}".strip()

    async def page_ids(self, *, collection: str) -> frozenset[str]:
        """The ids of every page now in a knowledge base, which a top-up checks gold pages against.

        Args:
            collection: The knowledge base whose page ids are read.

        Returns:
            The page ids.
        """
        statement = select(PageORM.id).where(PageORM.collection == collection)
        return frozenset((await self._session.execute(statement)).scalars().all())

    def _relation_slugs(self) -> frozenset[str]:
        """The schema's relations plus its claims' comparative relations."""
        slugs = {relation.slug for relation in self._spec.relations}
        if self._spec.claims is not None:
            slugs.update(self._spec.claims.comparative_relations)
        return frozenset(slugs)

    async def _pinpoint_counts(self, collection: str) -> list[StratumCount]:
        """Count claims per page type of their page, one pinpoint seed each at the fixed pinpoint difficulty."""
        statement = (
            select(PageORM.page_type, func.count().label("n"))
            .select_from(ClaimORM)
            .join(PageORM, PageORM.id == ClaimORM.page_id)
            .where(PageORM.collection == collection)
            .group_by(PageORM.page_type)
        )
        difficulty = derive_difficulty(1, 0, 1, self._thresholds)
        rows = (await self._session.execute(statement)).all()
        return [
            StratumCount(stratum=Stratum(archetype=Archetype.PINPOINT, difficulty=difficulty, anchor_page_type=row.page_type), available=row.n)
            for row in rows
        ]

    async def _survey_counts(self, collection: str, relations: frozenset[str]) -> list[StratumCount]:
        """Count hubs per `(page type, band)`, one survey seed per wide incoming fan-out over one relation."""
        src_page = aliased(PageORM, name="survey_src_inv")
        dst_page = aliased(PageORM, name="survey_dst_inv")
        n_contributors = func.count(EdgeORM.src_id.distinct())
        inner = (
            select(dst_page.page_type.label("page_type"), n_contributors.label("n_contributors"))
            .select_from(EdgeORM)
            .join(src_page, src_page.id == EdgeORM.src_id)
            .join(dst_page, dst_page.id == EdgeORM.dst_id)
            .where(src_page.collection == collection, dst_page.collection == collection, EdgeORM.rel.in_(relations))
            .group_by(EdgeORM.rel, EdgeORM.dst_id, dst_page.page_type)
            .having(n_contributors >= MIN_SURVEY_FANOUT)
            .subquery()
        )
        n_sources = inner.c.n_contributors + literal(1)  # the hub itself plus its distinct contributors
        band = band_case(n_sources, literal(0), n_sources, self._thresholds)
        statement = select(inner.c.page_type, band.label("band"), func.count().label("n")).group_by(inner.c.page_type, band)
        rows = (await self._session.execute(statement)).all()
        return [
            StratumCount(
                stratum=Stratum(archetype=Archetype.SURVEY, difficulty=_as_difficulty(row.band), anchor_page_type=row.page_type), available=row.n
            )
            for row in rows
        ]

    async def _trace_counts(self, collection: str, relations: frozenset[str]) -> list[StratumCount]:
        """Count distinct acyclic `src -> mid -> dst` paths per page type of the start page, at the fixed trace difficulty."""
        second = aliased(EdgeORM, name="trace_e2_inv")
        src_page = aliased(PageORM, name="trace_src_inv")
        mid_page = aliased(PageORM, name="trace_mid_inv")
        dst_page = aliased(PageORM, name="trace_dst_inv")
        path_key = EdgeORM.src_id.concat(">").concat(EdgeORM.dst_id).concat(">").concat(second.dst_id)
        statement = (
            select(src_page.page_type, func.count(path_key.distinct()).label("n"))
            .select_from(EdgeORM)
            .join(second, second.src_id == EdgeORM.dst_id)
            .join(src_page, src_page.id == EdgeORM.src_id)
            .join(mid_page, mid_page.id == EdgeORM.dst_id)
            .join(dst_page, dst_page.id == second.dst_id)
            .where(
                src_page.collection == collection,
                mid_page.collection == collection,
                dst_page.collection == collection,
                EdgeORM.rel.in_(relations),
                second.rel.in_(relations),
                EdgeORM.src_id != EdgeORM.dst_id,
                second.src_id != second.dst_id,
                EdgeORM.src_id != second.dst_id,
            )
            .group_by(src_page.page_type)
        )
        difficulty = derive_difficulty(TRACE_HOPS + 1, TRACE_HOPS, 1, self._thresholds)
        rows = (await self._session.execute(statement)).all()
        return [
            StratumCount(stratum=Stratum(archetype=Archetype.TRACE, difficulty=difficulty, anchor_page_type=row.page_type), available=row.n)
            for row in rows
        ]

    async def _synthesis_counts(self, collection: str, relations: frozenset[str]) -> list[StratumCount]:
        """Count source pages per `(page type, band)`, one synthesis seed per wide outgoing fan-out over one relation."""
        src_page = aliased(PageORM, name="synth_src_inv")
        dst_page = aliased(PageORM, name="synth_dst_inv")
        n_reached = func.count(EdgeORM.dst_id.distinct())
        section_count = select(func.count()).select_from(SectionORM).where(SectionORM.page_id == EdgeORM.src_id).scalar_subquery()
        n_sections = func.greatest(func.coalesce(section_count, literal(0)), literal(1))
        inner = (
            select(src_page.page_type.label("page_type"), n_reached.label("n_reached"), n_sections.label("n_sections"))
            .select_from(EdgeORM)
            .join(src_page, src_page.id == EdgeORM.src_id)
            .join(dst_page, dst_page.id == EdgeORM.dst_id)
            .where(src_page.collection == collection, dst_page.collection == collection, EdgeORM.rel.in_(relations))
            .group_by(EdgeORM.src_id, EdgeORM.rel, src_page.page_type)
            .having(n_reached >= MIN_SYNTHESIS_FANOUT)
            .subquery()
        )
        band = band_case(inner.c.n_reached, literal(0), inner.c.n_sections, self._thresholds)
        statement = select(inner.c.page_type, band.label("band"), func.count().label("n")).group_by(inner.c.page_type, band)
        rows = (await self._session.execute(statement)).all()
        return [
            StratumCount(
                stratum=Stratum(archetype=Archetype.SYNTHESIS, difficulty=_as_difficulty(row.band), anchor_page_type=row.page_type), available=row.n
            )
            for row in rows
        ]

    async def _passage_counts(self, collection: str) -> list[StratumCount]:
        """Count the sections `passage_sections` keeps per page type of their page, at the fixed passage difficulty."""
        statement = passage_sections(select(PageORM.page_type, func.count().label("n")).select_from(SectionORM), collection).group_by(
            PageORM.page_type
        )
        difficulty = derive_difficulty(1, 0, 1, self._thresholds)
        rows = (await self._session.execute(statement)).all()
        return [
            StratumCount(stratum=Stratum(archetype=Archetype.PASSAGE, difficulty=difficulty, anchor_page_type=row.page_type), available=row.n)
            for row in rows
        ]
