"""The public surface of schema recommendations."""

from engram.evals.recommendation.domain.adr import render_draft_adr
from engram.evals.recommendation.domain.detectors import SectionStat, StructuralDiagnosis
from engram.evals.recommendation.domain.gate import gate_recommendation
from engram.evals.recommendation.domain.recommendation import (
    BaselineSnapshot,
    CandidateEdge,
    CaseSnapshot,
    DraftRecommendation,
    KnowledgeBaseStamp,
    MinedLink,
    OversizedSection,
)
from engram.evals.recommendation.domain.verify import render_verify_markdown, verify_recommendation
from engram.evals.recommendation.infrastructure.mcp_recommendation import register_recommendation_tools
from engram.evals.recommendation.infrastructure.recommendation_files import load_recommendation, save_recommendation
from engram.evals.recommendation.infrastructure.schema_edits import ApplySchemaEditsReport, apply_schema_edits
from engram.evals.recommendation.internal_contracts import RecommendationProposer
from engram.evals.recommendation.service import (
    case_snapshots,
    remap_diagnosis,
    resolve_baseline_policy,
    resolve_rerun_policy,
    simulate_restructures,
    structural_diagnosis,
)

__all__ = [
    "ApplySchemaEditsReport",
    "BaselineSnapshot",
    "CandidateEdge",
    "CaseSnapshot",
    "DraftRecommendation",
    "KnowledgeBaseStamp",
    "MinedLink",
    "OversizedSection",
    "RecommendationProposer",
    "SectionStat",
    "StructuralDiagnosis",
    "apply_schema_edits",
    "case_snapshots",
    "gate_recommendation",
    "load_recommendation",
    "register_recommendation_tools",
    "remap_diagnosis",
    "render_draft_adr",
    "render_verify_markdown",
    "resolve_baseline_policy",
    "resolve_rerun_policy",
    "save_recommendation",
    "simulate_restructures",
    "structural_diagnosis",
    "verify_recommendation",
]
