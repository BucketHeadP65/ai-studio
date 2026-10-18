"""A deterministic row-level synthetic knowledge base builder for the scale tests.

Writes `kb.*` rows directly over an `AsyncSession`, with no markdown and no indexing pipeline, so a
5-10k-page knowledge base is ready in seconds. The builder guarantees two properties.

* **Exact structural ground truth.** Each archetype lives in its own page-id namespace and feeds
  exactly one seeder, so `KnowledgeBasePlan.ground_truth` is the exact per-stratum seed pool a
  `StratumInventoryQueries` must recompute.
* **Zipf hub skew.** Survey fan-in and synthesis fan-out follow a Zipf distribution, so a few pages
  attract most edges.

The plan is a pure function of the `SyntheticKnowledgeBaseSpec`, its integer `seed` included.
`plan_knowledge_base` does no IO. `build_knowledge_base` writes the plan with batched inserts.
"""

from __future__ import annotations

import random

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, insert, update

from engram.evals.generation.contracts import MIN_PASSAGE_CHARS, MIN_SURVEY_FANOUT, MIN_SYNTHESIS_FANOUT, TRACE_HOPS, Stratum, StratumCount
from engram.evals.shared.contracts import DEFAULT_DIFFICULTY_THRESHOLDS, Archetype, QaDifficulty, derive_difficulty
from engram.indexing.contracts import ClaimORM, EdgeORM, IndexFingerprintORM, PageORM, SectionORM
from engram.kb.contracts import KnowledgeBaseSchema, PageTypeSpec, RelationDecl

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from engram.evals.shared.contracts import DifficultyThresholds

# The structural floors come from `engram.evals.generation.contracts`, so no planned subgraph falls below the threshold that makes it a seed.
_MIN_CHAIN_NODES = TRACE_HOPS + 1  # the shortest chain that yields one 2-hop path

# The page_type stamped on every page that is not an archetype anchor. It never enters an inventory stratum.
_FILLER_KIND = "note"

# The facet key every page writes its planned facet values under, in the `facets` jsonb column.
_FACET_KEY = "tags"

# A stable total order over the difficulty set, so ground-truth counts come out in a fixed order.
_DIFFICULTY_ORDER: dict[QaDifficulty, int] = {"easy": 0, "medium": 1, "hard": 2}

_DEFAULT_PAGES = 5000


class SyntheticKnowledgeBaseSpec(BaseModel):
    """The parameters of a synthetic knowledge base: size, page type and facet mix, edge density, and Zipf skew.

    The five archetype page shares are normalised, so they need not sum to one. At least one must be positive.
    """

    model_config = ConfigDict(frozen=True)

    n_pages: int = Field(default=_DEFAULT_PAGES, ge=200)
    seed: int = 20260703
    anchor_page_types: tuple[str, ...] = ("note", "topic", "guide")
    facet_vocab: tuple[str, ...] = ("alpha", "beta", "gamma", "delta", "epsilon", "zeta")
    facets_per_page: int = Field(default=2, ge=0)
    zipf_exponent: float = Field(default=1.15, gt=0.0)
    survey_fanin: int = Field(default=4, ge=MIN_SURVEY_FANOUT)
    survey_max_fanin: int = Field(default=16, ge=MIN_SURVEY_FANOUT)
    synthesis_fanout: int = Field(default=3, ge=MIN_SYNTHESIS_FANOUT)
    synthesis_max_fanout: int = Field(default=12, ge=MIN_SYNTHESIS_FANOUT)
    trace_chain_length: int = Field(default=50, ge=_MIN_CHAIN_NODES)
    pinpoint_share: float = Field(default=0.40, ge=0.0)
    survey_share: float = Field(default=0.20, ge=0.0)
    trace_share: float = Field(default=0.15, ge=0.0)
    synthesis_share: float = Field(default=0.15, ge=0.0)
    passage_share: float = Field(default=0.10, ge=0.0)

    @model_validator(mode="after")
    def _check(self) -> SyntheticKnowledgeBaseSpec:
        """Reject a spec that cannot produce a well-formed knowledge base."""
        if self.pinpoint_share + self.survey_share + self.trace_share + self.synthesis_share + self.passage_share <= 0.0:
            msg = "at least one archetype share must be positive"
            raise ValueError(msg)
        if self.survey_max_fanin < self.survey_fanin:
            msg = "survey_max_fanin must be at least survey_fanin"
            raise ValueError(msg)
        if self.synthesis_max_fanout < self.synthesis_fanout:
            msg = "synthesis_max_fanout must be at least synthesis_fanout"
            raise ValueError(msg)
        if not self.anchor_page_types:
            msg = "anchor_page_types must be non-empty"
            raise ValueError(msg)
        return self


@dataclass(frozen=True)
class _Pinpoint:
    """One pinpoint page carrying a single claim (an easy pinpoint seed)."""

    page_id: str
    page_type: str


@dataclass(frozen=True)
class _Survey:
    """One survey hub and the contributor pages fanning into it over one relation (a survey seed)."""

    hub_id: str
    hub_page_type: str
    contributor_ids: tuple[str, ...]
    rel: str


@dataclass(frozen=True)
class _Trace:
    """One simple chain whose adjacent edges form its 2-hop trace paths, two fewer than its nodes."""

    node_ids: tuple[str, ...]
    rel: str


@dataclass(frozen=True)
class _Synthesis:
    """One synthesis source fanning out to distinct target pages over one relation (a synthesis seed)."""

    source_id: str
    source_kind: str
    target_ids: tuple[str, ...]
    n_sections: int
    rel: str


@dataclass(frozen=True)
class _Passage:
    """One passage page carrying a single section long enough to draw (an easy passage seed)."""

    page_id: str
    page_type: str


@dataclass(frozen=True)
class KnowledgeBasePlan:
    """The resolved plan for a synthetic knowledge base: every module plus its exact ground truth.

    The module tuples describe the rows `build_knowledge_base` writes. `ground_truth` is the per-stratum
    seed pool a `StratumInventoryQueries` must recompute exactly. The `total_*` counts are the sizes of
    what was written. `directed_edges` holds every `(src, dst)` edge, so a test can check a drawn trace path.
    """

    spec: SyntheticKnowledgeBaseSpec
    pinpoints: tuple[_Pinpoint, ...]
    surveys: tuple[_Survey, ...]
    traces: tuple[_Trace, ...]
    syntheses: tuple[_Synthesis, ...]
    passages: tuple[_Passage, ...]
    ground_truth: tuple[StratumCount, ...]
    directed_edges: frozenset[tuple[str, str]]
    total_pages: int
    total_claims: int
    total_edges: int
    total_sections: int
    total_trace_paths: int
    facets_by_page: dict[str, tuple[str, ...]] = field(default_factory=dict)


class _PageRow(TypedDict):
    """The `kb.pages` insert payload (server defaults fill the omitted columns)."""

    id: str
    collection: str
    page_type: str
    slug: str
    title: str
    status: str
    card_text: str
    body_hash: str
    facets: dict[str, list[str]]


class _SectionRow(TypedDict):
    """The `kb.sections` insert payload."""

    id: str
    collection: str
    page_id: str
    heading: str
    path: str
    ord: int
    body: str
    body_hash: str


class _ClaimRow(TypedDict):
    """The `kb.claims` insert payload."""

    id: str
    collection: str
    page_id: str
    block_id: str
    text: str
    quote: str
    kind: str
    status: str


class _EdgeRow(TypedDict):
    """The `kb.edges` insert payload, always within one collection."""

    src_collection: str
    src_id: str
    src_kind: str
    rel: str
    dst_collection: str
    dst_id: str
    dst_kind: str


class _FingerprintRow(TypedDict):
    """The single `kb.index_fingerprint` insert payload (the knowledge-base-identity row)."""

    collection: str
    indexer_version: str
    spec_hash: str
    vocabulary_hash: str
    embed_model: str


def zipf_weights(n: int, exponent: float) -> tuple[float, ...]:
    """The normalised Zipf weights over ranks 1..n: weight_i proportional to 1 / rank_i ** exponent.

    A larger exponent concentrates weight on the low ranks. The weights sum to one for any positive n.

    Args:
        n: The number of ranks (groups) to weight.
        exponent: The Zipf exponent, larger is more skewed. It must be positive.

    Returns:
        The n normalised weights in rank order, or an empty tuple when n is not positive.
    """
    if n <= 0:
        return ()
    raw = [1.0 / (rank**exponent) for rank in range(1, n + 1)]
    total = sum(raw)
    return tuple(value / total for value in raw)


def _skewed_counts(n_groups: int, average: int, floor: int, cap: int, exponent: float) -> tuple[int, ...]:
    """Per-group member counts distributed by Zipf weight, each clamped to `[floor, cap]`."""
    budget = n_groups * average
    return tuple(max(floor, min(cap, round(budget * weight))) for weight in zipf_weights(n_groups, exponent))


def _facets_for(page_id: str, spec: SyntheticKnowledgeBaseSpec) -> tuple[str, ...]:
    """A deterministic facet slice for a page, picked from the vocabulary by a stable hash of its id."""
    if not spec.facet_vocab or spec.facets_per_page <= 0:
        return ()
    start = sum(ord(char) for char in page_id) % len(spec.facet_vocab)
    count = min(spec.facets_per_page, len(spec.facet_vocab))
    return tuple(spec.facet_vocab[(start + offset) % len(spec.facet_vocab)] for offset in range(count))


def _budget(spec: SyntheticKnowledgeBaseSpec) -> dict[Archetype, int]:
    """The page budget per archetype, from the normalised archetype shares."""
    shares = {
        Archetype.PINPOINT: spec.pinpoint_share,
        Archetype.SURVEY: spec.survey_share,
        Archetype.TRACE: spec.trace_share,
        Archetype.SYNTHESIS: spec.synthesis_share,
        Archetype.PASSAGE: spec.passage_share,
    }
    total = sum(shares.values())
    return {archetype: round(spec.n_pages * share / total) for archetype, share in shares.items()}


def _plan_pinpoints(spec: SyntheticKnowledgeBaseSpec, rng: random.Random, budget: int) -> tuple[_Pinpoint, ...]:
    """Plan `budget` single-claim pinpoint pages, each assigned an anchor page_type from the mix."""
    return tuple(_Pinpoint(page_id=f"pp.{index:06d}", page_type=rng.choice(spec.anchor_page_types)) for index in range(budget))


def _plan_surveys(spec: SyntheticKnowledgeBaseSpec, rng: random.Random, budget: int) -> tuple[_Survey, ...]:
    """Plan survey hubs with Zipf-skewed contributor fan-in, where each contributor has out-degree one."""
    n_hubs = max(1, budget // (1 + spec.survey_fanin))
    fanins = _skewed_counts(n_hubs, spec.survey_fanin, MIN_SURVEY_FANOUT, spec.survey_max_fanin, spec.zipf_exponent)
    surveys: list[_Survey] = []
    for index, fanin in enumerate(fanins):
        contributor_ids = tuple(f"sv.con.{index:05d}.{contributor:03d}" for contributor in range(fanin))
        surveys.append(
            _Survey(hub_id=f"sv.hub.{index:05d}", hub_page_type=rng.choice(spec.anchor_page_types), contributor_ids=contributor_ids, rel="links_to")
        )
    return tuple(surveys)


def _plan_traces(spec: SyntheticKnowledgeBaseSpec, budget: int) -> tuple[_Trace, ...]:
    """Plan simple `links_to` chains, where each chain yields two fewer acyclic 2-hop paths than it has nodes."""
    length = spec.trace_chain_length
    n_chains = max(1, budget // length)
    return tuple(
        _Trace(node_ids=tuple(f"tr.{chain:04d}.{node:04d}" for node in range(length)), rel="links_to") for chain in range(n_chains)
    )


def _plan_syntheses(spec: SyntheticKnowledgeBaseSpec, rng: random.Random, budget: int) -> tuple[_Synthesis, ...]:
    """Plan synthesis sources with Zipf-skewed fan-out to dedicated targets and section counts cycling 1 to 3."""
    n_sources = max(1, budget // (1 + spec.synthesis_fanout))
    fanouts = _skewed_counts(n_sources, spec.synthesis_fanout, MIN_SYNTHESIS_FANOUT, spec.synthesis_max_fanout, spec.zipf_exponent)
    syntheses: list[_Synthesis] = []
    for index, fanout in enumerate(fanouts):
        target_ids = tuple(f"sy.tgt.{index:05d}.{target:03d}" for target in range(fanout))
        source_kind = rng.choice(spec.anchor_page_types)
        syntheses.append(
            _Synthesis(
                source_id=f"sy.src.{index:05d}",
                source_kind=source_kind,
                target_ids=target_ids,
                n_sections=1 + (index % 3),
                rel="summarizes",
            )
        )
    return tuple(syntheses)


def _plan_passages(spec: SyntheticKnowledgeBaseSpec, rng: random.Random, budget: int) -> tuple[_Passage, ...]:
    """Plan `budget` one-section passage pages, each assigned an anchor page_type from the mix."""
    return tuple(_Passage(page_id=f"ps.{index:06d}", page_type=rng.choice(spec.anchor_page_types)) for index in range(budget))


def _ground_truth(
    pinpoints: tuple[_Pinpoint, ...],
    surveys: tuple[_Survey, ...],
    traces: tuple[_Trace, ...],
    syntheses: tuple[_Synthesis, ...],
    passages: tuple[_Passage, ...],
    thresholds: DifficultyThresholds,
) -> tuple[StratumCount, ...]:
    """The exact per-stratum seed pool the modules construct, grouped as `StratumInventoryQueries` groups it."""
    available: dict[Stratum, int] = {}

    def _add(stratum: Stratum, count: int) -> None:
        available[stratum] = available.get(stratum, 0) + count

    for pinpoint in pinpoints:
        _add(Stratum(archetype=Archetype.PINPOINT, difficulty="easy", anchor_page_type=pinpoint.page_type), 1)
    for survey in surveys:
        n_sources = len(survey.contributor_ids) + 1
        band = derive_difficulty(n_sources, 0, n_sources, thresholds)
        _add(Stratum(archetype=Archetype.SURVEY, difficulty=band, anchor_page_type=survey.hub_page_type), 1)
    trace_band = derive_difficulty(TRACE_HOPS + 1, TRACE_HOPS, 1, thresholds)
    for trace in traces:
        _add(Stratum(archetype=Archetype.TRACE, difficulty=trace_band, anchor_page_type=_FILLER_KIND), max(len(trace.node_ids) - TRACE_HOPS, 0))
    for synthesis in syntheses:
        n_reached = len(synthesis.target_ids)
        band = derive_difficulty(n_reached, 0, max(synthesis.n_sections, 1), thresholds)
        _add(Stratum(archetype=Archetype.SYNTHESIS, difficulty=band, anchor_page_type=synthesis.source_kind), 1)
    passage_band = derive_difficulty(1, 0, 1, thresholds)
    for passage in passages:
        _add(Stratum(archetype=Archetype.PASSAGE, difficulty=passage_band, anchor_page_type=passage.page_type), 1)

    counts = [StratumCount(stratum=stratum, available=count) for stratum, count in available.items()]
    counts.sort(key=lambda count: (count.stratum.archetype.value, _DIFFICULTY_ORDER[count.stratum.difficulty], count.stratum.anchor_page_type))
    return tuple(counts)


def _directed_edges(
    surveys: tuple[_Survey, ...], traces: tuple[_Trace, ...], syntheses: tuple[_Synthesis, ...]
) -> frozenset[tuple[str, str]]:
    """Every directed `(src, dst)` edge the plan will insert, for verifying drawn trace paths."""
    edges: set[tuple[str, str]] = set()
    for survey in surveys:
        edges.update((contributor, survey.hub_id) for contributor in survey.contributor_ids)
    for trace in traces:
        edges.update(zip(trace.node_ids, trace.node_ids[1:], strict=False))
    for synthesis in syntheses:
        edges.update((synthesis.source_id, target) for target in synthesis.target_ids)
    return frozenset(edges)


def synthetic_schema(spec: SyntheticKnowledgeBaseSpec | None = None) -> KnowledgeBaseSchema:
    """The schema a synthetic knowledge base is read with: its page types and the relations its edges carry.

    Args:
        spec: The knowledge base the schema describes. `None` describes the default spec.

    Returns:
        A schema declaring the spec's anchor page types and the filler page type, and the relations `links_to` and
        `summarizes`.
    """
    anchors = (spec or SyntheticKnowledgeBaseSpec()).anchor_page_types
    names = tuple(dict.fromkeys((*anchors, _FILLER_KIND)))
    page_types = tuple(PageTypeSpec(page_type=page_type) for page_type in names)
    relations = tuple(RelationDecl(slug=slug, domain=_FILLER_KIND, range=_FILLER_KIND) for slug in ("links_to", "summarizes"))
    return KnowledgeBaseSchema(slug="synthetic", page_types=page_types, relations=relations)


def plan_knowledge_base(spec: SyntheticKnowledgeBaseSpec, thresholds: DifficultyThresholds = DEFAULT_DIFFICULTY_THRESHOLDS) -> KnowledgeBasePlan:
    """Resolve a `SyntheticKnowledgeBaseSpec` into the pure, deterministic `KnowledgeBasePlan` (no IO).

    The same spec, its `seed` included, always yields an identical plan.

    Args:
        spec: The knowledge base parameters.
        thresholds: The difficulty band cutoffs the ground-truth bands are derived against.

    Returns:
        The resolved plan the builder writes and the scale test asserts against.
    """
    rng = random.Random(spec.seed)
    budget = _budget(spec)
    pinpoints = _plan_pinpoints(spec, rng, budget[Archetype.PINPOINT])
    surveys = _plan_surveys(spec, rng, budget[Archetype.SURVEY])
    traces = _plan_traces(spec, budget[Archetype.TRACE])
    syntheses = _plan_syntheses(spec, rng, budget[Archetype.SYNTHESIS])
    passages = _plan_passages(spec, rng, budget[Archetype.PASSAGE])

    total_pages = (
        len(pinpoints)
        + sum(1 + len(survey.contributor_ids) for survey in surveys)
        + sum(len(trace.node_ids) for trace in traces)
        + sum(1 + len(synthesis.target_ids) for synthesis in syntheses)
        + len(passages)
    )
    total_edges = sum(len(survey.contributor_ids) for survey in surveys)
    total_edges += sum(len(trace.node_ids) - 1 for trace in traces)
    total_edges += sum(len(synthesis.target_ids) for synthesis in syntheses)
    return KnowledgeBasePlan(
        spec=spec,
        pinpoints=pinpoints,
        surveys=surveys,
        traces=traces,
        syntheses=syntheses,
        passages=passages,
        ground_truth=_ground_truth(pinpoints, surveys, traces, syntheses, passages, thresholds),
        directed_edges=_directed_edges(surveys, traces, syntheses),
        total_pages=total_pages,
        total_claims=len(pinpoints),
        total_edges=total_edges,
        total_sections=sum(synthesis.n_sections for synthesis in syntheses) + len(passages),
        total_trace_paths=sum(max(len(trace.node_ids) - TRACE_HOPS, 0) for trace in traces),
    )


def _page_row(page_id: str, page_type: str, collection: str, spec: SyntheticKnowledgeBaseSpec) -> _PageRow:
    """One `kb.pages` insert payload with a deterministic facet slice (its body_hash is id-derived)."""
    facets = list(_facets_for(page_id, spec))
    return _PageRow(
        id=page_id,
        collection=collection,
        page_type=page_type,
        slug=page_id,
        title=page_id,
        status="synthesized",
        card_text=f"card for {page_id}",
        body_hash=f"hash-{page_id}",
        facets={_FACET_KEY: facets},
    )


def _all_page_rows(plan: KnowledgeBasePlan, collection: str) -> list[_PageRow]:
    """Every page row across all modules, anchors first then their dedicated satellite pages."""
    spec = plan.spec
    rows = [_page_row(pinpoint.page_id, pinpoint.page_type, collection, spec) for pinpoint in plan.pinpoints]
    for survey in plan.surveys:
        rows.append(_page_row(survey.hub_id, survey.hub_page_type, collection, spec))
        rows.extend(_page_row(contributor, _FILLER_KIND, collection, spec) for contributor in survey.contributor_ids)
    for trace in plan.traces:
        rows.extend(_page_row(node, _FILLER_KIND, collection, spec) for node in trace.node_ids)
    for synthesis in plan.syntheses:
        rows.append(_page_row(synthesis.source_id, synthesis.source_kind, collection, spec))
        rows.extend(_page_row(target, _FILLER_KIND, collection, spec) for target in synthesis.target_ids)
    rows.extend(_page_row(passage.page_id, passage.page_type, collection, spec) for passage in plan.passages)
    return rows


def _all_section_rows(plan: KnowledgeBasePlan, collection: str) -> list[_SectionRow]:
    """Short sections for synthesis sources (what the section-count subquery reads), then one long section per passage page."""
    rows: list[_SectionRow] = []
    for synthesis in plan.syntheses:
        for ordinal in range(synthesis.n_sections):
            section_id = f"{synthesis.source_id}::s{ordinal}"
            rows.append(
                _SectionRow(
                    id=section_id,
                    collection=collection,
                    page_id=synthesis.source_id,
                    heading=f"Section {ordinal}",
                    path=f"s{ordinal}",
                    ord=ordinal,
                    body=f"body {section_id}",
                    body_hash=f"sec-{section_id}",
                )
            )
    for passage in plan.passages:
        section_id = f"{passage.page_id}::s0"
        rows.append(
            _SectionRow(
                id=section_id,
                collection=collection,
                page_id=passage.page_id,
                heading="Section 0",
                path="s0",
                ord=0,
                body=_passage_body(section_id),
                body_hash=f"sec-{section_id}",
            )
        )
    return rows


def _passage_body(section_id: str) -> str:
    """A deterministic section body of `MIN_PASSAGE_CHARS` characters naming its section."""
    return (f"passage {section_id} " * MIN_PASSAGE_CHARS)[:MIN_PASSAGE_CHARS]


def _all_claim_rows(plan: KnowledgeBasePlan, collection: str) -> list[_ClaimRow]:
    """One claim per pinpoint page (the pinpoint seed pool)."""
    return [
        _ClaimRow(
            id=f"{pinpoint.page_id}#c1",
            collection=collection,
            page_id=pinpoint.page_id,
            block_id="c1",
            text=f"claim on {pinpoint.page_id}",
            quote=f"quote on {pinpoint.page_id}",
            kind="observation",
            status="active",
        )
        for pinpoint in plan.pinpoints
    ]


def _all_edge_rows(plan: KnowledgeBasePlan, collection: str) -> list[_EdgeRow]:
    """Every resolved edge: survey contributors in, trace chains along, synthesis targets out."""
    rows: list[_EdgeRow] = []
    for survey in plan.surveys:
        rows.extend(
            _EdgeRow(
                src_collection=collection,
                src_id=contributor,
                src_kind=_FILLER_KIND,
                rel=survey.rel,
                dst_collection=collection,
                dst_id=survey.hub_id,
                dst_kind="page",
            )
            for contributor in survey.contributor_ids
        )
    for trace in plan.traces:
        rows.extend(
            _EdgeRow(src_collection=collection, src_id=src, src_kind=_FILLER_KIND, rel=trace.rel, dst_collection=collection, dst_id=dst, dst_kind="page")
            for src, dst in zip(trace.node_ids, trace.node_ids[1:], strict=False)
        )
    for synthesis in plan.syntheses:
        rows.extend(
            _EdgeRow(
                src_collection=collection,
                src_id=synthesis.source_id,
                src_kind=_FILLER_KIND,
                rel=synthesis.rel,
                dst_collection=collection,
                dst_id=target,
                dst_kind="page",
            )
            for target in synthesis.target_ids
        )
    return rows


async def _bulk_insert(
    session: AsyncSession,
    table: type[PageORM | SectionORM | ClaimORM | EdgeORM],
    rows: Sequence[Mapping[str, object]],
    *,
    batch: int = 1000,
) -> None:
    """Insert `rows` into `table` via batched executemany (never per-row ORM adds)."""
    for start in range(0, len(rows), batch):
        await session.execute(insert(table), rows[start : start + batch])


async def build_knowledge_base(
    session: AsyncSession,
    spec: SyntheticKnowledgeBaseSpec,
    *,
    collection: str,
    thresholds: DifficultyThresholds = DEFAULT_DIFFICULTY_THRESHOLDS,
) -> KnowledgeBasePlan:
    """Write a synthetic knowledge base for `spec` directly into the `kb.*` tables and return its plan.

    Writes pages, sections, claims, edges and the `kb.index_fingerprint` row, then commits once.

    Args:
        session: The owner `AsyncSession` the rows are written over.
        spec: The knowledge base parameters.
        collection: The collection every row is stamped with.
        thresholds: The difficulty band cutoffs the ground truth is derived against.

    Returns:
        The `KnowledgeBasePlan` that was written. Its ground truth and counts describe the written rows.
    """
    plan = plan_knowledge_base(spec, thresholds)
    await _bulk_insert(session, PageORM, _all_page_rows(plan, collection))
    await _bulk_insert(session, SectionORM, _all_section_rows(plan, collection))
    await _bulk_insert(session, ClaimORM, _all_claim_rows(plan, collection))
    await _bulk_insert(session, EdgeORM, _all_edge_rows(plan, collection))
    fingerprint: _FingerprintRow = {
        "collection": collection,
        "indexer_version": "synthetic",
        "spec_hash": f"synthetic-{spec.seed}",
        "vocabulary_hash": "synthetic",
        "embed_model": "none",
    }
    await session.execute(insert(IndexFingerprintORM), [fingerprint])
    await session.commit()
    return plan


# Deterministic drift mutations. Each moves `kb.*` rows of a built knowledge base to id-derived values and commits.


async def drift_page(session: AsyncSession, page_id: str, *, collection: str) -> None:
    """Move a page's `card_text` and `body_hash` together, as a reindex would.

    A case with a mirror then reads `mirror_stale` and a case without one reads `page_drifted`.

    Args:
        session: The owner session the update is issued over.
        page_id: The page to drift.
        collection: The collection the page belongs to.
    """
    statement = (
        update(PageORM)
        .where(PageORM.id == page_id, PageORM.collection == collection)
        .values(card_text=f"drifted card for {page_id}", body_hash=f"drifted-{page_id}")
    )
    await session.execute(statement)
    await session.commit()


async def drift_claim_text(session: AsyncSession, claim_id: str, *, collection: str) -> None:
    """Move a claim's `text` while leaving its owning page's card text and body hash untouched.

    A pinpoint case over the claim then reads `mirror_stale` while the page's anchor hash is unchanged.

    Args:
        session: The owner session the update is issued over.
        claim_id: The claim to drift.
        collection: The collection the claim belongs to.
    """
    statement = update(ClaimORM).where(ClaimORM.id == claim_id, ClaimORM.collection == collection).values(text=f"drifted text for {claim_id}")
    await session.execute(statement)
    await session.commit()


async def delete_page(session: AsyncSession, page_id: str, *, collection: str) -> None:
    """Delete a page and its sections and claims from the knowledge base.

    Args:
        session: The owner session the delete is issued over.
        page_id: The page to delete.
        collection: The collection the page belongs to.
    """
    await session.execute(delete(PageORM).where(PageORM.id == page_id, PageORM.collection == collection))
    await session.commit()
