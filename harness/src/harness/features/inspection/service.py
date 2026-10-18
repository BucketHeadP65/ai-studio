"""The use cases of the inspection slice: a follow-up to the inspector, and the commands an admin issues by hand.

An issued command is only queued. The running loop serves it like any other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.ai.history.contracts import recording_capabilities
from core.ai.inspection.contracts import INSPECTOR_AGENT_NAME, InspectorDeps, ask, inspector_chat_agent, model_name
from core.ai.transcripts.contracts import record_source

from harness.adapters.contracts import TierModel
from harness.features.inspection.domain import Answered, Issued

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.ai.history.contracts import RequestParametersSink
    from core.ai.inspection.contracts import InspectRuns, StoredQuestion, Sweep, SweepPass, Watch
    from core.ai.transcripts.contracts import RunsOfConversation, TranscriptLoaders

    from harness.adapters.contracts import ModelTiers
    from harness.domain.contracts import ConversationHistory, SessionTranscript
    from harness.features.inspection.internal_contracts import RunQueries


@dataclass(frozen=True)
class InspectorAnswerer:
    """What one follow-up to the inspector is answered with."""

    tiers: ModelTiers
    """How the tier a question was asked on becomes a model now."""
    conversation: RunsOfConversation
    """The conversation's earlier runs, which `earlier_runs` answers from."""
    transcript: SessionTranscript
    """Where the follow-up's own run is recorded."""
    parameters: RequestParametersSink
    """Where each of its requests' tool definitions and settings are recorded."""
    history: ConversationHistory
    """What that conversation last put in front of the model."""
    loaders: TranscriptLoaders
    """How the question's own run opens again."""

    async def answer(self, asked: StoredQuestion, text: str) -> Answered:
        """Reopen the question's run through `loaders` and ask the inspector's chat agent, recorded under the inspector's agent name.

        Args:
            asked: The question whose conversation continues.
            text: What to ask, in words.

        Returns:
            The inspector's answer, the conversation it is recorded under, and the model that gave it.

        Raises:
            RunEmptyError: when the question's run recorded no step.
            LookupError: when the question's source holds no such run.
            FileNotFoundError: when the file the question's source names does not exist.
            ValueError: when what the question's source names does not read as a transcript.
        """
        transcript = await self.loaders.load(asked.source)
        agent = inspector_chat_agent(
            TierModel(asked.tier, self.tiers), capabilities=recording_capabilities(self.transcript, self.parameters, agent_name=INSPECTOR_AGENT_NAME, deps_type=InspectorDeps)
        )
        deps = InspectorDeps(transcript=transcript, loaders=self.loaders, conversation=self.conversation)
        history = await self.history.history_for_next_run(conversation_id=str(asked.conversation_id))
        said = await ask(agent, text, deps=deps, history=history, conversation_id=asked.conversation_id)
        return Answered(answer=said, conversation_id=asked.conversation_id, model=model_name(agent))


async def issue_for_run(run_id: str, *, runs: RunQueries, watches: Sequence[Watch], lanes: InspectRuns) -> list[Issued] | None:
    """Issue `InspectRun` under every watch that selects the agent run, whatever its `when`.

    Args:
        run_id: The agent run's id in the record.
        runs: The record's agent runs.
        watches: The watches the agent run is checked against.
        lanes: The lanes the commands are issued on.

    Returns:
        One entry per watch that selects the agent run, each the command issued or the one already in flight.
        `None` when the record holds no such agent run.
    """
    found = await runs.run(run_id)
    if found is None:
        return None
    issued: list[Issued] = []
    for watch in watches:
        if watch.selects(agent_name=found.agent_name, conversation_id=str(found.conversation_id)):
            answer = await lanes.inspect_run(record_source(run_id), watch.name)
            issued.append(Issued(watch=watch.name, job_id=answer.job_id, created=answer.created))
    return issued


async def issue_since(hours: int, *, sweep: Sweep) -> list[tuple[str, SweepPass]]:
    """Run every one of the sweep's own watches over the last `hours`.

    Args:
        hours: How far back the window reaches, in hours.
        sweep: The sweep whose watches run.

    Returns:
        One pair per watch: the watch's name, and what it considered and issued.
    """
    return [(watch.name, await sweep.window(watch, hours)) for watch in sweep.watches]
