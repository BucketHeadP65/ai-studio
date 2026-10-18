r"""The structural seeder over a real, migrated ParadeDB.

Exercises :class:`~engram.evals.generation.infrastructure.seeds.SeedQueries` — the read-only structural seeder
that draws :class:`~engram.evals.shared.domain.Seed`\s from the indexed knowledge base, gold ids known *by
construction* from graph structure (the LLM never picks them). It runs over a freshly-migrated
database inside the shared ParadeDB container (the `migrated` fixture), connecting as the
**owner/primary** `memory` role (the `migrated` URL's admin credentials), never `kb_reader` — 0003
walls the reader off `kb.*`, and the seeder reads `kb.*` tables directly.

Rather than index a full multi-paper knowledge base (which would have to coerce the indexer into emitting a
2-hop chain, a wide fan-out, a comparison matrix, and a synthesis cluster all at once), the module
inserts the minimal `kb.*` rows directly over the admin session — the sanctioned fallback — so each
archetype's gold-id correctness is asserted against a hand-built, fully-known graph shape. The whole
module skips when Docker is unavailable (the `pg` fixture, via `migrated`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sqlalchemy import NullPool, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from engram.evals.generation.domain.freshness import CaseFreshnessRef
from engram.evals.generation.domain.mirror import gold_page_bodies, mirror_hash, passage_bodies, pinpoint_bodies
from engram.evals.generation.infrastructure.freshness import SqlFreshnessQueries
from engram.evals.generation.infrastructure.seeds import SeedQueries
from engram.evals.generation.infrastructure.stratum_inventory import StratumInventoryQueries
from engram.evals.shared.domain import DEFAULT_DIFFICULTY_THRESHOLDS, Archetype
from engram.indexing.contracts import ClaimORM, EdgeORM, PageORM, SectionORM
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine

_COLLECTION = "research"
_OTHER_COLLECTION = "cooking"


@pytest.fixture
async def engine(migrated: str) -> AsyncIterator[AsyncEngine]:
    """A throwaway admin engine over the freshly-migrated DB (owner role, `NullPool`)."""
    eng = create_async_engine(migrated, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


def _page(page_id: str, page_type: str, *, collection: str = _COLLECTION, body_hash: str | None = None) -> PageORM:
    """A minimal `kb.pages` row, body_hash derived from the id unless overridden."""
    return PageORM(
        id=page_id,
        collection=collection,
        page_type=page_type,
        slug=page_id.split(".", 1)[-1],
        title=page_id,
        status="synthesized",
        card_text=f"card for {page_id}",
        body_hash=body_hash or f"hash-{page_id}",
    )


def _section(page_id: str, ord_: int, *, collection: str = _COLLECTION, heading: str = "Findings", body: str | None = None) -> SectionORM:
    """A minimal `kb.sections` row under `page_id` (ltree `path` is a safe `sN` label)."""
    section_id = f"{page_id}::s{ord_}"
    return SectionORM(
        id=section_id,
        collection=collection,
        page_id=page_id,
        heading=heading,
        path=f"s{ord_}",
        ord=ord_,
        body=body if body is not None else f"section body {section_id}",
        body_hash=f"sec-hash-{section_id}",
    )


def _long_body(page_id: str, length: int) -> str:
    """A section body of exactly `length` characters naming `page_id`."""
    return (f"A passage of {page_id} about its setup. " * 40)[:length]


def _claim(
    page_id: str, n: int, *, collection: str = _COLLECTION, kind: str = "observation", subject_id: str | None = None
) -> ClaimORM:
    """A minimal `kb.claims` row under `page_id`. `subject_collection` pairs `subject_id` with `collection`."""
    return ClaimORM(
        id=f"{page_id}#c{n}",
        collection=collection,
        page_id=page_id,
        block_id=f"c{n}",
        text=f"claim {n} on {page_id}",
        quote=f"verbatim quote {n}",
        loc="§1",
        kind=kind,
        subject_id=subject_id,
        subject_collection=collection if subject_id is not None else None,
        status="active",
    )


def _edge(
    src_id: str, rel: str, dst_id: str, *, src_kind: str = "page", src_collection: str = _COLLECTION, dst_collection: str = _COLLECTION
) -> EdgeORM:
    """A minimal `kb.edges` row (`dst_kind` is always `page` for resolved edges)."""
    return EdgeORM(
        src_collection=src_collection,
        src_id=src_id,
        src_kind=src_kind,
        rel=rel,
        dst_collection=dst_collection,
        dst_id=dst_id,
        dst_kind="page",
    )


async def _seed_knowledge_base(session: AsyncSession) -> None:
    """Insert a hand-built graph with one fully-known instance of every archetype.

    The shape:

    * pinpoint: `paper.alpha` has a `^c1` claim.
    * `cmp.ab` (kind=comparison) `compares` `paper.alpha` and `paper.beta`, a two-way fan-out.
    * survey: `paper.foundational` has 3 incoming `builds_on` edges (from alpha/beta/gamma).
    * trace: a 2-hop `builds_on` chain `paper.gamma -> paper.beta -> paper.alpha`.
    * synthesis: `con.memory` (kind=concept) `synthesizes` alpha/beta/gamma across its sections.
    * passage: `paper.foundational` has one section of 400 characters and one of 399. `meta.policy` (a meta
      page) and the off-collection `paper.other` each have a long section that must never be drawn.
    * an off-collection `paper.other` whose claim must never be seeded under `research`.
    """
    session.add_all(
        [
            _page("paper.alpha", "paper"),
            _page("paper.beta", "paper"),
            _page("paper.gamma", "paper"),
            _page("paper.foundational", "paper"),
            _page("cmp.ab", "comparison"),
            _page("con.memory", "concept"),
            _page("meta.policy", "meta"),
            _page("paper.other", "paper", collection=_OTHER_COLLECTION),
        ]
    )
    # Flush the pages before the FK-bearing children: sections and claims FK
    # kb.pages.id and there is no ORM relationship to impose insert order across `add_all` calls.
    await session.flush()
    session.add_all(
        [
            _section("paper.alpha", 0),
            _section("con.memory", 0, heading="Themes"),
            _section("con.memory", 1, heading="State of evidence"),
            _section("con.memory", 2, heading="Open questions"),
            _section("paper.foundational", 0, heading="Setup", body=_long_body("paper.foundational", 400)),
            _section("paper.foundational", 1, heading="Limits", body=_long_body("paper.foundational", 399)),
            _section("meta.policy", 0, heading="Rules", body=_long_body("meta.policy", 600)),
            _section("paper.other", 0, collection=_OTHER_COLLECTION, heading="Setup", body=_long_body("paper.other", 600)),
        ]
    )
    session.add_all([_claim("paper.alpha", 1), _claim("paper.other", 1, collection=_OTHER_COLLECTION)])
    session.add_all(
        [
            # a two-way compares fan-out
            _edge("cmp.ab", "compares", "paper.alpha"),
            _edge("cmp.ab", "compares", "paper.beta"),
            # survey fan-out onto paper.foundational
            _edge("paper.alpha", "builds_on", "paper.foundational"),
            _edge("paper.beta", "builds_on", "paper.foundational"),
            _edge("paper.gamma", "builds_on", "paper.foundational"),
            # trace chain gamma -> beta -> alpha (a 2-hop path over builds_on)
            _edge("paper.gamma", "builds_on", "paper.beta"),
            _edge("paper.beta", "builds_on", "paper.alpha"),
            # synthesis
            _edge("con.memory", "synthesizes", "paper.alpha"),
            _edge("con.memory", "synthesizes", "paper.beta"),
            _edge("con.memory", "synthesizes", "paper.gamma"),
            # an off-collection edge that must never leak in
            _edge("paper.other", "builds_on", "paper.foundational", src_collection=_OTHER_COLLECTION),
        ]
    )
    await session.commit()


@pytest.fixture
async def seeded(engine: AsyncEngine) -> AsyncEngine:
    """The migrated engine after the hand-built knowledge base is inserted."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await _seed_knowledge_base(session)
    return engine


def _repo(session: AsyncSession) -> SeedQueries:
    """A `SeedQueries` over `session` bound to the default research spec."""
    return SeedQueries(session, research_spec())


async def _anchor_hashes(session: AsyncSession) -> dict[str, str]:
    """Map every page id to its `body_hash` (the staleness anchor the seeder must record)."""
    result = await session.execute(select(PageORM.id, PageORM.body_hash))
    return {row.id: row.body_hash for row in result.all()}


class TestSeedRepository:
    async def test_pinpoint_returns_single_page_gold_id(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            seeds = await _repo(session).pinpoint_seeds(collection=_COLLECTION, page_ids=(), limit=10)
            anchors = await _anchor_hashes(session)

        assert seeds, "expected at least one pinpoint seed"
        seed = next(s for s in seeds if s.gold_ids == ("paper.alpha",))
        assert seed.archetype is Archetype.PINPOINT
        assert seed.n_sources == 1
        assert seed.n_hops == 0
        assert seed.difficulty == "easy"
        assert any(text for text in seed.source_bodies), "pinpoint must carry the claim / body context"
        assert [a.unit_id for a in seed.anchors] == ["paper.alpha"]
        assert seed.anchors[0].body_hash == anchors["paper.alpha"]

    async def test_pinpoint_excludes_off_collection_pages(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            seeds = await _repo(session).pinpoint_seeds(collection=_COLLECTION, page_ids=(), limit=10)

        assert all("paper.other" not in s.gold_ids for s in seeds)

    async def test_pinpoint_narrows_to_page_ids(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            narrowed = await _repo(session).pinpoint_seeds(collection=_COLLECTION, page_ids=("paper.beta",), limit=10)

        # The only claim under `research` is on paper.alpha, so narrowing to paper.beta yields none.
        assert narrowed == []

    async def test_survey_returns_fan_out_above_minimum(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            seeds = await _repo(session).survey_seeds(collection=_COLLECTION, page_ids=(), limit=10)

        assert seeds, "expected a survey seed for the foundational paper's fan-out"
        seed = next(s for s in seeds if "paper.foundational" in s.gold_ids)
        assert seed.archetype is Archetype.SURVEY
        # 3 contributing src ids fan into the foundational paper (off-collection paper.other excluded).
        contributors = set(seed.gold_ids) - {"paper.foundational"}
        assert contributors == {"paper.alpha", "paper.beta", "paper.gamma"}
        assert seed.n_sources >= 4
        assert seed.difficulty == "hard"

    async def test_trace_returns_multi_hop_path(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            seeds = await _repo(session).trace_seeds(collection=_COLLECTION, page_ids=(), limit=10)
            anchors = await _anchor_hashes(session)

        assert seeds, "expected a trace seed for the builds_on chain"
        seed = seeds[0]
        assert seed.archetype is Archetype.TRACE
        assert seed.n_hops >= 2
        assert len(seed.gold_ids) >= 3
        # Every node on the path is a real page; anchors mirror the page body hashes.
        assert all(a.body_hash == anchors[a.unit_id] for a in seed.anchors)
        assert {a.unit_id for a in seed.anchors} == set(seed.gold_ids)
        assert seed.difficulty == "hard"

    async def test_synthesis_returns_synthesized_cluster(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            seeds = await _repo(session).synthesis_seeds(collection=_COLLECTION, page_ids=(), limit=10)

        assert seeds, "expected a synthesis seed for con.memory"
        # con.memory `synthesizes` all three papers across its 3 distinct sections; pick that cluster.
        seed = next(s for s in seeds if set(s.gold_ids) == {"paper.alpha", "paper.beta", "paper.gamma"})
        assert seed.archetype is Archetype.SYNTHESIS
        assert "con.memory" not in seed.gold_ids
        # con.memory has 3 distinct sections, so the seed reports them.
        assert seed.n_sections == 3
        assert seed.difficulty == "hard"

    async def test_limit_caps_seed_count(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            capped = await _repo(session).survey_seeds(collection=_COLLECTION, page_ids=(), limit=0)

        assert capped == []


class TestPassageSeeds:
    """A passage seed is one section of at least 400 characters on a non-meta page, its page the gold."""

    async def test_draws_a_long_section_with_its_page_as_gold(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            seeds = await _repo(session).passage_seeds(collection=_COLLECTION, page_ids=(), limit=10)
            anchors = await _anchor_hashes(session)

        (seed,) = seeds
        assert seed.archetype is Archetype.PASSAGE
        assert seed.seed_key == "passage:paper.foundational::s0"
        assert seed.gold_ids == ("paper.foundational",)
        assert seed.source_bodies == passage_bodies(_long_body("paper.foundational", 400))
        assert seed.n_hops == 0
        assert seed.n_sections == 1
        assert seed.difficulty == "easy"
        assert [a.unit_id for a in seed.anchors] == ["paper.foundational"]
        assert seed.anchors[0].body_hash == anchors["paper.foundational"]

    async def test_skips_meta_pages_shorter_sections_and_other_knowledge_bases(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            seeds = await _repo(session).passage_seeds(collection=_COLLECTION, page_ids=(), limit=50, salt="any")

        # meta.policy's 600-character section, paper.foundational's 399-character section, the short
        # sections of paper.alpha and con.memory, and the cooking section are never drawn.
        assert [seed.seed_key for seed in seeds] == ["passage:paper.foundational::s0"]

    async def test_a_band_other_than_easy_draws_nothing(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            repo = _repo(session)
            medium = await repo.passage_seeds(collection=_COLLECTION, page_ids=(), limit=10, band="medium")
            hard = await repo.passage_seeds(collection=_COLLECTION, page_ids=(), limit=10, band="hard")

        assert medium == []
        assert hard == []

    async def test_narrows_to_page_ids_and_to_the_anchor_page_type(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            repo = _repo(session)
            other_page = await repo.passage_seeds(collection=_COLLECTION, page_ids=("paper.alpha",), limit=10)
            concept = await repo.passage_seeds(collection=_COLLECTION, page_ids=(), limit=10, anchor_page_type="concept")
            paper = await repo.passage_seeds(collection=_COLLECTION, page_ids=(), limit=10, anchor_page_type="paper")

        assert other_page == []
        assert concept == []
        assert [seed.gold_ids for seed in paper] == [("paper.foundational",)]

    async def test_a_knowledge_base_with_no_qualifying_section_draws_nothing(self, many_pinpoints: AsyncEngine):
        async with AsyncSession(many_pinpoints, expire_on_commit=False) as session:
            seeds = await _repo(session).passage_seeds(collection=_COLLECTION, page_ids=(), limit=10)

        assert seeds == []

    async def test_the_census_counts_the_sections_the_draw_takes(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            inventory = await StratumInventoryQueries(session, research_spec(), DEFAULT_DIFFICULTY_THRESHOLDS).inventory(collection=_COLLECTION)

        passage = {
            (count.stratum.difficulty, count.stratum.anchor_page_type): count.available
            for count in inventory.counts
            if count.stratum.archetype is Archetype.PASSAGE
        }
        assert passage == {("easy", "paper"): 1}

    async def test_the_freshness_reader_rebuilds_the_drafting_text_from_the_section_body(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            (seed,) = await _repo(session).passage_seeds(collection=_COLLECTION, page_ids=(), limit=10)
            drafted = CaseFreshnessRef(
                case_name=f"{seed.seed_key}#0",
                seed_key=seed.seed_key,
                archetype=Archetype.PASSAGE,
                gold_ids=seed.gold_ids,
                anchors=seed.anchors,
                mirror_hash=mirror_hash(seed.source_bodies),
            )
            gone = drafted.model_copy(
                update={"case_name": "passage:paper.foundational::s9#0", "seed_key": "passage:paper.foundational::s9"}
            )
            sources = await SqlFreshnessQueries(session).mirror_sources(refs=[drafted, gone])

        # The section body alone, never the page's card text, and a section that is gone has no entry.
        assert sources == {drafted.case_name: seed.source_bodies}
        assert mirror_hash(sources[drafted.case_name]) == drafted.mirror_hash


class TestMirrorSourceHydration:
    """Every seed carries the non-empty `source_bodies` its case is drafted from.

    A seed shipping empty `source_bodies` makes the quote check reject every draft from it.
    Each archetype carries its gold pages' card texts in gold-id order (`mirror.gold_page_bodies`);
    pinpoint carries its claim-text / quote / page-card order (`mirror.pinpoint_bodies`).
    """

    async def test_pinpoint_source_bodies_follow_pinpoint_order(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            seeds = await _repo(session).pinpoint_seeds(collection=_COLLECTION, page_ids=(), limit=10)

        seed = next(s for s in seeds if s.gold_ids == ("paper.alpha",))
        # Claim text, then the claim's quote, then the owning page's card text — the seeder's order.
        assert seed.source_bodies == pinpoint_bodies("claim 1 on paper.alpha", "verbatim quote 1", "card for paper.alpha")
        assert all(seed.source_bodies)

    @pytest.mark.parametrize("method_name", ["survey_seeds", "trace_seeds", "synthesis_seeds"])
    async def test_non_pinpoint_source_bodies_are_gold_card_texts_in_gold_order(self, seeded: AsyncEngine, method_name: str):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            method = getattr(_repo(session), method_name)
            seeds = await method(collection=_COLLECTION, page_ids=(), limit=10)

        assert seeds, f"{method_name} should draw at least one seed from the seeded knowledge_base"
        for seed in seeds:
            # Non-pinpoint mirror = the gold pages' card texts, order-preserving, one per gold id (the D7 fix).
            expected = gold_page_bodies(seed.gold_ids, {gold_id: f"card for {gold_id}" for gold_id in seed.gold_ids})
            assert seed.source_bodies == expected
            assert seed.source_bodies == tuple(f"card for {gold_id}" for gold_id in seed.gold_ids)
            assert all(seed.source_bodies)
            assert len(seed.source_bodies) == len(seed.gold_ids)


@pytest.fixture
async def many_pinpoints(engine: AsyncEngine) -> AsyncEngine:
    """A migrated engine seeded with a dozen single-claim `research` papers (for salted-draw ordering)."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add_all([_page(f"paper.doc{i:02d}", "paper") for i in range(12)])
        await session.flush()
        session.add_all([_claim(f"paper.doc{i:02d}", 1) for i in range(12)])
        await session.commit()
    return engine


# The difficulty bands each archetype can structurally produce in the hand-built `seeded` knowledge_base:
# pinpoint is single-source (easy); the foundational
# hub fans in three sources (hard survey); the builds_on chain is two hops (hard trace); con.memory
# synthesizes three papers across three sections (hard synthesis) while the 2-way builds_on / compares
# fan-outs are medium synthesis. paper.foundational's one long section is an easy passage.
_FEASIBLE_BANDS: dict[str, set[str]] = {
    "pinpoint_seeds": {"easy"},
    "survey_seeds": {"hard"},
    "trace_seeds": {"hard"},
    "synthesis_seeds": {"medium", "hard"},
    "passage_seeds": {"easy"},
}


class TestStratifiedSaltedDraws:
    """The Slice-C draws: salted-hash determinism, stratum isolation, and SQL/`derive_difficulty` parity."""

    async def test_same_and_empty_salt_draws_are_deterministic(self, many_pinpoints: AsyncEngine):
        async with AsyncSession(many_pinpoints, expire_on_commit=False) as session:
            repo = _repo(session)
            salted_a = await repo.pinpoint_seeds(collection=_COLLECTION, page_ids=(), limit=5, salt="s1")
            salted_b = await repo.pinpoint_seeds(collection=_COLLECTION, page_ids=(), limit=5, salt="s1")
            empty_a = await repo.pinpoint_seeds(collection=_COLLECTION, page_ids=(), limit=5, salt="")
            empty_b = await repo.pinpoint_seeds(collection=_COLLECTION, page_ids=(), limit=5, salt="")

        # Same salt (and the empty-salt default) is a reproducible md5 draw, identical order every time.
        assert [s.seed_key for s in salted_a] == [s.seed_key for s in salted_b]
        assert [s.seed_key for s in empty_a] == [s.seed_key for s in empty_b]

    async def test_a_different_salt_reshuffles_the_draw(self, many_pinpoints: AsyncEngine):
        async with AsyncSession(many_pinpoints, expire_on_commit=False) as session:
            repo = _repo(session)
            alpha = await repo.pinpoint_seeds(collection=_COLLECTION, page_ids=(), limit=100, salt="alpha")
            omega = await repo.pinpoint_seeds(collection=_COLLECTION, page_ids=(), limit=100, salt="omega")

        alpha_keys = [s.seed_key for s in alpha]
        omega_keys = [s.seed_key for s in omega]
        assert len(alpha_keys) == 12
        assert set(alpha_keys) == set(omega_keys)  # the same twelve claims are drawn under either salt
        assert alpha_keys != omega_keys  # but the hash order differs, so low-id bias is gone

    @pytest.mark.parametrize("method_name", list(_FEASIBLE_BANDS))
    async def test_band_draws_match_derive_difficulty_and_partition_the_whole_draw(self, seeded: AsyncEngine, method_name: str):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            method = getattr(_repo(session), method_name)
            whole = await method(collection=_COLLECTION, page_ids=(), limit=50, salt="parity")
            by_band = {
                band: await method(collection=_COLLECTION, page_ids=(), limit=50, salt="parity", band=band)
                for band in ("easy", "medium", "hard")
            }

        # Parity: every seed drawn for a band derives to that band in Python (SQL predicate == derive_difficulty).
        for band, seeds in by_band.items():
            assert all(seed.difficulty == band for seed in seeds), f"{method_name} @ {band}: the SQL band drifted from derive_difficulty"
        # Feasible bands are non-empty in this knowledge base; infeasible ones draw nothing (never a misread gap).
        for band in ("easy", "medium", "hard"):
            if band in _FEASIBLE_BANDS[method_name]:
                assert by_band[band], f"{method_name} should draw {band} seeds from the seeded knowledge_base"
            else:
                assert by_band[band] == [], f"{method_name} must not draw {band} seeds"
        # The per-band draws partition the whole-archetype draw exactly (same salt, same budget).
        union = {seed.seed_key for seeds in by_band.values() for seed in seeds}
        assert union == {seed.seed_key for seed in whole}

    async def test_band_isolates_the_requested_synthesis_cell(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            repo = _repo(session)
            hard = await repo.synthesis_seeds(collection=_COLLECTION, page_ids=(), limit=50, salt="cell", band="hard")
            medium = await repo.synthesis_seeds(collection=_COLLECTION, page_ids=(), limit=50, salt="cell", band="medium")

        # The hard cell is con.memory's 3-way, 3-section synthesis; the medium cell is the 2-way fan-outs.
        assert {frozenset(s.gold_ids) for s in hard} == {frozenset({"paper.alpha", "paper.beta", "paper.gamma"})}
        assert all(s.difficulty == "hard" for s in hard)
        assert medium, "the 2-way builds_on / compares fan-outs are medium synthesis seeds"
        assert all(s.difficulty == "medium" for s in medium)
        assert all("con.memory" not in s.seed_key for s in medium)  # the hard cluster never leaks into the medium cell

    async def test_anchor_kind_narrows_to_the_anchor_page_type(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            repo = _repo(session)
            concept_synthesis = await repo.synthesis_seeds(collection=_COLLECTION, page_ids=(), limit=50, salt="k", anchor_page_type="concept")
            concept_pinpoint = await repo.pinpoint_seeds(collection=_COLLECTION, page_ids=(), limit=50, salt="k", anchor_page_type="concept")
            paper_pinpoint = await repo.pinpoint_seeds(collection=_COLLECTION, page_ids=(), limit=50, salt="k", anchor_page_type="paper")

        # Only con.memory (kind=concept) fans out over `synthesizes`, so a concept anchor isolates it.
        assert [s.seed_key for s in concept_synthesis] == ["synthesis:synthesizes:con.memory"]
        # The only research claim sits on paper.alpha (a paper), so a concept anchor yields no pinpoint.
        assert concept_pinpoint == []
        assert {s.gold_ids for s in paper_pinpoint} == {("paper.alpha",)}

    async def test_trace_staged_draw_returns_valid_acyclic_two_hop_paths(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            seeds = await _repo(session).trace_seeds(collection=_COLLECTION, page_ids=(), limit=50, salt="trace")

        assert seeds, "the staged draw must find the builds_on chain's two-hop paths"
        seen: set[tuple[str, ...]] = set()
        for seed in seeds:
            assert seed.n_hops == 2
            assert len(seed.gold_ids) == 3
            assert len(set(seed.gold_ids)) == 3  # acyclic: three distinct nodes
            assert seed.gold_ids not in seen  # the staged assembly never repeats a path
            seen.add(seed.gold_ids)
            assert seed.difficulty == "hard"
