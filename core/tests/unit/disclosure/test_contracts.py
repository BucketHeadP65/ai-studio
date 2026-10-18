"""Unit tests for the disclosure-envelope contracts (`core.ai.disclosure.contracts`).

A `DisclosureEnvelope` is the provider-agnostic spec vocabulary of the progressive-disclosure
protocol: one disclosed unit's identity, title, optional summary/content, its children (as
`ChildSummary` stubs), and the disclosure `mode` it was rendered under. These specs pin the
contract other code depends on: the envelope (and its children) are frozen, `approx_tokens`
yields a positive budget estimate, and the envelope round-trips through `model_dump`/
`model_validate` losslessly.
"""

from __future__ import annotations

import pytest

from pydantic import ValidationError

from core.ai.disclosure.contracts import ChildSummary, DisclosureEnvelope, approx_tokens


def _make_envelope() -> DisclosureEnvelope:
    return DisclosureEnvelope(
        ref="note/foo",
        kind="note",
        title="Foo",
        summary="A short summary of foo.",
        content="The full body of foo.",
        children=(
            ChildSummary(ref="note/foo/child-1", kind="note", title="Child 1", summary="First child."),
            ChildSummary(ref="note/foo/child-2", kind="note", title="Child 2", summary="Second child."),
        ),
    )


def test_envelope_is_frozen() -> None:
    envelope = _make_envelope()
    with pytest.raises(ValidationError):
        envelope.title = "Renamed"  # type: ignore[misc]


def test_child_summary_is_frozen() -> None:
    child = ChildSummary(ref="note/foo/child-1", kind="note", title="Child 1", summary="First child.")
    with pytest.raises(ValidationError):
        child.title = "Renamed"  # type: ignore[misc]


def test_approx_tokens_is_positive() -> None:
    assert approx_tokens(_make_envelope()) > 0


def test_envelope_round_trips_through_model_dump() -> None:
    envelope = _make_envelope()
    assert DisclosureEnvelope.model_validate(envelope.model_dump()) == envelope


def test_envelope_defaults() -> None:
    envelope = DisclosureEnvelope(ref="note/foo", kind="note", title="Foo")
    assert envelope.summary is None
    assert envelope.content is None
    assert envelope.children == ()
    assert envelope.mode == "strict"
