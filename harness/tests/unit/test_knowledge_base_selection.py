"""Which knowledge bases a set of arguments selects, read through the card's knowledge-base selector."""

from __future__ import annotations

from core.ai.workflows.contracts import KNOWLEDGE_BASE_CONTROL, InputPresentation, WorkflowPresentation

from harness.domain.contracts import selected_knowledge_bases

SELECTING = WorkflowPresentation(
    inputs=(
        InputPresentation(key="base", label="Knowledge base", control=KNOWLEDGE_BASE_CONTROL),
        InputPresentation(key="note", label="A note", control="text"),
    )
)


def test_a_selector_over_text_selects_that_name() -> None:
    assert selected_knowledge_bases(SELECTING, {"base": "notes", "note": "other"}) == frozenset({"notes"})


def test_a_selector_over_a_list_selects_every_name_in_it() -> None:
    assert selected_knowledge_bases(SELECTING, {"base": ["notes", "papers"]}) == frozenset({"notes", "papers"})


def test_a_parameter_the_card_does_not_select_through_selects_nothing() -> None:
    assert selected_knowledge_bases(SELECTING, {"note": "notes"}) == frozenset()


def test_no_card_selects_nothing() -> None:
    assert selected_knowledge_bases(None, {"base": "notes"}) == frozenset()


def test_a_value_that_is_not_a_name_selects_nothing() -> None:
    assert selected_knowledge_bases(SELECTING, {"base": 3}) == frozenset()
