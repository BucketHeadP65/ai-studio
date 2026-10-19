"""A capability that serves a host's own library tools to an agent as the workflow tools.

Point it at a toolset, name which of its tools search, fetch and write, and pass adapters that
translate each result after the host's tool returns. The agent then sees `search_workflows`,
`fetch_workflow`, `save_workflow` and `adapt_workflow` beside whichever host tools you expose.

Host output an adapter cannot translate becomes a `ModelRetry` naming what failed, never a
half-parsed card. The host's own words stay inside the adapters you pass. Every write is
validated against this run's steps before the host's write tool is called, so leaving that tool
out of `expose` makes the verified save the only route into the library.
"""

from __future__ import annotations

import json

from collections.abc import Callable, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field, replace
from typing import TYPE_CHECKING, Any, TypedDict

import yaml

from pydantic import ValidationError
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.tools import AgentDepsT, RunContext, ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset, FilteredToolset, FunctionToolset, PreparedToolset

from core.ai.workflows.domain.evidence import trust_level
from core.ai.workflows.domain.fingerprint import WorkflowFingerprint, drift, fingerprint, spec_identity
from core.ai.workflows.domain.intent import WorkflowIntent
from core.ai.workflows.domain.lineage import Lineage, Prediction, adapt
from core.ai.workflows.domain.presentation import WorkflowPresentation

# A runtime import even though only annotations name it: FunctionToolset resolves `_save` and
# `_adapt` signatures at runtime to build the tool schema, so under TYPE_CHECKING the name is gone.
from core.ai.workflows.domain.vocabulary import Workflow
from core.ai.workflows.exceptions import AdaptationRefusedError, LibraryError, SaveRefusedError
from core.ai.workflows.internal_contracts import SaveGuard
from core.ai.workflows.read_models import SavedWorkflow
from core.ai.workflows.service import verify_savable

if TYPE_CHECKING:
    from pydantic import JsonValue

    from core.ai.workflows.domain.vocabulary import InlineStep
    from core.ai.workflows.internal_contracts import EvidenceLog
    from core.ai.workflows.registry import StepRegistry

SEARCH_TOOL = "search_workflows"
FETCH_TOOL = "fetch_workflow"
SAVE_TOOL = "save_workflow"
ADAPT_TOOL = "adapt_workflow"
SEARCH_STEPS_TOOL = "search_steps"
FETCH_STEP_TOOL = "fetch_step"


@dataclass(frozen=True)
class WorkflowWrite:
    """One workflow on its way into a library, in the contract's words.

    The host wants this in its own shape, and that translation is `LibraryVocabulary.put_args`.
    """

    workflow: Workflow
    title: str
    summary: str
    applicability: str
    saved_by: str
    """Recorded, never inferred. The port supplies it, so a model cannot claim to be a human."""
    fingerprint: WorkflowFingerprint | None = None
    """The fingerprint the port derived at save time. A host that keeps it beside the workflow
    lets a later fetch answer "did the environment move under this"."""
    lineage: Lineage | None = None
    """The typed diff that produced this workflow, when it is an adaptation. Always derived by
    `adapt` from the ops it applied itself, so a model's own claim about parentage never reaches
    this."""
    uses: Sequence[str] | None = None
    """Reusable step ids this version was assembled from, for a host that keeps such a list."""
    presentation: WorkflowPresentation | None = None
    """The author's own words about this workflow, for the surfaces a person meets it on. `None`
    is a workflow nobody described, which is never words this code composed."""
    intent: WorkflowIntent | None = None
    """What the person who wanted this automation asked for, when the host holds it. `None` is a
    workflow nobody stated an intent for, and nothing here infers one from the workflow."""


@dataclass(frozen=True)
class LibraryVocabulary:
    """How one host's tools and words map onto the workflow contract.

    It holds a tool name, an argument builder and a post-run adapter for each verb. Reading is
    required and writing is optional. A read-only host leaves the write slots empty, and the port
    then offers no save tool at all. Set all three write slots or none, and both catalog slots or
    neither, or construction raises `ValueError`.
    """

    search_tool: str
    """The host tool that finds things."""
    fetch_tool: str
    """The host tool that returns one thing's content."""

    _: KW_ONLY

    search_args: Callable[[str, int], dict[str, Any]]
    """Our `(task, limit)` in the host's argument shape."""
    fetch_args: Callable[[str], dict[str, Any]]
    """Our workflow id in the host's argument shape, including any change to the id the host
    needs to return the workflow rather than a summary."""
    to_cards: Callable[[Any], Sequence[SavedWorkflow]]
    """Post-run adapter: the host's search output becomes contract cards."""
    to_spec: Callable[[Any], Workflow]
    """Post-run adapter: the host's fetch output becomes a validated workflow."""
    to_fingerprint: Callable[[Any], WorkflowFingerprint | None] | None = None
    """Post-run adapter: the recorded fingerprint out of the same fetch payload, if the host kept
    one. Without it, a fetch reports staleness as unknown, never as fresh."""

    catalog_tool: str | None = None
    """The host tool that lists everything, if the host has one at all."""
    catalog_args: Callable[[int], dict[str, Any]] | None = None
    """Our `limit` in the host's argument shape, for `catalog_tool`."""
    catalog_total: Callable[[Any], int | None] | None = None
    """Post-run adapter: how many workflows the library holds, out of `catalog_tool`'s raw answer.

    It returns `None` when the host's answer does not say, and the listing then claims no total.
    Left unset, no total is read even when the host reports one."""

    put_tool: str | None = None
    """The host tool that keeps something, if the host has one at all."""
    put_args: Callable[[WorkflowWrite], dict[str, Any]] | None = None
    """A workflow in the host's write shape. Page ids, section names and column names all live here."""
    to_id: Callable[[Any], str] | None = None
    """Post-run adapter: the host's write acknowledgement becomes the id we hand back. A
    write whose id we invented instead of reading would be unfetchable later."""

    search_steps_tool: str | None = None
    """The host tool that finds reusable steps, if the host offers one."""
    fetch_step_tool: str | None = None
    """The host tool that returns one reusable step's content."""
    put_step_tool: str | None = None
    """The host tool that writes a reusable step, if the host offers one."""
    step_args: Callable[[str], dict[str, Any]] | None = None
    """A reusable step id in the host's argument shape."""
    to_step: Callable[[JsonValue], InlineStep] | None = None
    """Post-run adapter: the host's fetch output becomes a validated inline step."""

    def __post_init__(self) -> None:
        write_slots = (self.put_tool, self.put_args, self.to_id)
        if any(slot is not None for slot in write_slots) and not all(slot is not None for slot in write_slots):
            msg = "a writable vocabulary needs all three of put_tool, put_args, and to_id, or none of them"
            raise ValueError(msg)
        catalog_slots = (self.catalog_tool, self.catalog_args)
        if any(slot is not None for slot in catalog_slots) and not all(slot is not None for slot in catalog_slots):
            msg = "a vocabulary with a catalog tool needs both catalog_tool and catalog_args, or neither"
            raise ValueError(msg)
        if self.catalog_total is not None and self.catalog_tool is None:
            msg = "catalog_total reads the catalog tool's own answer, so it needs a catalog_tool to read from"
            raise ValueError(msg)

    @property
    def can_write(self) -> bool:
        """Whether this host has a write surface the port can drive."""
        return self.put_tool is not None and self.put_args is not None and self.to_id is not None


class _WriteRefusedError(Exception):
    """A refusal on the shared write path, whose message the calling tool returns as its whole result."""


class FetchedWorkflow(TypedDict):
    """What `fetch_workflow` answers, as named fields a program reads without parsing prose."""

    summary: str
    fit: str
    fits_here: bool | None
    spec_yaml: str
    staleness: list[str] | None
    """What moved in the environment since this workflow was saved. `[]` means checked and fresh,
    a non-empty list names the drift, and `None` means unknown, such as a workflow with no
    fingerprint, which is neither fresh nor stale."""


_NOTE_LIMIT = 200
"""How much of the author's own prose one card line carries. A card is scanned, not read."""


def render_cards(cards: Sequence[SavedWorkflow], earned: Mapping[str, str] | None = None, *, applicability: bool = True) -> str:
    """One line per candidate workflow or reusable step, with the notes that decide reuse.

    A card with a presentation shows its purpose and what a run produces above the applicability
    text, in the author's own words only. A card without one shows neither line, never a sentence
    composed here.

    Args:
        cards: The workflows or reusable steps to list.
        earned: Each card id's trust line, shown under its card. A card missing here shows none.
        applicability: Whether to show each card's `when:` line. Pass False for a catalog listing,
            where the notes would swamp the titles a reader scans first.

    Returns:
        The listing, or one line saying nothing matched when `cards` is empty.
    """
    if not cards:
        return "(no saved workflow matches. Plan fresh instead of forcing a bad fit)"
    lines: list[str] = []
    for card in cards:
        trust = "provisional" if card.provisional else "trusted"
        lines.append(f"{card.id} [{trust}] {card.title}: {card.summary}")
        if earned and (record := earned.get(card.id)):
            lines.append(f"    earned: {record}")
        shown = card.presentation
        if shown is not None and shown.purpose.strip():
            lines.append(f"    purpose: {shown.purpose.strip()[:_NOTE_LIMIT]}")
        if shown is not None and shown.produces.strip():
            lines.append(f"    produces: {shown.produces.strip()[:_NOTE_LIMIT]}")
        if applicability and card.applicability:
            lines.append(f"    when: {card.applicability.strip()[:400]}")
    return "\n".join(lines)


def _words(purpose: str, produces: str, owner_participation: str, not_for: str) -> WorkflowPresentation | None:
    """The four sentences as a presentation, or `None` when the caller wrote none of them."""
    written = (purpose.strip(), produces.strip(), owner_participation.strip(), not_for.strip())
    if not any(written):
        return None
    return WorkflowPresentation(purpose=written[0], produces=written[1], owner_participation=written[2], not_for=written[3])


def _catalog_header(total: int | None, shown: int) -> str:
    """The line introducing a zero-hit listing: how many workflows follow, out of how many when `total` is known."""
    if total is None:
        noun = "saved workflow" if shown == 1 else "saved workflows"
        verb = "follows" if shown == 1 else "follow"
        return f"{shown} {noun} {verb}."
    if total == shown:
        noun = "saved workflow" if total == 1 else "saved workflows"
        pronoun, verb = ("it", "follows") if total == 1 else ("them", "follow")
        return f"{total} {noun}, all of {pronoun} {verb}."
    return f"{total} saved workflows, the first {shown} by title follow."


@dataclass
class WorkflowContract(AbstractCapability[AgentDepsT]):
    """A capability that exposes a host's library tools as the workflow tools, beside the host tools you choose.

    ```python
    agent = Agent(
        model,
        capabilities=[
            WorkflowContract(
                source=CombinedToolset([MCPToolset(library_client), library_writes]),
                vocabulary=host_vocabulary,  # a LibraryVocabulary: tool names + post-run adapters
                expose=("run_sql", "run_code", "graph_expand"),  # note: not the write tool
                hide=("submit_feedback",),
                registry=registry,  # no registry, no saving
                saved_by="agent:studio-author",
            )
        ],
    )
    ```
    """

    source: AbstractToolset[AgentDepsT]
    """The host's toolset: an MCP server, a function toolset, anything."""
    vocabulary: LibraryVocabulary
    """How to translate between the host's words and the contract's."""

    _: KW_ONLY

    expose: Sequence[str] | None = ()
    """Host tools to leave switched on for the agent, beside the contract's own tools.

    Empty by default. The verified save stays the only route into the library only while the
    host's raw write tool never reaches the model. `None` shows every host tool, for a host with
    nothing to withhold."""
    hide: Sequence[str] = field(default_factory=tuple)
    """Host tools to drop even if `expose` would allow them.

    Filtering hides a tool from the model only. The port still calls it, so a raw write tool can
    stay in the host's toolset."""
    registry: StepRegistry | None = None
    """This run's steps. The save tools are offered only when it is set, because every write is
    validated against it."""
    saved_by: str = "agent"
    """Whose provenance to stamp on writes through this port. Set it to something a reader
    can act on (`agent:studio-author`, `service:nightly-saver`)."""
    save_retries: int = 3
    """How many corrections the save tool allows. Each validator refusal consumes one."""
    save_guard: SaveGuard | None = None
    """The host's own bar for a workflow worth keeping, asked before either write tool writes.

    What it refuses, and in what words, is the host's business. Setting one also stops this port
    filling a blank title or summary from the workflow, so the guard judges what the caller wrote.
    `None` leaves the validator as the only check."""
    evidence_log: EvidenceLog | None = None
    """The evidence log beside this library, if it has one.

    When set, search hits and fetches show the model what each workflow has earned, derived by
    `trust_level` on every read and never stored. `None` shows no trust line at all."""
    save_requires_approval: bool = False
    """Mark the save tool as requiring approval, for hosts whose protocol can ask.

    Over ACP this turns every save into a client-side permission prompt. Leave it False on a host
    that cannot render the approval, or the chat loop stalls waiting for it."""
    guidance: str | None = None
    """Replaces the injected usage note. `''` turns the note off."""
    environment: Mapping[str, str] = field(default_factory=dict)
    """Extra entries for this run's fingerprint, such as capability versions. Stamped onto every
    save through this port, and compared with a workflow's recorded fingerprint on fetch."""
    save_capability_id: str | None = None
    """When set, `save_workflow` and `adapt_workflow` stay hidden until the model loads the
    deferred capability with this id through `load_capability`.

    Name a skill that teaches the workflow grammar, so loading it and revealing the write tools
    are one act. `None` keeps both always visible. `search_workflows` and `fetch_workflow` stay
    visible either way."""

    def get_toolset(self) -> AbstractToolset[AgentDepsT] | None:
        """The host's tools that survive filtering, plus the contract tools that wrap them.

        Returns:
            One toolset holding both, with the write tools tagged for deferred loading when
            `save_capability_id` is set.
        """
        contract: FunctionToolset[AgentDepsT] = FunctionToolset()
        contract.add_function(self._search, name=SEARCH_TOOL)
        contract.add_function(self._fetch, name=FETCH_TOOL)
        if self.vocabulary.search_steps_tool is not None:
            contract.add_function(self._search_steps, name=SEARCH_STEPS_TOOL)
            contract.add_function(self._fetch_step, name=FETCH_STEP_TOOL)
        if self.can_save:
            # Validation refusals come back as tool results, so retries here only cover the
            # plumbing surprises (a host tool gone missing) that still arrive as retries.
            deferred = self.save_capability_id is not None
            contract.add_function(
                self._save,
                name=SAVE_TOOL,
                retries=self.save_retries,
                requires_approval=self.save_requires_approval,
                defer_loading=deferred,
            )
            contract.add_function(self._adapt, name=ADAPT_TOOL, retries=self.save_retries, defer_loading=deferred)
        combined = CombinedToolset[AgentDepsT]([FilteredToolset(self.source, self._keep), contract])
        capability_id = self.save_capability_id
        if capability_id is None:
            return combined

        async def _tag_writes(_ctx: RunContext[AgentDepsT], tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
            return [replace(td, capability_id=capability_id) if td.name in (SAVE_TOOL, ADAPT_TOOL) else td for td in tool_defs]

        return PreparedToolset(combined, _tag_writes)

    @property
    def can_save(self) -> bool:
        """Both halves have to be true: a host that can be written, and a way to verify."""
        return self.registry is not None and self.vocabulary.can_write

    def get_instructions(self) -> Callable[[RunContext[AgentDepsT]], str] | None:
        """The usage note injected beside the tools, matched to what this port can actually do.

        Returns:
            The note as an instructions function, or None when `guidance` is `''`.
        """
        text = self._note()
        if text is None:
            return None

        def compose(_ctx: RunContext[AgentDepsT]) -> str:
            return text

        return compose

    def _note(self) -> str | None:
        """The note's text, matched to what this port can actually do."""
        if self.guidance is not None:
            return self.guidance or None
        note = (
            f"`{SEARCH_TOOL}` and `{FETCH_TOOL}` speak the workflow contract: cards with an "
            "applicability note, and validated specs. Search before designing anything, and before "
            "answering any request a saved workflow might already cover: a saved workflow built "
            "for that shape of question beats improvising one. Call it with the person's request in "
            "their own words, verbatim, never a rewrite into a topic, since the library ranks by "
            "the words themselves. A conversational turn (a greeting, "
            "a thanks, a question about this conversation) is not such a request: answer it "
            "directly, no search, no mention of the library. Where these tools live inside a code "
            f"sandbox, the probe is ONE small program away: `hits = await {SEARCH_TOOL}(task=...)` "
            "as its first line costs less than the answer being wrong, and skipping it because "
            "writing a program feels heavy is the exact failure this note exists to prevent. "
            "Reusing a saved workflow beats writing a new one, and the 'when not to use' half of "
            "an applicability note is usually what decides. The other tools are the host library's "
            "own. Use them for anything the contract does not cover. Whenever you DID search, what "
            "it returned and what you did with it appears in your reply. A hit that fits is "
            "OFFERED by name: one line on what running or adapting it would produce, the person "
            "decides. A near or no fit is declined out loud, one line naming what ruled it out. "
            "From the person's point of view, a library consulted silently is a library never consulted."
        )
        if self.evidence_log is not None:
            note += (
                " What a workflow has EARNED (its trust level, run and grade history) is already "
                "in these tools' answers: search hits carry it under each card, and a fetch returns "
                "the full record. Answer trust questions from those lines, never infer a level from "
                "prose, and never claim this surface cannot see trust."
            )
        if not self.can_save:
            return f"{note} This library is read-only from here, so do not promise to save anything."
        if self.vocabulary.search_steps_tool is not None:
            note += (
                f" This library also keeps reusable steps: `{SEARCH_STEPS_TOOL}` and `{FETCH_STEP_TOOL}` find "
                f"and read them, and naming one in `{SAVE_TOOL}`'s `uses` copies it into the workflow before it is written."
            )
        return (
            f"{note} `{SAVE_TOOL}` keeps a workflow, and it validates before writing: a spec with "
            "wiring problems is refused with the findings, so fix and call it again rather than "
            f"arguing. To change a SAVED workflow, use `{ADAPT_TOOL}` with edit ops rather than "
            "re-saving an edited copy: the re-save keeps an orphan, while the adaptation records "
            "its parent and keeps the trust the untouched nodes earned. Say why you changed it and "
            "claim what the change should show. The claim is what a later run grades, and an "
            "adaptation that claims nothing can never be shown to have worked. Save or adapt only "
            "when the person asks. Proposing is yours, deciding is theirs."
        )

    def _keep(self, ctx: RunContext[AgentDepsT], tool: ToolDefinition) -> bool:
        del ctx  # filtering is static here, and a host that needs per-run filtering can wrap again
        if tool.name in self.hide:
            return False
        return True if self.expose is None else tool.name in self.expose

    async def _call_host(self, ctx: RunContext[AgentDepsT], name: str, args: dict[str, Any]) -> JsonValue:
        tools = await self.source.get_tools(ctx)
        tool = tools.get(name)
        if tool is None:
            available = ", ".join(sorted(tools)) or "(none)"
            msg = f"the host has no tool {name!r}. It offers: {available}"
            raise ModelRetry(msg)
        return await self.source.call_tool(name, args, ctx, tool)

    async def _search(self, ctx: RunContext[AgentDepsT], task: str, limit: int = 5) -> str:
        """Find saved workflows that might already do this task.

        Args:
            ctx: The run, through which the host's search tool is reached.
            task: The person's request in their own words, verbatim, including any workflow
                name they used. Never a rewrite into a topic: the library ranks by the words
                themselves.
            limit: Maximum candidates to return.
        """
        if not task.strip():
            msg = "task is empty. Describe what you are trying to accomplish."
            raise ModelRetry(msg)
        try:
            raw = await self._call_host(ctx, self.vocabulary.search_tool, self.vocabulary.search_args(task, limit))
        except ModelRetry as exc:
            # Returning "no candidates" here would teach the model the library is empty. An
            # unchecked library is not an empty one, so the failure is named as itself.
            return (
                f"LIBRARY SEARCH UNAVAILABLE. The host's {self.vocabulary.search_tool!r} tool failed: {exc} "
                f"This is NOT 'nothing matched'. Do not conclude anything about what the library holds: "
                f"try the host's other tools if it has any, or say plainly that you could not check."
            )
        try:
            cards = self.vocabulary.to_cards(raw)
        except (ValueError, KeyError, TypeError, ValidationError) as exc:
            msg = (
                f"the library answered in a shape this adapter does not understand ({exc}). "
                f"Try a different phrasing, or use the host's own tools directly."
            )
            raise ModelRetry(msg) from exc
        kept = cards[:limit]
        if not kept:
            fallback = await self._catalog_fallback(ctx)
            if fallback is not None:
                return fallback
        earned = {card.id: line for card in kept if (line := await self._earned_line(card.id)) is not None}
        return render_cards(kept, earned=earned)

    async def _catalog_fallback(self, ctx: RunContext[AgentDepsT]) -> str | None:
        """Up to ten saved workflows listed after a search by word found nothing, or None without a catalog tool."""
        catalog_tool, catalog_args = self.vocabulary.catalog_tool, self.vocabulary.catalog_args
        if catalog_tool is None or catalog_args is None:
            return None
        try:
            raw = await self._call_host(ctx, catalog_tool, catalog_args(10))
        except ModelRetry as exc:
            return (
                f"LIBRARY SEARCH UNAVAILABLE. The host's {catalog_tool!r} tool failed: {exc} "
                f"This is NOT 'nothing matched'. Do not conclude anything about what the library holds: "
                f"try the host's other tools if it has any, or say plainly that you could not check."
            )
        try:
            catalog = self.vocabulary.to_cards(raw)
        except (ValueError, KeyError, TypeError, ValidationError) as exc:
            msg = (
                f"the library answered in a shape this adapter does not understand ({exc}). "
                f"Try a different phrasing, or use the host's own tools directly."
            )
            raise ModelRetry(msg) from exc
        if not catalog:
            return render_cards(catalog)  # nothing matched by word, and the library itself is empty
        total = self.vocabulary.catalog_total(raw) if self.vocabulary.catalog_total is not None else None
        earned = {card.id: line for card in catalog if (line := await self._earned_line(card.id)) is not None}
        return "\n".join(
            (
                _catalog_header(total, len(catalog)),
                render_cards(catalog, earned=earned, applicability=False),
                "Narrow with words from the request, or fetch one by id with fetch_workflow.",
            )
        )

    async def _earned_line(self, workflow_id: str, workflow: Workflow | None = None) -> str | None:
        """One trust line for this workflow, by content address when `workflow` is given, or None without an evidence log."""
        if self.evidence_log is None:
            return None
        if workflow is not None:
            record = trust_level(await self.evidence_log.history_for(spec_hash=spec_identity(workflow)))
        else:
            record = trust_level(await self.evidence_log.events_for(workflow_id))
        failed = f", {record.failed_runs} failed" if record.failed_runs else ""
        grades = f", {record.structural_grades} structural grades, mean {record.structural_mean:.3f}" if record.structural_mean is not None else ""
        return f"{record.level}, {record.runs_with_evidence} runs with evidence{failed}{grades}"

    async def _search_steps(self, ctx: RunContext[AgentDepsT], task: str, limit: int = 5) -> str:
        """Find reusable steps by what they do. Reuse one by naming its id in `save_workflow`'s `uses`."""
        if not task.strip():
            msg = "task is empty. Describe what you are trying to accomplish."
            raise ModelRetry(msg)
        vocabulary = self.vocabulary
        if vocabulary.search_steps_tool is None:
            msg = "this library keeps no reusable steps"
            raise ModelRetry(msg)
        try:
            raw = await self._call_host(ctx, vocabulary.search_steps_tool, vocabulary.search_args(task, limit))
        except ModelRetry as exc:
            # Returning "no candidates" here would teach the model the library is empty. An
            # unchecked library is not an empty one, so the failure is named as itself.
            return (
                f"LIBRARY SEARCH UNAVAILABLE. The host's {vocabulary.search_steps_tool!r} tool failed: {exc} "
                f"This is NOT 'nothing matched'. Do not conclude anything about what the library holds: "
                f"try the host's other tools if it has any, or say plainly that you could not check."
            )
        try:
            cards = vocabulary.to_cards(raw)
        except (ValueError, KeyError, TypeError, ValidationError) as exc:
            msg = (
                f"the library answered in a shape this adapter does not understand ({exc}). "
                f"Try a different phrasing, or use the host's own tools directly."
            )
            raise ModelRetry(msg) from exc
        if not cards:
            return "(no reusable step matches. Plan fresh instead of forcing a bad fit)"
        return render_cards(cards)

    async def _fetch_step(self, ctx: RunContext[AgentDepsT], step_id: str) -> str:
        """Show one reusable step whole: its name, inputs, output and tools, so a node can be wired to it."""
        vocabulary = self.vocabulary
        if vocabulary.fetch_step_tool is None or vocabulary.to_step is None or vocabulary.step_args is None:
            msg = "this library keeps no reusable steps"
            raise ModelRetry(msg)
        raw = await self._call_host(ctx, vocabulary.fetch_step_tool, vocabulary.step_args(step_id))
        try:
            step = vocabulary.to_step(raw)
        except (ValueError, KeyError, TypeError, ValidationError) as exc:
            msg = f"{step_id!r} did not yield a valid reusable step ({exc}). Either the id is wrong or that page is not a step."
            raise ModelRetry(msg) from exc
        return f"{step_id} is step {step.name!r}. Wire `work: {step.name}` and name it in `uses`:\n" + json.dumps(step.model_dump(mode="json"), indent=2)

    async def _copy_in(self, ctx: RunContext[AgentDepsT], workflow: Workflow, uses: Sequence[str]) -> Workflow:
        """`workflow` with every step in `uses` fetched from the library and appended to its `inline`."""
        vocabulary = self.vocabulary
        if vocabulary.fetch_step_tool is None or vocabulary.to_step is None or vocabulary.step_args is None:
            msg = "this library keeps no reusable steps. Drop `uses` and define the steps under inline"
            raise ModelRetry(msg)
        carried = {task.name for task in workflow.inline}
        copied: list[InlineStep] = []
        seen: set[str] = set()
        for step_id in uses:
            if step_id in seen:
                msg = f"{step_id!r} is named more than once in `uses`. Name each reusable step once."
                raise ModelRetry(msg)
            seen.add(step_id)
            raw = await self._call_host(ctx, vocabulary.fetch_step_tool, vocabulary.step_args(step_id))
            try:
                step = vocabulary.to_step(raw)
            except (ValueError, KeyError, TypeError, ValidationError) as exc:
                msg = f"{step_id!r} did not yield a valid reusable step ({exc}). It cannot be copied into this spec."
                raise ModelRetry(msg) from exc
            if step.name in carried:
                msg = f"{step_id} is step {step.name!r}, which this workflow already carries under inline. Drop one of the two"
                raise ModelRetry(msg)
            carried.add(step.name)
            copied.append(step)
        return workflow.model_copy(update={"inline": (*workflow.inline, *copied)})

    async def _save(
        self,
        ctx: RunContext[AgentDepsT],
        spec_yaml: str,
        applicability: str,
        title: str = "",
        summary: str = "",
        purpose: str = "",
        produces: str = "",
        owner_participation: str = "",
        not_for: str = "",
        uses: list[str] | None = None,
        intent: WorkflowIntent | None = None,
    ) -> str:
        """Keep a workflow in the library. It is validated first, and refused if it will not run.

        The workflow goes in as YAML text, the same text a proposal's `spec_yaml` slot carries. It
        is parsed and checked here the same way a draft is checked before it is proposed: a
        malformed workflow or a wiring problem is refused with every finding at once, never
        accepted and refused later.

        `purpose`, `produces`, `owner_participation` and `not_for` are the author's own words for
        the surfaces a person meets the workflow on, and a host may require them: pass them and
        they are kept, leave them out and whatever this library asks for is what you will hear
        back. Nothing here writes them on your behalf.

        `applicability` is a **separate argument, not a field of the workflow**. The graph
        describes what runs, and the card describes when to reach for it. The workflow has no such
        field and will reject one.

        If the graph needs a step this run does not register, the workflow may define it in
        `inline` (a name, a briefing, typed slots, a typed result), and it will be built and
        checked here like any other step. Prefer a registered step when one fits: it has been run
        before. Reach for `inline` when nothing fits, rather than bending the graph around the
        steps that happen to exist. A third option sits between the two: `uses` names a step
        already found in the library with `search_steps`, and it is copied into `inline` before
        anything else runs, so a step someone already wrote does not have to be written again.

        Args:
            ctx: The run, through which the host's write tool is reached.
            spec_yaml: The workflow graph as YAML, and only the graph.
            applicability: When to use this and when NOT to. Both halves. The second one is
                what stops a wrong reuse six months from now, and the workflow has nowhere to
                say it, which is why it is asked for separately.
            title: Human-facing name. Leave empty to use the workflow's own name.
            summary: One line for the card. Leave empty to use the workflow's own description.
            purpose: What this workflow is for, in your own words.
            produces: What a finished run leaves the person holding.
            owner_participation: What the person themselves does during a run.
            not_for: What to reach for something else for.
            uses: Reusable step ids, copied into the workflow's `inline` before validating. A name
                already under `inline`, or named here more than once, is refused.
            intent: What the person asked for, in their own words, and what they expect a run to
                do. Pass whatever has been stated, incomplete and all. Leave it out when nobody
                stated one, rather than composing a request nobody made.
        """
        try:
            workflow = Workflow.model_validate(yaml.safe_load(spec_yaml))
        except (ValidationError, yaml.YAMLError) as exc:
            return f"the spec is not well-formed yet:\n{str(exc)[:900]}\nFix the YAML and call {SAVE_TOOL} again. Nothing was written."
        if not applicability.strip():
            return (
                "applicability is empty. A card nobody can choose by is not worth saving. "
                "Call again with both halves: when to use it, and when NOT to."
            )
        if uses:
            try:
                workflow = await self._copy_in(ctx, workflow, uses)
            except ModelRetry as exc:
                return f"REFUSED. Nothing was written. {exc}"
        try:
            workflow_id = await self._verified_write(
                ctx,
                workflow,
                title=self._card_field(title, workflow.name.replace("-", " ").capitalize()),
                summary=self._card_field(summary, workflow.description),
                applicability=applicability,
                called=SAVE_TOOL,
                uses=uses,
                presentation=_words(purpose, produces, owner_participation, not_for),
                intent=intent,
            )
        except _WriteRefusedError as exc:
            return str(exc)
        return (
            f"saved as {workflow_id}: validated against this run's steps, written, and now findable "
            f"by {SEARCH_TOOL}. It is provisional until it has actually been run and recorded."
        )

    async def _adapt(
        self,
        ctx: RunContext[AgentDepsT],
        workflow_id: str,
        ops: list[dict[str, Any]],
        applicability: str,
        evidence: str = "",
        predictions: list[Prediction] | None = None,
        title: str = "",
        summary: str = "",
        purpose: str = "",
        produces: str = "",
        owner_participation: str = "",
        not_for: str = "",
        intent: WorkflowIntent | None = None,
    ) -> str:
        """Change a saved workflow by a typed diff, keeping the link to what it came from.

        Load the `saving-a-workflow` skill for the op and claim vocabulary before calling this.

        Args:
            ctx: The run, through which the host's fetch and write tools are reached.
            workflow_id: The saved workflow to adapt, verbatim from a search or fetch.
            ops: The edits, as data (`saving-a-workflow` has the full op vocabulary).
            applicability: When to use the child and when NOT to. Restate it even if unchanged,
                the card travels with this version.
            evidence: Why this version exists, in a sentence or two, for the person who later asks.
            predictions: What a later run of the child will show, graded off the evidence log
                (`saving-a-workflow` has the claim vocabulary).
            title: Human-facing name. Leave empty to keep the child's own name.
            summary: One line for the card. Leave empty to use the child's description.
            purpose: What the child is for, in your own words.
            produces: What a finished run of the child leaves the person holding.
            owner_participation: What the person themselves does during a run of it.
            not_for: What to reach for something else for.
            intent: What the person asked the child for, in their own words. Leave it out to keep
                the request the parent was asked for, and pass one only when somebody stated a
                different one.
        """
        if not ops:
            return "an adaptation with no operations is not an adaptation. Call again with at least one op."
        if not applicability.strip():
            return (
                "applicability is empty. A card nobody can choose by is not worth saving. "
                "Call again with both halves: when to use it, and when NOT to."
            )
        raw = await self._call_host(ctx, self.vocabulary.fetch_tool, self.vocabulary.fetch_args(workflow_id))
        try:
            parent = self.vocabulary.to_spec(raw)
        except (ValueError, KeyError, TypeError, ValidationError) as exc:
            return f"{workflow_id!r} did not yield a valid workflow spec to adapt ({exc}). Re-ground the id with {SEARCH_TOOL} and call again."
        try:
            child, lineage = adapt(parent, ops=ops, evidence=evidence, predictions=tuple(predictions or ()))
        except AdaptationRefusedError as exc:
            return f"the diff was refused, and nothing was applied: {exc}\nFix the op and call {ADAPT_TOOL} again."
        try:
            stored_at = await self._verified_write(
                ctx,
                child,
                title=self._card_field(title, child.name.replace("-", " ").capitalize()),
                summary=self._card_field(summary, child.description),
                applicability=applicability,
                lineage=lineage,
                called=ADAPT_TOOL,
                presentation=_words(purpose, produces, owner_participation, not_for),
                intent=intent,
            )
        except _WriteRefusedError as exc:
            return str(exc)
        carried = "every node" if not lineage.touched else f"every node except {', '.join(lineage.touched)}"
        claimed = (
            f" It claims {len(lineage.predictions)} thing(s), which the next completed run of v{child.version} grades."
            if lineage.predictions
            else " It claims nothing, so no run of it will confirm or refute anything."
        )
        return (
            f"adapted: {workflow_id} -> {stored_at} (v{child.version}). The parent is kept and linked "
            f"forward, and {carried} still carries its parent's run evidence. The child itself is "
            f"provisional until it has been run and recorded.{claimed}"
        )

    async def _verified_write(
        self,
        ctx: RunContext[AgentDepsT],
        workflow: Workflow,
        *,
        title: str,
        summary: str,
        applicability: str,
        called: str,
        lineage: Lineage | None = None,
        uses: Sequence[str] | None = None,
        presentation: WorkflowPresentation | None = None,
        intent: WorkflowIntent | None = None,
    ) -> str:
        """The one write path both tools share: the host's `save_guard`, then this run's steps, then the write."""
        registry, put_tool = self.registry, self.vocabulary.put_tool
        put_args, to_id = self.vocabulary.put_args, self.vocabulary.to_id
        if registry is None or put_tool is None or put_args is None or to_id is None:  # pragma: no cover (the tool is not offered)
            msg = "this library cannot be written to from here. Nothing was saved."
            raise _WriteRefusedError(msg)
        if self.save_guard is not None:
            refusal = self.save_guard(workflow, title=title, summary=summary, applicability=applicability, presentation=presentation)
            if refusal is not None:
                msg = f"REFUSED. Nothing was written. {refusal}\nWrite the missing words and call {called} again."
                raise _WriteRefusedError(msg)
        try:
            verify_savable(workflow, registry)
        except SaveRefusedError as exc:
            msg = (
                f"REFUSED. Nothing was written. The validator found:\n{exc}\n"
                f"Fix the spec and call {called} again. Do not describe it as saved."
            )
            raise _WriteRefusedError(msg) from exc
        write = WorkflowWrite(
            workflow=workflow,
            title=title,
            summary=summary,
            applicability=applicability,
            saved_by=self.saved_by,
            fingerprint=fingerprint(workflow, registry, environment=dict(self.environment)),
            lineage=lineage,
            uses=uses,
            presentation=presentation,
            intent=intent,
        )
        try:
            raw = await self._call_host(ctx, put_tool, put_args(write))
        except LibraryError as exc:
            # The library can refuse a write the validator cannot see, and its message is written
            # to be acted on. It goes back as a result, not an exception that kills the turn.
            msg = f"REFUSED. Nothing was written. The library said: {exc}"
            raise _WriteRefusedError(msg) from exc
        try:
            return to_id(raw)
        except (ValueError, KeyError, TypeError, ValidationError) as exc:
            msg = (
                f"the library accepted the write but answered in a shape this adapter cannot read ({exc}). "
                f"the workflow may or may not be saved, so check with {SEARCH_TOOL} before saving again."
            )
            raise _WriteRefusedError(msg) from exc

    def _card_field(self, given: str, derived: str) -> str:
        """A card field the caller left blank, derived from the workflow unless a `save_guard` is set."""
        return given.strip() or ("" if self.save_guard is not None else derived)

    async def workflow_for(self, ctx: RunContext[AgentDepsT], workflow_id: str) -> Workflow:
        """Fetch a workflow as data, for code that builds it rather than a model reading it.

        It uses the same adapter as `fetch_workflow`, so the object and the tool's answer never differ.

        Args:
            ctx: The run, through which the host's fetch tool is reached.
            workflow_id: The saved workflow's id.

        Returns:
            The validated workflow.

        Raises:
            ModelRetry: When the host offers no tool by the vocabulary's fetch name.
        """
        raw = await self._call_host(ctx, self.vocabulary.fetch_tool, self.vocabulary.fetch_args(workflow_id))
        return self.vocabulary.to_spec(raw)

    async def _fetch(self, ctx: RunContext[AgentDepsT], workflow_id: str) -> FetchedWorkflow:
        """Fetch one saved workflow: a summary, whether it runs here, and `spec_yaml` verbatim.

        Args:
            ctx: The run, through which the host's fetch tool is reached.
            workflow_id: An id copied verbatim from a search result.
        """
        raw = await self._call_host(ctx, self.vocabulary.fetch_tool, self.vocabulary.fetch_args(workflow_id))
        try:
            workflow = self.vocabulary.to_spec(raw)
        except (ValueError, KeyError, TypeError, ValidationError) as exc:
            msg = (
                f"{workflow_id!r} did not yield a valid workflow spec ({exc}). Either the id is wrong "
                f"or that workflow is not executable. Do not invent one to replace it."
            )
            raise ModelRetry(msg) from exc
        works = tuple(dict.fromkeys(node.work for node in workflow.nodes if node.kind == "run"))
        gates = ", ".join(node.id for node in workflow.nodes if node.kind == "gate") or "none"
        brought = tuple(task.name for task in workflow.inline)
        fit, fits_here = self._fit(works, brought=brought)
        staleness = self._staleness(raw, workflow)
        if staleness:
            fit = (
                f"STALE: the environment moved under this workflow: {'. '.join(staleness)}. "
                f"It may still validate, so re-check before trusting it. {fit}"
            )
        earned = await self._earned_line(workflow_id, workflow)
        return FetchedWorkflow(
            summary=(
                f"{workflow.name} v{workflow.version}: {workflow.description}\n"
                f"  parameters: {', '.join(workflow.parameters) or 'none'}\n"
                f"  steps: {', '.join(works)}\n"
                f"  brings its own steps: {', '.join(brought) or 'none'}\n"
                f"  gates: {gates}" + (f"\n  earned: {earned}" if earned else "")
            ),
            fit=fit,
            fits_here=fits_here,
            spec_yaml=yaml.safe_dump(workflow.model_dump(mode="json"), sort_keys=False, allow_unicode=True, width=1000),
            staleness=staleness,
        )

    def _staleness(self, raw: JsonValue, workflow: Workflow) -> list[str] | None:
        """What moved in the environment since the workflow was saved, or None when that cannot be answered."""
        if self.vocabulary.to_fingerprint is None or self.registry is None:
            return None
        try:
            recorded = self.vocabulary.to_fingerprint(raw)
        except (ValueError, KeyError, TypeError, ValidationError):
            recorded = None
        if recorded is None:
            return None
        return list(drift(recorded, fingerprint(workflow, self.registry, environment=dict(self.environment))))

    def _fit(self, works: Sequence[str], *, brought: Sequence[str] = ()) -> tuple[str, bool | None]:
        """Whether this run can execute the workflow, as prose plus True, False, or None when no registry can say."""
        if self.registry is None:
            return "fit: unknown from here. This port has no registry, so nothing can say whether the steps exist.", None
        own = set(brought)
        missing = tuple(work for work in works if work not in own and self.registry.get(work) is None)
        if missing:
            return (
                f"DOES NOT FIT THIS RUN: no step named {', '.join(missing)}. It was written for a different "
                f"environment. Do not plan to execute it and do not keep re-reading it. Design from the steps "
                f"this run does have."
            ), False
        if brought and not self.registry.performs_briefings:
            return (
                f"DOES NOT FIT THIS RUN: it brings {len(brought)} step(s) of its own ({', '.join(brought)}) and this "
                f"run cannot perform a briefing. It can only execute workflows naming steps that already exist here."
            ), False
        if brought:
            # Said out loud because "it names a step you do not have" reads as not fitting: a
            # workflow carrying its own steps does fit.
            return (
                f"FITS THIS RUN: the steps it names are registered here, and the {len(brought)} step(s) it brings "
                f"with it ({', '.join(brought)}) can be built from their briefings, so it runs as it stands."
            ), True
        return "FITS THIS RUN: every step it names is registered here, so it can be executed as it stands.", True


def cards_from_mapping(
    payload: Mapping[str, Any], *, key: str, card: Callable[[Mapping[str, Any]], SavedWorkflow]
) -> list[SavedWorkflow]:
    """The cards in a JSON object that holds a list of rows under one key.

    A row that is not an object is skipped.

    Args:
        payload: The host's answer.
        key: The key the rows sit under.
        card: Turns one row into a card.

    Returns:
        One card per row.

    Raises:
        TypeError: When `payload` holds no list under `key`.
    """
    rows = payload.get(key)
    if not isinstance(rows, list):
        msg = f"expected a list under {key!r}, got {type(rows).__name__}"
        raise TypeError(msg)
    return [card(row) for row in rows if isinstance(row, Mapping)]
