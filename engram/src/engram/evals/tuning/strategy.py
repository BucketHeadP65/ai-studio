"""Re-export core's retrieval-strategy axis for the tuning slice and its contracts.

The axis is the named `RetrievalStrategy` set, the `RetrievalProfile` primitives and `resolve_strategy`.
"""

from __future__ import annotations

from core.ai.retrieval.contracts import (
    DEFAULT_STRATEGY,
    EVAL_STRATEGY_NAMES,
    STRATEGY_NAMES,
    RetrievalProfile,
    RetrievalStrategy,
    resolve_strategy,
)

__all__ = ["DEFAULT_STRATEGY", "EVAL_STRATEGY_NAMES", "STRATEGY_NAMES", "RetrievalProfile", "RetrievalStrategy", "resolve_strategy"]
