"""The SELECT-only adapter that reads the current knowledge base state a freshness check compares against."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select, tuple_

from engram.evals.generation.domain.mirror import gold_page_bodies, passage_bodies, pinpoint_bodies
from engram.evals.shared.contracts import Archetype
from engram.indexing.contracts import ClaimORM, EdgeORM, PageORM, SectionORM

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from engram.evals.generation.domain.freshness import CaseFreshnessRef
    from engram.evals.generation.infrastructure.seeds import SupportsExecute

# A pinpoint case's seed key is `pinpoint:{claim_id}`. Stripping this prefix gives the claim id.
_PINPOINT_SEED_PREFIX = "pinpoint:"

# A passage case's seed key is `passage:{section_id}`. Stripping this prefix gives the section id.
_PASSAGE_SEED_PREFIX = "passage:"


class SqlFreshnessQueries:
    """Reads the current knowledge base state a freshness check compares a dataset against.

    You must pass an owner session, since the reader role has no grant on the `kb.*` tables.
    Every read is a `SELECT`.
    """

    def __init__(self, session: SupportsExecute) -> None:
        """Hold the session every read runs over.

        Args:
            session: The owner session. Only `SELECT`s are issued on it.
        """
        self._session = session

    async def page_state(self, *, collection: str, page_ids: frozenset[str]) -> Mapping[str, str]:
        """Read the current card body hashes for the requested pages, keyed by page id, in one SELECT.

        Args:
            collection: The knowledge base the pages must belong to.
            page_ids: The page ids to read, every anchored and gold page of the checked cases.

        Returns:
            Each page id that still exists, mapped to its current card body hash. An id missing from the
            mapping has left the knowledge base.
        """
        if not page_ids:
            return {}
        statement = select(PageORM.id, PageORM.body_hash).where(PageORM.collection == collection, PageORM.id.in_(page_ids))
        return {row.id: row.body_hash for row in (await self._session.execute(statement)).all()}

    async def mirror_sources(self, *, refs: Sequence[CaseFreshnessRef]) -> Mapping[str, tuple[str, ...]]:
        """Read the current parts of each case's drafting text, keyed by case name.

        A pinpoint case's parts are its claim's text and quote and its page's card text. A passage case's
        part is its section's body. Any other case's parts are its gold pages' card texts. A pinpoint case
        whose claim is gone, or a passage case whose section is gone, has no entry.

        Args:
            refs: The checked cases' freshness refs.

        Returns:
            Each case name mapped to its current drafting-text parts, for every case whose parts could be read.
        """
        pinpoint = [ref for ref in refs if ref.archetype == Archetype.PINPOINT]
        passage = [ref for ref in refs if ref.archetype == Archetype.PASSAGE]
        others = [ref for ref in refs if ref.archetype not in {Archetype.PINPOINT, Archetype.PASSAGE}]
        sources: dict[str, tuple[str, ...]] = {}
        sources.update(await self._pinpoint_sources(pinpoint))
        sources.update(await self._passage_sources(passage))
        sources.update(await self._gold_sources(others))
        return sources

    async def _pinpoint_sources(self, refs: Sequence[CaseFreshnessRef]) -> dict[str, tuple[str, ...]]:
        """Read each pinpoint case's claim text, quote and page card text, keyed by case name, cases sharing a claim included."""
        claim_to_cases: dict[str, list[str]] = {}
        for ref in refs:
            if ref.seed_key is not None:
                claim_to_cases.setdefault(ref.seed_key.removeprefix(_PINPOINT_SEED_PREFIX), []).append(ref.case_name)
        if not claim_to_cases:
            return {}
        statement = (
            select(ClaimORM.id, ClaimORM.text, ClaimORM.quote, PageORM.card_text)
            .join(PageORM, PageORM.id == ClaimORM.page_id)
            .where(ClaimORM.id.in_(list(claim_to_cases)))
        )
        return {
            case_name: pinpoint_bodies(row.text, row.quote, row.card_text)
            for row in (await self._session.execute(statement)).all()
            for case_name in claim_to_cases[row.id]
        }

    async def _passage_sources(self, refs: Sequence[CaseFreshnessRef]) -> dict[str, tuple[str, ...]]:
        """Read each passage case's section body, keyed by case name, cases sharing a section included."""
        section_to_cases: dict[str, list[str]] = {}
        for ref in refs:
            if ref.seed_key is not None:
                section_to_cases.setdefault(ref.seed_key.removeprefix(_PASSAGE_SEED_PREFIX), []).append(ref.case_name)
        if not section_to_cases:
            return {}
        statement = select(SectionORM.id, SectionORM.body).where(SectionORM.id.in_(list(section_to_cases)))
        return {
            case_name: passage_bodies(row.body)
            for row in (await self._session.execute(statement)).all()
            for case_name in section_to_cases[row.id]
        }

    async def _gold_sources(self, refs: Sequence[CaseFreshnessRef]) -> dict[str, tuple[str, ...]]:
        """Read each survey, trace or synthesis case's gold page card texts, keyed by case name."""
        gold_ids = frozenset(gold_id for ref in refs for gold_id in ref.gold_ids)
        card_texts: dict[str, str] = {}
        if gold_ids:
            statement = select(PageORM.id, PageORM.card_text).where(PageORM.id.in_(gold_ids))
            card_texts = {row.id: row.card_text for row in (await self._session.execute(statement)).all()}
        return {ref.case_name: gold_page_bodies(ref.gold_ids, card_texts) for ref in refs}

    async def edge_pairs_present(self, *, pairs: frozenset[tuple[str, str]]) -> frozenset[tuple[str, str]]:
        """Which of the requested `(src, dst)` pairs exist in `kb.edges` over any relation, in one `SELECT`.

        Args:
            pairs: The trace premises' consecutive hop pairs.

        Returns:
            The subset of `pairs` with at least one live edge.
        """
        if not pairs:
            return frozenset()
        statement = select(EdgeORM.src_id, EdgeORM.dst_id).where(tuple_(EdgeORM.src_id, EdgeORM.dst_id).in_(pairs)).distinct()
        return frozenset((row.src_id, row.dst_id) for row in (await self._session.execute(statement)).all())

    async def inbound_fans(self, *, keys: frozenset[tuple[str, str]]) -> Mapping[tuple[str, str], int]:
        """Each `(rel, dst)` key's distinct inbound source count in `kb.edges`, in one grouped `SELECT`.

        Args:
            keys: The survey premises' `(rel, dst)` hub keys.

        Returns:
            The key to distinct-`src_id` count mapping (a key with no live edges has no entry).
        """
        if not keys:
            return {}
        statement = (
            select(EdgeORM.rel, EdgeORM.dst_id, func.count(func.distinct(EdgeORM.src_id)).label("fan"))
            .where(tuple_(EdgeORM.rel, EdgeORM.dst_id).in_(keys))
            .group_by(EdgeORM.rel, EdgeORM.dst_id)
        )
        return {(row.rel, row.dst_id): row.fan for row in (await self._session.execute(statement)).all()}

    async def outbound_fans(self, *, keys: frozenset[tuple[str, str]]) -> Mapping[tuple[str, str], int]:
        """Each `(rel, src)` key's distinct reached-page count in `kb.edges`, in one grouped `SELECT`.

        Args:
            keys: The synthesis premises' `(rel, src)` fan keys.

        Returns:
            The key to distinct-`dst_id` count mapping (a key with no live edges has no entry).
        """
        if not keys:
            return {}
        statement = (
            select(EdgeORM.rel, EdgeORM.src_id, func.count(func.distinct(EdgeORM.dst_id)).label("fan"))
            .where(tuple_(EdgeORM.rel, EdgeORM.src_id).in_(keys))
            .group_by(EdgeORM.rel, EdgeORM.src_id)
        )
        return {(row.rel, row.src_id): row.fan for row in (await self._session.execute(statement)).all()}
