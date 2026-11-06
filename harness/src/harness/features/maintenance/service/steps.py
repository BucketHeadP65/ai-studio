"""The registered steps of step maintenance: list a changed step's consumers, check them, run its cases, plan, land and report.

The steps read and write the library through one port, and validate every candidate against the registry they are
registered on, so a candidate is checked against the steps a run would build it with. A model names a consumer and a
step version by id, and code copies the step from the library.
"""

from __future__ import annotations

import difflib

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from core.ai.workflows.contracts import (
    STEP_MAINTENANCE_AGENT,
    AdaptationRefusedError,
    JudgeGate,
    LibraryError,
    LiveTrialMarker,
    LiveTrialStep,
    MaintenanceFlag,
    MapNode,
    Prediction,
    PresentationRefusedError,
    RunNode,
    SaveRefusedError,
    adapt,
    canonical_step_yaml,
    fingerprint,
    flag_id,
    spec_identity,
    step_content_address,
    trust_across_versions,
    validate,
    verify_savable,
    version_hashes,
)
from core.observability import get_logger
from pydantic import JsonValue

from harness.domain.contracts import CaseReport, cases_address
from harness.features.maintenance.domain import (
    ADOPTION_EDITS,
    NOT_DECIDED,
    PAIR_CAP,
    ConsumerList,
    ConsumerPair,
    ConsumerSide,
    ContractCheck,
    DroppedDraft,
    HandOver,
    LandedChange,
    LandingReport,
    MaintenancePlan,
    PlanDecisions,
    PlannedChange,
    RepositoryChange,
    RepositoryVerdict,
    change_line,
    changed_ids,
    fenced,
    merged_plan,
    one_line,
)
from harness.features.maintenance.exceptions import LandingRefusedError
from harness.features.maintenance.service.changed_steps import newest_version
from harness.features.maintenance.service.live_trial import read_live_trial

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime

    from core.ai.workflows.contracts import ConsumerCard, InlineStep, Lineage, StepRegistry, StepVersion, TrustAcrossVersions, Workflow

    from harness.adapters.contracts import Notifier, StandInCalls
    from harness.features.maintenance.internal_contracts import CaseRunner, MaintenanceLibrary, RunHistoryQueries

_logger = get_logger(__name__)

_REFUSALS = (LibraryError, SaveRefusedError, ValueError, PresentationRefusedError)
"""What refuses one planned change at landing: the library, the validation, an edit operation that does not apply (a
malformed one included) or the card's words."""


@dataclass(frozen=True)
class _Candidate:
    """One consumer whose copy of a changed step is behind it or diverged, before the planner's evidence is read."""

    version: StepVersion
    card: ConsumerCard
    held: StepVersion

    @property
    def state(self) -> Literal["behind", "diverged"]:
        return "diverged" if self.card.entry.version is None else "behind"


type _Changed = tuple[tuple[StepVersion, tuple[StepVersion, ...]], ...]
"""Each changed step at its highest named version, beside every saved version of the step, by step name."""


class MaintenanceSteps:
    """The registered steps of step maintenance and its `check_change` tool, over one library.

    You must register the steps on a registry before any of them runs, because every candidate is validated against
    that registry. Only `land_changes` and `hand_over_repository_changes` write to the library, and `check_change`
    saves nothing.
    """

    def __init__(  # noqa: PLR0913 (each keyword is one collaborator the composition hands the steps)
        self,
        *,
        library: MaintenanceLibrary,
        history: RunHistoryQueries,
        notifier: Notifier,
        cases: CaseRunner,
        clock: Callable[[], datetime],
        environment: Mapping[str, str],
        current_run: Callable[[], str | None],
    ) -> None:
        """Hold the library, the evidence log, the way to the owner, the case runs, the clock and the run's stamps.

        Args:
            library: The library the consumers, the step versions and the flags live in.
            history: The evidence log, read for each consumer's trust.
            notifier: How the owner is told what landed and what a coding agent can apply.
            cases: How a step version's own cases run.
            clock: When a flag is raised. It must answer a time with its offset.
            environment: The host's stamps each adoption's fingerprint records.
            current_run: The id of the run a step works for, which an adoption's evidence names, or None outside a run.
        """
        self._library = library
        self._history = history
        self._notifier = notifier
        self._cases = cases
        self._clock = clock
        self._environment = dict(environment)
        self._current_run = current_run
        self._registry: StepRegistry | None = None

    def register(self, registry: StepRegistry) -> None:
        """Register every step on `registry`, and validate every candidate against it from then on.

        `plan_decisions` and `repository_verdict` are registered too, though no graph places them, so a briefing can
        answer in their types.

        Args:
            registry: The registry a step maintenance run executes on.

        Raises:
            RegistrationError: A step of the same name is already registered on `registry`.
        """
        self._registry = registry
        for step, replay_safe in (
            (self.list_consumers, False),
            (self.check_contracts, False),
            (self.run_step_cases, False),
            (self.prepare_plan, True),
            (self.land_changes, False),
            (self.report_changes, False),
            (self.select_disagreements, False),
            (self.plan_decisions, True),
            (self.repository_verdict, True),
            (self.hand_over_repository_changes, False),
        ):
            registry.register(step, replay_safe=replay_safe)

    async def list_consumers(self, changed_step_ids: str) -> ConsumerList:
        """Every consumer whose copy of a changed step is behind it or diverged, at most twenty, with what the planner decides by.

        Each named step is taken at its newest saved version, whichever version the id names. A consumer whose copy
        matches that version, or whose record declines it, is dropped and named. The consumers past the cap are named
        once each for a later run.

        Args:
            changed_step_ids: The changed step versions' ids, comma-separated.

        Returns:
            The pairs, by step name and then by consumer name, the consumers left for a later run and those dropped.

        Raises:
            LibraryError: A step id names no saved reusable step version, or a library read fails.
        """
        taken, left, dropped = await self._candidates(await self._changed(changed_step_ids))
        pairs = [await self._pair(candidate) for candidate in taken]
        left_ids = list(dict.fromkeys(candidate.card.workflow_id for candidate in left))
        return ConsumerList(pairs=pairs, left_for_next_run=left_ids, dropped=dropped)

    async def check_contracts(self, consumers: ConsumerList) -> list[ContractCheck]:
        """Each pair's contract check: whether the consumer validates with its copy replaced by the changed version.

        Args:
            consumers: The pairs `list_consumers` answered.

        Returns:
            One check per pair, in the pairs' order. A failing check is a result, not an error.

        Raises:
            LibraryError: A library read fails.
        """
        return [await self._check(pair.workflow_id, pair.step_id, ()) for pair in consumers.pairs]

    async def run_step_cases(self, changed_step_ids: str) -> list[CaseReport]:
        """The case report of each changed step version, and of each older version a behind consumer holds.

        Each step's case file is checked first, and one that cannot run answers a failed report saying why, while the
        other steps still run. A kept report is answered without running again. Each step's cases run over a stand-in
        list of their own.

        Args:
            changed_step_ids: The changed step versions' ids, comma-separated.

        Returns:
            The reports, the changed versions first.

        Raises:
            LibraryError: A step id names no saved reusable step version, or a library read fails.
            RuntimeError: The registry the cases run through cannot perform a step. The run then fails.
        """
        changed = await self._changed(changed_step_ids)
        taken, _, _ = await self._candidates(changed)
        versions = {version.step_id: version for version, _ in changed}
        for candidate in taken:
            if candidate.state == "behind":
                versions.setdefault(candidate.held.step_id, candidate.held)
        return [await self._case_report(version) for version in versions.values()]

    async def prepare_plan(self, consumers: ConsumerList, checks: list[ContractCheck], cases: list[CaseReport]) -> MaintenancePlan:
        """The drafted plan: one change per pair, kept and not decided, its evidence copied in.

        Each change carries its pair's contract check and the case reports of the changed version and of the version a
        behind consumer holds, so the planner decides and never writes evidence.

        Args:
            consumers: The pairs `list_consumers` answered.
            checks: The checks `check_contracts` answered over the same pairs.
            cases: The reports `run_step_cases` answered for the same changed steps.

        Returns:
            The plan, in the pairs' order.

        Raises:
            ValueError: A pair has no contract check.
        """
        by_pair = {(check.workflow_id, check.step_id): check for check in checks}
        changes: list[PlannedChange] = []
        for pair in consumers.pairs:
            check = by_pair.get((pair.workflow_id, pair.step_id))
            if check is None:
                msg = f"no contract check came for {pair.workflow_id} with {pair.step_id}. Wire check_contracts over the same consumer list"
                raise ValueError(msg)
            reported = {pair.step_id, pair.held_step_id} if pair.state == "behind" else {pair.step_id}
            changes.append(
                PlannedChange(
                    workflow_id=pair.workflow_id,
                    step_id=pair.step_id,
                    disposition="keep",
                    reason=NOT_DECIDED,
                    check=check,
                    cases=[report for report in cases if report.step_id in reported],
                )
            )
        return MaintenancePlan(changes=changes, left_for_next_run=list(consumers.left_for_next_run))

    async def land_changes(self, plan: MaintenancePlan, decisions: PlanDecisions) -> LandingReport:
        """Land the drafted plan as the planner decided it: save each adoption on live trial, decline each kept copy and flag each flagged pair.

        Each decision is merged into the drafted change with the same workflow id and step id, so the evidence stays as
        `prepare_plan` copied it. A drafted change no decision names writes nothing and lands as not decided, so a later
        run for its step offers it again. A decision that names no drafted change is refused, and so is every decision
        of a drafted change decided twice.

        Every adoption into one consumer lands as one version: the step of each is copied from the library, each
        change's further edit operations are applied, the candidate is validated and saved as the consumer's next
        version, numbered one above the highest saved version of its name so an archived version is never written over,
        saved by step maintenance. Its lineage touches the nodes of every replaced step, and its live trial marker
        covers each adopted step with the version it replaces as the baseline. A consumer whose newest version is still
        on live trial is built on that version, and the new marker keeps its baseline and its steps beside the new ones.
        Each adoption then resolves the open consumer flag of its pair. A kept pair writes a decline into the
        consumer's record, and a flagged pair writes a `consumer_flag` page with the contract check and the planner's
        reason, both on the version this landing saved for the consumer when it saved one. Every writable change lands
        before any refusal is raised. Each step a change was adopted, kept or flagged for resolves the open
        `plan_rejected` flag of that step version or an earlier one, and a flag a person resolved is left alone.

        A change is refused when its step or its consumer cannot be read, when its step version is not the newest of
        its name, when the consumer declines that version, or when someone saved a newer version of the consumer since
        the listing. An adoption is also refused when it carries an edit other than `rewire`, `add_parameter` or
        `set_output`. The adoptions into one consumer that pass those checks are refused together when their edits do
        not apply, when the candidate does not validate, when its card's words name what the workflow does not declare,
        when it names an agent this host cannot resolve, or when the library refuses the save. A kept pair is refused
        when the library refuses its decline.

        Args:
            plan: The drafted plan, as `prepare_plan` answered it.
            decisions: The planner's decisions the judge approved.

        Returns:
            What each change did, when nothing was refused.

        Raises:
            LandingRefusedError: One or more changes were refused. Every other change landed, the owner was sent one
                notice saying every change with its reason, and the error's report holds every outcome.
        """
        merged = merged_plan(plan, decisions)
        outcomes: dict[int, LandedChange] = {index: change for index, change in enumerate(merged) if isinstance(change, LandedChange)}
        adoptions: dict[str, list[tuple[int, PlannedChange]]] = {}
        for index, change in enumerate(merged):
            if isinstance(change, PlannedChange) and change.disposition == "adopt":
                adoptions.setdefault(change.workflow_id, []).append((index, change))
        saved: dict[str, str] = {}
        for index, change in enumerate(merged):
            if index in outcomes or isinstance(change, LandedChange):
                continue
            if change.disposition == "adopt":
                group = adoptions[change.workflow_id]
                landed = await self._adopt_all(change.workflow_id, [planned for _, planned in group])
                outcomes.update(zip((position for position, _ in group), landed, strict=True))
                if saved_id := next((each.saved_id for each in landed if each.outcome == "adopted"), ""):
                    saved[change.workflow_id] = saved_id
            else:
                outcomes[index] = await self._land(change, saved.get(change.workflow_id, change.workflow_id))
        changes = await self._settle_rejected_plans([outcomes[index] for index in range(len(merged))])
        report = LandingReport(changes=changes, left_for_next_run=list(plan.left_for_next_run))
        refused = sum(change.outcome == "refused" for change in changes)
        if refused:
            _, told = await self._tell_owner(f"Step maintenance refused {refused} of {len(changes)} planned changes", report)
            raise LandingRefusedError(report, told=told)
        return report

    async def report_changes(self, landed: LandingReport) -> str:
        """Tell the owner in one notice what changed in which workflow and why, and the consumers left for a later run.

        A notice that fails is said in the answer and never fails the run. A landing that changed nothing tells nobody.

        Args:
            landed: What the landing did.

        Returns:
            The notice's text and whether it went out.
        """
        if not landed.changes and not landed.left_for_next_run:
            return "Step maintenance changed nothing, so nobody was told."
        message, told = await self._tell_owner(f"Step maintenance landed {', '.join(sorted({change.step_id for change in landed.changes}))}", landed)
        return f"{message}\n{told}"

    async def _tell_owner(self, title: str, landed: LandingReport) -> tuple[str, str]:
        """Send the owner one notice saying every change of `landed` and the consumers past the cap, and answer it and whether it went out."""
        lines = [change_line(change) for change in landed.changes]
        if landed.left_for_next_run:
            lines.append(
                f"Left for a later run, past the cap of {PAIR_CAP} pairs: {', '.join(landed.left_for_next_run)}. Step maintenance "
                f"takes them when it next runs for their steps, with a step's next version, or run step-maintenance by hand "
                f"with the same step ids to take them now."
            )
        message = "\n".join(lines)
        try:
            delivered = await self._notifier.notify(title, message)
        except Exception as exc:  # noqa: BLE001 (a notice that fails is said in the answer, never raised into the run)
            return message, f"The notice to the owner failed: {exc}"
        return message, "The owner was told." if delivered else "The notice to the owner was not delivered."

    async def select_disagreements(self, landed: LandingReport, consumers: ConsumerList) -> list[RepositoryChange]:
        """One repository change to draft per changed step that some consumers adopted and others kept or flagged.

        Each carries the changed step version whole, as its canonical YAML read from the library. Each side holds every
        consumer with its outcome, the planner's reason and what the consumer is for. The drafted fields are empty.

        Args:
            landed: What the landing did.
            consumers: The pairs `list_consumers` answered.

        Returns:
            The repository changes, empty when no step's consumers split.

        Raises:
            LibraryError: A changed step version cannot be read.
        """
        pairs = {(pair.workflow_id, pair.step_id): pair for pair in consumers.pairs}
        sides: dict[str, list[tuple[ConsumerPair, ConsumerSide]]] = {}
        for change in landed.changes:
            pair = pairs.get((change.workflow_id, change.step_id))
            outcome = change.outcome
            if pair is None or outcome in ("refused", "not_decided"):
                continue
            side = ConsumerSide(
                workflow_id=change.workflow_id,
                outcome=outcome,
                reason=change.reason,
                nodes=list(pair.nodes),
                diff=pair.diff,
                presentation=pair.presentation,
                intent=pair.intent,
            )
            sides.setdefault(change.step_id, []).append((pair, side))
        selected: list[RepositoryChange] = []
        for step_id, held in sides.items():
            adopted = [side for _, side in held if side.outcome == "adopted"]
            kept = [side for _, side in held if side.outcome != "adopted"]
            if adopted and kept:
                step_yaml = canonical_step_yaml(await self._library.fetch_step(step_id))
                selected.append(RepositoryChange(step=held[0][0].step, step_id=step_id, step_yaml=step_yaml, adopted=adopted, kept=kept))
        return selected

    async def plan_decisions(self, plan: MaintenancePlan) -> PlanDecisions:
        """No decision for any change of `plan`, the type the planner answers in.

        No graph places this step. Handed these decisions, the landing writes nothing for any drafted change of `plan`.

        Args:
            plan: The drafted plan.

        Returns:
            No decisions.
        """
        del plan
        return PlanDecisions(decisions=[])

    async def repository_verdict(self, step: str) -> RepositoryVerdict:
        """An undecided verdict on the drafted repository change for `step`, the type a repository judge answers in.

        No graph places this step.

        Args:
            step: The reusable step the drafted change is about.

        Returns:
            The undecided verdict.
        """
        return RepositoryVerdict(step=step, decision="undecided", reason="not yet judged")

    async def hand_over_repository_changes(
        self, report: str, disagreements: list[RepositoryChange], drafts: list[RepositoryChange], verdicts: list[RepositoryVerdict]
    ) -> HandOver:
        """Write each draft its judge kept as a `repository_change` flag page, drop the rest with why, and tell the owner about the pages.

        A draft is matched to its verdict by step. A draft no verdict came for, and a step of `disagreements` no draft
        came for, is dropped and named. A kept draft's page, `mf.change.<step>`, holds the drafted step files and file
        edits in its Proposed change section. The owner is told once, and only when a page was written. A flag page or a
        notice that fails is said in the answer and never fails the run. The answer carries `report` too, so the run's
        answer holds the whole report.

        Args:
            report: The owner's report of the landing, as `report_changes` answered it.
            disagreements: The repository changes `select_disagreements` answered.
            drafts: The drafted repository changes.
            verdicts: The judges' verdicts on the drafts.

        Returns:
            The report, the flag pages written, the drafts dropped and what failed.
        """
        verdict_of = {verdict.step: verdict for verdict in verdicts}
        drafted = {draft.step for draft in drafts}
        flags: list[str] = []
        dropped: list[DroppedDraft] = []
        problems: list[str] = []
        for draft in drafts:
            verdict = verdict_of.get(draft.step)
            if verdict is None:
                dropped.append(DroppedDraft(step=draft.step, reason="no judge's verdict came back for it"))
                continue
            if verdict.decision != "keep":
                dropped.append(DroppedDraft(step=draft.step, reason=verdict.reason or f"its judge answered {verdict.decision}"))
                continue
            page = flag_id("repository_change", draft.step)
            try:
                written = await self._library.put_flag(self._repository_flag(draft, verdict, await self._library.fetch_step(draft.step_id)))
            except (LibraryError, ValueError) as exc:
                problems.append(f"{page} was not written: {exc}")
                continue
            if written is None:
                dropped.append(DroppedDraft(step=draft.step, reason=f"{page} was already resolved for {draft.step_id}, so it was left as it is"))
            else:
                flags.append(written)
        dropped.extend(
            DroppedDraft(step=change.step, reason="its drafter answered no draft, so there was nothing to judge")
            for change in disagreements
            if change.step not in drafted
        )
        if flags:
            problems.extend(await self._tell_about_repository_changes(flags, dropped))
        return HandOver(report=report, flags=flags, dropped=dropped, problems=problems)

    async def check_change(self, workflow_id: str, step_id: str, extra_ops: tuple[dict[str, JsonValue], ...] = ()) -> str:
        """Build the consumer version an adoption would save, run its contract check, and say whether the landing would adopt it.

        Call it on every change you plan to adopt, with the further edit operations it needs, before you answer. The
        landing builds the same version and refuses it when the check fails, when the step version is not the newest of
        its name, when the consumer declines that version, when someone saved a newer version of the consumer, or when
        an edit is anything but `rewire`, `add_parameter` or `set_output`. The answer says which. A repository change
        you draft may hold any edit, such as a `replace_inline_step`, because the landing never lands a draft: read
        only the contract check for it.

        Args:
            workflow_id: The consumer's workflow version id, `wf.<name>.v<version>`, as the pair names it.
            step_id: The changed step version's id, `st.<name>.v<version>`, as the pair names it.
            extra_ops: Edit operations applied after the copy is replaced by the step, in the form an adaptation takes,
                such as `{"op": "rewire", "id": "<node>", "input": "<input>", "to": {"node": "<other node>"}}`,
                `{"op": "add_parameter", "name": "<name>", "with": {"description": "...", "json_schema": {...}}}` or
                `{"op": "set_output", "to": {"node": "<node>"}}`. Leave it empty for a plain adoption.

        Returns:
            `passes` and that the landing would adopt it, `passes the contract check` and why the landing refuses it as
            an adoption, `fails` and each validation issue, or why nothing could be checked.
        """
        try:
            step = await self._library.fetch_step(step_id)
            refusal = await self._adoption_refusal(workflow_id, step_id, step, extra_ops)
            check = await self._check(workflow_id, step_id, extra_ops)
        except LibraryError as exc:
            return f"cannot check: {exc}. Use the workflow id and the step id the pair names."
        edits = f" and {len(extra_ops)} further edit operations" if extra_ops else ""
        if check.passed and refusal is None:
            return f"passes: {workflow_id} validates with {step_id} in place of its copy{edits}. The landing would adopt it."
        if check.passed:
            return (
                f"passes the contract check: {workflow_id} validates with {step_id} in place of its copy{edits}. "
                f"The landing refuses it as an adoption: {refusal}."
            )
        issues = "\n".join(check.issues)
        failed = (
            f"fails: {workflow_id} does not validate with {step_id} in place of its copy{edits}:\n{issues}\n"
            f"Add edit operations that fix each issue and check again, or plan keep or flag for this consumer."
        )
        return failed if refusal is None else f"{failed}\nThe landing refuses it as an adoption too: {refusal}."

    def _registered(self) -> StepRegistry:
        if self._registry is None:
            msg = "the step maintenance steps are not registered on a registry, so no candidate can be validated. Call register first"
            raise RuntimeError(msg)
        return self._registry

    async def _changed(self, changed_step_ids: str) -> _Changed:
        """Each named step at its newest saved version, by step name, refused when an id names no saved version."""
        taken: dict[str, tuple[StepVersion, tuple[StepVersion, ...]]] = {}
        for step_id in changed_ids(changed_step_ids):
            newest, saved = await newest_version(step_id, fetch_step=self._library.fetch_step, step_versions=self._library.step_versions)
            taken[newest.name] = (newest, saved)
        return tuple(taken[name] for name in sorted(taken))

    async def _candidates(self, changed: _Changed) -> tuple[list[_Candidate], list[_Candidate], list[str]]:
        """The behind or diverged consumers within the cap, those past it, and the ids of the current or declined ones, each once."""
        candidates: list[_Candidate] = []
        dropped: dict[str, None] = {}
        for version, saved in changed:
            by_version = {each.version: each for each in saved}
            for card in await self._library.consumers_of(version.name):
                entry = card.entry
                if entry.declined == version.step_hash or (entry.version is not None and entry.version >= version.version):
                    dropped[card.workflow_id] = None
                    continue
                held_version = entry.version if entry.version is not None else entry.diverged_from
                held = by_version.get(held_version) if held_version is not None else None
                if held is None:
                    msg = f"{card.workflow_id} records {version.name} v{held_version}, which no saved version of {version.name} is"
                    raise LibraryError(msg)
                candidates.append(_Candidate(version=version, card=card, held=held))
        candidates.sort(key=lambda candidate: (candidate.version.name, candidate.card.name, candidate.card.version))
        return candidates[:PAIR_CAP], candidates[PAIR_CAP:], list(dropped)

    async def _pair(self, candidate: _Candidate) -> ConsumerPair:
        """The pair one candidate makes, with its diff, its nodes, its card's words and its trust."""
        workflow_id = candidate.card.workflow_id
        name = candidate.version.name
        workflow = await self._library.fetch_workflow(workflow_id)
        copy = next((step for step in workflow.inline if step.name == name), None)
        if copy is None:
            msg = f"{workflow_id} records a copy of {name} that its workflow does not hold"
            raise LibraryError(msg)
        card = await self._library.fetch_card(workflow_id)
        trust = await self._trust(workflow)
        return ConsumerPair(
            workflow_id=workflow_id,
            workflow=workflow.name,
            title=card.title if card is not None else workflow.name,
            step=name,
            step_id=candidate.version.step_id,
            step_hash=candidate.version.step_hash,
            state=candidate.state,
            held_step_id=candidate.held.step_id,
            nodes=_nodes_running(workflow, name),
            diff=_diff(copy, f"{workflow_id} {name}", candidate.version),
            presentation=card.presentation if card is not None else None,
            intent=card.intent if card is not None else None,
            trust_level=trust.level,
            trust=trust.current,
            total_trust=trust.total,
        )

    async def _trust(self, workflow: Workflow) -> TrustAcrossVersions:
        """The workflow's trust per version and in total, folded over every saved version of its name."""
        hashes = await version_hashes(workflow, versions=self._library, fetch_workflow=self._library.fetch_workflow)
        events = await self._history.history_for_any(spec_hashes=hashes.keys())
        return trust_across_versions(events, hashes, spec_identity(workflow))

    async def _check(self, workflow_id: str, step_id: str, extra_ops: Sequence[Mapping[str, JsonValue]]) -> ContractCheck:
        """The contract check of one consumer with its copy replaced by the step `step_id` names, then `extra_ops` applied."""
        workflow = await self._library.fetch_workflow(workflow_id)
        step = await self._library.fetch_step(step_id)
        try:
            candidate, _ = _adopted(workflow, ((step, extra_ops),), evidence="")
        except ValueError as exc:
            return ContractCheck(workflow_id=workflow_id, step_id=step_id, passed=False, issues=[str(exc)])
        report = validate(candidate, self._registered())
        issues = [f"[{issue.kind}] {issue.where}: {issue.detail}" for issue in report.issues]
        return ContractCheck(workflow_id=workflow_id, step_id=step_id, passed=report.ok, issues=issues)

    async def _case_report(self, version: StepVersion) -> CaseReport:
        """One step version's case report: a failed one naming why when its case file cannot run, else its run's report."""
        cases = await self._library.fetch_step_cases(version.step_id)
        if cases is not None and cases.get("cases") and (refusal := self._cases.refusal(version.step_id, cases)) is not None:
            return CaseReport(
                step_id=version.step_id,
                step_hash=version.step_hash,
                cases_hash=cases_address(cases),
                results=(),
                blocked_by=None,
                no_cases=False,
                stand_in_calls=0,
                refused=refusal,
            )
        calls: StandInCalls = []
        return await self._cases.perform(version.step, version.step_id, cases, calls)

    async def _land(self, change: PlannedChange, base: str) -> LandedChange:
        """Land one kept or flagged change on the consumer version `base`, or record why it was refused."""
        try:
            step = await self._library.fetch_step(change.step_id)
            if (refusal := await self._stale_pair(base, change.step_id, step)) is not None:
                return _landed(change, "refused", refusal)
            workflow = await self._library.fetch_workflow(base)
            if change.disposition == "keep":
                await self._library.record_decline(base, step=step.name, step_hash=step_content_address(step), because="kept")
                return _landed(change, "kept", change.reason)
            return await self._flag(change, base, workflow, step)
        except _REFUSALS as exc:
            return _landed(change, "refused", str(exc))

    async def _adopt_all(self, base: str, changes: Sequence[PlannedChange]) -> list[LandedChange]:
        """Land every adoption into the consumer version `base` as one version, each refused change with why, in their order."""
        outcomes: dict[int, LandedChange] = {}
        ready: list[tuple[int, PlannedChange, InlineStep]] = []
        for index, change in enumerate(changes):
            try:
                step = await self._library.fetch_step(change.step_id)
                refusal = await self._adoption_refusal(base, change.step_id, step, change.extra_ops)
            except _REFUSALS as exc:
                outcomes[index] = _landed(change, "refused", str(exc))
                continue
            if refusal is not None:
                outcomes[index] = _landed(change, "refused", refusal)
            else:
                ready.append((index, change, step))
        if ready:
            adopted = [(change, step) for _, change, step in ready]
            try:
                landed = await self._adopt(base, adopted)
            except _REFUSALS as exc:
                landed = [_landed(change, "refused", _together(str(exc), change, adopted, base)) for change, _ in adopted]
            outcomes.update(zip((index for index, _, _ in ready), landed, strict=True))
        return [outcomes[index] for index in range(len(changes))]

    async def _stale_pair(self, base: str, step_id: str, step: InlineStep) -> str | None:
        """Why the pair is stale: the step version is not the newest of its name, the consumer declines it, or someone saved past the consumer."""
        newest_step = max(await self._library.step_versions(step.name), key=lambda saved: saved.version)
        if newest_step.step_id != step_id:
            return f"{step_id} is not the newest version of {step.name}, {newest_step.step_id} is. The next run plans {newest_step.step_id}"
        address = step_content_address(step)
        if any(entry.step == step.name and entry.declined == address for entry in await self._library.fetch_consumer_record(base)):
            return f"{base} declines {step_id}, so step maintenance leaves the pair alone until a newer version arrives"
        newest = await self._library.current(base)
        if newest != base:
            return (
                f"{newest} was saved after {base}, so this change no longer applies to the newest version. Step maintenance plans "
                f"it again when it next runs for {step.name}, with its next version, or when you run step-maintenance with {step_id}"
            )
        return None

    async def _adoption_refusal(self, base: str, step_id: str, step: InlineStep, extra_ops: Sequence[Mapping[str, JsonValue]]) -> str | None:
        """Why the landing refuses this adoption before building it, or None: a stale pair, or an edit it may not carry."""
        if (stale := await self._stale_pair(base, step_id, step)) is not None:
            return stale
        if refused := [str(op.get("op")) for op in extra_ops if op.get("op") not in ADOPTION_EDITS]:
            return (
                f"an adoption may carry only the edits rewire, add_parameter and set_output, and this one carries "
                f"{', '.join(refused)}. Plan keep or flag for this consumer, or adopt it without those edits"
            )
        return None

    async def _adopt(self, base: str, adopted: Sequence[tuple[PlannedChange, InlineStep]]) -> list[LandedChange]:
        """Save the consumer's next version with every step of `adopted` on live trial, then resolve each pair's open consumer flag."""
        workflow = await self._library.fetch_workflow(base)
        run = self._current_run()
        ran_in = f" in the run {run}" if run else ""
        if len(adopted) == 1:
            ((change, _),) = adopted
            evidence = f"Step maintenance adopted {change.step_id} in {base}{ran_in}. {change.reason}"
        else:
            reasons = " ".join(f"{change.step_id}: {change.reason}" for change, _ in adopted)
            evidence = f"Step maintenance adopted {', '.join(change.step_id for change, _ in adopted)} in {base}{ran_in}. {reasons}"
        child, lineage = _adopted(workflow, [(step, change.extra_ops) for change, step in adopted], evidence=evidence)
        child = child.model_copy(update={"version": await self._next_version(workflow.name)})
        registry = self._registered()
        verify_savable(child, registry)
        try:
            stamp = fingerprint(child, registry, environment=self._environment)
        except LookupError as exc:
            refusal = f"the next version of {base} names an agent this host cannot resolve: {exc}"
            return [_landed(change, "refused", _together(refusal, change, adopted, base)) for change, _ in adopted]
        card = await self._library.fetch_card(base)
        if card is None:
            msg = f"{base} has no card to copy its title, summary and applicability from"
            raise LibraryError(msg)
        steps = tuple(LiveTrialStep(step=step.name, step_hash=step_content_address(step)) for _, step in adopted)
        earlier = await self._open_live_trial(base, workflow)
        saved_id = await self._library.put(
            child,
            title=card.title,
            summary=card.summary,
            applicability=card.applicability,
            provenance={"saved_by": STEP_MAINTENANCE_AGENT},
            fingerprint=stamp,
            lineage=lineage,
            presentation=card.presentation,
            intent=card.intent,
            live_trial=earlier.with_steps(steps) if earlier is not None else LiveTrialMarker(previous=base, steps=steps),
        )
        return [
            _landed(change, "adopted", change.reason, saved_id=saved_id, notes=await self._resolve_consumer_flag(workflow.name, step.name, saved_id, change.step_id))
            for change, step in adopted
        ]

    async def _open_live_trial(self, base: str, workflow: Workflow) -> LiveTrialMarker | None:
        """The marker of the consumer version `base` while its live trial is still open, or None."""
        marker = await self._library.fetch_live_trial(base)
        if marker is None:
            return None
        runs = await read_live_trial(self._library, self._history, base, workflow, marker)
        return None if runs.is_over() else marker

    async def _settle_rejected_plans(self, changes: Sequence[LandedChange]) -> list[LandedChange]:
        """Resolve the open `plan_rejected` flag of each step a change landed for, a failed resolution noted on its first landed change."""
        notes: dict[str, str] = {}
        for step_id in dict.fromkeys(change.step_id for change in changes if change.outcome in ("adopted", "kept", "flagged")):
            if (note := await self._resolve_rejected_plan(step_id)) is not None:
                notes[step_id] = note
        settled: list[LandedChange] = []
        for change in changes:
            note = notes.pop(change.step_id, None) if change.outcome in ("adopted", "kept", "flagged") else None
            settled.append(change if note is None else change.model_copy(update={"notes": [*change.notes, note]}))
        return settled

    async def _resolve_rejected_plan(self, step_id: str) -> str | None:
        """Resolve as step maintenance the step's open `plan_rejected` flag about `step_id` or an earlier version, answering a note when that fails."""
        try:
            step = await self._library.fetch_step(step_id)
        except LibraryError as exc:
            return f"the open plan_rejected flag of {step_id} was not read: {exc}"
        page = flag_id("plan_rejected", step.name)
        try:
            stored = await self._library.fetch_flag(page)
            if stored is None or stored.status != "open":
                return None
            versions = {saved.step_id: saved.version for saved in await self._library.step_versions(step.name)}
            flagged, landed = versions.get(stored.step_id), versions.get(step_id)
            if flagged is None or landed is None or flagged > landed:
                return None
            run = self._current_run()
            ran_in = f" in the run {run}" if run else ""
            await self._library.resolve_flag(page, by=STEP_MAINTENANCE_AGENT, reason=f"Step maintenance landed a plan for {step_id}{ran_in}.")
        except (LibraryError, ValueError) as exc:
            _logger.warning("a plan for {step_id} landed, and the open flag {flag} was not resolved: {reason}", step_id=step_id, flag=page, reason=str(exc))
            return f"the open flag {page} was not resolved: {exc}"
        return None

    async def _next_version(self, name: str) -> int:
        """One above the highest saved version of the workflow `name`, archived and superseded ones included."""
        return max(version for version, _ in await self._library.versions(name)) + 1

    async def _resolve_consumer_flag(self, workflow_name: str, step_name: str, saved_id: str, step_id: str) -> list[str]:
        """Resolve the pair's open consumer flag as settled by the adoption, answering a note when that fails."""
        page = flag_id("consumer_flag", step_name, workflow_name)
        try:
            stored = await self._library.fetch_flag(page)
            if stored is not None and stored.status == "open":
                await self._library.resolve_flag(page, by=STEP_MAINTENANCE_AGENT, reason=f"{saved_id} adopted {step_id}.")
        except (LibraryError, ValueError) as exc:
            _logger.warning("{saved_id} adopted {step_id}, and the open flag {flag} was not resolved: {reason}", saved_id=saved_id, step_id=step_id, flag=page, reason=str(exc))
            return [f"the open flag {page} was not resolved: {exc}"]
        return []

    async def _flag(self, change: PlannedChange, base: str, workflow: Workflow, step: InlineStep) -> LandedChange:
        """Write the pair's consumer flag with its contract check and the planner's reason, with a note when it was not written."""
        check = await self._check(base, change.step_id, ())
        page = flag_id("consumer_flag", step.name, workflow.name)
        flag = MaintenanceFlag(
            kind="consumer_flag",
            step=step.name,
            step_id=change.step_id,
            step_hash=step_content_address(step),
            workflow=base,
            title=f"Consumer flag on {step.name} in {workflow.name}",
            summary=f"Decide whether {base} adopts {change.step_id}, keeps its copy of {step.name}, or changes first.",
            evidence=f"Step maintenance flagged {base} for {change.step_id}. The planner's reason:\n\n{fenced(change.reason)}\n\n{_check_text(check)}",
            raised_by=STEP_MAINTENANCE_AGENT,
            raised_at=self._clock(),
        )
        try:
            written = await self._library.put_flag(flag)
        except (LibraryError, ValueError) as exc:
            _logger.warning("{workflow} was flagged for {step_id}, and its flag {flag} was not written: {reason}", workflow=base, step_id=change.step_id, flag=page, reason=str(exc))
            return _landed(change, "flagged", change.reason, notes=[f"the flag {page} was not written: {exc}"])
        if written is None:
            return _landed(change, "flagged", change.reason, notes=[f"the flag {page} was already resolved for {change.step_id}, so it was left as it is"])
        return _landed(change, "flagged", change.reason)

    def _repository_flag(self, draft: RepositoryChange, verdict: RepositoryVerdict, step: InlineStep) -> MaintenanceFlag:
        """The `repository_change` flag of one kept draft: both sides and the judge's reason, and the drafted files."""
        if step.name != draft.step:
            msg = f"the draft names the step {draft.step}, and {draft.step_id} is a version of {step.name}"
            raise ValueError(msg)
        evidence = "\n\n".join((
            f"Step maintenance landed {draft.step_id}, and the consumers of {draft.step} split.",
            f"Adopted it:\n\n{_sides_text(draft.adopted)}",
            f"Kept their copy or were flagged:\n\n{_sides_text(draft.kept)}",
            f"The judge kept the draft:\n\n{fenced(verdict.reason)}",
        ))
        parts = [fenced(draft.proposal)] if draft.proposal else []
        for label, files in (("Step file", draft.step_files), ("File edit", draft.file_edits)):
            parts.extend(f"{label} `{one_line(file.path)}`:\n\n{fenced(file.text)}" for file in files)
        return MaintenanceFlag(
            kind="repository_change",
            step=draft.step,
            step_id=draft.step_id,
            step_hash=step_content_address(step),
            title=f"Repository change for {draft.step}",
            summary=f"Decide whether a coding agent applies the drafted change to {draft.step} and imports it.",
            evidence=evidence,
            proposed_change="\n\n".join(parts),
            raised_by=STEP_MAINTENANCE_AGENT,
            raised_at=self._clock(),
        )

    async def _tell_about_repository_changes(self, flags: Sequence[str], dropped: Sequence[DroppedDraft]) -> list[str]:
        """Tell the owner once which repository changes wait for a coding agent, answering a problem when that fails."""
        message = f"Step maintenance drafted {len(flags)} repository changes a coding agent can apply: {', '.join(flags)}."
        if dropped:
            message += " Dropped: " + ", ".join(f"{item.step} ({item.reason})" for item in dropped) + "."
        try:
            delivered = await self._notifier.notify("Repository changes to apply", message)
        except Exception as exc:  # noqa: BLE001 (a notice that fails is said in the answer, never raised into the run)
            return [f"the notice to the owner failed: {exc}"]
        return [] if delivered else ["the notice to the owner was not delivered"]


def _adopted(
    workflow: Workflow, replacements: Sequence[tuple[InlineStep, Sequence[Mapping[str, JsonValue]]]], *, evidence: str
) -> tuple[Workflow, Lineage]:
    """The consumer's next version with each copy replaced by its step, each followed by its edits, refused as `AdaptationRefusedError`."""
    ops: list[Mapping[str, JsonValue]] = []
    for step, extra_ops in replacements:
        ops.extend(({"op": "replace_inline_step", "name": step.name, "with": step.model_dump(mode="json")}, *extra_ops))
    try:
        return adapt(workflow, ops=ops, evidence=evidence, predictions=(Prediction(claim="completes"),))
    except AdaptationRefusedError:
        raise
    except ValueError as exc:
        msg = f"an edit operation does not apply: {exc}"
        raise AdaptationRefusedError(msg) from exc


def _nodes_running(workflow: Workflow, name: str) -> list[str]:
    """The ids of the workflow's nodes that run the step `name`, by its own name or as a map's turn."""
    return [
        node.id
        for node in workflow.nodes
        if (isinstance(node, RunNode | JudgeGate) and node.work == name) or (isinstance(node, MapNode) and name in node.turns)
    ]


def _diff(copy: InlineStep, label: str, version: StepVersion) -> str:
    """The unified diff of the canonical YAML from a consumer's copy to a saved step version."""
    lines = difflib.unified_diff(
        canonical_step_yaml(copy).splitlines(), canonical_step_yaml(version.step).splitlines(), fromfile=label, tofile=version.step_id, lineterm=""
    )
    return "\n".join(lines)


def _landed(
    change: PlannedChange,
    outcome: Literal["adopted", "kept", "flagged", "refused"],
    reason: str,
    *,
    saved_id: str = "",
    notes: Sequence[str] = (),
) -> LandedChange:
    return LandedChange(
        workflow_id=change.workflow_id,
        step_id=change.step_id,
        disposition=change.disposition,
        outcome=outcome,
        reason=reason,
        saved_id=saved_id,
        notes=list(notes),
    )


def _together(refusal: str, change: PlannedChange, adopted: Sequence[tuple[PlannedChange, InlineStep]], base: str) -> str:
    """Why one adoption was refused when it was refused with the others into the same consumer version."""
    others = [other.step_id for other, _ in adopted if other is not change]
    if not others:
        return refusal
    return f"{refusal}. It was adopted together with {', '.join(others)} into one next version of {base}, so none of them landed"


def _check_text(check: ContractCheck) -> str:
    """A contract check as a flag page's evidence says it."""
    if check.passed:
        return f"The contract check passed: {check.workflow_id} validates with {check.step_id} in place of its copy."
    return f"The contract check failed for {check.workflow_id} with {check.step_id}:\n\n{fenced(chr(10).join(check.issues))}"


def _sides_text(sides: Sequence[ConsumerSide]) -> str:
    """Each consumer on one side of a repository change with its outcome and the planner's reason, fenced."""
    return fenced("\n".join(f"{side.workflow_id} ({side.outcome}): {side.reason}" for side in sides))
