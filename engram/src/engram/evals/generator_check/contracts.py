"""The generator check: the use case, its result and error, the rankings and tau-b it rests on, its scorer port and its report writer."""

from engram.evals.generator_check.domain import RANK_DECIMALS, GeneratorCheck, SetupRanking, generator_check, kendall_tau_b, setup_ranks
from engram.evals.generator_check.exceptions import SetupsTiedError
from engram.evals.generator_check.infrastructure.files import write_generator_check
from engram.evals.generator_check.internal_contracts import SetupScorer
from engram.evals.generator_check.service import check_generator

__all__ = [
    "RANK_DECIMALS",
    "GeneratorCheck",
    "SetupRanking",
    "SetupScorer",
    "SetupsTiedError",
    "check_generator",
    "generator_check",
    "kendall_tau_b",
    "setup_ranks",
    "write_generator_check",
]
