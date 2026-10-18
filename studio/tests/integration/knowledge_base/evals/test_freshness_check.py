r"""The freshness-check property suite over a real, migrated knowledge_base.

The pure verdict core is exhaustively unit-tested; here the whole seam is exercised end to end against
a deterministic synthetic knowledge base (`tests/support/synthetic_knowledge_base.py`) on a real database: build a knowledge base,
generate a mirror-era pinpoint dataset with a network-free quoting drafter, then mutate `memory.*` rows and
sweep the dataset with the real `CheckFreshnessHandler` to observe the exact drift each mutation causes.

Five properties are proven:

* **zero false positives** — an untouched knowledge base yields every case `fresh` (freshness fraction 0.0), and
  the sweep writes its YAML + markdown artifacts;
* **page drift** — updating one page's card text and body hash flags exactly that page's dependent case
  `mirror_stale` (mirror-era precedence), everything else `fresh`;
* **gold gone** — deleting a gold page flags its case `gold_gone`;
* **the closed hole** — updating a claim's text (leaving its page's card text and body hash intact) flags
  the mirror-era pinpoint case `mirror_stale` while its page-level anchor stays silent (empty
  `moved_page_ids`), which a page-only check would have missed;
* **top-up composition** — a stale case retires with `reason="stale"`, its band redrafts with the next
  free ordinal, untouched survivors carry over byte-stable, and the new version + ledger + `.current`
  pointer are all correct.

The whole module skips when Docker is unavailable (the `pg` fixture, via `migrated`). Tests are
synchronous and wrap `asyncio.run`, doing the file IO in the sync body (the `test_gen_dataset.py` shape),
so the async helpers stay free of blocking `pathlib` calls.
"""

from __future__ import annotations

import asyncio

from pathlib import Path
from typing import TYPE_CHECKING

from engram.evals.commands import CheckFreshness, GenerateEvalDataset
from engram.evals.generation.contracts import (
    CaseCurator,
    EvaluationDataset,
    GenerationConfig,
    SeedCaseDraft,
    load_dataset,
    load_stamp,
    save_dataset,
)
from engram.evals.generation.infrastructure.files import load_freshness_report
from engram.evals.handlers import CheckFreshnessHandler, GenerateEvalDatasetHandler
from engram.evals.shared.domain import AnchorHash, Archetype, QaCaseMeta, QaInput, RetrievalAnswer
from engram.indexing.contracts import EdgeORM, PageORM
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.infrastructure.synthetic_knowledge_base import (
    SyntheticKnowledgeBaseSpec,
    build_knowledge_base,
    delete_page,
    drift_claim_text,
    drift_page,
    synthetic_schema,
)
from pydantic_evals import Case
from sqlalchemy import NullPool, delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from studio.knowledge_base.wiring.evals import (
    freshness_reader_factory_provider,
    inventory_reader_factory_provider,
    seed_repository_factory_provider,
)

if TYPE_CHECKING:
    from engram.evals.commands import FreshnessCheckedReport, GenerateEvalDatasetReport
    from engram.evals.generation.contracts import CaseDrafter, CaseFreshness, EvaluationCase
    from engram.evals.shared.domain import Seed
    from sqlalchemy.ext.asyncio import AsyncEngine

_COLLECTION = "research"
_SALT = "freshsalt"
_TOTAL_CASES = 6

# A single anchor kind so the pinpoint stratum is one cell: the salted draw and next-free-ordinal naming
# then stay unambiguous across the generate -> top-up version bump the composition property leans on.
_KNOWLEDGE_BASE_SPEC = SyntheticKnowledgeBaseSpec(n_pages=200, seed=20260704, anchor_page_types=("note",))


class _SeedQuotingDrafter:
    """A `CaseDrafter` quoting a verbatim prefix of each seed's own source body (faithful by construction).

    Network-free and per-seed, so a pinpoint knowledge base yields one distinct, gate-passing case per claim; the
    quote tracks the current claim text, so a redraft after a drift stays faithful to the mutated source.
    """

    async def draft(self, seed: Seed, *, instructions: str, target: int) -> list[SeedCaseDraft]:
        del instructions
        quote = seed.source_bodies[0][:12]
        return [SeedCaseDraft(question=f"Q-{seed.seed_key}-{index}", answer="A", support_quotes=[quote]) for index in range(target)]


def _quoting_drafter(model: str | None) -> CaseDrafter:
    """A `DrafterFactory` returning the seed-quoting drafter (the model spec is irrelevant here)."""
    del model
    return _SeedQuotingDrafter()


def _generate_handler(engine: AsyncEngine, *, target_per_seed: int = 1) -> GenerateEvalDatasetHandler:
    """Build the real generate handler (real seeder / reader / freshness reader) over `engine`."""
    spec = synthetic_schema(_KNOWLEDGE_BASE_SPEC)
    config = GenerationConfig(target_distribution={Archetype.PINPOINT: 1.0}, total_cases=_TOTAL_CASES, target_per_seed=target_per_seed)
    return GenerateEvalDatasetHandler(
        seed_repository_factory_provider(engine, spec),
        CaseCurator(FakeEmbedding(), dedup_threshold=0.99),
        config,
        _quoting_drafter,
        inventory_reader_factory_provider(engine, spec, config),
        freshness_reader_factory_provider(engine),
    )


async def _generate(admin_url: str, out: Path, *, target_per_seed: int = 1) -> Path:
    """Build the synthetic knowledge base and generate a mirror-era pinpoint `v1`; return its version path."""
    engine = create_async_engine(admin_url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await build_knowledge_base(session, _KNOWLEDGE_BASE_SPEC, collection=_COLLECTION)
        handler = _generate_handler(engine, target_per_seed=target_per_seed)
        report = await handler(GenerateEvalDataset(collection=_COLLECTION, out=str(out), archetypes=(Archetype.PINPOINT,), salt=_SALT))
    finally:
        await engine.dispose()
    return Path(report.out)


async def _mutate_and_sweep(
    admin_url: str, version_path: Path, out_dir: Path, *, page_id: str | None = None, claim_id: str | None = None, delete: bool = False
) -> tuple[FreshnessCheckedReport, dict[str, CaseFreshness]]:
    """Apply one drift to the live knowledge base, then sweep `version_path` with the real handler.

    Exactly one mutation is applied: `delete` removes `page_id`, `claim_id` drifts a claim's text, and a
    bare `page_id` drifts that page's card text + body hash. With all three absent the knowledge base is untouched.
    """
    engine = create_async_engine(admin_url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            if delete and page_id is not None:
                await delete_page(session, page_id, collection=_COLLECTION)
            elif claim_id is not None:
                await drift_claim_text(session, claim_id, collection=_COLLECTION)
            elif page_id is not None:
                await drift_page(session, page_id, collection=_COLLECTION)
        handler = CheckFreshnessHandler(freshness_reader_factory_provider(engine), out_dir)
        report = await handler(CheckFreshness(collection=_COLLECTION, dataset=str(version_path), out=str(out_dir)))
    finally:
        await engine.dispose()
    verdicts = {verdict.case_name: verdict for verdict in load_freshness_report(Path(report.out)).verdicts}
    return report, verdicts


async def _mutate_and_top_up(admin_url: str, out: Path, claim_id: str) -> GenerateEvalDatasetReport:
    """Drift `claim_id` (making its case stale), then top up the `current` version to a stale-aware `v2`."""
    engine = create_async_engine(admin_url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await drift_claim_text(session, claim_id, collection=_COLLECTION)
        handler = _generate_handler(engine)
        command = GenerateEvalDataset(
            collection=_COLLECTION, out=str(out), archetypes=(Archetype.PINPOINT,), top_up_from="current", count=_TOTAL_CASES
        )
        report = await handler(command)
    finally:
        await engine.dispose()
    return report


def _pinpoint_target(case: EvaluationCase) -> tuple[str, str]:
    """The (gold page id, claim id) a pinpoint case was drafted from (its claim is `{page_id}#c1`)."""
    assert case.metadata is not None
    page_id = case.metadata.anchor_page_id
    assert page_id is not None
    return page_id, f"{page_id}#c1"


def _ordinal_of(case_name: str) -> int:
    """The draft ordinal of a `{seed_key}#{ordinal}` case name (the seed key may itself contain `#`)."""
    return int(case_name.rsplit("#", 1)[-1])


def _case_content(case: EvaluationCase) -> tuple[object, object, object, object]:
    """A case's identity + content (name, inputs, ground truth, metadata) for a cross-version equality check.

    Each component is a pydantic value object with field equality, so two survivors compare equal iff their
    drafted content is unchanged across the version bump.
    """
    return (case.name, case.inputs, case.expected_output, case.metadata)


class TestFreshnessSweepProperties:
    """The five freshness properties over a real synthetic knowledge base (D6)."""

    def test_untouched_knowledge_base_is_all_fresh(self, migrated: str, tmp_path: Path):
        version_path = asyncio.run(_generate(migrated, tmp_path / "research.yaml"))
        cases = load_dataset(version_path).cases

        report, verdicts = asyncio.run(_mutate_and_sweep(migrated, version_path, tmp_path / "freshness"))

        # Zero-false-positive property: an untouched knowledge base flags nothing.
        assert cases, "the pinpoint knowledge base must yield at least one case"
        assert all(verdict.verdict == "fresh" for verdict in verdicts.values())
        assert report.counts["fresh"] == len(cases)
        assert report.staleness_fraction == 0.0
        # The read-only sweep still writes its two artifacts.
        assert Path(report.out).exists()
        assert Path(report.markdown).exists()

    def test_same_seed_siblings_all_fresh_on_untouched_knowledge_base(self, migrated: str, tmp_path: Path):
        version_path = asyncio.run(_generate(migrated, tmp_path / "research.yaml", target_per_seed=2))
        cases = load_dataset(version_path).cases

        _report, verdicts = asyncio.run(_mutate_and_sweep(migrated, version_path, tmp_path / "freshness"))

        # Regression guard: cases sharing one pinpoint seed (same claim, ordinals #0/#1) must ALL
        # recompute their mirror — a claim-to-case mapping that keeps only one sibling silently
        # turns the rest into D10 recompute failures (false mirror_stale on an untouched knowledge base).
        by_seed: dict[str, list[str]] = {}
        for case in cases:
            assert case.name is not None
            by_seed.setdefault(case.name.rsplit("#", 1)[0], []).append(case.name)
        sibling_groups = [names for names in by_seed.values() if len(names) >= 2]
        assert sibling_groups, "expected at least one seed with two sibling cases"
        assert all(verdict.verdict == "fresh" for verdict in verdicts.values())

    def test_updating_one_page_flags_only_its_dependent(self, migrated: str, tmp_path: Path):
        version_path = asyncio.run(_generate(migrated, tmp_path / "research.yaml"))
        cases = load_dataset(version_path).cases
        victim = cases[0]
        page_id, _claim_id = _pinpoint_target(victim)

        _report, verdicts = asyncio.run(_mutate_and_sweep(migrated, version_path, tmp_path / "freshness", page_id=page_id))

        # The mirror-era case whose page's card text moved reads mirror_stale, attributing the moved page.
        assert victim.name is not None
        assert verdicts[victim.name].verdict == "mirror_stale"
        assert page_id in verdicts[victim.name].moved_page_ids
        # Every other case is untouched.
        others = [verdict for name, verdict in verdicts.items() if name != victim.name]
        assert others, "expected sibling cases to prove the drift is isolated"
        assert all(verdict.verdict == "fresh" for verdict in others)

    def test_deleting_a_gold_page_flags_gold_gone(self, migrated: str, tmp_path: Path):
        version_path = asyncio.run(_generate(migrated, tmp_path / "research.yaml"))
        cases = load_dataset(version_path).cases
        victim = cases[0]
        page_id, _claim_id = _pinpoint_target(victim)

        _report, verdicts = asyncio.run(_mutate_and_sweep(migrated, version_path, tmp_path / "freshness", page_id=page_id, delete=True))

        assert victim.name is not None
        assert verdicts[victim.name].verdict == "gold_gone"
        assert page_id in verdicts[victim.name].moved_page_ids
        others = [verdict for name, verdict in verdicts.items() if name != victim.name]
        assert all(verdict.verdict == "fresh" for verdict in others)

    def test_claim_text_drift_is_mirror_stale_while_page_anchor_stays_silent(self, migrated: str, tmp_path: Path):
        version_path = asyncio.run(_generate(migrated, tmp_path / "research.yaml"))
        cases = load_dataset(version_path).cases
        victim = cases[0]
        _page_id, claim_id = _pinpoint_target(victim)

        _report, verdicts = asyncio.run(_mutate_and_sweep(migrated, version_path, tmp_path / "freshness", claim_id=claim_id))

        # The closed hole: the claim text is in the pinpoint mirror, so the recomputed mirror hash moves
        # (mirror_stale), yet the page body hash is untouched, so the page-level anchor stays silent — an
        # empty `moved_page_ids` is exactly what a page-only check would have (wrongly) called fresh.
        assert victim.name is not None
        assert verdicts[victim.name].verdict == "mirror_stale"
        assert verdicts[victim.name].moved_page_ids == ()
        others = [verdict for name, verdict in verdicts.items() if name != victim.name]
        assert all(verdict.verdict == "fresh" for verdict in others)

    def test_top_up_retires_stale_and_redrafts_with_a_fresh_ordinal(self, migrated: str, tmp_path: Path):
        out = tmp_path / "research.yaml"
        v1_path = asyncio.run(_generate(migrated, out))
        v1 = load_dataset(v1_path)
        v1_by_name = {case.name: case for case in v1.cases}
        victim = v1.cases[0]
        assert victim.name is not None
        _page_id, claim_id = _pinpoint_target(victim)
        v1_bytes = v1_path.read_bytes()  # captured before the top-up: v1 must stay byte-identical (immutability)

        v2_report = asyncio.run(_mutate_and_top_up(migrated, out, claim_id))
        v2_path = Path(v2_report.out)

        # Version + pointer discipline: v2 is a fresh immutable version parented on v1, current -> v2.
        assert v2_path.name == "research.v2.yaml"
        assert v2_report.retired == 1
        assert (tmp_path / "research.current").read_text(encoding="utf-8").strip() == "research.v2.yaml"
        assert v1_path.read_bytes() == v1_bytes

        # Ledger: the stale case retired as `stale` (not displaced, not gold_gone).
        stamp = load_stamp(v2_path)
        assert stamp is not None
        assert stamp.parent_version == "research.v1.yaml"
        assert len(stamp.retired) == 1
        record = stamp.retired[0]
        assert record.case_name == victim.name
        assert record.reason == "stale"

        # Composition: the retired name is gone, the band healed back to its budget, and the sole newcomer
        # is a redraft carrying a fresh ordinal (never reusing the retired #0).
        v2 = load_dataset(v2_path)
        v2_names = {case.name for case in v2.cases}
        assert victim.name not in v2_names
        assert len(v2.cases) == _TOTAL_CASES
        added = v2_names - set(v1_by_name)
        removed = set(v1_by_name) - v2_names
        assert removed == {victim.name}
        assert len(added) == 1
        newcomer = next(iter(added))
        assert newcomer is not None
        assert _ordinal_of(newcomer) >= 1

        # Untouched survivors carry over stable across the version bump.
        v2_by_name = {case.name: case for case in v2.cases}
        for name in set(v1_by_name) - {victim.name}:
            assert _case_content(v2_by_name[name]) == _case_content(v1_by_name[name])


class TestStructuralPremises:
    """The D12 property over a real knowledge base: a deleted trace edge breaks the premise, never the verdict."""

    def test_deleting_a_trace_edge_breaks_the_premise_but_not_the_verdict(self, migrated: str, tmp_path: Path):
        asyncio.run(_run_trace_premise_property(migrated, tmp_path))


async def _run_trace_premise_property(admin_url: str, tmp_path: Path) -> None:
    """Build a knowledge base, hand-craft a trace-named case over a live edge, sweep, delete the edge, resweep."""
    engine = create_async_engine(admin_url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await build_knowledge_base(session, _KNOWLEDGE_BASE_SPEC, collection=_COLLECTION)
            edge = (
                await session.execute(
                    select(EdgeORM.src_id, EdgeORM.dst_id)
                    .join(PageORM, PageORM.id == EdgeORM.src_id)
                    .where(PageORM.collection == _COLLECTION)
                    .limit(1)
                )
            ).first()
            assert edge is not None, "the synthetic knowledge base must carry at least one edge"
            src, dst = edge.src_id, edge.dst_id
            rows = (await session.execute(select(PageORM.id, PageORM.body_hash).where(PageORM.id.in_([src, dst])))).all()
            hashes = {row.id: row.body_hash for row in rows}

        case = Case(
            name=f"trace:{src}>{dst}#0",
            inputs=QaInput(question=f"How does {src} lead to {dst}?"),
            expected_output=RetrievalAnswer(answer="a", cited_source_ids=[page_id for page_id in (src, dst) if page_id in hashes]),
            metadata=QaCaseMeta(
                anchor_page_id=src,
                difficulty="easy",
                archetype=Archetype.TRACE,
                anchors=tuple(AnchorHash(unit_id=page_id, body_hash=body_hash) for page_id, body_hash in hashes.items()),
            ),
        )
        dataset_path = tmp_path / "trace.yaml"
        save_dataset(EvaluationDataset(name="trace-premise", cases=[case], evaluators=[]), dataset_path)
        out_dir = tmp_path / "freshness"

        handler = CheckFreshnessHandler(freshness_reader_factory_provider(engine), out_dir)
        before = await handler(CheckFreshness(collection=_COLLECTION, dataset=str(dataset_path), out=str(out_dir)))
        assert (before.n_premise_checked, before.n_premise_broken) == (1, 0)

        async with AsyncSession(engine, expire_on_commit=False) as session:
            await session.execute(delete(EdgeORM).where(EdgeORM.src_id == src, EdgeORM.dst_id == dst))
            await session.commit()

        after = await handler(CheckFreshness(collection=_COLLECTION, dataset=str(dataset_path), out=str(out_dir)))
    finally:
        await engine.dispose()

    # The premise broke (report-only), the pages are intact, so the verdict ladder never moved (D12).
    assert (after.n_premise_checked, after.n_premise_broken) == (1, 1)
    artifact = load_freshness_report(Path(after.out))
    (premise,) = artifact.structural
    assert premise.missing == (f"edge {src}->{dst} gone",)
    assert all(verdict.verdict == "fresh" for verdict in artifact.verdicts)
    assert "Structural integrity" in Path(after.markdown).read_text(encoding="utf-8")
