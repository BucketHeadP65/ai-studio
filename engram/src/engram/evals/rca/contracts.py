"""The public surface of root-cause analysis."""

from engram.evals.rca.domain.diagnosis import CaseLike, Diagnosis, FailingCase, ReportLike, diagnose
from engram.evals.rca.domain.rca_report import build_rca_prompt, render_rca_report_markdown
from engram.evals.rca.domain.reflection import (
    BucketPrediction,
    DraftReflection,
    PredictionOutcome,
    ReflectiveFinding,
    gate_reflection,
    grade_predictions,
)
from engram.evals.rca.infrastructure.logfire_query import build_logfire_query_client
from engram.evals.rca.infrastructure.saved_queries import load_named_queries
from engram.evals.rca.internal_contracts import Reflector, ReflectorFactory
from engram.evals.rca.service import build_rca_report

__all__ = [
    "BucketPrediction",
    "CaseLike",
    "Diagnosis",
    "DraftReflection",
    "FailingCase",
    "PredictionOutcome",
    "ReflectiveFinding",
    "Reflector",
    "ReflectorFactory",
    "ReportLike",
    "build_logfire_query_client",
    "build_rca_prompt",
    "build_rca_report",
    "diagnose",
    "gate_reflection",
    "grade_predictions",
    "load_named_queries",
    "render_rca_report_markdown",
]
