"""Unit tests for the `CaseCurator`.

The curator trims a raw pool of generated cases into a balanced, non-redundant dataset:

* `dedup` embeds each case's question once (a single batched call) and greedily keeps cases whose
  cosine similarity to every already-kept case stays below the constructor threshold, dropping the
  near-duplicates while preserving input order;
* `stratify_cells` buckets cases by their `(archetype, difficulty)` stratum and samples each cell up to
  the quota a `QuotaAllocation` gives it (pooling quotas across anchor kinds a case cannot distinguish);
* `stratify` buckets cases by their `meta.archetype` and samples each bucket up to its share of the
  `total` budget (deterministic input order), excluding cases whose archetype is `None` or absent
  from the target distribution. Both delegate to one sampling core.

The embedder is faked with a small in-test deterministic stand-in that returns scripted vectors
keyed by the question text, so the cosine geometry under test is exact and reproducible, no network,
no hash-seeding. The real `PydanticAiEmbedder.embed_batch` satisfies the same `BatchEmbedder`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.embedding import Embeddings

from engram.evals.generation.contracts import AuthoredCase, CaseCurator, QuotaAllocation, Stratum
from engram.evals.generation.domain.allocation import CellQuota
from engram.evals.shared.domain import Archetype, ExpectedQa, QaCaseMeta, QaInput

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from engram.evals.shared.domain import QaDifficulty


class ScriptedEmbedder:
    """A deterministic `BatchEmbedder` returning a scripted vector per question text.

    `vectors` maps a question string to the vector the embedder returns for it (a public, scriptable
    attribute, repo fakes convention). `batches` records the per-call grouping so a test can assert
    the curator issues exactly one batched embed.
    """

    def __init__(self, vectors: Mapping[str, list[float]]) -> None:
        self.vectors = dict(vectors)
        self.batches: list[list[str]] = []

    async def embed_batch(self, texts: Sequence[str]) -> Embeddings:
        batch = list(texts)
        self.batches.append(batch)
        return Embeddings(vectors=[self.vectors[text] for text in batch], input_tokens=0)


def _case(
    question: str, *, archetype: Archetype | None = Archetype.PINPOINT, difficulty: QaDifficulty = "easy", paper_id: str = "paper.x"
) -> AuthoredCase:
    """Build an `AuthoredCase` with a given question text, archetype, and difficulty (other fields are filler)."""
    return AuthoredCase(
        question=QaInput(question=question),
        expected=ExpectedQa(answer="a", expected_source_ids=(paper_id,)),
        meta=QaCaseMeta(anchor_page_id=paper_id, difficulty=difficulty, archetype=archetype),
    )


def _allocation(*cells: tuple[Archetype, QaDifficulty, str, int]) -> QuotaAllocation:
    """Build a `QuotaAllocation` from `(archetype, difficulty, anchor_page_type, quota)` tuples."""
    return QuotaAllocation(
        cells=tuple(CellQuota(stratum=Stratum(archetype=a, difficulty=d, anchor_page_type=k), quota=q) for a, d, k, q in cells)
    )


class TestDedup:
    async def test_drops_a_near_duplicate_pair_keeping_the_first(self):
        # q1 and q2 point almost the same way (cosine ~1, above threshold); q3 is orthogonal.
        embedder = ScriptedEmbedder({"q1": [1.0, 0.0], "q2": [1.0, 0.01], "q3": [0.0, 1.0]})
        curator = CaseCurator(embedder, dedup_threshold=0.9)
        cases = [_case("q1"), _case("q2"), _case("q3")]

        kept = await curator.dedup(cases)

        assert [c.question.question for c in kept] == ["q1", "q3"]

    async def test_keeps_all_when_every_pair_is_below_threshold(self):
        # Three mutually orthogonal vectors, every pairwise cosine is 0 < threshold.
        embedder = ScriptedEmbedder({"q1": [1.0, 0.0, 0.0], "q2": [0.0, 1.0, 0.0], "q3": [0.0, 0.0, 1.0]})
        curator = CaseCurator(embedder, dedup_threshold=0.5)
        cases = [_case("q1"), _case("q2"), _case("q3")]

        kept = await curator.dedup(cases)

        assert [c.question.question for c in kept] == ["q1", "q2", "q3"]

    async def test_embeds_every_question_in_a_single_batched_call(self):
        embedder = ScriptedEmbedder({"q1": [1.0, 0.0], "q2": [0.0, 1.0]})
        curator = CaseCurator(embedder, dedup_threshold=0.9)

        await curator.dedup([_case("q1"), _case("q2")])

        assert embedder.batches == [["q1", "q2"]]

    async def test_a_zero_norm_vector_does_not_crash_and_is_not_a_duplicate(self):
        # A zero vector has undefined cosine, guarded to 0 similarity, so it is never a duplicate.
        embedder = ScriptedEmbedder({"q1": [0.0, 0.0], "q2": [0.0, 0.0]})
        curator = CaseCurator(embedder, dedup_threshold=0.9)

        kept = await curator.dedup([_case("q1"), _case("q2")])

        assert [c.question.question for c in kept] == ["q1", "q2"]

    async def test_empty_pool_returns_empty(self):
        curator = CaseCurator(ScriptedEmbedder({}), dedup_threshold=0.9)

        assert await curator.dedup([]) == []



class TestStratifyCells:
    def test_caps_each_cell_at_its_quota(self):
        pinpoint = [_case(f"p{i}", archetype=Archetype.PINPOINT, difficulty="easy") for i in range(3)]
        synthesis = [_case(f"c{i}", archetype=Archetype.SYNTHESIS, difficulty="hard") for i in range(3)]
        curator = CaseCurator(ScriptedEmbedder({}), dedup_threshold=0.9)
        allocation = _allocation((Archetype.PINPOINT, "easy", "paper", 2), (Archetype.SYNTHESIS, "hard", "paper", 2))

        kept = curator.stratify_cells(pinpoint + synthesis, allocation)

        questions = [c.question.question for c in kept]
        assert sorted(questions) == ["c0", "c1", "p0", "p1"]
        assert len(kept) == 4

    def test_pools_quota_across_anchor_kinds_of_one_archetype_difficulty(self):
        # An AuthoredCase carries no anchor kind, so two cells sharing (archetype, difficulty) pool their
        # quotas into one bucket: 1 + 1 = 2 cases kept from the three available.
        pinpoint = [_case(f"p{i}", archetype=Archetype.PINPOINT, difficulty="easy") for i in range(3)]
        curator = CaseCurator(ScriptedEmbedder({}), dedup_threshold=0.9)
        allocation = _allocation((Archetype.PINPOINT, "easy", "paper", 1), (Archetype.PINPOINT, "easy", "note", 1))

        kept = curator.stratify_cells(pinpoint, allocation)

        assert [c.question.question for c in kept] == ["p0", "p1"]

    def test_excludes_cases_outside_the_allocation(self):
        pinpoint = [_case("p0", archetype=Archetype.PINPOINT, difficulty="easy")]
        wrong_band = [_case("p_hard", archetype=Archetype.PINPOINT, difficulty="hard")]  # (pinpoint, hard) not allocated
        unlabelled = [_case("u0", archetype=None)]
        curator = CaseCurator(ScriptedEmbedder({}), dedup_threshold=0.9)
        allocation = _allocation((Archetype.PINPOINT, "easy", "paper", 5))

        kept = curator.stratify_cells(pinpoint + wrong_band + unlabelled, allocation)

        assert [c.question.question for c in kept] == ["p0"]

    def test_caps_the_result_at_the_allocation_total(self):
        pinpoint = [_case(f"p{i}", archetype=Archetype.PINPOINT, difficulty="easy") for i in range(5)]
        synthesis = [_case(f"c{i}", archetype=Archetype.SYNTHESIS, difficulty="hard") for i in range(5)]
        curator = CaseCurator(ScriptedEmbedder({}), dedup_threshold=0.9)
        # Cells sum to 4, so the result never exceeds 4 even with ten cases available.
        allocation = _allocation((Archetype.PINPOINT, "easy", "paper", 2), (Archetype.SYNTHESIS, "hard", "paper", 2))

        kept = curator.stratify_cells(pinpoint + synthesis, allocation)

        assert len(kept) == 4

    def test_empty_allocation_returns_empty(self):
        curator = CaseCurator(ScriptedEmbedder({}), dedup_threshold=0.9)

        assert curator.stratify_cells([_case("p0")], QuotaAllocation(cells=())) == []
