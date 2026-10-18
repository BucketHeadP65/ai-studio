"""The public surface of the outcome-evaluation slice.

Each name resolves on first access, so asking for the trial toolset loads no case-custody code.
"""

from typing import TYPE_CHECKING

from core.lazy_imports import lazy_module_attributes

if TYPE_CHECKING:
    from engram.evals.outcomes.domain.comparison import StrategyPairVerdict, graph_pair_verdict
    from engram.evals.outcomes.domain.evaluators import DEFAULT_KS, citation_source_metrics, ndcg_at_k, page_of, ranked_metrics
    from engram.evals.outcomes.domain.governance import CaseSeal, LabelExposure, MethodologyAudit, audit_cycle
    from engram.evals.outcomes.domain.withheld import WithheldRefs
    from engram.evals.outcomes.exceptions import WithheldEvidenceLeakError
    from engram.evals.outcomes.infrastructure.toolset import GuardedTracingToolset

__all__ = [
    "DEFAULT_KS",
    "CaseSeal",
    "GuardedTracingToolset",
    "LabelExposure",
    "MethodologyAudit",
    "StrategyPairVerdict",
    "WithheldEvidenceLeakError",
    "WithheldRefs",
    "audit_cycle",
    "citation_source_metrics",
    "graph_pair_verdict",
    "ndcg_at_k",
    "page_of",
    "ranked_metrics",
]

_LAZY_IMPORTS = {
    "CaseSeal": "engram.evals.outcomes.domain.governance",
    "LabelExposure": "engram.evals.outcomes.domain.governance",
    "MethodologyAudit": "engram.evals.outcomes.domain.governance",
    "audit_cycle": "engram.evals.outcomes.domain.governance",
    "StrategyPairVerdict": "engram.evals.outcomes.domain.comparison",
    "graph_pair_verdict": "engram.evals.outcomes.domain.comparison",
    "GuardedTracingToolset": "engram.evals.outcomes.infrastructure.toolset",
    "WithheldRefs": "engram.evals.outcomes.domain.withheld",
    "WithheldEvidenceLeakError": "engram.evals.outcomes.exceptions",
    "citation_source_metrics": "engram.evals.outcomes.domain.evaluators",
    "DEFAULT_KS": "engram.evals.outcomes.domain.evaluators",
    "ndcg_at_k": "engram.evals.outcomes.domain.evaluators",
    "page_of": "engram.evals.outcomes.domain.evaluators",
    "ranked_metrics": "engram.evals.outcomes.domain.evaluators",
}


__getattr__, __dir__ = lazy_module_attributes(__name__, _LAZY_IMPORTS, __all__)
