"""Check a dataset generator: score a human-judged and a generated dataset under every search setup and compare the rankings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.evals.generator_check.domain import GeneratorCheck, generator_check
from engram.evals.scoring.contracts import SEARCH_SETUPS, score_report_stem

if TYPE_CHECKING:
    from pathlib import Path

    from engram.evals.generator_check.internal_contracts import SetupScorer


async def check_generator(human: Path, generated: Path, *, collection: str, scorer: SetupScorer) -> GeneratorCheck:
    """Score both datasets under every search setup, one scoring at a time, and compare how each ranks the setups.

    Two datasets that would write a score report of the same name, under any two setups, are refused before either is
    scored, and so is a dataset file that does not exist.

    Args:
        human: The human-judged dataset file.
        generated: The generated dataset file.
        collection: The knowledge base both are searched in.
        scorer: Scores one dataset under one setup.

    Returns:
        Both rankings, Kendall's tau-b between them, the agreeing setup pairs and the verdict.

    Raises:
        ValueError: When the two datasets would write a score report of the same name.
        FileNotFoundError: When either dataset file does not exist. The message names the missing path.
        SetupsTiedError: When either dataset gives every setup the same nDCG@10 at the precision the check ranks on.
    """
    shared = {score_report_stem(human, setup) for setup in SEARCH_SETUPS} & {score_report_stem(generated, setup) for setup in SEARCH_SETUPS}
    if shared:
        msg = (
            f"the human-judged dataset {human.name} and the generated dataset {generated.name} would both write the score report "
            f"'{min(shared)}.score.yaml', so their score reports would overwrite each other"
        )
        raise ValueError(msg)
    for name, dataset in (("human-judged", human), ("generated", generated)):
        if not dataset.is_file():
            msg = f"the {name} dataset {dataset} does not exist"
            raise FileNotFoundError(msg)
    human_ndcg: dict[str, float] = {}
    generated_ndcg: dict[str, float] = {}
    for setup in SEARCH_SETUPS:
        human_ndcg[setup] = await scorer(human, collection=collection, setup=setup)
        generated_ndcg[setup] = await scorer(generated, collection=collection, setup=setup)
    return generator_check(human_ndcg, generated_ndcg)
