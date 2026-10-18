"""The stratified salted-hash carve that picks a subset of cases, and the salted key it ranks by."""

from __future__ import annotations

import hashlib

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from engram.evals.generation.domain.datasets import EvaluationCase
    from engram.evals.shared.contracts import Archetype, RatedDifficulty

type _CaseStratum = tuple[Archetype | None, RatedDifficulty]


def salted_key(text: str, salt: str) -> str:
    """Return the md5 digest of `{text}|{salt}`, a stable ranking key for one salt.

    The same text and salt always give the same key. The key is computed in Python and does not match the
    `md5(key || salt)` order the SQL seed draws use.

    Args:
        text: The stable text to rank, such as a case name or a seed key.
        salt: The salt that changes the order from one draw to the next.

    Returns:
        The hexadecimal md5 digest.
    """
    return hashlib.md5(f"{text}|{salt}".encode(), usedforsecurity=False).hexdigest()


def case_stratum(case: EvaluationCase) -> tuple[Archetype | None, RatedDifficulty]:
    """Return a case's archetype and difficulty band, the stratum a carve groups it under.

    A case with no metadata has no archetype and the difficulty `"medium"`. A case whose metadata has no
    difficulty, a human-judged one, is `"unrated"`.

    Args:
        case: The case to place.

    Returns:
        The `(archetype, difficulty)` pair, with `None` for an unlabelled archetype.
    """
    metadata = case.metadata
    if metadata is None:
        return (None, "medium")
    return (metadata.archetype, metadata.difficulty or "unrated")


def carve_strata(
    cases: Sequence[EvaluationCase], *, total: int, min_per_stratum: int, salt: str
) -> tuple[list[EvaluationCase], list[tuple[Archetype | None, RatedDifficulty]]]:
    """Select `total` cases stratum by stratum, taking the leading cases of each in salted-key order.

    You must cap `total` at the number of cases. A stratum with fewer than `min_per_stratum` cases folds
    into the band of its difficulty with no archetype. The quotas split `total` across the bands in
    proportion to their sizes by largest remainder. The same cases and salt always give the same selection.

    Args:
        cases: The cases to carve from.
        total: How many cases to select.
        min_per_stratum: The smallest stratum kept as its own band. Zero keeps every stratum.
        salt: The salt of the salted-key order.

    Returns:
        The selected cases in band order, then salted-key order within a band, and the strata that folded.
    """
    strata: dict[_CaseStratum, list[EvaluationCase]] = {}
    for case in cases:
        strata.setdefault(case_stratum(case), []).append(case)

    folded: list[_CaseStratum] = []
    bands: dict[_CaseStratum, list[EvaluationCase]] = {}
    for stratum, members in strata.items():
        if len(members) < min_per_stratum:
            folded.append(stratum)
            bands.setdefault((None, stratum[1]), []).extend(members)
        else:
            bands.setdefault(stratum, []).extend(members)

    quotas = _allocate({key: len(members) for key, members in bands.items()}, total)
    selected: list[EvaluationCase] = []
    for key, members in bands.items():
        selected.extend(_salted_order(members, salt=salt)[: quotas[key]])
    return selected, folded


def _salted_order(cases: Sequence[EvaluationCase], *, salt: str) -> list[EvaluationCase]:
    """The cases in ascending salted-key order of their names."""
    return sorted(cases, key=lambda case: salted_key(case.name or "", salt))


def _allocate(sizes: Mapping[_CaseStratum, int], total: int) -> dict[_CaseStratum, int]:
    """Split `total` across strata in proportion to their sizes by largest remainder, ties by label."""
    grand = sum(sizes.values())
    if grand == 0 or total <= 0:
        return dict.fromkeys(sizes, 0)

    exact = {key: total * size / grand for key, size in sizes.items()}
    quotas = {key: int(value) for key, value in exact.items()}
    remainder = total - sum(quotas.values())
    ranked = sorted(sizes, key=lambda key: (exact[key] - quotas[key], _tie_break_label(key)), reverse=True)
    for key in ranked[:remainder]:
        quotas[key] += 1
    return quotas


def _tie_break_label(stratum: _CaseStratum) -> str:
    """The stable `archetype|difficulty` label that orders strata with equal remainders."""
    archetype, difficulty = stratum
    return f"{'' if archetype is None else archetype.value}|{difficulty}"
