"""One rules pass: the named rules, built from the facts, over the sources a loader opens."""

import pytest

from core.ai.inspection.detectors import Rules
from core.ai.inspection.domain.facts import ToolFacts
from core.ai.inspection.rules import RULES
from core.ai.inspection.rules_pass import RulesPass
from core.ai.transcripts.contracts import TranscriptLoaders
from tests.support.fakes.inspection import Opens, RuleThatRaises, call, question, result, retried, transcript


def two_runs() -> TranscriptLoaders:
    refused = transcript(question(), call("c1"), retried("c1"), source="test:refused")
    clean = transcript(question(), call("c1"), result("c1"), source="test:clean")
    return TranscriptLoaders(by_key={"test": Opens(refused=refused, clean=clean)})


async def test_leads_come_from_every_case_and_the_report_counts_them() -> None:
    passed = RulesPass(name="unit", rules=["schema_rejected_arguments", "duplicate_call"], facts=ToolFacts(), loaders=two_runs())

    outcome = await passed.run(["test:refused", "test:clean"])

    assert [(lead.source, lead.symptom) for lead in outcome.leads] == [("test:refused", "schema_rejected_arguments")]
    assert set(outcome.inspected) == {"test:refused", "test:clean"}
    by_name = {case.name: case for case in outcome.report.cases}
    assert by_name["test:refused"].scores["schema_rejected_arguments"].value == 1
    assert by_name["test:clean"].scores["schema_rejected_arguments"].value == 0


async def test_a_source_the_loader_cannot_open_is_reported_and_the_rest_still_run() -> None:
    passed = RulesPass(name="unit", rules=["schema_rejected_arguments"], facts=ToolFacts(), loaders=two_runs())

    outcome = await passed.run(["test:gone", "test:refused"])

    assert set(outcome.inspected) == {"test:refused"}
    assert len(outcome.leads) == 1
    assert [failed.name for failed in outcome.report.failures] == ["test:gone"]


def test_an_unknown_rule_is_refused_at_construction() -> None:
    with pytest.raises(KeyError):
        RulesPass(name="unit", rules=["no_such_rule"], facts=ToolFacts(), loaders=two_runs())


async def test_a_rule_that_raises_is_reported_not_swallowed() -> None:
    registry = Rules(by_key={**RULES.by_key, "breaks": lambda _facts: RuleThatRaises()})
    passed = RulesPass(name="unit", rules=["breaks", "schema_rejected_arguments"], facts=ToolFacts(), loaders=two_runs(), registry=registry)

    outcome = await passed.run(["test:refused"])

    assert [(failure.source, failure.rule) for failure in outcome.rule_failures] == [("test:refused", "RuleThatRaises")]
    assert "boom" in outcome.rule_failures[0].message
    assert [found.symptom for found in outcome.leads] == ["schema_rejected_arguments"]
