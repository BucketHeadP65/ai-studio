"""Unit specs for the coverage-scaled `GenerationPipeline`.

The pipeline is pure orchestration over already-built collaborators, it does no file IO. It censuses
the knowledge base per stratum through an injected reader, allocates a per-cell case quota over the feasible
strata, and for each cell draws its stratum's structural seeds (salted, `band` + `anchor_page_type`
narrowed), drafts cases per seed, runs each draft through the `accept` quote check (so a
fabricated quote is dropped), tags each survivor with its stable `{seed_key}#{ordinal}` identity,
deduplicates, stratifies to the allocation within the `total_cases` budget, and builds an `EvaluationDataset`.

Every collaborator is faked network-free: a `FakeInventoryReader` returns a canned `StratumInventory`,
a hand-written `FakeSeedQueries` (satisfying `SupportsSeeding`) returns canned seeds per archetype
and records the `(limit, band, anchor_page_type)` of each draw, a `FakeCaseDrafter` quotes verbatim from each
seed's own source bodies so the gate keeps quote-checked drafts (and a scripted fabricator proves the gate
drops a hallucinated quote), and a real `CaseCurator` over the deterministic `FakeEmbedding` exercises
the dedup geometry. One test drives the real `PydanticAiDrafter` over a `TestModel` to prove the
pydantic-ai path is wired and never touches the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from pydantic import ValidationError
from pydantic_ai.models.test import TestModel
from pydantic_evals import Dataset

from engram.evals.generation.contracts import (
    CaseCurator,
    GenerationConfig,
    GenerationPipeline,
    SeedCaseDraft,
    Stratum,
    StratumCount,
    StratumInventory,
    allocate,
)
from engram.evals.generation.domain.drafting import SeedCaseDrafts
from engram.evals.generation.infrastructure.drafter import PydanticAiDrafter
from engram.evals.generation.service.pipeline import _SEED_LIMIT_BUFFER, GeneratedDraw
from engram.evals.shared.domain import AnchorHash, Archetype, DifficultyThresholds, QaDifficulty, RetrievalAnswer, Seed
from engram.testing.fakes.embedding import FakeEmbedding

if TYPE_CHECKING:
    from engram.evals.generation.contracts import AuthoredCase, CaseDrafter, QuotaAllocation

# Two source bodies a draft's support quotes are checked against (whitespace-normalized substring).
_BODY_PINPOINT = "Graph index construction incurs substantial token cost during entity and relation extraction."
_BODY_CROSS_SECTION_A = "The map-reduce summarization step dominates query-time latency for global questions."
_BODY_CROSS_SECTION_B = "Local search answers entity-centric questions with far lower latency than global search."


def _pinpoint_seed(suffix: str) -> Seed:
    """A single-source pinpoint seed (one gold id, no hop, one section, easy by default)."""
    return Seed(
        archetype=Archetype.PINPOINT,
        gold_ids=(f"paper.p{suffix}",),
        source_bodies=(_BODY_PINPOINT,),
        seed_key=f"pinpoint:paper.p{suffix}",
        anchors=(AnchorHash(unit_id=f"paper.p{suffix}", body_hash=f"hp{suffix}"),),
    )


def _cross_section_seed(suffix: str) -> Seed:
    """A cross-section synthesis seed (two gold ids spanning three sections, hard by default)."""
    return Seed(
        archetype=Archetype.SYNTHESIS,
        gold_ids=(f"paper.c{suffix}a", f"paper.c{suffix}b"),
        source_bodies=(_BODY_CROSS_SECTION_A, _BODY_CROSS_SECTION_B),
        seed_key=f"synthesis:extends:paper.c{suffix}",
        n_sections=3,
        anchors=(
            AnchorHash(unit_id=f"paper.c{suffix}a", body_hash=f"hc{suffix}a"),
            AnchorHash(unit_id=f"paper.c{suffix}b", body_hash=f"hc{suffix}b"),
        ),
    )


def _inventory(*cells: tuple[Archetype, QaDifficulty, str, int], collection: str = "research") -> StratumInventory:
    """Build a `StratumInventory` from `(archetype, difficulty, anchor_page_type, available)` tuples."""
    return StratumInventory(
        collection=collection,
        counts=tuple(StratumCount(stratum=Stratum(archetype=a, difficulty=d, anchor_page_type=k), available=n) for a, d, k, n in cells),
    )


class FakeInventoryReader:
    """A `SupportsInventory` stand-in returning a canned `StratumInventory`, recording the collections asked.

    `canned` is the inventory every `inventory` call returns (a public, scriptable attribute), and
    `collections` records each requested collection so a test can assert the pipeline censuses the knowledge base
    it was asked to.
    """

    def __init__(self, inventory: StratumInventory) -> None:
        self.canned = inventory
        self.collections: list[str] = []

    async def inventory(self, *, collection: str) -> StratumInventory:
        self.collections.append(collection)
        return self.canned


@dataclass
class _SeedCall:
    """One recorded seeder call: the `limit` and the salted `band` / `anchor_page_type` stratum it was drawn for."""

    limit: int
    band: QaDifficulty | None
    anchor_page_type: str | None


class FakeSeedQueries:
    """A `SupportsSeeding` stand-in returning canned seeds per archetype, capped at `limit`.

    `seeds` maps an `Archetype` to the seeds its seeder method should return (a public, scriptable
    attribute, repo fakes convention). `calls` records the per-archetype `_SeedCall`s so a test can assert
    the pipeline draws per cell with the right per-cell `limit`, `band`, and `anchor_page_type`. Each method
    honours only `limit`, the structural narrowing on `collection` / `page_ids` is the real repository's
    concern, exercised in its own suite.
    """

    def __init__(self, seeds: dict[Archetype, list[Seed]]) -> None:
        self.seeds = {archetype: list(values) for archetype, values in seeds.items()}
        self.calls: dict[Archetype, list[_SeedCall]] = {}

    async def pinpoint_seeds(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        del collection, page_ids, salt
        return self._for(Archetype.PINPOINT, limit, band, anchor_page_type)

    async def survey_seeds(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        del collection, page_ids, salt
        return self._for(Archetype.SURVEY, limit, band, anchor_page_type)

    async def trace_seeds(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        del collection, page_ids, salt
        return self._for(Archetype.TRACE, limit, band, anchor_page_type)

    async def synthesis_seeds(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        del collection, page_ids, salt
        return self._for(Archetype.SYNTHESIS, limit, band, anchor_page_type)

    async def passage_seeds(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        del collection, page_ids, salt
        return self._for(Archetype.PASSAGE, limit, band, anchor_page_type)

    def _for(self, archetype: Archetype, limit: int, band: QaDifficulty | None, anchor_page_type: str | None) -> list[Seed]:
        self.calls.setdefault(archetype, []).append(_SeedCall(limit=limit, band=band, anchor_page_type=anchor_page_type))
        return self.seeds.get(archetype, [])[:limit]


class FakeCaseDrafter:
    """A `CaseDrafter` that drafts `target` cases per seed, quoting verbatim from the seed itself.

    Each draft's support quote is a verbatim slice of the seed's first source body, so the `accept`
    gate keeps every draft regardless of archetype, the pipeline's quote-checked-by-construction path.
    """

    def __init__(self, *, quote_len: int = 12) -> None:
        self.quote_len = quote_len

    async def draft(self, seed: Seed, *, instructions: str, target: int) -> list[SeedCaseDraft]:
        del instructions
        quote = seed.source_bodies[0][: self.quote_len]
        return [SeedCaseDraft(question=f"Q-{seed.seed_key}-{index}", answer="A", support_quotes=[quote]) for index in range(target)]


class FabricatingCaseDrafter:
    """A `CaseDrafter` whose every draft quotes text absent from any source body (the gate drops all)."""

    async def draft(self, seed: Seed, *, instructions: str, target: int) -> list[SeedCaseDraft]:
        del instructions
        return [
            SeedCaseDraft(question=f"Q-{seed.seed_key}-{index}", answer="A", support_quotes=["a phrase that is nowhere"])
            for index in range(target)
        ]


class CopyingCaseCurator:
    """A `SupportsCuration` fake whose `dedup` returns fresh copies of its survivors.

    Case identity must ride on the case itself, never on an identity-keyed side table: a curator
    that copies instances would break name stamping (`KeyError` at `names[id(authored)]`), whereas
    copies must carry their names through unchanged. A plain fake with no embedder or dedup
    threshold, it also proves the pipeline drives only the `SupportsCuration` slice.
    """

    async def dedup(self, cases: list[AuthoredCase]) -> list[AuthoredCase]:
        return [case.model_copy() for case in cases]

    def stratify_cells(self, cases: list[AuthoredCase], allocation: QuotaAllocation) -> list[AuthoredCase]:
        del allocation
        return cases


def _config(
    *, distribution: dict[Archetype, float], total_cases: int, target_per_seed: int, thresholds: DifficultyThresholds | None = None
) -> GenerationConfig:
    """Build a `GenerationConfig` from typed parts (thresholds default to the standard bands)."""
    if thresholds is None:
        return GenerationConfig(target_distribution=distribution, total_cases=total_cases, target_per_seed=target_per_seed)
    return GenerationConfig(
        target_distribution=distribution, total_cases=total_cases, target_per_seed=target_per_seed, thresholds=thresholds
    )


def _pipeline(
    repo: FakeSeedQueries, reader: FakeInventoryReader, config: GenerationConfig, *, drafter: CaseDrafter | None = None, salt: str = ""
) -> GenerationPipeline:
    """Build the pipeline over a real curator (a high dedup cutoff so orthogonal hash vectors never collapse)."""
    curator = CaseCurator(FakeEmbedding(), dedup_threshold=0.99)
    return GenerationPipeline(repo, drafter or FakeCaseDrafter(), curator, config, reader, salt=salt)


class TestGenerationConfig:
    def test_is_frozen(self):
        config = _config(distribution={Archetype.PINPOINT: 1.0}, total_cases=4, target_per_seed=2)

        with pytest.raises(ValidationError):
            config.total_cases = 99

    def test_defaults_match_the_spec(self):
        config = GenerationConfig(target_distribution={Archetype.PINPOINT: 1.0})

        assert config.total_cases == 50
        assert config.target_per_seed == 3
        assert config.dedup_threshold == 0.92
        assert config.thresholds == DifficultyThresholds()
        assert config.salt is None


class TestGeneratePipeline:
    async def test_spans_requested_archetypes_with_seed_gold_ids(self):
        pinpoint, cross_section = _pinpoint_seed("1"), _cross_section_seed("1")
        repo = FakeSeedQueries({Archetype.PINPOINT: [pinpoint], Archetype.SYNTHESIS: [cross_section]})
        reader = FakeInventoryReader(_inventory((Archetype.PINPOINT, "easy", "paper", 5), (Archetype.SYNTHESIS, "hard", "paper", 5)))
        config = _config(distribution={Archetype.PINPOINT: 0.5, Archetype.SYNTHESIS: 0.5}, total_cases=10, target_per_seed=1)
        pipeline = _pipeline(repo, reader, config)

        dataset = (await pipeline.generate(collection="research", page_ids=(), archetypes=[Archetype.PINPOINT, Archetype.SYNTHESIS])).dataset

        assert isinstance(dataset, Dataset)
        assert reader.collections == ["research"]  # the knowledge base was censused for the requested collection
        archetypes = {case.metadata.archetype for case in dataset.cases if case.metadata is not None}
        assert archetypes == {Archetype.PINPOINT, Archetype.SYNTHESIS}
        # Every case carries its seed's gold ids verbatim (the thesis: gold known by construction).
        by_archetype = {pinpoint.archetype: pinpoint.gold_ids, cross_section.archetype: cross_section.gold_ids}
        for case in dataset.cases:
            assert case.metadata is not None
            assert case.metadata.archetype is not None
            assert isinstance(case.expected_output, RetrievalAnswer)
            assert tuple(case.expected_output.cited_source_ids) == by_archetype[case.metadata.archetype]

    async def test_returns_the_census_and_allocation_it_drew_from(self):
        # The draw carries the census and allocation the pipeline generated against, so the caller can
        # stamp what was actually drawn rather than re-reading a knowledge base that may have drifted.
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed("1")]})
        inventory = _inventory((Archetype.PINPOINT, "easy", "paper", 5))
        reader = FakeInventoryReader(inventory)
        config = _config(distribution={Archetype.PINPOINT: 1.0}, total_cases=6, target_per_seed=1)
        pipeline = _pipeline(repo, reader, config)

        draw = await pipeline.generate(collection="research", page_ids=(), archetypes=[Archetype.PINPOINT])

        assert isinstance(draw, GeneratedDraw)
        assert isinstance(draw.dataset, Dataset)
        assert draw.inventory == inventory
        assert draw.allocation == allocate(config.target_distribution, config.total_cases, inventory, config.thresholds)

    async def test_draws_each_cell_with_its_stratum_band_and_anchor_page_type(self):
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed("1")], Archetype.SYNTHESIS: [_cross_section_seed("1")]})
        reader = FakeInventoryReader(_inventory((Archetype.PINPOINT, "easy", "paper", 5), (Archetype.SYNTHESIS, "hard", "moc", 5)))
        config = _config(distribution={Archetype.PINPOINT: 0.5, Archetype.SYNTHESIS: 0.5}, total_cases=10, target_per_seed=1)
        pipeline = _pipeline(repo, reader, config)

        await pipeline.generate(collection="research", page_ids=(), archetypes=[])

        # One draw per allocated cell, each narrowed to the cell's stratum band + anchor kind.
        assert repo.calls[Archetype.PINPOINT][0].band == "easy"
        assert repo.calls[Archetype.PINPOINT][0].anchor_page_type == "paper"
        assert repo.calls[Archetype.SYNTHESIS][0].band == "hard"
        assert repo.calls[Archetype.SYNTHESIS][0].anchor_page_type == "moc"

    async def test_skips_cells_outside_the_requested_archetypes(self):
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed("1")], Archetype.SYNTHESIS: [_cross_section_seed("1")]})
        reader = FakeInventoryReader(_inventory((Archetype.PINPOINT, "easy", "paper", 5), (Archetype.SYNTHESIS, "hard", "paper", 5)))
        config = _config(distribution={Archetype.PINPOINT: 0.5, Archetype.SYNTHESIS: 0.5}, total_cases=10, target_per_seed=1)
        pipeline = _pipeline(repo, reader, config)

        dataset = (await pipeline.generate(collection="research", page_ids=(), archetypes=[Archetype.PINPOINT])).dataset

        assert Archetype.SYNTHESIS not in repo.calls  # the synthesis cell was skipped
        assert {case.metadata.archetype for case in dataset.cases if case.metadata is not None} == {Archetype.PINPOINT}

    async def test_respects_the_total_cases_budget(self):
        # Far more seeds and drafts than the budget, stratify_cells must cap the output at total_cases.
        repo = FakeSeedQueries(
            {
                Archetype.PINPOINT: [_pinpoint_seed(str(i)) for i in range(20)],
                Archetype.SYNTHESIS: [_cross_section_seed(str(i)) for i in range(20)],
            }
        )
        reader = FakeInventoryReader(_inventory((Archetype.PINPOINT, "easy", "paper", 20), (Archetype.SYNTHESIS, "hard", "paper", 20)))
        config = _config(distribution={Archetype.PINPOINT: 0.5, Archetype.SYNTHESIS: 0.5}, total_cases=4, target_per_seed=3)
        pipeline = _pipeline(repo, reader, config)

        dataset = (await pipeline.generate(collection="research", page_ids=(), archetypes=[Archetype.PINPOINT, Archetype.SYNTHESIS])).dataset

        assert len(dataset.cases) == config.total_cases

    async def test_drops_fabricated_quotes_at_the_gate(self):
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed("1")]})
        reader = FakeInventoryReader(_inventory((Archetype.PINPOINT, "easy", "paper", 5)))
        config = _config(distribution={Archetype.PINPOINT: 1.0}, total_cases=10, target_per_seed=3)
        pipeline = _pipeline(repo, reader, config, drafter=FabricatingCaseDrafter())

        dataset = (await pipeline.generate(collection="research", page_ids=(), archetypes=[Archetype.PINPOINT])).dataset

        # Every draft cited a fabricated quote, so the gate dropped all of them, an empty dataset.
        assert dataset.cases == []

    async def test_per_cell_seed_limit_uses_the_buffer(self):
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed(str(i)) for i in range(20)]})
        reader = FakeInventoryReader(_inventory((Archetype.PINPOINT, "easy", "paper", 20)))
        # The single pinpoint cell gets quota round(1.0 * 6) = 6; limit = ceil(6 / 3 * buffer).
        config = _config(distribution={Archetype.PINPOINT: 1.0}, total_cases=6, target_per_seed=3)
        pipeline = _pipeline(repo, reader, config)

        await pipeline.generate(collection="research", page_ids=(), archetypes=[Archetype.PINPOINT])

        assert repo.calls[Archetype.PINPOINT][0].limit == 4  # ceil(6 / 3 * 2.0)
        assert _SEED_LIMIT_BUFFER == 2.0

    async def test_stable_seed_derived_case_names(self):
        seed = _pinpoint_seed("1")
        repo = FakeSeedQueries({Archetype.PINPOINT: [seed]})
        reader = FakeInventoryReader(_inventory((Archetype.PINPOINT, "easy", "paper", 5)))
        # Two drafts per seed, so the seed's two cases get ordinals 0 and 1 under its stable key.
        config = _config(distribution={Archetype.PINPOINT: 1.0}, total_cases=10, target_per_seed=2)
        pipeline = _pipeline(repo, reader, config)

        dataset = (await pipeline.generate(collection="research", page_ids=(), archetypes=[Archetype.PINPOINT])).dataset

        assert {case.name for case in dataset.cases} == {f"{seed.seed_key}#0", f"{seed.seed_key}#1"}

    async def test_stable_names_survive_a_curator_that_copies_cases(self):
        # The seed-derived identity must survive a curator that returns copies rather than the
        # very instances it was given (see CopyingCaseCurator).
        seed = _pinpoint_seed("1")
        repo = FakeSeedQueries({Archetype.PINPOINT: [seed]})
        reader = FakeInventoryReader(_inventory((Archetype.PINPOINT, "easy", "paper", 5)))
        config = _config(distribution={Archetype.PINPOINT: 1.0}, total_cases=2, target_per_seed=2)
        curator = CopyingCaseCurator()
        pipeline = GenerationPipeline(repo, FakeCaseDrafter(), curator, config, reader)

        dataset = (await pipeline.generate(collection="research", page_ids=(), archetypes=[Archetype.PINPOINT])).dataset

        assert len(dataset.cases) >= 1
        assert all(case.name is not None and case.name.startswith(f"{seed.seed_key}#") for case in dataset.cases)

    async def test_custom_thresholds_flip_the_derived_difficulty(self):
        # A cross-section seed (2 sources across 3 sections) is hard by default but medium when the
        # cross-section section cutoff is raised above 3, proving config.thresholds reaches accept + allocate.
        repo = FakeSeedQueries({Archetype.SYNTHESIS: [_cross_section_seed("1")]})
        reader = FakeInventoryReader(_inventory((Archetype.SYNTHESIS, "medium", "paper", 5)))
        lenient = DifficultyThresholds(cross_section_sections=4)
        config = _config(distribution={Archetype.SYNTHESIS: 1.0}, total_cases=10, target_per_seed=1, thresholds=lenient)
        pipeline = _pipeline(repo, reader, config)

        dataset = (await pipeline.generate(collection="research", page_ids=(), archetypes=[Archetype.SYNTHESIS])).dataset

        assert dataset.cases
        assert all(case.metadata is not None and case.metadata.difficulty == "medium" for case in dataset.cases)

    async def test_drives_a_pydantic_ai_drafter_network_free(self):
        # The real PydanticAiDrafter over a TestModel: one pinpoint archetype so the single seed's
        # body contains the shared TestModel quote verbatim and the gate keeps the one case.
        seed = _pinpoint_seed("1")
        quote = _BODY_PINPOINT[:20]
        drafts = SeedCaseDrafts(cases=[SeedCaseDraft(question="Q1", answer="A1", support_quotes=[quote])])
        model = TestModel(call_tools=[], custom_output_args=drafts.model_dump())
        repo = FakeSeedQueries({Archetype.PINPOINT: [seed]})
        reader = FakeInventoryReader(_inventory((Archetype.PINPOINT, "easy", "paper", 5)))
        config = _config(distribution={Archetype.PINPOINT: 1.0}, total_cases=10, target_per_seed=1)
        pipeline = _pipeline(repo, reader, config, drafter=PydanticAiDrafter(model))

        dataset = (await pipeline.generate(collection="research", page_ids=(), archetypes=[Archetype.PINPOINT])).dataset

        assert len(dataset.cases) == 1
        case = dataset.cases[0]
        assert case.inputs.question == "Q1"
        assert case.name == f"{seed.seed_key}#0"
        assert isinstance(case.expected_output, RetrievalAnswer)
        assert tuple(case.expected_output.cited_source_ids) == seed.gold_ids


class TestHistoricallyUniqueNaming:
    async def test_used_name_shifts_a_seeds_drafts_past_the_retired_ordinal(self):
        seed = _pinpoint_seed("1")
        repo = FakeSeedQueries({Archetype.PINPOINT: [seed]})
        reader = FakeInventoryReader(_inventory((Archetype.PINPOINT, "easy", "paper", 5)))
        config = _config(distribution={Archetype.PINPOINT: 1.0}, total_cases=10, target_per_seed=2)
        pipeline = _pipeline(repo, reader, config)

        draw = await pipeline.generate(
            collection="research", page_ids=(), archetypes=[Archetype.PINPOINT], used_names=frozenset({f"{seed.seed_key}#0"})
        )
        dataset = draw.dataset

        # #0 is retired, so the two drafts take the next free ordinals #1 then #2 (a retired #0 is never reused).
        assert {case.name for case in dataset.cases} == {f"{seed.seed_key}#1", f"{seed.seed_key}#2"}

    async def test_skips_over_used_ordinals_without_reuse(self):
        seed = _pinpoint_seed("1")
        repo = FakeSeedQueries({Archetype.PINPOINT: [seed]})
        reader = FakeInventoryReader(_inventory((Archetype.PINPOINT, "easy", "paper", 5)))
        config = _config(distribution={Archetype.PINPOINT: 1.0}, total_cases=10, target_per_seed=2)
        pipeline = _pipeline(repo, reader, config)
        used = frozenset({f"{seed.seed_key}#0", f"{seed.seed_key}#2"})

        dataset = (await pipeline.generate(collection="research", page_ids=(), archetypes=[Archetype.PINPOINT], used_names=used)).dataset

        # #0 and #2 are taken but #1 is free, so the drafts fill #1 then skip over #2 to #3.
        assert {case.name for case in dataset.cases} == {f"{seed.seed_key}#1", f"{seed.seed_key}#3"}

    async def test_empty_used_names_reproduces_the_positional_naming(self):
        seed = _pinpoint_seed("1")
        repo = FakeSeedQueries({Archetype.PINPOINT: [seed]})
        reader = FakeInventoryReader(_inventory((Archetype.PINPOINT, "easy", "paper", 5)))
        config = _config(distribution={Archetype.PINPOINT: 1.0}, total_cases=10, target_per_seed=2)
        pipeline = _pipeline(repo, reader, config)

        draw = await pipeline.generate(collection="research", page_ids=(), archetypes=[Archetype.PINPOINT], used_names=frozenset())
        dataset = draw.dataset

        # An empty used-name set leaves the plain per-seed ordinals unchanged.
        assert {case.name for case in dataset.cases} == {f"{seed.seed_key}#0", f"{seed.seed_key}#1"}

    async def test_seeds_absent_from_used_names_are_unaffected(self):
        seed_one, seed_two = _pinpoint_seed("1"), _pinpoint_seed("2")
        repo = FakeSeedQueries({Archetype.PINPOINT: [seed_one, seed_two]})
        reader = FakeInventoryReader(_inventory((Archetype.PINPOINT, "easy", "paper", 5)))
        config = _config(distribution={Archetype.PINPOINT: 1.0}, total_cases=10, target_per_seed=1)
        pipeline = _pipeline(repo, reader, config)

        draw = await pipeline.generate(
            collection="research", page_ids=(), archetypes=[Archetype.PINPOINT], used_names=frozenset({f"{seed_one.seed_key}#0"})
        )
        dataset = draw.dataset

        # Only seed_one has a retired name, so it shifts to #1 while seed_two keeps its plain #0.
        assert {case.name for case in dataset.cases} == {f"{seed_one.seed_key}#1", f"{seed_two.seed_key}#0"}
