"""InspectorAnswerer: the collaborator a follow-up route hands off to, over pydantic-ai's own `FunctionModel`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from core.ai.inspection.contracts import StoredQuestion
from core.ai.transcripts.contracts import Step, Transcript, TranscriptLoaders
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai_harness.step_persistence import InMemoryStepStore

from harness.adapters.model_list import ModelList
from harness.adapters.model_tiers import ModelTiers, TierSetting, set_tier
from harness.features.inspection.service import InspectorAnswerer
from tests.support.fakes.history import FakeConversationHistory
from tests.support.fakes.inspection import FakeRunsOfConversation
from tests.support.fakes.model_listing import FakeModelListing
from tests.support.fakes.request_parameters import FakeRequestParametersSink
from tests.support.state import a_state

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.workflows.contracts import Tier
    from core.thinking import Thinking
    from pydantic_ai.models import Model

AT = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
A_CONVERSATION = UUID("55555555-5555-4555-8555-555555555555")
SOURCE = "record:run-1"


def a_transcript() -> Transcript:
    """One step: the prompt, a call, its return, the answer."""
    messages = [
        ModelRequest(parts=[UserPromptPart(content="index the notes", timestamp=AT)]),
        ModelResponse(parts=[ToolCallPart(tool_name="write_note", args={"path": "a.md"}, tool_call_id="c1")], timestamp=AT),
        ModelRequest(parts=[ToolReturnPart(tool_name="write_note", content="the page already exists", tool_call_id="c1", timestamp=AT)]),
        ModelResponse(parts=[TextPart(content="done")], timestamp=AT),
    ]
    return Transcript(source=SOURCE, steps=(Step(messages=tuple(messages), ordinals=tuple(range(len(messages)))),), durations_ms={})


class OpensTheRun:
    """A loader that answers this test's one transcript, and nothing else."""

    async def __call__(self, locator: str) -> Transcript:
        if locator != "run-1":
            msg = f"no run {locator!r}"
            raise LookupError(msg)
        return a_transcript()


def a_question(*, tier: Tier = "worker") -> StoredQuestion:
    return StoredQuestion(
        question_id=1,
        conversation_id=A_CONVERSATION,
        kind="attribute",
        source=SOURCE,
        watch="every-run",
        lead_id=1,
        tier=tier,
        model="claude-test",
        asked_at=AT,
        answer="lead 1: finding 1 by claude-test",
    )


class SaysWhatItWasHanded:
    """A model function that answers one line and keeps every message and conversation id it was handed."""

    def __init__(self, said: str) -> None:
        self.said = said
        self.seen: list[ModelMessage] = []
        self.conversations: list[str | None] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        self.seen = list(messages)
        self.conversations.append(messages[-1].conversation_id)
        return ModelResponse(parts=[TextPart(content=self.said)])


def tiers_asking_for(home: Path, *, tier: Tier, model_id: str, said: SaysWhatItWasHanded) -> ModelTiers:
    """Tiers under `home`, `tier` set to `model_id`, and no other id ever built.

    The `build` seam asserts the id it is handed, so a follow-up that runs on any tier but the question's own
    fails here rather than only differing in the answer.
    """

    def build(built_id: str, /, *, thinking: Thinking) -> Model:
        del thinking
        assert built_id == model_id, f"asked to build {built_id!r}, not the question's own tier's model {model_id!r}"
        return FunctionModel(said, model_name=model_id)

    tiers = ModelTiers(state=a_state(home), models=ModelList(listing=FakeModelListing()), build=build)
    set_tier(tiers, tier, TierSetting(model=model_id, thinking="high"))
    return tiers


async def test_the_follow_up_runs_on_the_questions_own_tier_and_names_who_answered(tmp_path: Path) -> None:
    said = SaysWhatItWasHanded("Because the description promised a check the tool never made.")
    tiers = tiers_asking_for(tmp_path, tier="worker", model_id="gpt-5.4", said=said)
    history = FakeConversationHistory(
        {str(A_CONVERSATION): [ModelResponse(parts=[TextPart(content="I blamed the tool description.")], timestamp=AT)]}
    )
    transcript = InMemoryStepStore()
    asking = InspectorAnswerer(
        tiers=tiers,
        conversation=FakeRunsOfConversation(),
        transcript=transcript,
        parameters=FakeRequestParametersSink(),
        history=history,
        loaders=TranscriptLoaders(by_key={"record": OpensTheRun()}),
    )
    asked = a_question(tier="worker")

    answered = await asking.answer(asked, "why did you blame the tool description and not the instructions?")

    assert answered.answer == "Because the description promised a check the tool never made."
    assert answered.conversation_id == A_CONVERSATION
    assert answered.model == "gpt-5.4"
    assert said.conversations == [str(A_CONVERSATION)], "the new request carries the question's own conversation id"
    assert history.asked == [str(A_CONVERSATION)], "the follow-up starts from what that conversation last put in front of the model"
    texts = [part.content for message in said.seen for part in message.parts if isinstance(part, TextPart)]
    assert "I blamed the tool description." in texts

    recorded = await transcript.list_runs(conversation_id=str(A_CONVERSATION))
    assert len(recorded) == 1, "the exchange is recorded under the question's own conversation"
    assert recorded[0].agent_name == "inspector"
    assert recorded[0].parent_run_id is None
