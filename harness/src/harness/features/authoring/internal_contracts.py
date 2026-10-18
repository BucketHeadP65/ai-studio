"""The authoring slice's own ports: one turn in flight, the chat behind the routes, and what its tools call out through."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence

    from core.ai.history.contracts import StepUsage
    from core.ai.workflows.contracts import Workflow, WorkflowArguments
    from pydantic import JsonValue
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.ui import NativeEvent

    from harness.domain.contracts import PageContext
    from harness.features.authoring.domain import SourceRun, ToolResultIn
    from harness.session import Runtime


class TurnEvents(Protocol):
    """One turn's events, and the close that ends the run behind them.

    A turn holds its session for as long as it streams. A reader that stops early must close the
    events. The close records how the turn ended and gives the session back.
    """

    def __aiter__(self) -> AsyncIterator[NativeEvent]:
        """Iterate the turn's events.

        Returns:
            The events, in the order the run produces them.
        """
        ...

    async def __anext__(self) -> NativeEvent:
        """The next event of the run.

        Returns:
            The event.

        Raises:
            StopAsyncIteration: The run is over.
        """
        ...

    async def aclose(self) -> None:
        """End the turn now, whether or not its events are exhausted."""
        ...


@dataclass(frozen=True)
class Turn:
    """One turn in flight: its events, the step readings they leave, and what it settled."""

    events: TurnEvents
    """The turn's events. Closing them ends the run."""
    usage: list[StepUsage]
    """The step readings, filled by the turn's own model tap as each model response finishes."""
    settle: Callable[[], Awaitable[Mapping[str, JsonValue] | None]]
    """What the turn settled, as the payload the panel reads.

    Call it once the run's events are exhausted. It answers `None` for a turn that ended waiting on
    the browser, which has no reply to settle."""


class EndedRuns(Protocol):
    """The ended runs an adaptation may cite. It only reads.

    It answers one run's identity and what that run was observed to do, and nothing else the
    archive holds.
    """

    def find(self, run_id: str) -> SourceRun | None:
        """The run `run_id` names.

        Args:
            run_id: The run an adaptation cites.

        Returns:
            The run, or `None` when nothing here archived one under it. A caller refuses the citation on `None`.
        """
        ...


class Chat(Protocol):
    """What the chat controller needs of the service behind it."""

    async def send_message(
        self,
        session_id: str,
        messages: Sequence[ModelMessage],
        *,
        context: PageContext | None = None,
        tool_results: Sequence[ToolResultIn] = (),
        design_only: bool = False,
    ) -> Turn:
        """Run the agent on the caller's new messages, with what the session was already holding.

        What came before `messages` is read from the session's live conversation. It is read from
        the record only when that session is holding nothing, which is a session this process
        rebuilt rather than ran. A turn carrying `tool_results` resumes the stopped run against the
        live conversation and never reads the record.

        Args:
            session_id: The session the turn runs in.
            messages: This request's own messages only.
            context: The page the person is looking at, folded into the prompt.
            tool_results: The browser's answers to the calls the previous turn stopped for.
            design_only: Whether this turn asks for a design and nothing else. The agent is then
                offered no verb that starts a run or changes a standing schedule. It only narrows,
                so a caller that passes nothing gets the ordinary surface.

        Returns:
            The turn in flight.

        Raises:
            SessionUnknownError: No session has that id.
            SessionBusyError: A turn is already in flight in that session.
            PartAnsweredError: The request answers only some of the calls the previous turn stopped for.
            TurnEmptyError: The request says nothing and answers nothing.
        """
        ...


class LaunchWorkflow(Protocol):
    """The launch path `digest_now` starts a workflow through, in the shape of `running.start`."""

    async def __call__(self, session: Runtime, *, spec: Workflow, workflow_id: str, params: WorkflowArguments, runner: str) -> None:
        """Start `spec` as `workflow_id` in `session`, in the background, logged like any other run.

        Args:
            session: The session the run belongs to.
            spec: The workflow to run.
            workflow_id: The workflow's id.
            params: The workflow's arguments.
            runner: The launcher the run's evidence record and the archive name.
        """
        ...


class PermissionAsker(Protocol):
    """How the external-agent plugin asks a person, through a gate asker, to allow a call.

    A gate asker's own `ask` satisfies it, so a person decides an external agent's permission
    request exactly as they decide a gate.
    """

    async def __call__(self, gate_id: str, rendered: str, /) -> bool:
        """Show `rendered` under `gate_id` and wait for the person's answer.

        Args:
            gate_id: The gate the request is shown under.
            rendered: The request, rendered for a person.

        Returns:
            True to allow the call, False to refuse it.
        """
        ...


class KnowledgeBaseRefresher(Protocol):
    """What `digest_now` needs of the studio: a source queued on a knowledge base, and its refresh run."""

    async def queue_source(self, knowledge_base: str, url: str) -> str | None:
        """Put `url` on `knowledge_base`'s source queue.

        Args:
            knowledge_base: The knowledge base's registered name.
            url: The source to request.

        Returns:
            `None` once the source is queued, else the refusal in words the model reads.
        """
        ...

    async def sync_and_index(self, knowledge_base: str) -> str | None:
        """Run `knowledge_base`'s refresh, which syncs and then indexes it, and wait for the refresh to land.

        A refresh already underway is waited on in place of a new one.

        Args:
            knowledge_base: The knowledge base's registered name.

        Returns:
            `None` once the refresh lands done, else the refusal in words the model reads.
        """
        ...

    async def digest_workflow(self, knowledge_base: str) -> str:
        """The name of the workflow `knowledge_base`'s schema gives under `sources.digest_workflow`.

        Args:
            knowledge_base: The knowledge base's registered name.

        Returns:
            The workflow name, empty when the schema names none.

        Raises:
            DigestWorkflowUnreadableError: When the studio cannot serve the schema.
        """
        ...
