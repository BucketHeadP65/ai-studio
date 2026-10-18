"""The drafted case shapes and the quote check that keeps a draft only when its quotes are verbatim in the seed."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from engram.evals.generation.domain.datasets import AuthoredCase
from engram.evals.generation.domain.mirror import assemble_mirror, mirror_hash
from engram.evals.shared.contracts import DEFAULT_DIFFICULTY_THRESHOLDS, ExpectedQa, QaCaseMeta, QaInput, derive_difficulty
from engram.kb.contracts import quotes_in_mirror

if TYPE_CHECKING:
    from engram.evals.shared.contracts import DifficultyThresholds, Seed


class SeedCaseDraft(BaseModel):
    """One drafted case as the gold-from-seed agent emits it.

    Each support quote is a short snippet copied verbatim from the seed's source bodies. `accept`
    verifies them before a case is kept.
    """

    model_config = ConfigDict(frozen=True)

    question: str
    answer: str
    support_quotes: list[str]  # >=1 SHORT snippets, each copied VERBATIM from the seed's source bodies


class SeedCaseDrafts(BaseModel):
    """The gold-from-seed agent's structured output: a batch of seed-grounded drafts."""

    model_config = ConfigDict(frozen=True)

    cases: list[SeedCaseDraft]


def accept(draft: SeedCaseDraft, *, seed: Seed, thresholds: DifficultyThresholds = DEFAULT_DIFFICULTY_THRESHOLDS) -> AuthoredCase | None:
    """Keep a seed-grounded draft only when every support quote is verbatim in the seed's source bodies.

    The gold source ids are the seed's `gold_ids`, so only the model's quotes are checked. Difficulty comes
    from the seed's structural shape through `derive_difficulty`, and the built case carries the seed's
    archetype and source-provenance anchors.

    Args:
        draft: The drafted case to vet (question, answer, support quotes).
        seed: The structural seed supplying the gold ids, archetype, anchors, and difficulty shape.
        thresholds: The difficulty band cutoffs passed to `derive_difficulty`.

    Returns:
        The built `AuthoredCase`, or `None` when the seed has no gold ids, the draft has no support
        quotes, or any support quote is not verbatim in the seed's source bodies.
    """
    if not seed.gold_ids or not draft.support_quotes:
        return None
    mirror = assemble_mirror(seed.source_bodies)
    if not all(quotes_in_mirror(quote, mirror) for quote in draft.support_quotes):
        return None
    difficulty = derive_difficulty(seed.n_sources, seed.n_hops, seed.n_sections, thresholds)
    return AuthoredCase(
        question=QaInput(question=draft.question),
        expected=ExpectedQa(answer=draft.answer, expected_source_ids=seed.gold_ids),
        meta=QaCaseMeta(
            anchor_page_id=seed.gold_ids[0],
            difficulty=difficulty,
            archetype=seed.archetype,
            anchors=seed.anchors,
            mirror_hash=mirror_hash(seed.source_bodies),
        ),
    )
