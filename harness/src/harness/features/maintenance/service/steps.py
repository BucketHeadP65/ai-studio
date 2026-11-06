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
    NOT_YET_DECIDED,
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
    PlannedChange,
    RepositoryChange,
    RepositoryVerdict,
    change_line,
    changed_ids,
    fenced,
    one_line,
)
from harness.features.maintenance.exceptions import LandingRefusedError

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

        `repository_verdict` is registered too, though no graph places it, so a briefing can answer in its type.

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
            (self.select_disagreements, True),
            (self.repository_verdict, True),
            (self.hand_over_repository_changes, False),
        ):
            registry.register(step, replay_safe=replay_safe)

    async def list_consumers(self, changed_step_ids: str) -> ConsumerList:
        """Every consumer whose copy of a changed step is behind it or diverged, at most twenty, with what the planner decides by.

        Each named step is taken at its newest saved version, whichever version the id names. A consumer whose copy
        matches that version, or whose record declines it, is dropped and counted. The consumers past the cap are named
        once each for the next run.

        Args:
            changed_step_ids: The changed step versions' ids, comma-separated.

        Returns:
            The pairs, by step name and then by consumer name, the consumers left for the next run and the count dropped.

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
        """The plan the planner starts from: one change per pair, kept and not yet decided, its evidence copied in.

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
                    reason=NOT_YET_DECIDED,
                    check=check,
                    cases=[report for report in cases if report.step_id in reported],
                )
            )
        return MaintenancePlan(changes=changes, left_for_next_run=list(consumers.left_for_next_run))

    async def land_changes(self, plan: MaintenancePlan) -> LandingReport:
        """Land every planned change: save each adoption on live trial, decline each kept copy and flag each flagged pair.

        An adoption copies the step from the library, applies the change's further edit operations, validates the
        candidate and saves it as the consumer's next version, saved by step maintenance, with a live trial marker naming
        the version it replaces. It then resolves the open consumer flag of that pair. A kept pair writes a decline into
        the consumer's record, and a flagged pair writes a `consumer_flag` page with the contract check and the
        planner's reason. Changes land one at a time, so a second change to one consumer builds on the first. A change
        to a consumer someone saved past since the listing is refused. Every writable change lands before any refusal is
        raised.

        Args:
            plan: The plan the judge approved.

        Returns:
            What each change did, when nothing was refused.

        Raises:
            LandingRefusedError: One or more changes were refused. Every other change landed, and the error's report
                holds every outcome.
        """
        saved: dict[str, str] = {}
        changes = [await self._land(change, saved) for change in plan.changes]
        report = LandingReport(changes=changes, left_for_next_run=list(plan.left_for_next_run))
        if any(change.outcome == "refused" for change in changes):
            raise LandingRefusedError(report)
        return report

    async def report_changes(self, landed: LandingReport) -> str:
        """Tell the owner in one notice what changed in which workflow and why, and the consumers left for the next run.

        A notice that fails is said in the answer and never fails the run. A landing that changed nothing tells nobody.

        Args:
            landed: What the landing did.

        Returns:
            The notice's text and whether it went out.
        """
        lines = [change_line(change) for change in landed.changes]
        if landed.left_for_next_run:
            lines.append(f"Left for the next run, past the cap of {PAIR_CAP} pairs: {', '.join(landed.left_for_next_run)}.")
        if not lines:
            return "Step maintenance changed nothing, so nobody was told."
        message = "\n".join(lines)
        title = f"Step maintenance landed {', '.join(sorted({change.step_id for change in landed.changes}))}"
        try:
            delivered = await self._notifier.notify(title, message)
        except Exception as exc:  # noqa: BLE001 (a notice that fails is said in the answer, never raised into the run)
            return f"{message}\nThe notice to the owner failed: {exc}"
        return f"{message}\n{'The owner was told.' if delivered else 'The notice to the owner was not delivered.'}"

    async def select_disagreements(self, landed: LandingReport, consumers: ConsumerList) -> list[RepositoryChange]:
        """One repository change to draft per changed step that some consumers adopted and others kept or flagged.

        Each side holds every consumer with its outcome, the planner's reason and what the consumer is for. The drafted
        fields are empty.

        Args:
            landed: What the landing did.
            consumers: The pairs `list_consumers` answered.

        Returns:
            The repository changes, empty when no step's consumers split.
        """
        pairs = {(pair.workflow_id, pair.step_id): pair for pair in consumers.pairs}
        sides: dict[str, list[tuple[ConsumerPair, ConsumerSide]]] = {}
        for change in landed.changes:
            pair = pairs.get((change.workflow_id, change.step_id))
            outcome = change.outcome
            if pair is None or outcome == "refused":
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
                selected.append(RepositoryChange(step=held[0][0].step, step_id=step_id, adopted=adopted, kept=kept))
        return selected

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
        """Build the consumer version an adoption would save and run its contract check on it, saving nothing.

        Call it on every change you plan to adopt, with the further edit operations it needs, before you answer. The
        landing builds the same version and refuses one whose check fails.

        Args:
            workflow_id: The consumer's workflow version id, `wf.<name>.v<version>`, as the pair names it.
            step_id: The changed step version's id, `st.<name>.v<version>`, as the pair names it.
            extra_ops: Edit operations applied after the copy is replaced by the step, in the form an adaptation takes,
                such as `{"op": "rewire", "id": "<node>", "input": "<input>", "to": {"node": "<other node>"}}` or
                `{"op": "add_parameter", "name": "<name>", "with": {"description": "...", "json_schema": {...}}}`.
                Leave it empty for a plain adoption.

        Returns:
            `passes` and what was checked, or `fails` and each validation issue, or why nothing could be checked.
        """
        try:
            check = await self._check(workflow_id, step_id, extra_ops)
        except LibraryError as exc:
            return f"cannot check: {exc}. Use the workflow id and the step id the pair names."
        edits = f" and {len(extra_ops)} further edit operations" if extra_ops else ""
        if check.passed:
            return f"passes: {workflow_id} validates with {step_id} in place of its copy{edits}."
        issues = "\n".join(check.issues)
        return (
            f"fails: {workflow_id} does not validate with {step_id} in place of its copy{edits}:\n{issues}\n"
            f"Add edit operations that fix each issue and check again, or plan keep or flag for this consumer."
        )

    def _registered(self) -> StepRegistry:
        if self._registry is None:
            msg = "the step maintenance steps are not registered on a registry, so no candidate can be validated. Call register first"
            raise RuntimeError(msg)
        return self._registry

    async def _changed(self, changed_step_ids: str) -> _Changed:
        """Each named step at its newest saved version, by step name, refused when an id names no saved version."""
        saved_of: dict[str, tuple[StepVersion, ...]] = {}
        for step_id in changed_ids(changed_step_ids):
            name = (await self._library.fetch_step(step_id)).name
            saved = saved_of.get(name) or await self._library.step_versions(name)
            if not any(each.step_id == step_id for each in saved):
                msg = f"{step_id} is not a saved version of the reusable step {name}"
                raise LibraryError(msg)
            saved_of[name] = saved
        return tuple((max(saved_of[name], key=lambda each: each.version), saved_of[name]) for name in sorted(saved_of))

    async def _candidates(self, changed: _Changed) -> tuple[list[_Candidate], list[_Candidate], int]:
        """The behind or diverged consumers within the cap, those past it, and how many current or declined ones were dropped."""
        candidates: list[_Candidate] = []
        dropped = 0
        for version, saved in changed:
            by_version = {each.version: each for each in saved}
            for card in await self._library.consumers_of(version.name):
                entry = card.entry
                if entry.declined == version.step_hash or (entry.version is not None and entry.version >= version.version):
                    dropped += 1
                    continue
                held_version = entry.version if entry.version is not None else entry.diverged_from
                held = by_version.get(held_version) if held_version is not None else None
                if held is None:
                    msg = f"{card.workflow_id} records {version.name} v{held_version}, which no saved version of {version.name} is"
                    raise LibraryError(msg)
                candidates.append(_Candidate(version=version, card=card, held=held))
        candidates.sort(key=lambda candidate: (candidate.version.name, candidate.card.name, candidate.card.version))
        return candidates[:PAIR_CAP], candidates[PAIR_CAP:], dropped

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
            candidate, _ = _adopted(workflow, step, extra_ops, evidence="")
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

    async def _land(self, change: PlannedChange, saved: dict[str, str]) -> LandedChange:
        """Land one change over the consumer version this landing last saved for it, or record why it was refused."""
        base = saved.get(change.workflow_id, change.workflow_id)
        try:
            step = await self._library.fetch_step(change.step_id)
            await self._refuse_a_stale_pair(change, base, step)
            workflow = await self._library.fetch_workflow(base)
            if change.disposition == "adopt":
                landed = await self._adopt(change, base, workflow, step)
                if landed.outcome == "adopted":
                    saved[change.workflow_id] = landed.saved_id
                return landed
            if change.disposition == "keep":
                await self._library.record_decline(base, step=step.name, step_hash=step_content_address(step), because="kept")
                return _landed(change, "kept", change.reason)
            return await self._flag(change, base, workflow, step)
        except _REFUSALS as exc:
            return _landed(change, "refused", str(exc))

    async def _refuse_a_stale_pair(self, change: PlannedChange, base: str, step: InlineStep) -> None:
        """Refuse a change whose step version is not the newest of its name, which the consumer declines, or whose consumer someone saved past."""
        newest_step = max(await self._library.step_versions(step.name), key=lambda saved: saved.version)
        if newest_step.step_id != change.step_id:
            msg = f"{change.step_id} is not the newest version of {step.name}, {newest_step.step_id} is. The next run plans {newest_step.step_id}"
            raise LibraryError(msg)
        address = step_content_address(step)
        if any(entry.step == step.name and entry.declined == address for entry in await self._library.fetch_consumer_record(base)):
            msg = f"{base} declines {change.step_id}, so step maintenance leaves the pair alone until a newer version arrives"
            raise LibraryError(msg)
        newest = await self._library.current(base)
        if newest != base:
            msg = f"{newest} was saved after {base}, so this change no longer applies to the newest version. The next run plans it again"
            raise LibraryError(msg)

    async def _adopt(self, change: PlannedChange, base: str, workflow: Workflow, step: InlineStep) -> LandedChange:
        """Save the consumer's next version with `step` on live trial, then resolve the pair's open consumer flag."""
        run = self._current_run()
        ran_in = f" in the run {run}" if run else ""
        evidence = f"Step maintenance adopted {change.step_id} in {base}{ran_in}. {change.reason}"
        child, lineage = _adopted(workflow, step, change.extra_ops, evidence=evidence)
        expected = {**_inline_addresses(workflow), step.name: step_content_address(step)}
        if (held := _inline_addresses(child)) != expected:
            changed = sorted(name for name in expected.keys() | held.keys() if expected.get(name) != held.get(name))
            msg = (
                f"the further edit operations change inline steps other than {step.name}: {', '.join(changed)}. An adoption "
                f"changes only the step it adopts, and its further edits may only be rewire, add_parameter and set_output"
            )
            raise AdaptationRefusedError(msg)
        registry = self._registered()
        verify_savable(child, registry)
        try:
            stamp = fingerprint(child, registry, environment=self._environment)
        except LookupError as exc:
            return _landed(change, "refused", f"the next version of {base} names an agent this host cannot resolve: {exc}")
        card = await self._library.fetch_card(base)
        if card is None:
            msg = f"{base} has no card to copy its title, summary and applicability from"
            raise LibraryError(msg)
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
            live_trial=LiveTrialMarker(previous=base, step=step.name, step_hash=step_content_address(step)),
        )
        notes = await self._resolve_consumer_flag(workflow.name, step.name, saved_id, change.step_id)
        return _landed(change, "adopted", change.reason, saved_id=saved_id, notes=notes)

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


def _adopted(workflow: Workflow, step: InlineStep, extra_ops: Sequence[Mapping[str, JsonValue]], *, evidence: str) -> tuple[Workflow, Lineage]:
    """The consumer's next version with its copy replaced by `step`, then `extra_ops` applied, refused as `AdaptationRefusedError`."""
    replace: dict[str, JsonValue] = {"op": "replace_inline_step", "name": step.name, "with": step.model_dump(mode="json")}
    try:
        return adapt(workflow, ops=(replace, *extra_ops), evidence=evidence, predictions=(Prediction(claim="completes"),))
    except AdaptationRefusedError:
        raise
    except ValueError as exc:
        msg = f"an edit operation does not apply: {exc}"
        raise AdaptationRefusedError(msg) from exc


def _inline_addresses(workflow: Workflow) -> dict[str, str]:
    """Each inline step's content address, by step name."""
    return {inline.name: step_content_address(inline) for inline in workflow.inline}


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


def _check_text(check: ContractCheck) -> str:
    """A contract check as a flag page's evidence says it."""
    if check.passed:
        return f"The contract check passed: {check.workflow_id} validates with {check.step_id} in place of its copy."
    return f"The contract check failed for {check.workflow_id} with {check.step_id}:\n\n{fenced(chr(10).join(check.issues))}"


def _sides_text(sides: Sequence[ConsumerSide]) -> str:
    """Each consumer on one side of a repository change with its outcome and the planner's reason, fenced."""
    return fenced("\n".join(f"{side.workflow_id} ({side.outcome}): {side.reason}" for side in sides))
