"""Session kind rides the span fold and the runtime; hydration respects a scoped forget."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from harness.domain.events import ConversationForgotten, SessionStarted, TurnSettled, TurnStarted, session_spans
from harness.features.sessions.api_models import SessionSummary, UsageTotals
from harness.session import hydration_facts
from tests.support.state import a_session_log

if TYPE_CHECKING:
    from pathlib import Path


def _mint(session_id: str, kind: str, tmp_path: Path) -> None:
    a_session_log(tmp_path).record(SessionStarted(session_id=session_id, session_kind=kind))


def test_the_span_fold_carries_the_kind_the_start_event_named(tmp_path: Path) -> None:
    _mint("s-master", "master", tmp_path)
    started = a_session_log(tmp_path).record(TurnStarted(entry="spa", chars=2, session_id="s-master", prompt_excerpt=None))
    assert started is not None
    a_session_log(tmp_path).record(TurnSettled(run_id="run-1", started_seq=started.seq))

    span = next(span for span in session_spans(a_session_log(tmp_path).events()) if span.session_id == "s-master")
    assert span.session_kind == "master"
    assert span.run_ids == ("run-1",)


def test_a_span_with_no_start_event_defaults_to_chat(tmp_path: Path) -> None:
    a_session_log(tmp_path).record(TurnStarted(entry="spa", chars=2, session_id="s-legacy", prompt_excerpt=None))
    span = next(span for span in session_spans(a_session_log(tmp_path).events()) if span.session_id == "s-legacy")
    assert span.session_kind == "chat"


def test_hydration_facts_drop_runs_from_before_the_forget(tmp_path: Path) -> None:
    _mint("s-1", "master", tmp_path)
    first = a_session_log(tmp_path).record(TurnStarted(entry="spa", chars=2, session_id="s-1", prompt_excerpt=None))
    assert first is not None
    a_session_log(tmp_path).record(TurnSettled(run_id="run-old", started_seq=first.seq))
    a_session_log(tmp_path).record(ConversationForgotten(session_id="s-1"))
    second = a_session_log(tmp_path).record(TurnStarted(entry="spa", chars=2, session_id="s-1", prompt_excerpt=None))
    assert second is not None
    a_session_log(tmp_path).record(TurnSettled(run_id="run-new", started_seq=second.seq))

    kind, run_ids = hydration_facts(a_session_log(tmp_path).events(), "s-1")
    assert kind == "master", "the kind survives a forget"
    assert run_ids == ("run-new",), "a forgotten run must never come back at hydration"


def test_summary_accepts_the_master_kind() -> None:
    stamp = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
    summary = SessionSummary(
        id="s",
        project_id="p",
        title="t",
        started_at=stamp,
        last_activity_at=stamp,
        archived=False,
        answering=False,
        turn_count=0,
        usage=UsageTotals(),
        kind="master",
        cost_usd=Decimal(0),
        context=None,
    )
    assert summary.kind == "master"
