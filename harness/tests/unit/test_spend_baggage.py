"""A step's spans carry the run they work for, so a cost can be read off the span that holds it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import logfire

from harness.composition import step_baggage
from harness.session import ActiveRun

if TYPE_CHECKING:
    from logfire.testing import CaptureLogfire


def _attributes_of(capfire: CaptureLogfire, name: str) -> dict[str, object]:
    spans = [span for span in capfire.exporter.exported_spans_as_dict() if span["name"] == name]
    assert len(spans) == 1, f"expected one span named {name!r}, saw {[span['name'] for span in spans]}"
    return dict(spans[0]["attributes"])


def test_a_span_under_a_bound_run_carries_its_identities(capfire: CaptureLogfire) -> None:
    active = ActiveRun()
    active.run_id = "run-1"
    active.workflow_id = "wf.digest.v9"

    with step_baggage(active, "draft"), logfire.span("chat deepseek"):
        pass

    attributes = _attributes_of(capfire, "chat deepseek")
    assert attributes["run_id"] == "run-1"
    assert attributes["workflow"] == "wf.digest.v9"
    assert attributes["step"] == "draft"


def test_a_span_under_no_run_carries_nothing(capfire: CaptureLogfire) -> None:
    with step_baggage(ActiveRun(), "draft"), logfire.span("chat deepseek"):
        pass

    attributes = _attributes_of(capfire, "chat deepseek")
    assert "run_id" not in attributes
    assert "step" not in attributes
