"""The ports of the workflow engine: what a host or an adapter supplies for the engine to call.

The library, the evidence log and the knowledge base's reads and writes are storage ports. The rest are the seams a run
calls to perform a step, ask a gate and report progress.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pydantic import JsonValue

    from core.ai.workflows.domain.evidence import Approval, Grade, RunEvidence
    from core.ai.workflows.domain.fingerprint import WorkflowFingerprint
    from core.ai.workflows.domain.intent import WorkflowIntent
    from core.ai.workflows.domain.lineage import Lineage
    from core.ai.workflows.domain.presentation import WorkflowPresentation
    from core.ai.workflows.domain.run_events import ReviewedArtifactRef
    from core.ai.workflows.domain.vocabulary import AgentStep, Briefing, Program, Workflow
    from core.ai.workflows.graph_builder import GateDecision
    from core.ai.workflows.read_models import SavedWorkflow
    from core.ai.workflows.registry import StepImplementation


@runtime_checkable
class WorkflowLibrary(Protocol):
    """What a library does. Six methods are required and the rest are optional.

    **Required:** `search`, `fetch_workflow`, `put`, `redescribe`, `set_archived`, `archived`. A
    library that does those six is a library.

    **Optional:** `fetch_fingerprint`, `fetch_lineage`, `fetch_card`, `locate`, `current`,
    `catalog`, `catalog_size`. Callers reach these through `getattr`, and each has a defined answer
    for *absent*: staleness unknown rather than fresh, an adaptation undescribed, a parent with
    no address, a version that is its own newest, a library that cannot list, a total nobody can
    say. A library that answers them must use these names, but no library has to answer them.
    """

    async def search(self, task: str, *, limit: int = 5) -> Sequence[SavedWorkflow]:
        """Propose saved workflows for a task description, best first.

        An archived workflow is never proposed.

        Args:
            task: The task description to match.
            limit: The most candidates to return.

        Returns:
            The candidates' cards, best first.
        """
        ...

    async def fetch_workflow(self, workflow_id: str) -> Workflow:
        """The workflow `workflow_id` names, ready to run.

        Args:
            workflow_id: The library's id for the workflow.

        Returns:
            The workflow.

        Raises:
            WorkflowUnknownError: If the library has no such workflow. It is a `KeyError` and a
                `LibraryError`, so a caller may catch either.
        """
        ...

    async def fetch_fingerprint(self, workflow_id: str) -> WorkflowFingerprint | None:
        """The environment stamp recorded when the workflow was saved, or None if none was kept.

        `None` means staleness is unknown, never that the workflow is fresh.

        Args:
            workflow_id: The library's id for the workflow.

        Returns:
            The stamp, or None.

        Raises:
            WorkflowUnknownError: If the library has no such workflow.
        """
        ...

    async def fetch_lineage(self, workflow_id: str) -> Lineage | None:
        """Where this workflow came from, or None if nobody adapted anything to make it.

        Args:
            workflow_id: The library's id for the workflow.

        Returns:
            The lineage, or None.

        Raises:
            WorkflowUnknownError: If the library has no such workflow.
        """
        ...

    async def fetch_card(self, workflow_id: str) -> SavedWorkflow | None:
        """One workflow's card (the words a person reads to choose it), or None if the library does not have it.

        It answers for an archived workflow too, with `archived_at` set, so a run's history can
        still show what it ran.

        Args:
            workflow_id: The library's id for the workflow.

        Returns:
            The card, or None.
        """
        ...

    async def locate(self, name: str, version: int) -> str | None:
        """The id one version of a workflow is kept under, or None if this library does not have it.

        **Only the library mints ids.** A caller never builds an id such as `f"{name}@{version}"`
        itself, which would be right for one library and wrong for another.

        Args:
            name: The workflow's name.
            version: The version number.

        Returns:
            The id, or None.
        """
        ...

    async def current(self, workflow_id: str) -> str:
        """The id of the newest live version of the workflow `workflow_id` names, or `workflow_id` itself.

        It answers `workflow_id` itself whenever the library cannot name a newer one: an id it did
        not mint, a name with no live version, or a version that is already the newest. A caller
        that runs whatever workflow an id names must resolve the id here first, so the reference
        follows the workflow rather than one version of it.

        Args:
            workflow_id: The library's id for any version of the workflow.

        Returns:
            The id of the newest live version, or `workflow_id`.
        """
        ...

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
        """Keep a workflow and return its id. The caller must check the workflow with `verify_savable` first.

        A library keeps `presentation` and `intent` only when they are given. Absent is stored as
        absent, so a workflow saved without either carries no trace of them.

        Args:
            workflow: The workflow to keep.
            title: The card's title.
            summary: The card's one-sentence summary.
            applicability: When to use the workflow and when not to.
            provenance: Facts about who saved the workflow, such as `saved_by`.
            fingerprint: The environment stamp `save` derived, kept so retrieval can answer whether
                the environment moved under this workflow.
            lineage: The diff that produced this workflow. Kept, never overwriting the parent.
            presentation: The author's words about the workflow, checked against the workflow
                before anything is written.
            intent: What the person who wanted the automation asked for, kept as given and
                checked against nothing.

        Returns:
            The library's id for the workflow.

        Raises:
            PresentationRefusedError: If the presentation names an input or an artifact the
                workflow does not declare.
        """
        ...

    async def redescribe(
        self, workflow_id: str, *, title: str, summary: str, applicability: str, presentation: WorkflowPresentation | None = None
    ) -> None:
        """Rewrite a workflow's card, never the workflow itself. Editing the workflow is an adaptation, not this.

        With `presentation` absent the library keeps whatever words it already holds, so a caller
        rewriting a title cannot silently drop the author's sentences. A presentation given
        replaces the kept one whole, checked against the workflow first.

        Args:
            workflow_id: The library's id for the workflow.
            title: The new title.
            summary: The new one-sentence summary.
            applicability: The new note on when to use the workflow and when not to.
            presentation: The author's words to keep in place of the old ones, or None to keep
                the old ones.

        Raises:
            LibraryError: If the library has no such workflow.
            PresentationRefusedError: If the presentation names an input or an artifact the
                workflow does not declare. Nothing is written in that case.
        """
        ...

    async def set_archived(self, workflow_id: str, *, archived: bool) -> None:
        """Mark a workflow archived, or clear that mark. Its runs, versions and lineage stay.

        Idempotent: archiving an already-archived workflow keeps the first `archived_at`, and
        restoring one that is not archived does nothing. Schedules pointing at the workflow are
        untouched. Archiving is not deletion.

        Args:
            workflow_id: The library's id for the workflow.
            archived: True to archive the workflow, False to restore it.

        Raises:
            LibraryError: If the library has no such workflow.
        """
        ...

    async def archived(self, *, limit: int = 50) -> Sequence[SavedWorkflow]:
        """The archived cards, most recently archived first, each with `archived_at` set.

        Args:
            limit: The most cards to return.

        Returns:
            The archived cards.
        """
        ...

    async def catalog(self, *, limit: int = 50) -> Sequence[SavedWorkflow]:
        """Every live workflow, ordered by title and then id, each scored `0.0`.

        It is for when a search by word has nothing to rank: the whole library, for a person or a
        model to judge fit from each applicability note. An archived workflow is never listed, the
        same rule `search` follows.

        Args:
            limit: The most cards to return.

        Returns:
            The live cards.
        """
        ...

    async def catalog_size(self) -> int:
        """How many non-archived workflows this library holds: the total `catalog` lists from.

        When a library lacks this method, a caller says nothing about the total and never
        assumes zero.

        Returns:
            The count.
        """
        ...


class SaveGuard(Protocol):
    """A host's own bar for a workflow worth keeping, checked before any write leaves this process.

    The host decides what a keepable workflow is (what words it must carry, what a placeholder
    is, what it refuses to invent), and the library layer never learns any of it. Answer `None`
    to let the write through, or the refusal text whoever called the write should read.
    """

    def __call__(
        self,
        workflow: Workflow,
        *,
        title: str,
        summary: str,
        applicability: str,
        presentation: WorkflowPresentation | None,
    ) -> str | None:
        """Judge one workflow and the words it would be kept under. `None` passes it.

        Args:
            workflow: The workflow to judge.
            title: The title it would be kept under.
            summary: The summary it would be kept under.
            applicability: The note saying when to use it and when not to.
            presentation: Its presentation, when it has one.

        Returns:
            A refusal that says what to fix, or `None` when the workflow passes.
        """
        ...


class EvidenceLog(Protocol):
    """The append-only log of run evidence, approvals and grades, readable by content address.

    The integrator decides where it lives.
    """

    async def append(self, event: RunEvidence | Approval | Grade) -> None:
        """Add one event to the log.

        Args:
            event: The event to add.
        """
        ...

    async def events_for(self, workflow_id: str) -> Sequence[RunEvidence | Approval | Grade]:
        """Every event filed under one library id.

        Args:
            workflow_id: The library's id for the workflow.

        Returns:
            The events.
        """
        ...

    async def history_for(self, *, spec_hash: str) -> tuple[RunEvidence | Approval | Grade, ...]:
        """Everything held about one workflow, found by what it is rather than where it was kept.

        An implementation applies `require_spec_identity` to `spec_hash` before answering.

        Args:
            spec_hash: The workflow's content address.

        Returns:
            The events carrying `spec_hash`, oldest first.

        Raises:
            ValueError: If `spec_hash` is empty.
        """
        ...


class ReviewedArtifactQueries(Protocol):
    """Reads the values behind the references this run already issued, and nothing else."""

    def read(self, ref: ReviewedArtifactRef) -> JsonValue:
        """The reviewed value `ref` addresses.

        Args:
            ref: A reference this run issued.

        Returns:
            The reviewed value.
        """
        ...


class ApprovalHook(Protocol):
    """Who answers a `PersonGate`, and who a judge escalates to when it cannot decide.

    The workflow layer only receives the verdict and its source. A hook answering a plain boolean
    is still accepted and adapted at the boundary.
    """

    async def review(self, gate_id: str, value: object, *, note: str = "") -> GateDecision | bool:
        """Ask whichever person answers this gate.

        Args:
            gate_id: Which gate this is.
            value: What is being reviewed.
            note: What a judge said when it could not decide, shown ahead of the value.

        Returns:
            The decision, or a plain boolean from a hook that answers one.
        """
        ...


class OrchestratorDecider(Protocol):
    """Who answers an `OrchestratorGate`: the host's standing orchestrator agent.

    It is a term of the registry, not an argument to one run, so a run with no orchestrator
    refuses an orchestrator-gated workflow before it starts, the way a run with no sandbox refuses
    an inline program. It receives the gate's id, the `charge` the workflow wrote, and the reviewed
    value itself. The value arrives unrendered, so whoever supplies the decider renders it the way
    its people read it.
    """

    async def __call__(self, gate_id: str, charge: str, value: object, /) -> GateDecision:
        """Decide one orchestrator gate from its charge and the reviewed value.

        Args:
            gate_id: The gate being decided.
            charge: The brief the gate hands its decider.
            value: The value under review.

        Returns:
            The decision on the gate.
        """
        ...


class StepExecutor(Protocol):
    """How a step is performed: here, or somewhere durable.

    Every call of a step goes through this one port, so it is the only place durable execution
    enters. Implementations receive the node id (what a durable engine records the call under),
    the step's introspected contract, and the resolved keyword arguments.
    """

    async def perform(self, node_id: str, implementation: StepImplementation, kwargs: Mapping[str, object]) -> object:
        """Perform one step with its resolved arguments and return what it produced.

        Args:
            node_id: The node performing the step.
            implementation: The step to perform.
            kwargs: Its resolved arguments by parameter name.

        Returns:
            What the step produced.
        """
        ...


class RunObserver(Protocol):
    """Told which node is running, as it happens.

    Nodes in flight together all report `started` before any of them reports `finished`, so a
    caller can show parallel branches as they run. Each report names its node exactly, including
    the node a failure belongs to. Implementations must not raise and should not block, because
    every call is on the execution path.
    """

    def started(self, node_id: str) -> None:
        """Report that a node started.

        Args:
            node_id: The node that started.
        """
        ...

    def finished(self, node_id: str, *, failed: bool = False) -> None:
        """Report that a node finished, and whether it failed.

        Args:
            node_id: The node that finished.
            failed: Whether it failed.
        """
        ...


class PerformStep(Protocol):
    """What performing a briefing means in this run. The host decides it, not this package.

    `task` and `output_type` are positional-only, so the remaining keywords are the step's own
    inputs, named exactly as the workflow wired them.
    """

    async def __call__(self, task: Briefing, output_type: type, /, **inputs: object) -> object:
        """Perform one briefing with the step's own inputs and return its output.

        Args:
            task: The briefing to perform.
            output_type: The type the result is validated against.
            **inputs: The step's own inputs, named as the workflow wired them.

        Returns:
            The briefing's output.
        """
        ...


class CodeRunner(Protocol):
    """What running an inline program means in this run: which sandbox, and what a tool call does in it.

    The workflow supplies the source and the host supplies the sandbox that runs it. `exec`, `eval`
    and a subprocess are not implementations of this protocol, and a host with none passes `None`
    and gets a refusal. It takes the same positional-only shape as `PerformStep`.
    """

    async def __call__(self, step: Program, output_type: type, /, **inputs: object) -> object:
        """Run one program in the host's sandbox with the step's own inputs and return its output.

        Args:
            step: The program to run.
            output_type: The type the result is validated against.
            **inputs: The step's own inputs, named as the workflow wired them.

        Returns:
            The program's output.
        """
        ...


class AgentRunner(Protocol):
    """What running an inline agent step means in this run: the host's own agent framework, not this package's.

    The host supplies the merge rules of `Agent.from_spec` (model, output type, tools,
    capabilities). It takes the same positional-only shape as `PerformStep` and `CodeRunner`, so
    the remaining keywords are the step's own inputs, named as the workflow wired them.
    `output_type` is what the result is validated against.
    """

    async def __call__(self, step: AgentStep, output_type: type, /, **inputs: object) -> object:
        """Run one agent step with the step's own inputs and return its output.

        Args:
            step: The agent step to run.
            output_type: The type the result is validated against.
            **inputs: The step's own inputs, named as the workflow wired them.

        Returns:
            The agent step's output.
        """
        ...


class AgentResolver(Protocol):
    """What an `AgentStep.agent` string reference means in this run: the host's own registry, not this package's.

    It is called with the referenced name, positional-only. It returns the document exactly as an
    inline `agent` mapping would carry it, so a resolved reference is checked and built the same
    way an inline document is.

    Raises:
        LookupError: `name` does not resolve here, because it is unregistered or registered but
            not yet usable. The message reaches the workflow verbatim, so it should say why and,
            where there is one, the fix.
    """

    def __call__(self, name: str, /) -> Mapping[str, JsonValue]:
        """The agent document a string reference names.

        Args:
            name: The referenced agent name.

        Returns:
            The agent document exactly as an inline `agent` mapping would carry it.
        """
        ...


class Rows(Protocol):
    """The two asyncpg calls the read side needs."""

    async def fetch(self, sql: str, /, *args: object) -> Sequence[Mapping[str, Any]]:
        """Every row the query matches, each addressable by column name.

        Args:
            sql: The query.
            *args: Its bind parameters, in order.

        Returns:
            The rows.
        """
        ...

    async def fetchval(self, sql: str, /, *args: object) -> str | Mapping[str, JsonValue] | None:
        """The first column of the first row, or None for no rows.

        Here that is always a `jsonb` frontmatter document.

        Args:
            sql: The query.
            *args: Its bind parameters, in order.

        Returns:
            A string from a connection with no json codec, a mapping from one that decodes json,
            or None for no such page.
        """
        ...


class Ingest(Protocol):
    """The write side: hand over one authoring payload and the `collection` it belongs in, and the knowledge base does the rest.

    A failure raises. There is no partial success to interpret.
    """

    async def __call__(self, payload: Mapping[str, Any], *, collection: str) -> None:
        """Validate one authoring payload against the page grammar and land it as rows.

        Args:
            payload: The authoring payload.
            collection: The knowledge base partition the rows land in.
        """
        ...
