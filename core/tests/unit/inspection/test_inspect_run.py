"""The InspectRun handler: one run in, leads stored, single-cause findings written, the rest queued for the inspector."""

from dataclasses import dataclass, replace

import pytest

from pydantic_evals.evaluators import Evaluator

from core.ai.inspection.detectors import Inspected, InspectionContext, Rules, record_lead
from core.ai.inspection.domain.facts import ToolFacts
from core.ai.inspection.domain.leads import Lead
from core.ai.inspection.domain.taxonomy import TAXONOMY, Blame, Finding, RootCause, Surface, SurfaceKind, Symptom, Taxonomy
from core.ai.inspection.domain.watches import Reach, Watch
from core.ai.inspection.inspect_run import InspectRun, InspectRunHandler
from core.ai.inspection.read_models import StoredBlame
from core.ai.inspection.rules import RULES
from core.ai.transcripts.contracts import RunEmptyError, SchemeUnknownError, Span, Transcript, TranscriptLoaders
from core.cqrs.contracts import JobContext
from tests.support.fakes.inspection import (
    FakeLeadQueue,
    FakeLeadsAndFindings,
    Opens,
    RuleThatRaises,
    answer,
    call,
    question,
    result,
    retried,
    transcript,
)

CONTEXT = JobContext(job_id=1, kind="inspect_run", key="author:test:run", attempt=1)


HAND_WRITTEN = RootCause(
    name="hand_written_page", meaning="The agent wrote by hand what no tool could make.", fix_lands_on=(SurfaceKind.TOOL,)
)
IMPROVISED = Symptom(
    name="improvised_page",
    shows_in="tool-call",
    what_you_see="The agent wrote the page itself rather than calling a tool that makes one.",
    caught_by="rule",
    horizon="moment",
    possible_root_causes=(HAND_WRITTEN,),
)
A_TAXONOMY = Taxonomy(root_causes=(*TAXONOMY.root_causes, HAND_WRITTEN), symptoms=(*TAXONOMY.symptoms, IMPROVISED))


@dataclass
class ImprovisedPage(Evaluator[str, Inspected, None]):
    """A test rule that leads at the first call for whichever symptom it was built with."""

    symptom: str = IMPROVISED.name

    def evaluate(self, ctx: InspectionContext) -> dict[str, int]:
        record_lead(ctx, A_TAXONOMY.symptom(self.symptom), [Span(step=0, message=1, part=0)], found_by="improvised_page")
        return {self.symptom: 1}


def a_handler(
    *, inspector: Reach = "every_run", symptom: str = IMPROVISED.name
) -> tuple[InspectRunHandler, FakeLeadsAndFindings, FakeLeadQueue]:
    store = FakeLeadsAndFindings()
    queue = FakeLeadQueue()
    refused_twice = transcript(question(), call("c1"), retried("c1"), call("c2"), retried("c2"), source="test:run")
    loaders = TranscriptLoaders(by_key={"test": Opens(run=refused_twice)})
    registry = Rules(by_key={**RULES.by_key, "improvised_page": lambda _facts: ImprovisedPage(symptom=symptom)})
    rules = ("schema_rejected_arguments", "retried_after_clear_error", "improvised_page")
    watches = {"author": Watch(name="author", agent="harness-author", rules=rules, inspector=inspector)}
    handler = InspectRunHandler(
        watches=watches, facts=ToolFacts(), loaders=loaders, store=store, queue=queue, registry=registry, taxonomy=A_TAXONOMY
    )
    return handler, store, queue


async def test_leads_are_stored_single_cause_findings_written_and_the_rest_queued() -> None:
    handler, store, queue = a_handler()

    answer = await handler(InspectRun(source="test:run", watch="author"), CONTEXT)

    symptoms = sorted(row.symptom for row in store.leads)
    assert symptoms == ["improvised_page", "retried_after_clear_error", "schema_rejected_arguments", "schema_rejected_arguments"]
    (finding,) = store.findings
    assert finding.symptom == "improvised_page"
    assert finding.blames == [
        StoredBlame(root_cause="hand_written_page", surface_kind="tool", surface_name="lookup_city", why=IMPROVISED.what_you_see)
    ]
    assert finding.found_by == "rule"
    assert sorted(queue.queued) == sorted(row.lead_id for row in store.leads if row.symptom != "improvised_page")
    assert answer == "test:run: 4 leads, 1 findings, 3 queued, 0 settled, scan"
    assert queue.scans == [("test:run", "author")]


async def test_an_inspect_run_job_reads_its_transcript_once() -> None:
    handler, _store, _queue = a_handler()
    opens = handler.loaders.by_key["test"]
    assert isinstance(opens, Opens)

    await handler(InspectRun(source="test:run", watch="author"), CONTEXT)

    assert opens.opened == ["run"]


async def test_a_symptom_a_model_must_decide_is_never_decided_by_a_rule() -> None:
    handler, store, queue = a_handler(symptom="no_tool_for_the_job")

    answer = await handler(InspectRun(source="test:run", watch="author"), CONTEXT)

    assert store.findings == []
    assert sorted(queue.queued) == sorted(row.lead_id for row in store.leads)
    assert answer == "test:run: 4 leads, 0 findings, 4 queued, 0 settled, scan"


async def test_a_watch_without_the_inspector_leaves_its_undecided_leads_as_leads() -> None:
    handler, store, queue = a_handler(inspector="never")

    answer = await handler(InspectRun(source="test:run", watch="author"), CONTEXT)

    assert store.leads != []
    assert queue.queued == []
    assert queue.scans == []
    assert answer == "test:run: 4 leads, 1 findings, 0 queued, 0 settled, no scan"


async def test_a_run_the_loader_cannot_open_answers_run_gone_and_stores_nothing() -> None:
    handler, store, queue = a_handler()

    answer = await handler(InspectRun(source="test:gone", watch="author"), CONTEXT)

    assert answer == "test:gone: run gone"
    assert store.leads == []
    assert queue.queued == []


class RaisesRunEmptyError:
    """A loader whose run exists but recorded no step."""

    async def __call__(self, locator: str) -> Transcript:
        msg = f"run {locator!r} recorded no step"
        raise RunEmptyError(msg)


async def test_a_run_that_recorded_no_step_completes_with_no_steps() -> None:
    handler, store, queue = a_handler()
    empty = replace(handler, loaders=TranscriptLoaders(by_key={"test": RaisesRunEmptyError()}))

    answer = await empty(InspectRun(source="test:run", watch="author"), CONTEXT)

    assert answer == "test:run: no steps"
    assert store.leads == []
    assert queue.queued == []


async def test_an_unknown_watch_is_refused() -> None:
    handler, _, _ = a_handler()

    with pytest.raises(KeyError, match="nobody"):
        await handler(InspectRun(source="test:run", watch="nobody"), CONTEXT)


async def test_the_same_run_inspected_twice_stores_each_finding_once() -> None:
    handler, store, _ = a_handler()
    handler = replace(
        handler, watches={"author": Watch(name="author", agent="harness-author", rules=("improvised_page",), inspector="every_run")}
    )
    payload = InspectRun(source="test:run", watch="author")

    first = await handler(payload, CONTEXT)
    first_ids = [row.lead_id for row in store.leads]
    second = await handler(payload, CONTEXT)

    assert len(store.findings) == 1
    assert [row.lead_id for row in store.leads] == first_ids
    assert first == "test:run: 1 leads, 1 findings, 0 queued, 0 settled, scan"
    assert second == f"test:run: {len(first_ids)} leads, 0 findings, 0 queued, {len(first_ids)} settled, scan"


async def test_a_settled_lead_is_never_queued_for_the_inspector() -> None:
    handler, store, queue = a_handler(symptom="no_tool_for_the_job")
    pre = Lead(source="test:run", symptom="no_tool_for_the_job", spans=(Span(step=0, message=1, part=0),), found_by="improvised_page")
    (settled_id,) = await store.add_leads("author", [pre])
    finding = Finding(
        symptom=A_TAXONOMY.symptom("no_tool_for_the_job"),
        evidence=pre.spans,
        blames=(Blame(what=A_TAXONOMY.root_cause("missing_tool"), where=Surface(kind=SurfaceKind.TOOL, name=""), why="already answered"),),
        found_by="inspector",
    )
    await store.add_finding(finding, source="test:run", lead_id=settled_id, found_by="inspector", model="test-model")

    answer = await handler(InspectRun(source="test:run", watch="author"), CONTEXT)

    assert settled_id not in queue.queued
    assert len(store.findings) == 1
    assert answer == "test:run: 4 leads, 0 findings, 3 queued, 1 settled, scan"


class Breaks:
    """A loader whose store is unreachable."""

    async def __call__(self, locator: str) -> Transcript:
        msg = f"the record refused the connection for {locator!r}"
        raise ConnectionError(msg)


async def test_a_loader_that_cannot_reach_its_store_fails_the_job() -> None:
    handler, store, queue = a_handler()
    broken = replace(handler, loaders=TranscriptLoaders(by_key={"test": Breaks()}))

    with pytest.raises(ConnectionError):
        await broken(InspectRun(source="test:run", watch="author"), CONTEXT)

    assert store.leads == []
    assert store.findings == []
    assert queue.queued == []


class Unparseable:
    """A loader whose file does not read as a transcript."""

    async def __call__(self, locator: str) -> Transcript:
        msg = f"line 1 of {locator!r} does not read as a record"
        raise ValueError(msg)


@pytest.mark.parametrize(
    ("source", "error"),
    [("other:run", SchemeUnknownError), ("run", ValueError), ("broken:run", ValueError)],
    ids=["a scheme no loader is registered for", "a source with no scheme", "a transcript that does not parse"],
)
async def test_a_transcript_that_is_neither_gone_nor_empty_fails_the_job(source: str, error: type[Exception]) -> None:
    handler, store, queue = a_handler()
    loaders = TranscriptLoaders(by_key={**handler.loaders.by_key, "broken": Unparseable()})

    with pytest.raises(error):
        await replace(handler, loaders=loaders)(InspectRun(source=source, watch="author"), CONTEXT)

    assert store.leads == []
    assert store.findings == []
    assert queue.queued == []


async def test_a_broken_rule_fails_the_job_before_anything_is_stored() -> None:
    handler, store, queue = a_handler()
    registry = Rules(by_key={**handler.registry.by_key, "improvised_page": lambda _facts: RuleThatRaises()})

    with pytest.raises(RuntimeError, match="rules failed"):
        await replace(handler, registry=registry)(InspectRun(source="test:run", watch="author"), CONTEXT)

    assert store.leads == []
    assert store.findings == []
    assert queue.queued == []


async def test_a_rule_the_registry_does_not_know_is_refused() -> None:
    handler, store, queue = a_handler()
    watches = {"author": Watch(name="author", rules=("schema_rejected_argument",), inspector="every_run")}

    with pytest.raises(KeyError, match="schema_rejected_argument"):
        await replace(handler, watches=watches)(InspectRun(source="test:run", watch="author"), CONTEXT)

    assert store.leads == []
    assert store.findings == []
    assert queue.queued == []


async def test_when_a_rule_led_the_inspector_scans_the_run_and_takes_its_leads() -> None:
    handler, store, queue = a_handler(inspector="when_a_rule_led")

    answer = await handler(InspectRun(source="test:run", watch="author"), CONTEXT)

    assert sorted(queue.queued) == sorted(row.lead_id for row in store.leads if row.symptom != "improvised_page")
    assert answer == "test:run: 4 leads, 1 findings, 3 queued, 0 settled, scan"
    assert queue.scans == [("test:run", "author")]


async def test_when_no_rule_led_the_inspector_does_not_scan() -> None:
    store = FakeLeadsAndFindings()
    queue = FakeLeadQueue()
    clean = transcript(question(), call("c1"), result("c1"), source="test:run")
    loaders = TranscriptLoaders(by_key={"test": Opens(run=clean)})
    watches = {
        "author": Watch(
            name="author",
            agent="harness-author",
            rules=("schema_rejected_arguments", "retried_after_clear_error"),
            inspector="when_a_rule_led",
        )
    }
    handler = InspectRunHandler(watches=watches, facts=ToolFacts(), loaders=loaders, store=store, queue=queue)

    answer = await handler(InspectRun(source="test:run", watch="author"), CONTEXT)

    assert queue.queued == []
    assert queue.scans == []
    assert answer == "test:run: 0 leads, 0 findings, 0 queued, 0 settled, no scan"


async def test_a_run_whose_leads_are_all_settled_is_not_scanned_again() -> None:
    handler, _, queue = a_handler(inspector="when_a_rule_led")
    handler = replace(
        handler, watches={"author": Watch(name="author", agent="harness-author", rules=("improvised_page",), inspector="when_a_rule_led")}
    )
    payload = InspectRun(source="test:run", watch="author")

    await handler(payload, CONTEXT)
    await handler(payload, CONTEXT)

    assert queue.scans == [("test:run", "author")]


async def test_the_terminal_transcript_is_scanned_even_when_earlier_leads_are_settled() -> None:
    handler, _, queue = a_handler(inspector="when_a_rule_led")
    handler = replace(handler, watches={"author": Watch(name="author", rules=("improvised_page",))})

    await handler(InspectRun(source="test:run", watch="author"), CONTEXT)
    await handler(InspectRun(source="test:run", watch="author", terminal_event=7), CONTEXT)

    assert queue.scans == [("test:run", "author"), ("test:run", "author")]
    assert queue.scan_terminals == [None, 7]


async def test_every_run_scans_a_run_that_called_a_tool_even_when_no_rule_led() -> None:
    store = FakeLeadsAndFindings()
    queue = FakeLeadQueue()
    clean = transcript(question(), call("c1"), result("c1"), source="test:run")
    loaders = TranscriptLoaders(by_key={"test": Opens(run=clean)})
    watches = {
        "author": Watch(
            name="author", agent="harness-author", rules=("schema_rejected_arguments", "retried_after_clear_error"), inspector="every_run"
        )
    }
    handler = InspectRunHandler(watches=watches, facts=ToolFacts(), loaders=loaders, store=store, queue=queue)

    answer = await handler(InspectRun(source="test:run", watch="author"), CONTEXT)

    assert queue.scans == [("test:run", "author")]
    assert answer == "test:run: 0 leads, 0 findings, 0 queued, 0 settled, scan"


async def test_every_run_scans_text_only_runs() -> None:
    store = FakeLeadsAndFindings()
    queue = FakeLeadQueue()
    no_tool = transcript(question(), answer(), source="test:run")
    loaders = TranscriptLoaders(by_key={"test": Opens(run=no_tool)})
    watches = {"author": Watch(name="author", agent="harness-author", inspector="every_run")}
    handler = InspectRunHandler(watches=watches, facts=ToolFacts(), loaders=loaders, store=store, queue=queue)

    reply = await handler(InspectRun(source="test:run", watch="author"), CONTEXT)

    assert queue.scans == [("test:run", "author")]
    assert reply == "test:run: 0 leads, 0 findings, 0 queued, 0 settled, scan"


@pytest.mark.parametrize(("sample_rate", "scans"), [(0.0, []), (1.0, [("test:run", "author")])])
async def test_clean_text_only_runs_obey_the_configured_sampling_rate(sample_rate: float, scans: list[tuple[str, str]]) -> None:
    queue = FakeLeadQueue()
    loaders = TranscriptLoaders(by_key={"test": Opens(run=transcript(question(), answer(), source="test:run"))})
    watch = Watch(name="author", rules=("duplicate_call",), clean_run_sample_rate=sample_rate)
    handler = InspectRunHandler(watches={"author": watch}, facts=ToolFacts(), loaders=loaders, store=FakeLeadsAndFindings(), queue=queue)

    await handler(InspectRun(source="test:run", watch="author"), CONTEXT)

    assert queue.scans == scans


async def test_never_leaves_every_lead_to_the_rules() -> None:
    handler, _, queue = a_handler(inspector="never")

    answer = await handler(InspectRun(source="test:run", watch="author"), CONTEXT)

    assert queue.queued == []
    assert queue.scans == []
    assert answer == "test:run: 4 leads, 1 findings, 0 queued, 0 settled, no scan"
