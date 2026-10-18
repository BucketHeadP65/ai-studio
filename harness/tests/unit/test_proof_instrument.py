"""One instrument set for both paths — the fairness rules, executable.

Extraction reads results only (what came back is what was surfaced); the guard refuses a
call that targets a withheld page before it happens; the scrub drops withheld lines from
listings. Both paths share these objects, so neither can get private accounting.
"""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from proof.instrument import REFUSAL, RecordingModel, ToolCall, ToolTrace, guard_args, scrub_result, surfaced_ids

WITHHELD = frozenset({"q.when-to-skip-retrieval", "q.llm-judged-eval-validity"})


def test_surfaced_ids_reads_results_and_folds_sections() -> None:
    trace = ToolTrace(calls=[
        ToolCall(tool="search", args='{"query": "memory"}', result='[{"id": "paper.mem-2024#intro"}, {"id": "ent.rag"}]'),
        ToolCall(tool="run_sql", args="{}", result="paper.skills-2023 | 0.91"),
    ])
    assert surfaced_ids(trace) == frozenset({"paper.mem-2024", "ent.rag", "paper.skills-2023"})


def test_surfaced_ids_ignores_arguments() -> None:
    trace = ToolTrace(calls=[ToolCall(tool="fetch", args='{"id": "paper.asked-for"}', result="nothing found")])
    assert surfaced_ids(trace) == frozenset()


def test_guard_refuses_a_call_targeting_a_withheld_page() -> None:
    assert guard_args('{"id": "q.when-to-skip-retrieval"}', WITHHELD) == REFUSAL
    assert guard_args('{"query": "SELECT * FROM api.cards"}', WITHHELD) is None


def test_scrub_drops_withheld_lines_and_reports_it() -> None:
    text = "paper.a | fine\nq.llm-judged-eval-validity | the answer sheet\npaper.b | fine"
    scrubbed, removed = scrub_result(text, WITHHELD)
    assert removed
    assert "q.llm-judged" not in scrubbed
    assert "paper.a" in scrubbed
    assert "paper.b" in scrubbed


def test_scrub_of_clean_text_changes_nothing() -> None:
    scrubbed, removed = scrub_result("paper.a | fine", WITHHELD)
    assert scrubbed == "paper.a | fine"
    assert not removed


async def test_recording_model_counts_requests_and_tokens() -> None:
    recording = RecordingModel(TestModel())
    agent: Agent[None, str] = Agent(recording, output_type=str)
    await agent.run("hello")
    assert recording.requests >= 1
    assert recording.output_tokens >= 0  # TestModel reports usage; exact numbers are its business
