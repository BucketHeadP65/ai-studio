"""The rules over calls and answers: each one, the case it fires on, and one it stays silent on."""

from pydantic_ai.messages import CompactionPart, ModelResponse, ToolCallPart

from core.ai.inspection.domain.facts import ToolFacts
from core.ai.inspection.rules import (
    RULES,
    DeniedActionRepeated,
    DuplicateCall,
    RetriedAfterError,
    SchemaRejectedArguments,
    ToolRefusedCall,
    call_key,
)
from core.ai.transcripts.contracts import Span
from tests.support.fakes.inspection import AT, answer, call, leads_of, question, result, retried, transcript


def test_call_key_ignores_argument_order() -> None:
    first = call("c1", args={"a": 1, "b": 2}).parts[0]
    second = call("c2", args={"b": 2, "a": 1}).parts[0]
    assert isinstance(first, ToolCallPart)
    assert isinstance(second, ToolCallPart)

    assert call_key(first) == call_key(second)


async def test_schema_rejected_arguments_points_at_the_refusal_that_carries_the_reason() -> None:
    given = transcript(question(), call("c1"), retried("c1"), call("c2", args={"name": "orders"}), result("c2"))

    found = await leads_of(SchemaRejectedArguments(), given)

    assert [(lead.symptom, lead.spans) for lead in found] == [("schema_rejected_arguments", (Span(step=0, message=2, part=0),))]


async def test_schema_rejected_arguments_counts_only_a_call_the_schema_rejected() -> None:
    given = transcript(
        question(),
        call("c1"),
        retried("c1", cause="validation_error"),
        call("c2", args={"name": "orders"}),
        retried("c2", "no knowledge base named orders", cause="model_retry"),
        call("c3", tool_name="lookup_town"),
        retried("c3", "unknown tool name", cause="unknown_tool", tool_name="lookup_town"),
    )

    found = await leads_of(SchemaRejectedArguments(), given)

    assert [(lead.symptom, lead.spans) for lead in found] == [("schema_rejected_arguments", (Span(step=0, message=2, part=0),))]


async def test_tool_refused_call_points_at_the_refusal_the_tool_answered_with() -> None:
    given = transcript(
        question(),
        call("c1"),
        retried("c1", cause="validation_error"),
        call("c2", args={"name": "orders"}),
        retried("c2", "no knowledge base named orders", cause="model_retry"),
        call("c3", args={"name": "Paris"}),
        result("c3", "answered"),
    )

    found = await leads_of(ToolRefusedCall(), given)

    assert [(lead.symptom, lead.spans) for lead in found] == [("tool_refused_call", (Span(step=0, message=4, part=0),))]


async def test_a_retry_whose_cause_is_unknown_is_neither_a_rejection_nor_a_refusal() -> None:
    given = transcript(question(), call("c1"), retried("c1", "try again", cause=None))

    assert await leads_of(SchemaRejectedArguments(), given) == []
    assert await leads_of(ToolRefusedCall(), given) == []


async def test_retried_after_error_pairs_a_retried_call_with_its_repeat() -> None:
    given = transcript(question(), call("c1"), retried("c1", "no knowledge base named x", cause="model_retry"), call("c2"), result("c2"))

    found = await leads_of(RetriedAfterError(), given)

    assert [(lead.symptom, lead.spans) for lead in found] == [
        ("retried_after_clear_error", (Span(step=0, message=1, part=0), Span(step=0, message=3, part=0)))
    ]


async def test_retried_after_error_pairs_the_failed_call_with_its_repeat() -> None:
    given = transcript(question(), call("c1"), result("c1", "no such table", outcome="failed"), call("c2"), result("c2"), answer())

    found = await leads_of(RetriedAfterError(), given)

    assert [lead.symptom for lead in found] == ["retried_after_clear_error"]
    assert found[0].spans == (Span(step=0, message=1, part=0), Span(step=0, message=3, part=0))


async def test_retried_after_error_stays_silent_when_the_repeat_changes_the_arguments() -> None:
    given = transcript(
        question(), call("c1"), result("c1", "no such table", outcome="failed"), call("c2", args={"name": "orders"}), result("c2")
    )

    assert await leads_of(RetriedAfterError(), given) == []


async def test_denied_action_repeated_pairs_the_denied_call_with_any_later_repeat() -> None:
    given = transcript(
        question(),
        call("c1"),
        result("c1", "denied", outcome="denied"),
        call("c2", args={"name": "orders"}),
        result("c2"),
        call("c3"),
        result("c3"),
    )

    found = await leads_of(DeniedActionRepeated(), given)

    assert [lead.symptom for lead in found] == ["denied_action_repeated"]
    assert found[0].spans == (Span(step=0, message=1, part=0), Span(step=0, message=5, part=0))


async def test_a_denied_call_never_repeated_is_not_a_lead() -> None:
    given = transcript(
        question(), call("c1"), result("c1", "denied", outcome="denied"), call("c2", args={"name": "orders"}), result("c2"), answer()
    )

    assert await leads_of(DeniedActionRepeated(), given) == []


async def test_duplicate_call_pairs_a_repeat_of_a_successful_call() -> None:
    given = transcript(question(), call("c1"), result("c1"), call("c2"), result("c2"), answer())

    found = await leads_of(DuplicateCall(world_changing=frozenset()), given)

    assert [(lead.symptom, lead.spans) for lead in found] == [
        ("duplicate_call", (Span(step=0, message=1, part=0), Span(step=0, message=3, part=0)))
    ]


async def test_duplicate_call_stays_silent_across_a_world_changing_call() -> None:
    given = transcript(
        question(),
        call("c1"),
        result("c1"),
        call("w", tool_name="write_note", args={"path": "a.md"}),
        result("w", "written", tool_name="write_note"),
        call("c2"),
        result("c2"),
    )

    assert await leads_of(DuplicateCall(world_changing=frozenset({"write_note"})), given) == []


async def test_duplicate_call_stays_silent_across_a_compaction() -> None:
    compaction = ModelResponse(parts=[CompactionPart(content="earlier turns summarised")], timestamp=AT)
    given = transcript(question(), call("c1"), result("c1"), compaction, call("c2"), result("c2"))

    assert await leads_of(DuplicateCall(world_changing=frozenset()), given) == []


def test_the_registry_builds_every_rule_of_this_module_from_the_facts() -> None:
    facts = ToolFacts(world_changing=frozenset({"write_note"}))

    built = {
        name: RULES.select(name)(facts)
        for name in ("schema_rejected_arguments", "retried_after_clear_error", "denied_action_repeated", "duplicate_call")
    }

    assert isinstance(built["duplicate_call"], DuplicateCall)
    assert built["duplicate_call"].world_changing == frozenset({"write_note"})
