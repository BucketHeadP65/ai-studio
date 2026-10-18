"""The public surface of retrieval-policy tuning.

Each name resolves on first access, so importing this module loads no other module of the slice.
"""

from typing import TYPE_CHECKING

from core.lazy_imports import lazy_module_attributes

if TYPE_CHECKING:
    from engram.evals.tuning.domain.objective import TuningObjective
    from engram.evals.tuning.domain.proposal import ProposedPolicy
    from engram.evals.tuning.infrastructure.mcp_tuning import register_tuning_tools
    from engram.evals.tuning.internal_contracts import PolicyEvaluator, PolicyProposer
    from engram.evals.tuning.policy import (
        DEFAULT_INSTRUCTIONS,
        POLICY_MODULE_PATH,
        RetrievalPolicy,
        baseline_policy,
        effective_baseline_policy,
        load_active_policy,
        load_policy,
        save_policy,
    )
    from engram.evals.tuning.service import TuningLoop, TuningResult
    from engram.evals.tuning.strategy import DEFAULT_STRATEGY, EVAL_STRATEGY_NAMES, RetrievalProfile, RetrievalStrategy, resolve_strategy

__all__ = [
    "DEFAULT_INSTRUCTIONS",
    "DEFAULT_STRATEGY",
    "EVAL_STRATEGY_NAMES",
    "POLICY_MODULE_PATH",
    "PolicyEvaluator",
    "PolicyProposer",
    "ProposedPolicy",
    "RetrievalPolicy",
    "RetrievalProfile",
    "RetrievalStrategy",
    "TuningLoop",
    "TuningObjective",
    "TuningResult",
    "baseline_policy",
    "effective_baseline_policy",
    "load_active_policy",
    "load_policy",
    "register_tuning_tools",
    "resolve_strategy",
    "save_policy",
]

_LAZY_IMPORTS = {
    "PolicyEvaluator": "engram.evals.tuning.internal_contracts",
    "PolicyProposer": "engram.evals.tuning.internal_contracts",
    "TuningObjective": "engram.evals.tuning.domain.objective",
    "ProposedPolicy": "engram.evals.tuning.domain.proposal",
    "TuningLoop": "engram.evals.tuning.service",
    "TuningResult": "engram.evals.tuning.service",
    "register_tuning_tools": "engram.evals.tuning.infrastructure.mcp_tuning",
    "DEFAULT_INSTRUCTIONS": "engram.evals.tuning.policy",
    "POLICY_MODULE_PATH": "engram.evals.tuning.policy",
    "RetrievalPolicy": "engram.evals.tuning.policy",
    "baseline_policy": "engram.evals.tuning.policy",
    "effective_baseline_policy": "engram.evals.tuning.policy",
    "load_active_policy": "engram.evals.tuning.policy",
    "load_policy": "engram.evals.tuning.policy",
    "save_policy": "engram.evals.tuning.policy",
    "DEFAULT_STRATEGY": "engram.evals.tuning.strategy",
    "EVAL_STRATEGY_NAMES": "engram.evals.tuning.strategy",
    "RetrievalProfile": "engram.evals.tuning.strategy",
    "RetrievalStrategy": "engram.evals.tuning.strategy",
    "resolve_strategy": "engram.evals.tuning.strategy",
}


__getattr__, __dir__ = lazy_module_attributes(__name__, _LAZY_IMPORTS, __all__)
