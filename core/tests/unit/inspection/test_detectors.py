"""The pieces every rule shares: the inspected output, the lead helper, the tool facts, the rule registry."""

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator

from core.ai.inspection.detectors import Inspected, InspectionContext, Rules, record_lead
from core.ai.inspection.domain.facts import ToolFacts
from core.ai.inspection.domain.taxonomy import TAXONOMY
from core.ai.transcripts.contracts import Span, Step, Transcript

AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def a_transcript() -> Transcript:
    question = ModelRequest(parts=[UserPromptPart(content="hello", timestamp=AT)])
    return Transcript(source="test:run", steps=(Step(messages=(question,), ordinals=(0,)),))


@dataclass
class AlwaysSees(Evaluator[str, Inspected, None]):
    """A rule for the test: one lead on the first part of every transcript."""

    def evaluate(self, ctx: InspectionContext) -> dict[str, int]:
        record_lead(ctx, TAXONOMY.symptom("unsupported_claim"), [Span(step=0, message=0, part=0)], found_by="always_sees")
        return {"unsupported_claim": 1}


async def test_a_rule_appends_a_lead_to_the_case_output_and_reports_a_count() -> None:
    async def load(source: str) -> Inspected:
        return Inspected(transcript=a_transcript())

    dataset = Dataset[str, Inspected, None](name="test", cases=[Case(name="test:run", inputs="test:run")], evaluators=[AlwaysSees()])
    report = await dataset.evaluate(load, progress=False)

    (case,) = report.cases
    assert case.output is not None
    assert [found.symptom for found in case.output.leads] == ["unsupported_claim"]
    assert case.output.leads[0].source == "test:run"
    assert case.output.leads[0].found_by == "always_sees"
    assert case.scores["unsupported_claim"].value == 1


def test_tool_facts_default_to_knowing_nothing() -> None:
    facts = ToolFacts()

    assert facts.world_changing == frozenset()
    assert facts.result_cap_chars is None


def test_the_rule_registry_builds_a_rule_from_the_facts_and_refuses_an_unknown_name() -> None:
    seen: list[ToolFacts] = []

    def factory(facts: ToolFacts) -> Evaluator[str, Inspected, None]:
        seen.append(facts)
        return AlwaysSees()

    rules = Rules(by_key={"always_sees": factory})
    built = rules.select("always_sees")(ToolFacts(result_cap_chars=10))

    assert isinstance(built, AlwaysSees)
    assert seen[0].result_cap_chars == 10
    with pytest.raises(KeyError):
        rules.select("never_heard_of")
