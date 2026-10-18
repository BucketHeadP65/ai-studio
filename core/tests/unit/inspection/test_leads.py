"""A lead: one symptom seen at some spans, keyed by a digest of the spans."""

import pytest

from pydantic import ValidationError

from core.ai.inspection.domain.leads import Lead
from core.ai.transcripts.contracts import Span


def test_the_digest_depends_on_the_spans_only() -> None:
    spans = (Span(step=0, message=1, part=0), Span(step=0, message=3, part=0))

    one = Lead(source="record:run-a", symptom="duplicate_call", spans=spans, found_by="duplicate_call")
    other = Lead(source="record:run-b", symptom="duplicate_call", spans=spans, found_by="other_rule")

    assert one.spans_digest == other.spans_digest
    assert len(one.spans_digest) == 64


def test_different_spans_give_a_different_digest() -> None:
    first = Lead(source="record:run-a", symptom="duplicate_call", spans=(Span(step=0, message=1, part=0),), found_by="r")
    second = Lead(source="record:run-a", symptom="duplicate_call", spans=(Span(step=0, message=2, part=0),), found_by="r")

    assert first.spans_digest != second.spans_digest


def test_a_lead_needs_at_least_one_span() -> None:
    with pytest.raises(ValidationError):
        Lead(source="record:run-a", symptom="duplicate_call", spans=(), found_by="r")
