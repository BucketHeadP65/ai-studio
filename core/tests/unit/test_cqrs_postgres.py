"""The Postgres statements compile for the PostgreSQL dialect and the outbox row shape is JSON-safe."""

from __future__ import annotations

from sqlalchemy.dialects import postgresql

from core.cqrs.contracts import Event
from core.cqrs.infrastructure.postgres import CLAIM, outbox_rows


class _Happened(Event):
    knowledge_base: str = "research"


def test_the_claim_statement_compiles_with_skip_locked():
    sql = str(CLAIM.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "state IN ('queued', 'failed') AND available_at <= now()" in sql
    assert "make_interval(secs => lease_seconds)" in sql


def test_outbox_rows_carry_the_wire_name_and_a_json_payload():
    event = _Happened()
    (row,) = outbox_rows([event])
    assert row["event_id"] == event.event_id
    assert row["event_type"] == "_Happened"
    assert row["payload"] == event.model_dump(mode="json")
