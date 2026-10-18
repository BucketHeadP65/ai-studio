"""The store's row types, validated once off what the driver hands back."""

from datetime import UTC, datetime

from core.ai.history.contracts import row_as
from core.ai.inspection.read_models import StoredFinding, StoredLead
from core.ai.transcripts.contracts import Span

AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def test_a_lead_row_decodes_its_spans_from_jsonb_text() -> None:
    row = row_as(
        StoredLead,
        {
            "lead_id": 1,
            "source": "record:run-a",
            "watch": "author",
            "symptom": "duplicate_call",
            "spans": '[{"step": 0, "message": 1, "part": 0}]',
            "spans_digest": "abc",
            "found_by": "duplicate_call",
            "seen_at": AT,
        },
    )

    assert row.spans == [Span(step=0, message=1, part=0)]


def test_a_finding_row_carries_its_blames_as_plain_names() -> None:
    row = row_as(
        StoredFinding,
        {
            "finding_id": 1,
            "lead_id": None,
            "source": "record:run-a",
            "symptom": "no_tool_for_the_job",
            "spans": [{"step": 0, "message": 1, "part": 0}],
            "blames": [{"root_cause": "missing_tool", "surface_kind": "tool", "surface_name": "", "why": "the only cause"}],
            "found_by": "rule",
            "model": None,
            "decided_at": AT,
        },
    )

    assert row.blames[0].root_cause == "missing_tool"
    assert row.lead_id is None


def test_a_stored_lead_row_without_the_refusal_columns_still_validates() -> None:
    row = {
        "lead_id": 1,
        "source": "record:r",
        "watch": "w",
        "symptom": "duplicate_call",
        "spans": "[]",
        "spans_digest": "d",
        "found_by": "duplicate_call",
        "seen_at": AT,
    }

    stored = row_as(StoredLead, row)

    assert stored.attempted_at is None
    assert stored.refusal is None
