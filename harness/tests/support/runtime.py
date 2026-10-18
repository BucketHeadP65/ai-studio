"""One session for a test that needs a session and not an agent: an empty registry, a library that holds nothing."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict, Unpack

from core.ai.workflows.contracts import StepRegistry
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from harness.session import Runtime
from tests.support.state import a_log_under, a_throwaway_state

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from core.ai.workflows.contracts import (
        Lineage,
        SavedWorkflow,
        Workflow,
        WorkflowFingerprint,
        WorkflowIntent,
        WorkflowLibrary,
        WorkflowPresentation,
    )
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo
    from pydantic_ai.tools import DeferredToolRequests

    from harness.domain.ports import SessionEvents, SessionTranscript
    from harness.settings import StateSettings
    from harness.workspace import Workspace


class EmptyLibrary:
    """A workflow library holding nothing: the six verbs, answering as an empty one does."""

    def __init__(self) -> None:
        self.saved: list[Workflow] = []

    async def search(self, task: str, *, limit: int = 5) -> Sequence[SavedWorkflow]:
        """Nothing to propose."""
        del task, limit
        return ()

    async def fetch_workflow(self, workflow_id: str) -> Workflow:
        """Nothing is kept under any id.

        Raises:
            KeyError: always, since this library holds no spec.
        """
        raise KeyError(workflow_id)

    async def put(
        self,
        workflow: Workflow,
        *,
        title: str,
        summary: str,
        applicability: str,
        provenance: Mapping[str, str],
        fingerprint: WorkflowFingerprint | None = None,
        lineage: Lineage | None = None,
        presentation: WorkflowPresentation | None = None,
        intent: WorkflowIntent | None = None,
    ) -> str:
        """Keep the spec and answer the id it was kept under."""
        del title, summary, applicability, provenance, fingerprint, lineage, presentation, intent
        self.saved.append(workflow)
        return f"{workflow.name}.v{workflow.version}"

    async def redescribe(
        self, workflow_id: str, *, title: str, summary: str, applicability: str, presentation: WorkflowPresentation | None = None
    ) -> None:
        """Nothing to rewrite."""
        del workflow_id, title, summary, applicability, presentation

    async def set_archived(self, workflow_id: str, *, archived: bool) -> None:
        """Nothing to archive."""
        del workflow_id, archived

    async def archived(self, *, limit: int = 50) -> Sequence[SavedWorkflow]:
        """Nothing has been archived."""
        del limit
        return ()

    async def fetch_fingerprint(self, workflow_id: str) -> WorkflowFingerprint | None:
        """No stamp was ever kept, so staleness is unknown."""
        del workflow_id
        return None

    async def fetch_lineage(self, workflow_id: str) -> Lineage | None:
        """Nothing here was adapted from anything."""
        del workflow_id
        return None

    async def fetch_card(self, workflow_id: str) -> SavedWorkflow | None:
        """No card is kept for any id."""
        del workflow_id
        return None

    async def locate(self, name: str, version: int) -> str | None:
        """This library mints no ids, so it can address no version."""
        del name, version
        return None

    async def current(self, workflow_id: str) -> str:
        """The id itself: this library can never name a newer version."""
        return workflow_id

    async def catalog(self, *, limit: int = 50) -> Sequence[SavedWorkflow]:
        """Nothing to list."""
        del limit
        return ()

    async def catalog_size(self) -> int:
        """Nothing is held."""
        return 0


class _RuntimeOverrides(TypedDict, total=False):
    """The fields of a session a test hands in instead of the default this module builds."""

    registry: StepRegistry
    library: WorkflowLibrary
    agent: Agent[Runtime, str | DeferredToolRequests]
    session_id: str | None
    session_kind: str
    session_log: SessionEvents
    store: SessionTranscript
    state: StateSettings
    workspace: Workspace | None


def a_silent_agent() -> Agent[Runtime, str]:
    """An agent that answers the same short text to anything, so no test reaches a model."""

    def answer(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return ModelResponse(parts=[TextPart("ok")])

    return Agent(FunctionModel(function=answer), deps_type=Runtime)


def a_runtime(**overrides: Unpack[_RuntimeOverrides]) -> Runtime:
    """One session over an empty registry, an empty library and an agent that reaches no model.

    Its state home is a throwaway directory of its own unless the caller names one, so a test that
    forgets to point a session somewhere still cannot write into this checkout's real `.state/`.
    Its session log is a real one under that same home, so a session it mints is one a listing can
    read back.

    Args:
        **overrides: Any field of the session this test cares about, each winning over the default.

    Returns:
        A session a route can resolve, hydrate and record against.
    """
    state = overrides.get("state") or a_throwaway_state()
    built: _RuntimeOverrides = {
        "registry": StepRegistry(),
        "library": EmptyLibrary(),
        "agent": a_silent_agent(),
        "state": state,
        "session_log": a_log_under(state),
    }
    built.update(overrides)
    return Runtime(**built)
