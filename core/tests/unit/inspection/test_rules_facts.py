"""The rules that need a declared fact, and the one that reads how the run ended."""

from pydantic_ai.messages import CompactionPart, ModelResponse, ToolCallPart

from core.ai.inspection.domain.facts import ToolFacts
from core.ai.inspection.rules import RULES, BudgetExhaustedUnfinished, ResultOverflowedContext
from core.ai.transcripts.contracts import Span, Step, Transcript
from tests.support.fakes.inspection import AT, answer, call, leads_of, question, result, transcript


async def test_result_overflowed_context_needs_the_cap_and_a_compaction_after() -> None:
    huge = "x" * 500
    compaction = ModelResponse(parts=[CompactionPart(content="summary")], timestamp=AT)
    given = transcript(question(), call("c1"), result("c1", huge), compaction, answer())

    found = await leads_of(ResultOverflowedContext(cap_chars=100), given)

    assert [lead.spans for lead in found] == [(Span(step=0, message=2, part=0),)]
    assert await leads_of(ResultOverflowedContext(cap_chars=None), given) == []
    assert await leads_of(ResultOverflowedContext(cap_chars=100), transcript(question(), call("c1"), result("c1", huge), answer())) == []


async def test_result_overflowed_context_sees_a_step_that_shrank() -> None:
    huge = "x" * 500
    first = Step(messages=(question(), call("c1"), result("c1", huge)), ordinals=(0, 1, 2))
    second = Step(messages=(question(), answer()), ordinals=(0, 3))
    given = Transcript(source="test:run", steps=(first, second))

    found = await leads_of(ResultOverflowedContext(cap_chars=100), given)

    assert [lead.spans for lead in found] == [(Span(step=0, message=2, part=0),)]


async def test_result_overflowed_context_ignores_a_step_that_shrank_before_the_return() -> None:
    huge = "x" * 500
    wide = Step(messages=(question(), call("c1"), result("c1")), ordinals=(0, 1, 2))
    narrow = Step(messages=(question(), answer()), ordinals=(0, 3))
    oversized = Step(messages=(question(), answer(), call("c2"), result("c2", huge)), ordinals=(0, 3, 4, 5))
    later = Step(messages=(question(), answer()), ordinals=(0, 6))

    before_only = Transcript(source="test:run", steps=(wide, narrow, oversized))
    then_a_shrink = Transcript(source="test:run", steps=(wide, narrow, oversized, later))

    assert await leads_of(ResultOverflowedContext(cap_chars=100), before_only) == []
    found = await leads_of(ResultOverflowedContext(cap_chars=100), then_a_shrink)
    assert [lead.spans for lead in found] == [(Span(step=2, message=3, part=0),)]


async def test_budget_exhausted_unfinished_reads_the_last_response() -> None:
    cut = ModelResponse(
        parts=[ToolCallPart(tool_name="lookup_city", args={"name": "x"}, tool_call_id="c9")], timestamp=AT, finish_reason="length"
    )
    given = transcript(question(), cut)

    found = await leads_of(BudgetExhaustedUnfinished(), given)

    assert [lead.spans for lead in found] == [(Span(step=0, message=1, part=0),)]
    assert await leads_of(BudgetExhaustedUnfinished(), transcript(question(), answer(finish_reason="length"))) == []
    assert await leads_of(BudgetExhaustedUnfinished(), transcript(question(), answer())) == []


async def test_budget_exhausted_unfinished_reads_past_a_return_folded_in_after_the_cut() -> None:
    cut = ModelResponse(
        parts=[ToolCallPart(tool_name="lookup_city", args={"name": "x"}, tool_call_id="c9")], timestamp=AT, finish_reason="length"
    )
    given = transcript(question(), cut, result("c9"))

    found = await leads_of(BudgetExhaustedUnfinished(), given)

    assert [lead.spans for lead in found] == [(Span(step=0, message=1, part=0),)]


def test_the_registry_builds_the_fact_rules_from_the_facts() -> None:
    facts = ToolFacts(result_cap_chars=8_000)

    overflow = RULES.select("result_overflowed_context")(facts)

    assert isinstance(overflow, ResultOverflowedContext)
    assert overflow.cap_chars == 8_000
    assert isinstance(RULES.select("budget_exhausted_unfinished")(facts), BudgetExhaustedUnfinished)


def test_the_registry_holds_the_six_rules_this_deployment_ships() -> None:
    assert set(RULES.by_key) == {
        "schema_rejected_arguments",
        "retried_after_clear_error",
        "denied_action_repeated",
        "duplicate_call",
        "result_overflowed_context",
        "budget_exhausted_unfinished",
    }
