"""What an evaluation run read answers, and what a finished run is written from."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, JsonValue


class EvaluationCaseResultRow(BaseModel):
    """One evaluation case result: one case's scores in one run."""

    model_config = ConfigDict(frozen=True)

    case_name: str
    scores: dict[str, JsonValue]  # per-case recall@k / precision@k / mrr
    n_tool_calls: int
    total_tokens: int
    duration_s: float


class EvaluationRunSummary(BaseModel):
    """One evaluation run's summary, the row every evaluation run writes."""

    model_config = ConfigDict(frozen=True)

    run_id: str  # run id (uuid or Logfire trace id), the ORM's `id` PK
    dataset: str
    model_name: str
    variant: dict[str, JsonValue]  # the experiment axes for this run
    metrics: dict[str, JsonValue]  # aggregate recall@k / precision@k / mrr / tokens / latency
    started_at: datetime
    finished_at: datetime | None = None
    logfire_url: str | None = None


class EvaluationRunDetail(EvaluationRunSummary):
    """One evaluation run's summary with its evaluation case results."""

    case_results: tuple[EvaluationCaseResultRow, ...] = ()
