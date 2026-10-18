"""The search setups ranked by nDCG@10 in each dataset, Kendall's tau-b between the two rankings, and the verdict.

Each nDCG@10 is rounded to `RANK_DECIMALS` decimals, the precision the reports print, before the setups are ranked. A
setup's rank is one plus the number of setups with a higher rounded nDCG@10, so setups that print the same nDCG@10 share
a rank and the next rank skips. Tau-b, the agreeing pairs and the verdict read the same rounded values.
"""

from __future__ import annotations

import math

from itertools import combinations
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from engram.evals.generator_check.exceptions import SetupsTiedError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

RANK_DECIMALS = 4
"""How many decimals of nDCG@10 the setups are ranked on, the same the reports print."""


class SetupRanking(BaseModel):
    """One search setup's nDCG@10 and rank in the human-judged dataset and in the generated one."""

    model_config = ConfigDict(frozen=True)

    setup: str
    """The search setup's name."""
    human_ndcg: float
    """The human-judged dataset's mean nDCG@10 under the setup, unrounded."""
    human_rank: int
    """The setup's rank in the human-judged dataset, 1 for the highest nDCG@10."""
    generated_ndcg: float
    """The generated dataset's mean nDCG@10 under the setup, unrounded."""
    generated_rank: int
    """The setup's rank in the generated dataset, 1 for the highest nDCG@10."""


class GeneratorCheck(BaseModel):
    """Both datasets' rankings of the search setups, Kendall's tau-b between them, the agreeing setup pairs and the verdict."""

    model_config = ConfigDict(frozen=True)

    setups: tuple[SetupRanking, ...]
    """One row per search setup, in the order the setups were scored."""
    tau_b: float
    """Kendall's tau-b between the two rankings, from -1 for reversed orders to 1 for the same order."""
    agreeing_pairs: int
    """How many setup pairs both datasets order the same way, a pair both tie included."""
    pairs: int
    """How many setup pairs there are."""
    passed: bool
    """True when both datasets rank the setups in the same order, which is when tau-b is 1."""
    rank_decimals: int
    """How many decimals of nDCG@10 the ranks, tau-b, the agreeing pairs and the verdict read. The rows keep it unrounded."""


def setup_ranks(ndcg: Mapping[str, float]) -> dict[str, int]:
    """The rank of each search setup by its nDCG@10, 1 for the highest.

    Setups with the same nDCG@10 share a rank, and the next rank skips.

    Args:
        ndcg: Each setup's nDCG@10, by setup name.

    Returns:
        Each setup's rank, by setup name, in the order of `ndcg`.
    """
    return {setup: 1 + sum(other > value for other in ndcg.values()) for setup, value in ndcg.items()}


def kendall_tau_b(first: Sequence[float], second: Sequence[float]) -> float:
    """Kendall's tau-b between two orders of the same items, corrected for ties in either.

    Item `i` of `first` and item `i` of `second` are the same item's two values.

    Args:
        first: Each item's value in the first order.
        second: Each item's value in the second order.

    Returns:
        A value from -1 for reversed orders to 1 for the same order.

    Raises:
        ValueError: When the two orders differ in length, or when either gives every item the same value, where tau-b
            is undefined.
    """
    if len(first) != len(second):
        msg = f"tau-b needs two orders of the same length, got {len(first)} and {len(second)}"
        raise ValueError(msg)
    pairs = list(zip(_pair_signs(first), _pair_signs(second), strict=True))
    tied_first = sum(sign == 0 for sign, _ in pairs)
    tied_second = sum(sign == 0 for _, sign in pairs)
    if tied_first == len(pairs) or tied_second == len(pairs):
        msg = "tau-b is undefined when every value is the same in one order"
        raise ValueError(msg)
    concordant = sum(left * right > 0 for left, right in pairs)
    discordant = sum(left * right < 0 for left, right in pairs)
    return (concordant - discordant) / math.sqrt((len(pairs) - tied_first) * (len(pairs) - tied_second))


def generator_check(human: Mapping[str, float], generated: Mapping[str, float]) -> GeneratorCheck:
    """Compare how the human-judged and the generated dataset rank the search setups by nDCG@10.

    Each nDCG@10 is rounded to `RANK_DECIMALS` decimals before anything is ranked or compared. The check passes only
    when both rank the setups in the same order: every setup pair is ordered the same way by both, a pair both tie
    included. The rows follow the order of `human` and keep each nDCG@10 unrounded.

    Args:
        human: The human-judged dataset's nDCG@10 under each setup, by setup name.
        generated: The generated dataset's nDCG@10 under the same setups, by setup name.

    Returns:
        Both rankings, Kendall's tau-b between them, the agreeing setup pairs and the verdict.

    Raises:
        ValueError: When the two name different setups.
        SetupsTiedError: When either dataset gives every setup the same rounded nDCG@10. The message names the dataset
            and the value, and the usual causes when the value is 0.
    """
    if set(human) != set(generated):
        msg = f"the two datasets were scored under different search setups: {', '.join(human)} and {', '.join(generated)}"
        raise ValueError(msg)
    setups = list(human)
    human_rounded = {setup: round(human[setup], RANK_DECIMALS) for setup in setups}
    generated_rounded = {setup: round(generated[setup], RANK_DECIMALS) for setup in setups}
    for name, rounded in (("human-judged", human_rounded), ("generated", generated_rounded)):
        if len(set(rounded.values())) <= 1:
            raise SetupsTiedError(_tied_message(name, next(iter(rounded.values()), 0.0)))
    human_values = list(human_rounded.values())
    generated_values = list(generated_rounded.values())
    human_ranks = setup_ranks(human_rounded)
    generated_ranks = setup_ranks(generated_rounded)
    signs = list(zip(_pair_signs(human_values), _pair_signs(generated_values), strict=True))
    agreeing = sum(left == right for left, right in signs)
    return GeneratorCheck(
        setups=tuple(
            SetupRanking(
                setup=setup,
                human_ndcg=human[setup],
                human_rank=human_ranks[setup],
                generated_ndcg=generated[setup],
                generated_rank=generated_ranks[setup],
            )
            for setup in setups
        ),
        tau_b=kendall_tau_b(human_values, generated_values),
        agreeing_pairs=agreeing,
        pairs=len(signs),
        passed=agreeing == len(signs),
        rank_decimals=RANK_DECIMALS,
    )


def generator_check_markdown(check: GeneratorCheck, *, human: str, generated: str, collection: str) -> str:
    """The markdown summary of a generator check: the verdict, both rankings, tau-b and the agreeing setup pairs.

    Args:
        check: The generator check.
        human: The human-judged dataset file's name.
        generated: The generated dataset file's name.
        collection: The knowledge base searched.

    Returns:
        The summary.
    """
    verdict = (
        "The check passed: both datasets rank the search setups in the same order."
        if check.passed
        else "The check failed: the two datasets rank the search setups in different orders."
    )
    lines = [
        f"# Generator check: {generated} against {human} on {collection}",
        "",
        verdict,
        "",
        "| Search setup | Human nDCG@10 | Human rank | Generated nDCG@10 | Generated rank |",
        "|---|---|---|---|---|",
        *(
            f"| {row.setup} | {row.human_ndcg:.{check.rank_decimals}f} | {row.human_rank} "
            f"| {row.generated_ndcg:.{check.rank_decimals}f} | {row.generated_rank} |"
            for row in check.setups
        ),
        "",
        f"Kendall's tau-b: {check.tau_b:.4f}.",
        f"Setup pairs both datasets order the same way: {check.agreeing_pairs} of {check.pairs}.",
        (
            f"Each dataset ranks the setups by its own nDCG@10 rounded to {check.rank_decimals} decimals, highest first, "
            "and setups with the same rounded nDCG@10 share a rank."
        ),
    ]
    return "\n".join(lines) + "\n"


def _tied_message(dataset: str, ndcg: float) -> str:
    """The refusal for a dataset that gives every setup the same rounded nDCG@10, with the usual causes of a 0."""
    message = (
        f"the {dataset} dataset gives every search setup the same nDCG@10, {ndcg:.{RANK_DECIMALS}f}, so it ranks no setup above another"
    )
    if ndcg == 0:
        message += ". An nDCG@10 of 0 usually means the wrong collection, or gold ids that map to no source document"
    return message


def _pair_signs(values: Sequence[float]) -> list[int]:
    """For each pair of positions `i < j`, 1 when value `i` is higher, -1 when it is lower and 0 when they tie."""
    return [(left > right) - (left < right) for left, right in combinations(values, 2)]
