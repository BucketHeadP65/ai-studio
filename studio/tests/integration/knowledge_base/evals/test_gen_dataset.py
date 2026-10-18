r"""Gold-from-seed golden-QA generation over a real, indexed ParadeDB.

Indexes a one-paper mini-knowledge_base (carrying a `^c1` claim so the structural seeder finds a pinpoint
pattern), draws a real :class:`~engram.evals.shared.domain.Seed` from the indexed knowledge base with
:class:`~engram.evals.generation.infrastructure.seeds.SeedQueries` (gold ids known *by construction* from graph
structure, never picked by an LLM), then drafts a case with a network-free `TestModel` whose
`custom_output_args` force one draft whose `support_quote` is a verbatim substring of the seed's
source bodies. The :func:`~engram.evals.generation.contracts.accept` gate keeps it; a sibling draft whose
quote is fabricated is dropped. The surviving dataset is saved and reloaded via
:func:`~engram.evals.generation.contracts.load_dataset`, proving the seed -> draft -> gate -> persist pipeline
end-to-end against real infrastructure.

The whole module skips when Docker is unavailable (the `pg` fixture, via `migrated`). Tests are
synchronous: the `indexed` fixture drives `memory index` (`asyncio.run` internally) and the
generation body wraps its own `asyncio.run` — the `tests/integration/test_search.py` shape.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

import pytest

from core.cqrs.contracts import JobQueries, MemoryJobs, MessageBus
from engram.evals.commands import GenerateEvalDataset
from engram.evals.generation.contracts import (
    CaseCurator,
    GenerationConfig,
    SeedCaseDraft,
    accept,
    build_dataset,
    load_dataset,
    load_stamp,
    save_dataset,
)
from engram.evals.generation.domain.drafting import SeedCaseDrafts
from engram.evals.generation.infrastructure.drafter import PydanticAiDrafter
from engram.evals.generation.infrastructure.seeds import SeedQueries
from engram.evals.generation.infrastructure.stratum_inventory import StratumInventoryQueries
from engram.evals.handlers import GenerateEvalDatasetHandler
from engram.evals.shared.domain import DEFAULT_DIFFICULTY_THRESHOLDS, Archetype
from engram.indexing.contracts import FilesystemKnowledgeBaseReader, IndexKnowledgeBase, IndexKnowledgeBaseHandler, KnowledgeBaseReader
from engram.indexing.infrastructure import orm
from engram.kb.contracts import load_knowledge_base_schema
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from engram.testing.specs import research_spec
from pydantic_ai.models.test import TestModel
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from typer.testing import CliRunner

from studio.knowledge_base.cli import app
from studio.knowledge_base.indexing.infrastructure.validation_report_sink import FileValidationReportSink
from studio.knowledge_base.wiring.contracts import AdminSqlGateway
from studio.knowledge_base.wiring.evals import (
    freshness_reader_factory_provider,
    inventory_reader_factory_provider,
    seed_repository_factory_provider,
)
from studio.knowledge_base.wiring.indexing import KnowledgeBaseUnitOfWorkFactory

if TYPE_CHECKING:
    from pathlib import Path

    from engram.evals.commands import GenerateEvalDatasetReport
    from engram.evals.generation.contracts import CaseDrafter, EvaluationDataset, StratumInventory
    from engram.evals.shared.domain import Seed
    from sqlalchemy.ext.asyncio import AsyncEngine

runner = CliRunner()

_COLLECTION = "research"
_PAPER_ID = "paper.gen-demo"
_PAPER = """\
---
id: paper.gen-demo
type: paper
title: Synthetic Generation Demo
summary: Synthetic question generation must quote the paper verbatim to stay faithful.
status: synthesized
zotero_key: GENDEMO1
year: 2025
paper_type: method
source_hash: gd001
tasks: [long-doc-qa]
stages: [generation]
---

## TL;DR

Faithful synthetic questions cite real page ids and quote the source verbatim.

## Findings

- Synthetic question generation must quote the paper verbatim to stay faithful. ^c1
    - loc:: §1
    - kind:: observation
"""

_MIRROR = "---\nsource_key: GENDEMO1\nprovider: example-library\nsource_hash: gd001\n---\n# Mirror\n\n## 1 Body\n\nSynthetic generation demo source.\n"


def _build_knowledge_base(root: Path) -> Path:
    builder = KnowledgeBaseBuilder(root)
    builder.add_unit_document("Papers", "gen-demo", _PAPER)
    builder.add_source_mirror("gen-demo", _MIRROR)
    builder.add_vocabulary()
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema()
    return root


class _Factory:
    """A `ctx.obj` factory whose bus runs a real-PG, fake-embed index handler (e2e shape)."""

    def __init__(self, engine: AsyncEngine, knowledge_base: Path) -> None:
        self._reader = FilesystemKnowledgeBaseReader(knowledge_base)
        handler = IndexKnowledgeBaseHandler(
            self._reader, FakeEmbedding(), KnowledgeBaseUnitOfWorkFactory(engine), FileValidationReportSink(knowledge_base), spec=load_knowledge_base_schema(knowledge_base)
        )
        self._bus = MessageBus({IndexKnowledgeBase: handler}, {})
        self._gateway = AdminSqlGateway(engine)  # the identity announce's own read, over the same admin engine

    def __call__(self) -> _Container:
        return _Container(self._bus, self._gateway, self._reader)


class _Container:
    """Minimal async container exposing the message bus and identity-announce reads to the CLI command."""

    def __init__(self, bus: MessageBus, gateway: AdminSqlGateway, reader: KnowledgeBaseReader) -> None:
        self._bus = bus
        self._gateway = gateway
        self._reader = reader
        self._jobs = MemoryJobs()

    async def get(self, key: type) -> object:
        if key is MessageBus:
            return self._bus
        if key is AdminSqlGateway:
            return self._gateway
        if key is JobQueries:
            return self._jobs
        if key is KnowledgeBaseReader:
            return self._reader
        msg = f"no registration for {key!r}"
        raise KeyError(msg)


@pytest.fixture
def indexed(migrated: str, tmp_path: Path) -> str:
    """Index the one-paper mini knowledge base into the freshly-migrated DB; return the admin URL."""
    knowledge_base = _build_knowledge_base(tmp_path)
    engine = create_async_engine(migrated, poolclass=NullPool)

    async def _register() -> None:
        await orm.register_pgvector(engine)

    asyncio.run(_register())
    result = runner.invoke(app, ["index", "--knowledge-base", str(knowledge_base)], obj=_Factory(engine, knowledge_base))
    assert result.exit_code == 0, result.stdout
    return migrated


async def _seed_from_knowledge_base(admin_url: str) -> Seed:
    """Draw a single real pinpoint seed from the indexed knowledge base over an admin session."""
    engine = create_async_engine(admin_url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            repo = SeedQueries(session, research_spec())
            seeds = await repo.pinpoint_seeds(collection=_COLLECTION, page_ids=(), limit=5)
    finally:
        await engine.dispose()
    assert seeds, "expected at least one pinpoint seed from the indexed mini knowledge base"
    return seeds[0]


async def _generate_and_reload(admin_url: str, tmp_path: Path) -> tuple[int, EvaluationDataset, Seed]:
    """Seed structurally, draft one faithful + one fabricated case, gate, save + reload."""
    seed = await _seed_from_knowledge_base(admin_url)
    mirror = "\n".join(seed.source_bodies)
    quote = " ".join(mirror.split()[:5])  # a guaranteed-verbatim substring of the seed's source bodies

    drafts = SeedCaseDrafts(
        cases=[
            SeedCaseDraft(
                question="What must a faithful synthetic question do?", answer="Quote the source verbatim.", support_quotes=[quote]
            ),
            SeedCaseDraft(question="Fabricated?", answer="No.", support_quotes=["a phrase that is nowhere in any source body"]),
        ]
    )
    drafter = PydanticAiDrafter(TestModel(call_tools=[], custom_output_args=drafts.model_dump()))
    raw = await drafter.draft(seed, instructions="write one grounded factual case", target=2)
    cases = [case for draft in raw if (case := accept(draft, seed=seed)) is not None]

    out = tmp_path / "gen-demo.yaml"
    save_dataset(build_dataset(cases), out)
    return len(cases), load_dataset(out), seed


class TestGenDatasetOverIndexedDb:
    def test_faithful_seed_case_survives_and_round_trips(self, indexed: str, tmp_path: Path):
        kept, reloaded, seed = asyncio.run(_generate_and_reload(indexed, tmp_path))

        # The fabricated-quote draft is dropped by the gate; only the verbatim one survives.
        assert kept == 1
        assert len(reloaded.cases) == 1
        case = reloaded.cases[0]
        assert case.metadata is not None
        # The gold ids are the seed's, known by construction (the indexed paper id), never the model's.
        assert case.metadata.anchor_page_id == _PAPER_ID
        assert seed.gold_ids == (_PAPER_ID,)
        assert case.metadata.archetype == Archetype.PINPOINT
        assert case.metadata.anchors, "the pinpoint seed must carry source-provenance anchors"
        assert case.expected_output is not None
        assert case.expected_output.cited_source_ids == [_PAPER_ID]
        assert tuple(case.expected_output.cited_source_ids) == seed.gold_ids


# A verbatim prefix of the indexed paper's `^c1` claim text, so the TestModel drafter's fixed quote
# passes the quote check against whichever seed the handler draws from the knowledge base.
_QUOTE = "Synthetic question generation must quote"


def _make_drafter(model: str | None) -> CaseDrafter:
    """A `DrafterFactory` returning a network-free `TestModel` drafter that quotes the claim verbatim."""
    del model
    drafts = SeedCaseDrafts(
        cases=[
            SeedCaseDraft(
                question="What must a faithful synthetic question do?", answer="Quote the source verbatim.", support_quotes=[_QUOTE]
            )
        ]
    )
    return PydanticAiDrafter(TestModel(call_tools=[], custom_output_args=drafts.model_dump()))


async def _run_generate_handler(admin_url: str, out: Path) -> GenerateEvalDatasetReport:
    """Run the real generate handler (real seeder + reader over the admin engine) into a versioned dataset."""
    engine = create_async_engine(admin_url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    spec = research_spec()
    config = GenerationConfig(target_distribution={Archetype.PINPOINT: 1.0}, total_cases=5, target_per_seed=1)
    handler = GenerateEvalDatasetHandler(
        seed_repository_factory_provider(engine, spec),
        CaseCurator(FakeEmbedding(), dedup_threshold=0.99),
        config,
        _make_drafter,
        inventory_reader_factory_provider(engine, spec, config),
        freshness_reader_factory_provider(engine),
    )
    try:
        return await handler(GenerateEvalDataset(collection=_COLLECTION, out=str(out), archetypes=(Archetype.PINPOINT,), salt="testsalt"))
    finally:
        await engine.dispose()


async def _read_inventory(admin_url: str) -> StratumInventory:
    """Census the indexed knowledge base's stratum inventory over a real owner session."""
    engine = create_async_engine(admin_url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            reader = StratumInventoryQueries(session, research_spec(), DEFAULT_DIFFICULTY_THRESHOLDS)
            return await reader.inventory(collection=_COLLECTION)
    finally:
        await engine.dispose()


async def _read_page_ids(admin_url: str) -> frozenset[str]:
    """Read the indexed knowledge base's current page-id set over a real owner session (the top-up gold check)."""
    engine = create_async_engine(admin_url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            reader = StratumInventoryQueries(session, research_spec(), DEFAULT_DIFFICULTY_THRESHOLDS)
            return await reader.page_ids(collection=_COLLECTION)
    finally:
        await engine.dispose()


class TestGenerateHandlerOverIndexedDb:
    """The real generate handler writes an immutable, stamped, pointer-tracked dataset version."""

    def test_writes_versioned_stamped_dataset(self, indexed: str, tmp_path: Path):
        out = tmp_path / "research.yaml"

        report = asyncio.run(_run_generate_handler(indexed, out))

        version_path = tmp_path / "research.v1.yaml"
        assert version_path.exists()
        assert report.version == "research.v1.yaml"
        assert report.out == str(version_path)
        assert (tmp_path / "research.current").read_text(encoding="utf-8").strip() == "research.v1.yaml"

        reloaded = load_dataset(version_path)
        assert len(reloaded.cases) == 1
        case = reloaded.cases[0]
        assert case.metadata is not None
        assert case.metadata.anchor_page_id == _PAPER_ID
        assert case.metadata.archetype == Archetype.PINPOINT
        assert case.name is not None
        assert case.name.startswith("pinpoint:")  # the stable seed-derived identity
        assert case.name.endswith("#0")

        stamp = load_stamp(version_path)
        assert stamp is not None
        assert stamp.salt == "testsalt"
        assert stamp.knowledge_base_fingerprint.endswith(":research")  # the index spec hash plus the collection
        assert stamp.budget == 5
        assert stamp.parent_version is None
        assert stamp.coverage == {"pinpoint/easy/paper": 1}  # one claim, capped by the pinpoint pool

    def test_reader_counts_the_pinpoint_pool(self, indexed: str):
        inventory = asyncio.run(_read_inventory(indexed))

        counts = {
            (count.stratum.archetype, count.stratum.difficulty, count.stratum.anchor_page_type): count.available for count in inventory.counts
        }
        # The one-paper mini knowledge base has a single `^c1` claim on a `paper`-kind page and no edges or
        # comparisons, so only the pinpoint stratum is non-empty.
        assert counts == {(Archetype.PINPOINT, "easy", "paper"): 1}

    def test_reader_lists_the_current_page_ids(self, indexed: str):
        # The top-up gold-existence check reads the collection's live page set; the indexed paper's
        # page is present, so a top-up would never flag its gold `gold_gone` (other meta stubs may ride
        # along, the check only cares that a case's gold pages are still there).
        assert _PAPER_ID in asyncio.run(_read_page_ids(indexed))


# -- top-up drill over a real indexed DB -------------------------------------
#
# A four-claim paper gives the pinpoint pool four seeds, so the top-up chain has room to both retire
# (a tightened budget) and generate (a widened budget) over real infrastructure, exercising the whole
# lifecycle: immutable versions, the stamp lineage, the `.current` pointer move, and the retirement
# ledger. The exact hash-displaced set is the pure planner's concern (unit tests); here the assertion
# is the end-to-end file discipline against an indexed knowledge_base.

_TOPUP_PAPER_ID = "paper.topup-demo"
_TOPUP_PAPER = """\
---
id: paper.topup-demo
type: paper
title: Coverage Top-Up Demo
summary: A multi-claim paper so the pinpoint pool has room to retire and to backfill.
status: synthesized
zotero_key: TOPUP1
year: 2025
paper_type: method
source_hash: tu001
tasks: [long-doc-qa]
stages: [generation]
---

## TL;DR

Coverage top-up maintains a golden dataset against a changed knowledge base without regenerating it.

## Findings

- Coverage top-up retires displaced cases deterministically under consistent hashing. ^c1
    - loc:: §1
    - kind:: observation
- Coverage top-up drafts fresh cases only for the under-covered strata. ^c2
    - loc:: §1
    - kind:: observation
- Coverage top-up writes an immutable new version and never mutates the parent. ^c3
    - loc:: §1
    - kind:: observation
- Coverage top-up records every retirement in the new version's stamped ledger. ^c4
    - loc:: §1
    - kind:: observation
"""

_TOPUP_MIRROR = "---\nsource_key: TOPUP1\nprovider: example-library\nsource_hash: tu001\n---\n# Mirror\n\n## 1 Body\n\nCoverage top-up demo source body.\n"


def _build_topup_knowledge_base(root: Path) -> Path:
    builder = KnowledgeBaseBuilder(root)
    builder.add_unit_document("Papers", "topup-demo", _TOPUP_PAPER)
    builder.add_source_mirror("topup-demo", _TOPUP_MIRROR)
    builder.add_vocabulary()
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema()
    return root


@pytest.fixture
def topup_indexed(migrated: str, tmp_path: Path) -> str:
    """Index the four-claim top-up mini knowledge base into the freshly-migrated DB; return the admin URL."""
    knowledge_base = _build_topup_knowledge_base(tmp_path)
    engine = create_async_engine(migrated, poolclass=NullPool)

    async def _register() -> None:
        await orm.register_pgvector(engine)

    asyncio.run(_register())
    result = runner.invoke(app, ["index", "--knowledge-base", str(knowledge_base)], obj=_Factory(engine, knowledge_base))
    assert result.exit_code == 0, result.stdout
    return migrated


class _SeedQuotingDrafter:
    """A `CaseDrafter` quoting a verbatim prefix of each seed's own source body (faithful by construction).

    Unlike the fixed-output `TestModel` drafter, this varies per seed, so a multi-claim knowledge base yields one
    distinct, gate-passing case per claim (the top-up drill needs several pinpoint cases to churn).
    """

    async def draft(self, seed: Seed, *, instructions: str, target: int) -> list[SeedCaseDraft]:
        del instructions
        quote = seed.source_bodies[0][:12]
        return [SeedCaseDraft(question=f"Q-{seed.seed_key}-{index}", answer="A", support_quotes=[quote]) for index in range(target)]


def _quoting_drafter(model: str | None) -> CaseDrafter:
    """A `DrafterFactory` returning the seed-quoting drafter (the model spec is irrelevant here)."""
    del model
    return _SeedQuotingDrafter()


def _topup_handler(engine: AsyncEngine) -> GenerateEvalDatasetHandler:
    """Build the real generate handler (real seeder + reader over `engine`) with a four-case budget."""
    spec = research_spec()
    config = GenerationConfig(target_distribution={Archetype.PINPOINT: 1.0}, total_cases=4, target_per_seed=1)
    return GenerateEvalDatasetHandler(
        seed_repository_factory_provider(engine, spec),
        CaseCurator(FakeEmbedding(), dedup_threshold=0.99),
        config,
        _quoting_drafter,
        inventory_reader_factory_provider(engine, spec, config),
        freshness_reader_factory_provider(engine),
    )


async def _run_topup_drill(admin_url: str, out: Path) -> dict[str, GenerateEvalDatasetReport]:
    """Generate v1 (four cases), then top up to v2 (budget two -> retire) and v3 (budget four -> backfill)."""
    engine = create_async_engine(admin_url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        handler = _topup_handler(engine)
        base = GenerateEvalDataset(collection=_COLLECTION, out=str(out), archetypes=(Archetype.PINPOINT,), salt="topupsalt")
        v1 = await handler(base.model_copy(update={"count": 4}))
        dry = await handler(base.model_copy(update={"top_up_from": "current", "count": 2, "dry_run": True}))
        v2 = await handler(base.model_copy(update={"top_up_from": "current", "count": 2}))
        v3 = await handler(base.model_copy(update={"top_up_from": "current", "count": 4}))
    finally:
        await engine.dispose()
    return {"v1": v1, "dry": dry, "v2": v2, "v3": v3}


class TestTopUpDrillOverIndexedDb:
    """A top-up chain over a real indexed knowledge base: retire, backfill, immutable versions, moving pointer."""

    def test_top_up_chain_retires_backfills_and_keeps_versions_immutable(self, topup_indexed: str, tmp_path: Path):
        out = tmp_path / "research.yaml"

        reports = asyncio.run(_run_topup_drill(topup_indexed, out))
        v1_bytes = (tmp_path / "research.v1.yaml").read_bytes()  # captured AFTER the whole drill: v1 must be untouched

        # v1: four pinpoint cases from the four claims.
        assert reports["v1"].version == "research.v1.yaml"
        assert reports["v1"].n_cases == 4

        # dry-run: previews retiring two (budget two over four cases), writes nothing.
        dry = reports["dry"]
        assert dry.plan is not None
        assert dry.version == ""
        assert dry.retired == len(dry.plan.retire) == 2
        assert dry.plan.generate == ()  # a tightened budget only retires

        # v2: the tightened budget retired two into the stamped ledger; the parent is untouched.
        assert reports["v2"].version == "research.v2.yaml"
        assert reports["v2"].retired == 2
        assert reports["v2"].n_cases == 2
        v2_stamp = load_stamp(tmp_path / "research.v2.yaml")
        assert v2_stamp is not None
        assert v2_stamp.parent_version == "research.v1.yaml"
        assert len(v2_stamp.retired) == 2
        assert {record.reason for record in v2_stamp.retired} == {"displaced"}

        # v3: the widened budget backfilled the two-case gap and retired nothing.
        assert reports["v3"].version == "research.v3.yaml"
        assert reports["v3"].retired == 0
        assert reports["v3"].n_cases == 4
        v3_stamp = load_stamp(tmp_path / "research.v3.yaml")
        assert v3_stamp is not None
        assert v3_stamp.parent_version == "research.v2.yaml"

        # Immutability + pointer: every version file still exists, v1 is byte-identical, current -> v3.
        for version in ("research.v1.yaml", "research.v2.yaml", "research.v3.yaml"):
            assert (tmp_path / version).exists()
        assert (tmp_path / "research.v1.yaml").read_bytes() == v1_bytes
        assert (tmp_path / "research.current").read_text(encoding="utf-8").strip() == "research.v3.yaml"

        # Every case carries the stable seed-derived identity.
        v2_names = [case.name for case in load_dataset(tmp_path / "research.v2.yaml").cases]
        v3_names = [case.name for case in load_dataset(tmp_path / "research.v3.yaml").cases]
        assert all(name is not None and name.startswith("pinpoint:") for name in v2_names + v3_names)

        # v2 only retired (no generation), so its two survivors keep their original #0 ordinal.
        assert all(name is not None and name.endswith("#0") for name in v2_names)

        # v3 backfilled the two-case gap by redrafting the seeds v2 displaced. The chain-walked used-name
        # set continues each redraft past its retired #0, so the two newcomers carry #1 while the two
        # survivors keep #0 — historically-unique names, never reusing a retired ordinal.
        assert sum(name is not None and name.endswith("#0") for name in v3_names) == 2
        assert sum(name is not None and name.endswith("#1") for name in v3_names) == 2
