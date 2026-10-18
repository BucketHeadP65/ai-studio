"""Path A: a fresh agent per question, no memory of any prior run, over the guarded knowledge_base.

This is the fairness baseline the reuse path (Path B) is measured against: same knowledge base, same
guard, same withheld question pages, same `RecordingModel` meter -- the only thing different
is that this path starts from nothing every time. `BASELINE_BUDGET` is deliberately generous
(twice the artifact's own step budget) because the point of this path is what a from-scratch
agent needs to reconstruct an answer, not how cheaply it can be made to stop early; a run that
still fails inside that budget failed on its own terms, not on a stingy cap.
"""

from __future__ import annotations

import asyncio
import time
import traceback

from pathlib import Path
from typing import TYPE_CHECKING

from core.observability import get_logger
from core.settings import read_infrastructure
from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

from harness.adapters.contracts import KnowledgeBaseReading, knowledge_base_capability, model, studio_http_client
from harness.domain.contracts import DEFINED_STEP_BUDGET, NOT_FOR_STEPS
from harness.settings import HarnessSettings, settings_file
from proof.groundtruth import WITHHELD, load_questions, unresolved
from proof.instrument import RecordingModel, ToolCall, ToolTrace, surfaced_ids
from proof.knowledge_base import GuardedKnowledgeBase
from proof.records import RunRecord, build_record, write_record

if TYPE_CHECKING:
    from proof.groundtruth import ResearchQuestion

_logger = get_logger(__name__)

BASELINE_BUDGET = 2 * DEFINED_STEP_BUDGET
"""Generous cap -- runaway insurance, never the claim. The report states it and verifies it
was at least the artifact's collective step budget."""

BASELINE_INSTRUCTIONS = (
    "You are answering a contested research question from a knowledge base of papers and "
    "entities. Search the knowledge base, read the primary sources that bear on the fork, and answer "
    "with a short synthesis that cites the pages you relied on by id. Batch your retrieval; "
    "stop when the evidence converges. Do not look for pre-written answers -- question pages "
    "are withheld from this task."
)

OUT = Path(__file__).parent / "out"


async def run_question(question: ResearchQuestion, knowledge_base: GuardedKnowledgeBase, trace: ToolTrace) -> RunRecord:
    """Run one question from scratch through a fresh agent, and score it against groundtruth.

    No retries: a failure is caught, logged, and recorded as a row -- never re-run to green.
    """
    recording = RecordingModel(model(read_infrastructure(settings_file()), HarnessSettings().models))
    reads = tuple(tool.name for tool in await knowledge_base.list_tools() if tool.name not in NOT_FOR_STEPS)
    agent: Agent[None, str] = Agent(
        recording,
        output_type=str,
        toolsets=[knowledge_base.filtered(lambda _ctx, tool: tool.name in reads)],
        instructions=BASELINE_INSTRUCTIONS,
    )
    started, failed, answer, failure = time.monotonic(), False, "", None
    try:
        result = await agent.run(question.question, usage_limits=UsageLimits(request_limit=BASELINE_BUDGET))
        answer = result.output
    except Exception as exc:  # a failed run is a row, never a retry-to-green
        _logger.exception("baseline run of {question} failed", question=question.id)
        traceback.print_exc()  # logfire may not be configured; the sweep must never fail silently
        failed, failure = True, f"{type(exc).__name__}: {exc}"
    return build_record(
        question, path="baseline", trace=trace, model=recording, seconds=time.monotonic() - started, failed=failed, answer=answer, failure=failure
    )


async def _known_ids(knowledge_base: GuardedKnowledgeBase, cited: frozenset[str]) -> frozenset[str]:
    """Which of the cited ids the live knowledge base actually has, read through the one guarded SQL door.

    Asked about the cited set directly, in chunks, because `run_sql` caps its result at 50 rows
    ("narrow your filters") -- enumerating the whole knowledge base through that window silently returned
    an alphabetical prefix and every score downstream would have deflated. Checked against
    `groundtruth.unresolved` before any run so a renamed or missing page fails loudly here.
    """
    found: set[str] = set()
    ordered = sorted(cited)
    for start in range(0, len(ordered), 40):
        quoted = ", ".join(f"'{page}'" for page in ordered[start : start + 40])
        result = await knowledge_base.direct_call_tool("run_sql", {"sql": f"SELECT id FROM api.cards WHERE id IN ({quoted})"})
        found |= surfaced_ids(ToolTrace(calls=[ToolCall(tool="run_sql", args="{}", result=str(result))]))
    return frozenset(found)


async def main() -> None:
    """Run every question in the knowledge base's groundtruth set, from scratch, and write its record."""
    questions = load_questions()
    cited = frozenset().union(*(question.sources for question in questions))
    # The knowledge base in-process rather than over `engram mcp`: same two tools, same guard around them.
    # `GuardedKnowledgeBase` wraps a `KnowledgeBaseSurface`, and engram's native one satisfies it unchanged. The
    # studio answers which knowledge bases the door speaks for, so it must be up for this sweep.
    settings = HarnessSettings()
    async with (
        studio_http_client(settings.studio_api) as studio,
        knowledge_base_capability(studio, reading=KnowledgeBaseReading(guidance=settings.knowledge_base_guidance.guidance)) as engram,
        GuardedKnowledgeBase(inner=engram, trace=ToolTrace(), withheld=frozenset(WITHHELD)) as knowledge_base,
    ):
        gaps = unresolved(questions, await _known_ids(knowledge_base, cited))
        if gaps:
            msg = f"groundtruth cites ids the live knowledge base does not have: {dict(gaps)}"
            raise RuntimeError(msg)
        # a fresh trace per question: swap knowledge_base.trace before each run so records never bleed
        for question in questions:
            knowledge_base.trace = ToolTrace()
            record = await run_question(question, knowledge_base, knowledge_base.trace)
            print(write_record(record, OUT), record.recall, record.requests)


if __name__ == "__main__":
    asyncio.run(main())
