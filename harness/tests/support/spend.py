"""Spend limits for a test whose agent answers with a test model, which no price list prices.

An unpriced response makes the spend brake warn. These price every response at nothing, so a test model counts its
requests and spends no money.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from pydantic_ai_harness.spend import Budget, SpendLimits

from harness.session import Runtime

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelResponse


def free(response: ModelResponse) -> Decimal:
    """Price any response at nothing."""
    del response
    return Decimal(0)


def free_spend() -> SpendLimits[Runtime]:
    """Spend limits of five dollars a run that price every response at nothing."""
    return SpendLimits[Runtime](budgets=[Budget(usd=Decimal(5), window="run")], price=free)
