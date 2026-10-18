"""The ORM rows of the `kb.evaluation_runs` and `kb.evaluation_case_results` tables.

SQLAlchemy resolves the `Mapped[...]` annotations when it configures the mappers.
So `datetime` and `JsonValue` stay imported at module scope, never under `TYPE_CHECKING`.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import DateTime, Float, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from engram.db.contracts import KbBase

_SCHEMA = {"schema": "kb"}


class EvaluationRunORM(KbBase):
    """One evaluation run's summary row."""

    __tablename__ = "evaluation_runs"
    __table_args__ = _SCHEMA

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # run id (uuid or Logfire trace id)
    dataset: Mapped[str] = mapped_column(Text)
    model_name: Mapped[str] = mapped_column(Text)
    variant: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)  # the experiment axes for this run
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metrics: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)  # aggregate recall@k / precision@k / mrr / tokens / latency
    logfire_url: Mapped[str | None] = mapped_column(Text, nullable=True)


class EvaluationCaseResultORM(KbBase):
    """One evaluation case result row, under the `EvaluationRunORM` it belongs to."""

    __tablename__ = "evaluation_case_results"
    __table_args__ = _SCHEMA

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(Text, ForeignKey("kb.evaluation_runs.id"))
    case_name: Mapped[str] = mapped_column(Text)
    scores: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)  # per-case recall@k / precision@k / mrr
    n_tool_calls: Mapped[int] = mapped_column(Integer)
    total_tokens: Mapped[int] = mapped_column(Integer)
    duration_s: Mapped[float] = mapped_column(Float)
