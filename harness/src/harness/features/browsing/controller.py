"""The browsing routes: the registered steps, the library listing, one workflow's page, card edits, archive and restore.

The three write routes change only the card a person reads about a version that already exists,
and whether that version is archived. They never change the workflow a run executes.
"""

from __future__ import annotations

import asyncio

from pathlib import Path

import yaml

from core.ai.workflows.contracts import (
    EvidenceLog,
    LibraryError,
    Lineage,
    PredictionOutcome,
    PresentationRefusedError,
    RunEvidence,
    SavedWorkflow,
    Workflow,
    carried_trust,
    check_birth,
    contract_of,
    drift,
    fingerprint,
    grade,
    prediction_record,
    prepare,
    spec_identity,
    trust_level,
)
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import JsonValue, ValidationError
from wireup.integration.fastapi import WireupRoute

from harness.adapters.contracts import environment_stamps
from harness.domain.contracts import DraftReadiness, card_readiness
from harness.features.browsing import service
from harness.features.browsing.api_models import WorkflowCardPatch
from harness.features.browsing.exceptions import CardRefusedError
from harness.features.browsing.service import saved_card
from harness.features.running.contracts import RunArchiveQueries, SpendLogQueries, workflow_spend
from harness.features.sessions.contracts import Sessions
from harness.rendering import graph_payload, prediction_payload, signature_of, type_name
from harness.session import Runtime
from harness.settings import SelfAuthoringSettings, custom_root

type _JsonObject = dict[str, JsonValue]
type _Trust = dict[str, str | int | float | list[str] | None]


def _shape_counts(nodes: list[_JsonObject]) -> dict[str, int]:
    """Count a built graph's steps, person gates and agent gates, skipping its parameter nodes."""
    steps = person_gates = agent_gates = 0
    for node in nodes:
        node_type = node["kind"]
        if node_type == "param":
            continue
        if node_type == "gate":
            person_gates += 1
        elif node_type in {"judge", "orchestrator-gate"}:
            agent_gates += 1
        else:
            steps += 1
    return {"steps": steps, "person_gates": person_gates, "agent_gates": agent_gates}


class BrowsingController:
    """The browsing routes, under `/api`."""

    router = APIRouter(prefix="/api", tags=["browsing"], route_class=WireupRoute)

    def __init__(
        self, sessions: Sessions, archive: RunArchiveQueries, spend: SpendLogQueries, evidence: EvidenceLog, self_authoring: SelfAuthoringSettings
    ) -> None:
        """Hold the sessions, the run archive, the spend log, the evidence log and the self-authoring settings.

        Args:
            sessions: The sessions this process holds. Every route reads through the default session.
            archive: The run archive each workflow's newest ended run is read from.
            spend: The spend log each workflow's recent spend is read from.
            evidence: The evidence log every trust level and graded claim is read from.
            self_authoring: The settings whose custom root the environment stamps are taken over.
        """
        self._sessions = sessions
        self._archive = archive
        self._spend = spend
        self._evidence = evidence
        self._self_authoring = self_authoring

    @router.get("/steps")
    async def steps(self) -> JSONResponse:
        """The steps this app itself registers, each with its real signature and tools."""
        session = self._sessions.default
        payload: list[_JsonObject] = []
        for name in session.registry.names:
            implementation = session.registry.get(name)
            if implementation is None:  # pragma: no cover
                continue
            contract = contract_of(implementation)
            payload.append(
                {
                    "name": contract.name,
                    "description": contract.description,
                    "signature": signature_of(contract),
                    "output": type_name(contract.output),
                    "tools": list(contract.tools),
                }
            )
        return JSONResponse(payload)

    @router.get("/library")
    async def library_listing(  # noqa: C901 (one gathered closure per fact a card carries)
        self, task: str | None = None, *, archived: bool = False
    ) -> JSONResponse:
        """What the library holds, and for each workflow whether this app could run it.

        With `archived`, the listing shows the archived workflows instead, in the same shape. Without
        `task`, it shows the whole library, or the matches for the word "workflow" when the library
        cannot list itself. With `task`, it shows the workflows that search matches.
        """
        session = self._sessions.default
        if archived:
            found = await session.library.archived(limit=50)
        elif task is not None:
            found = await session.library.search(task, limit=12)
        else:
            catalog = getattr(session.library, "catalog", None)
            found = await catalog(limit=50) if catalog is not None else await session.library.search("workflow", limit=12)
        evidence = self._evidence

        async def trust(workflow_id: str) -> tuple[str, _Trust]:
            """The trust the workflow page reports for this workflow."""
            try:
                workflow: Workflow | None = await session.library.fetch_workflow(workflow_id)
            except (LibraryError, ValidationError):
                workflow = None
            return workflow_id, await _trust(evidence, workflow, workflow_id)

        async def fit(workflow_id: str) -> tuple[str, _JsonObject | None]:
            try:
                workflow = await session.library.fetch_workflow(workflow_id)
            except (LibraryError, ValidationError):
                return workflow_id, None
            needs: list[str] = sorted({node.work for node in workflow.nodes if node.kind == "run"})
            # An inline step is never missing, because the workflow holds it.
            own: set[str] = {inline_step.name for inline_step in workflow.inline}
            fitting: _JsonObject = {
                "needs": [step for step in needs if step not in own],
                "missing": [step for step in needs if step not in own and session.registry.get(step) is None],
                "brings": [*sorted(own)],
                # The version as the library reports it, never read off the id.
                "version": workflow.version,
            }
            return workflow_id, fitting

        async def shape(workflow_id: str) -> tuple[str, dict[str, int]]:
            """Node counts from the graph the workflow page builds, or zeros when the workflow cannot be read."""
            try:
                workflow = await session.library.fetch_workflow(workflow_id)
            except (LibraryError, ValidationError):
                return workflow_id, {"steps": 0, "person_gates": 0, "agent_gates": 0}
            ready = prepare(workflow, session.registry)
            return workflow_id, _shape_counts(graph_payload(workflow, ready.registry, {})["nodes"])

        async def runs(workflow_id: str) -> tuple[str, _JsonObject]:
            """This workflow's newest ended run, from the run archive."""
            records = self._archive.list_for(workflow_id, limit=1)
            newest = records[0] if records else None
            return workflow_id, {
                "last": None if newest is None else {"outcome": newest.outcome, "started_at": newest.started_at, "ended_at": newest.ended_at},
            }

        async def spend(workflow_id: str) -> tuple[str, dict[str, object]]:
            """What this workflow's newest runs cost together, from the spend log."""
            return workflow_id, await workflow_spend(self._spend, workflow_id)

        fit_by_id = dict(await asyncio.gather(*(fit(w.id) for w in found)))
        trust_by_id = dict(await asyncio.gather(*(trust(w.id) for w in found)))
        shape_by_id = dict(await asyncio.gather(*(shape(w.id) for w in found)))
        runs_by_id = dict(await asyncio.gather(*(runs(w.id) for w in found)))
        spend_by_id = dict(await asyncio.gather(*(spend(w.id) for w in found)))
        return JSONResponse(
            [
                {
                    "id": w.id,
                    "title": w.title,
                    "summary": w.summary,
                    "provisional": w.provisional,
                    "score": w.score,
                    "archived_at": w.archived_at.isoformat() if w.archived_at else None,
                    "presentation": _presentation(w),
                    "intent": _intent(w),
                    "trust": trust_by_id[w.id],
                    "shape": shape_by_id[w.id],
                    "runs": runs_by_id[w.id],
                    "spend": spend_by_id[w.id],
                    **(fit_by_id.get(w.id) or {"needs": [], "missing": None, "brings": [], "version": None}),
                }
                for w in found
            ]
        )

    @router.get("/workflow/{workflow_id}")
    async def stored_workflow(self, workflow_id: str) -> JSONResponse:
        """Open one saved workflow: its graph, its card, its trust, and whether this app could run it.

        Whether the workflow fits this app and whether it is well formed are answered separately.
        """
        session = self._sessions.default
        try:
            workflow = await session.library.fetch_workflow(workflow_id)
        except LibraryError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=404)
        # The report and the graph share one preparation. A second one would mint different step classes.
        ready = prepare(workflow, session.registry)
        evidence = self._evidence
        trust = await _trust(evidence, workflow, workflow_id)
        staleness = await _staleness(session, workflow, workflow_id, custom_root(self._self_authoring))
        lineage = await _lineage(session, workflow_id)
        claims = await _claims(evidence, workflow, workflow_id, lineage)
        birth = await _changes_since_first_run(evidence, session, workflow, workflow_id)
        inline_names = {inline_step.name for inline_step in workflow.inline}
        missing = sorted({node.work for node in workflow.nodes if node.kind == "run" and ready.registry.get(node.work) is None})
        card = await saved_card(session, workflow_id, workflow)
        return JSONResponse(
            {
                "ok": True,
                "id": workflow_id,
                "name": workflow.name,
                "version": workflow.version,
                "description": workflow.description,
                # The card a person reads: title, summary, applicability, and whether it is still provisional.
                "title": card.title,
                "summary": card.summary,
                "applicability": card.applicability,
                "provisional": card.provisional,
                "archived_at": card.archived_at.isoformat() if card.archived_at else None,
                # The author's own words about the workflow, or `null` when the library kept none.
                # Nothing is composed in their place.
                "presentation": _presentation(card),
                # What the person who wanted this workflow asked for, or `null` when the library kept
                # none. It is never read off the workflow.
                "intent": _intent(card),
                # The same reading the card route refuses on, so a form and a refusal name the same missing words.
                "readiness": card_readiness(
                    workflow, ready, title=card.title, summary=card.summary, presentation=card.presentation
                ).model_dump(mode="json"),
                "graph": graph_payload(workflow, ready.registry, {}),
                "issues": [f"[{issue.kind}] {issue.where}: {issue.detail}" for issue in ready.report.issues],
                "fits": not missing,
                "missing": missing,
                "brings": sorted(inline_names),
                "spec_yaml": yaml.safe_dump(workflow.model_dump(mode="json"), sort_keys=False, allow_unicode=True, width=1000),
                # `basis` distinguishes `unverified` on `none` (nothing ever ran it) from `unverified`
                # on `address-only` (evidence here cannot be tied to this workflow).
                "trust": trust,
                # `[]` is checked and fresh, and a list names what moved. `null` is unknown (the
                # workflow carries no stamp), which is a weaker claim than fresh.
                "staleness": staleness,
                # The diff this workflow was adapted by, or `null` when it is no adaptation. Its
                # `parent_id` is `null` when the library no longer holds the parent.
                "lineage": await _with_parent(session, lineage),
                # `[]` means it still does what its first successful run did, and a list says what
                # moved. `null` means nothing has ever run it, which is not a failed check.
                "birth": birth,
                # Which nodes of a child are exactly the nodes its parent's run recorded. `null` when
                # there is no parent or no completed run of it.
                "carried": await _inherited_nodes(evidence, session, lineage),
                # `null` when the adaptation claimed nothing, so there is nothing to grade.
                "predictions": prediction_payload(claims) if claims is not None else None,
                "prediction_record": _scoreboard(claims),
                "spend": await workflow_spend(self._spend, workflow_id),
            }
        )

    @router.patch("/workflow/{workflow_id}/card", name="redescribe_workflow")
    async def redescribe(self, workflow_id: str, body: WorkflowCardPatch) -> JSONResponse:
        """Rewrite one workflow's card: its title, summary, applicability and the author's own words. The workflow is not changed."""
        return await redescribe_workflow(self._sessions.default, workflow_id, body)

    @router.post("/workflow/{workflow_id}/archive", name="archive_workflow")
    async def archive(self, workflow_id: str) -> JSONResponse:
        """Take one workflow out of the library. It leaves search and the listing, and its runs and versions stay."""
        return await archive_workflow(self._sessions.default, workflow_id)

    @router.post("/workflow/{workflow_id}/restore", name="restore_workflow")
    async def restore(self, workflow_id: str) -> JSONResponse:
        """Put one archived workflow back in the library, where search and the listing see it again."""
        return await restore_workflow(self._sessions.default, workflow_id)


async def redescribe_workflow(session: Runtime, workflow_id: str, body: WorkflowCardPatch) -> JSONResponse:
    """Rewrite one workflow's card: its title, summary, applicability and the author's own words.

    The patch is merged onto the card the library holds, and the merged card is read for readiness.
    A field this patch carries that fails refuses the whole write. A field it leaves alone that fails
    is reported, and the write goes through. The workflow itself is never changed.

    Args:
        session: The session whose library holds the workflow.
        workflow_id: The library id of the workflow.
        body: The fields to change. An absent field keeps what the library holds.

    Returns:
        The refreshed card and its readiness reading. A 400 names what was refused, and a 404 names a
        workflow the library does not hold.
    """
    try:
        rewritten = await service.redescribe_workflow(session, workflow_id, body)
    except CardRefusedError as exc:
        payload = {"ok": False, "message": str(exc), "readiness": exc.readiness.model_dump(mode="json")}
        return JSONResponse(payload, status_code=400)
    except PresentationRefusedError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    except LibraryError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=404)
    return JSONResponse(_card_payload(workflow_id, rewritten.card, rewritten.readiness))


async def archive_workflow(session: Runtime, workflow_id: str) -> JSONResponse:
    """Take one workflow out of the library. It leaves search and the listing, and its runs and versions stay.

    The agent is offered this only behind the person's approval.

    Args:
        session: The session whose library holds the workflow.
        workflow_id: The library id of the workflow.

    Returns:
        The refreshed card, or a 404 that names a workflow the library does not hold.
    """
    try:
        card = await service.archive_workflow(session, workflow_id)
    except LibraryError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=404)
    return JSONResponse(_card_payload(workflow_id, card))


async def restore_workflow(session: Runtime, workflow_id: str) -> JSONResponse:
    """Put one archived workflow back in the library, where search and the listing see it again.

    The agent is offered this only behind the person's approval.

    Args:
        session: The session whose library holds the workflow.
        workflow_id: The library id of the workflow.

    Returns:
        The refreshed card, or a 404 that names a workflow the library does not hold.
    """
    try:
        card = await service.restore_workflow(session, workflow_id)
    except LibraryError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=404)
    return JSONResponse(_card_payload(workflow_id, card))


def _card_payload(workflow_id: str, card: SavedWorkflow, readiness: DraftReadiness | None = None) -> _JsonObject:
    """The refreshed card, with the readiness reading when one was taken and `null` when none was."""
    return {
        "ok": True,
        "id": workflow_id,
        "title": card.title,
        "summary": card.summary,
        "applicability": card.applicability,
        "provisional": card.provisional,
        "archived_at": card.archived_at.isoformat() if card.archived_at else None,
        "presentation": _presentation(card),
        "intent": _intent(card),
        "readiness": readiness.model_dump(mode="json") if readiness is not None else None,
    }


def _presentation(card: SavedWorkflow) -> _JsonObject | None:
    """The author's words this card carries, as data, or `null` when it carries none."""
    return None if card.presentation is None else card.presentation.model_dump(mode="json")


def _intent(card: SavedWorkflow) -> _JsonObject | None:
    """What this workflow was asked for, as data, or `null` when the library kept no intent."""
    return None if card.intent is None else card.intent.model_dump(mode="json")


async def _trust(evidence: EvidenceLog, workflow: Workflow | None, workflow_id: str) -> _Trust:
    """What this exact workflow has earned by its content address, or nothing when it could not be read."""
    if workflow is None:
        record, basis, unconfirmed = trust_level(()), "none", 0
    else:
        history = await evidence.history_for(spec_hash=spec_identity(workflow), workflow_id=workflow_id)
        record, basis, unconfirmed = trust_level(history.matched), history.basis, len(history.legacy)
    return {
        "level": record.level,
        "runs_with_evidence": record.runs_with_evidence,
        "failed_runs": record.failed_runs,
        "approved_by": list(record.approved_by),
        "basis": basis,
        "unconfirmed": unconfirmed,
        "structural_grades": record.structural_grades,
        "structural_mean": record.structural_mean,
        "structural_last": record.structural_last,
    }


async def _claims(
    evidence: EvidenceLog, workflow: Workflow, workflow_id: str, lineage: Lineage | None
) -> tuple[PredictionOutcome, ...] | None:
    """What this version's adaptation claimed, graded against this version's own runs."""
    if lineage is None or not lineage.predictions:
        return None
    events = await evidence.events_for(workflow_id)
    return grade(lineage, events, version=workflow.version)


def _scoreboard(claims: tuple[PredictionOutcome, ...] | None) -> _JsonObject | None:
    """The graded claims folded into a score for the adapter that made them."""
    if claims is None:
        return None
    record = prediction_record(claims)
    return {**record.model_dump(mode="json"), "hit_rate": record.hit_rate}


async def _staleness(session: Runtime, workflow: Workflow, workflow_id: str, custom: Path) -> list[str] | None:
    """What moved in the environment since this workflow was stamped, or `None` when it carries no stamp."""
    fetch = getattr(session.library, "fetch_fingerprint", None)
    if fetch is None:
        return None
    try:
        recorded = await fetch(workflow_id)
    except (LibraryError, KeyError, ValidationError):
        return None
    if recorded is None:
        return None
    return list(drift(recorded, fingerprint(workflow, session.registry, environment=environment_stamps(custom))))


async def _lineage(session: Runtime, workflow_id: str) -> Lineage | None:
    """The typed diff that produced this workflow, if it is an adaptation of something."""
    fetch = getattr(session.library, "fetch_lineage", None)
    if fetch is None:
        return None
    try:
        return await fetch(workflow_id)
    except (LibraryError, KeyError, ValidationError):
        return None


async def _with_parent(session: Runtime, lineage: Lineage | None) -> _JsonObject | None:
    """The diff plus the parent's library id when the library still resolves it, or `None` without a diff."""
    if lineage is None:
        return None
    return {**lineage.model_dump(mode="json"), "parent_id": await _parent_id(session, lineage)}


async def _inherited_nodes(evidence: EvidenceLog, session: Runtime, lineage: Lineage | None) -> list[str] | None:
    """Nodes this child inherits its parent's evidence for, read from the parent's first completed run."""
    if lineage is None:
        return None
    parent_id = await _parent_id(session, lineage)
    if parent_id is None:
        return None
    events = await evidence.events_for(parent_id)
    born = next(
        (
            event
            for event in events
            if isinstance(event, RunEvidence)
            and event.outcome == "completed"
            and event.produced
            and event.workflow_version == lineage.parent_version
        ),
        None,
    )
    return None if born is None else sorted(carried_trust(born, lineage))


async def _parent_id(session: Runtime, lineage: Lineage) -> str | None:
    """Where the library keeps the version this workflow came from, if it still keeps it at all."""
    locate = getattr(session.library, "locate", None)
    if locate is None:
        return None
    try:
        return await locate(lineage.parent_name, lineage.parent_version)
    except (LibraryError, KeyError, ValidationError):
        return None


async def _changes_since_first_run(evidence: EvidenceLog, session: Runtime, workflow: Workflow, workflow_id: str) -> list[str] | None:
    """What changed in this workflow since its first completed run, or `None` when nothing has run it."""
    events = await evidence.events_for(workflow_id)
    born = next((e for e in events if isinstance(e, RunEvidence) and e.outcome == "completed" and e.produced), None)
    if born is None:
        return None
    return list(check_birth(workflow, session.registry, born))
