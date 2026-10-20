"""Path B: fetch the saved artifact instead of reasoning from scratch, and grade what actually ran.

This is the reuse path the baseline (`proof/baseline.py`) is measured against: same knowledge base, same
guard, same withheld question pages, same `RecordingModel` meter -- the only thing different is
that this path starts from a stored artifact and adapts nothing, rather than starting from nothing.

Three honesty rules hold no matter what:

* `RefusingApproval.review` RAISES on any gate. A gate reached in a headless proof run is an
  error, never an auto-click -- fabricating a human's "yes" would poison the ledger with evidence
  nobody gave.
* A failed run is recorded as a failed `RunEvidence` and a failed `RunRecord` row -- never
  retried. Reaching a gate counts as an ordinary failure here, exactly like any other exception.
* A `Grade` is written only for a completed, uncontaminated run, and its `graded_run_at` links to
  the `RunEvidence` it grades -- a score is never filed loose, disconnected from what produced it.

Before any of that: `prepare(spec, registry)` and the spec's unsatisfied parameters are checked
*outside* the no-retry boundary. A spec that does not check out against this registry, or that
wants more or fewer than exactly the one parameter this runner fills, is not a failed run -- it
never got a chance to run at all, and this aborts loudly rather than filing a row for it.
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback

from pathlib import Path
from typing import TYPE_CHECKING

from core.ai.workflows.contracts import (
    EvidenceLog,
    FromNode,
    Grade,
    LibraryCollection,
    Outcome,
    RunEvidence,
    WorkflowState,
    approved_gates,
    build_graph,
    drift,
    fingerprint,
    prepare,
    shapes_of,
    spec_identity,
    unsatisfied_parameters,
)
from core.observability import get_logger
from core.settings import read_infrastructure

from harness.adapters.contracts import (
    KnowledgeBaseReading,
    ModelFactory,
    ModelTiers,
    environment_stamps,
    knowledge_base_capability,
    model,
    query_embedder,
    sandbox_pool,
    store_connection,
    studio_http_client,
)
from harness.adapters.wiring import model_list
from harness.composition import build_environment
from harness.features.running.contracts import RunRecords
from harness.session import NoSessionLog
from harness.settings import HarnessSettings, custom_root, settings_file
from proof.baseline import _known_ids
from proof.groundtruth import WITHHELD, load_questions, unresolved
from proof.instrument import RecordingModel, ToolTrace
from proof.knowledge_base import GuardedKnowledgeBase
from proof.records import RunRecord, build_record, read_only_ingest, write_record

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.ai.workflows.contracts import SavedWorkflow, StepRegistry

    from proof.groundtruth import ResearchQuestion

_logger = get_logger(__name__)

_NOT_CHECKED = "The reuse proof runs the graph directly and does not check its outcome."
"""Why a reuse run's checked outcome is unknown."""

OUT = Path(__file__).parent / "out"
DISTILLATION = OUT / "distillation.json"


class RefusingApproval:
    """A gate reached headless is an error, never an auto-click -- fabricated human evidence poisons the ledger."""

    async def review(self, gate_id: str, value: object, *, note: str = "") -> bool:
        """Refuse to answer for a human. Reaching this at all means the run fails, honestly."""
        del value, note
        msg = f"gate {gate_id!r} reached in a headless run; the proof harness will not fabricate approval"
        raise RuntimeError(msg)


def hit_rank(hits: Sequence[SavedWorkflow], workflow_id: str) -> int | None:
    """This artifact's one-based rank among `hits`, or `None` if the search never surfaced it."""
    return next((position for position, hit in enumerate(hits, start=1) if hit.id == workflow_id), None)


async def run_reuse(
    question: ResearchQuestion,
    *,
    store: LibraryCollection,
    registry: StepRegistry,
    trace: ToolTrace,
    recording: RecordingModel,
    workflow_id: str,
    ledger: EvidenceLog,
    custom: Path,
) -> RunRecord:
    """Search, fetch, check drift, run the artifact headless, attest, and grade what actually ran.

    No retries: a run that raises -- including `RefusingApproval` refusing a gate -- is caught,
    logged, and recorded as a failed `RunEvidence` and a failed row, never re-run to green. A
    `Grade` is written only when the run completed clean of the withheld-question contamination
    check, and its `graded_run_at` is the `RunEvidence` it grades.

    Raises:
        RuntimeError: If `workflow_id` does not check out against `registry` -- an abort, not a
            failed row, because a spec that never validated never got a chance to run.
        ValueError: If the spec wants more or fewer than exactly the one parameter this runner
            fills (the question's text).
    """
    hits = await store.search(question.question)
    rank = hit_rank(hits, workflow_id)
    spec = await store.fetch_workflow(workflow_id)
    recorded = await store.fetch_fingerprint(workflow_id)
    prepared = prepare(spec, registry)
    if not prepared.report.ok:
        detail = "; ".join(f"{issue.where}: {issue.detail}" for issue in prepared.report.issues)
        msg = f"{workflow_id} does not check out against this registry: {detail}"
        raise RuntimeError(msg)
    moved = tuple(drift(recorded, fingerprint(spec, prepared.registry, environment=environment_stamps(custom)))) if recorded else None
    missing = unsatisfied_parameters(spec, [])
    if len(missing) != 1:
        msg = f"{workflow_id} wants parameters {missing!r}; the runner fills exactly one"
        raise ValueError(msg)
    state = WorkflowState(params={next(iter(missing)): question.question})
    started, failed, failure = time.monotonic(), False, None
    try:
        # The BASE registry, not prepared.registry: build_graph prepares internally
        # (validate-by-construction), and a registry that already includes the artifact's
        # brought steps makes that second pass refuse every one of them as "already
        # registered". Exactly how the app's own launch calls it.
        graph = build_graph(spec, registry, approval=RefusingApproval())
        await graph.run(state=state)
    except Exception as exc:  # a failed run is a row, never a retry-to-green -- see module docstring
        _logger.exception("reuse run of {question} failed", question=question.id)
        traceback.print_exc()  # logfire may not be configured; the sweep must never fail silently
        failed, failure = True, f"{type(exc).__name__}: {exc}"
    seconds = time.monotonic() - started
    answer = ""
    if not failed and isinstance(spec.output, FromNode):
        produced = state.values.get(spec.output.node)
        answer = produced if isinstance(produced, str) else json.dumps(produced, default=str)
    spec_hash = spec_identity(spec)
    evidence = RunEvidence(
        workflow_id=workflow_id,
        spec_hash=spec_hash,
        workflow_version=spec.version,
        outcome="failed" if failed else "completed",
        result=Outcome(status="unknown", summary=_NOT_CHECKED),
        gates_approved=approved_gates(spec, state.values),
        gates_auto=(),
        gates_orchestrated=(),
        params={name: str(value) for name, value in state.params.items()},
        produced=shapes_of(state.values),
        runner="proof:reuse",
    )
    await ledger.append(evidence)
    record = build_record(
        question,
        path="reuse",
        trace=trace,
        model=recording,
        seconds=seconds,
        failed=failed,
        answer=answer,
        failure=failure,
        workflow_id=workflow_id,
        hit_rank=rank,
        drift=moved,
    )
    if not failed and not record.contaminated:
        await ledger.append(
            Grade(
                workflow_id=workflow_id,
                spec_hash=spec_hash,
                workflow_version=spec.version,
                judge_kind="structural",
                judge="recall/groundtruth-research-v1",
                measure="evidence-recall",
                score=record.recall,
                graded_run_at=evidence.at,
            )
        )
    return record


async def main() -> None:
    """Run every question in the knowledge base's groundtruth set through the saved artifact, and write its record."""
    workflow_id = json.loads(DISTILLATION.read_text(encoding="utf-8"))["workflow_id"]
    settings = HarnessSettings()
    state = settings.state
    custom = custom_root(settings.self_authoring)
    infrastructure = read_infrastructure(settings_file())
    # The app's own builder, so the ids a tier entry is checked against here are the ids it is checked against there.
    tiers = ModelTiers(state=state, models=model_list(infrastructure), build=ModelFactory(infrastructure, settings.models))
    connection = await store_connection()
    try:
        embed = query_embedder() if settings.store.hybrid_search else None
        store = LibraryCollection(connection, read_only_ingest, embed=embed)
        records = RunRecords.under(state, retention_days=settings.run.archive_days)
        questions = load_questions()
        cited = frozenset().union(*(question.sources for question in questions))
        # In-process, like the baseline sweep -- the two paths must read the same knowledge base the
        # same way, or the comparison the proof rests on is comparing doors, not workflows.
        async with (
            sandbox_pool() as sandbox,
            studio_http_client(settings.studio_api) as studio,
            knowledge_base_capability(studio, reading=KnowledgeBaseReading(guidance=settings.knowledge_base_guidance.guidance)) as engram,
            GuardedKnowledgeBase(inner=engram, trace=ToolTrace(), withheld=frozenset(WITHHELD)) as knowledge_base,
        ):
            # One derivation, in baseline.py, for both paths -- reuse.py once carried its own
            # copy, which shipped with the exact bug the shared one had already fixed.
            gaps = unresolved(questions, await _known_ids(knowledge_base, cited))
            if gaps:
                msg = f"groundtruth cites ids the live knowledge base does not have: {dict(gaps)}"
                raise RuntimeError(msg)
            # Fail loudly here, before spending a single run, rather than discovering on
            # question one that the saved artifact does not check out against this environment.
            spec = await store.fetch_workflow(workflow_id)
            probe = prepare(
                spec,
                await build_environment(
                    RecordingModel(model(infrastructure, settings.models)),
                    engram=knowledge_base,
                    sandbox=sandbox,
                    external_agents=dict,
                    session_log=NoSessionLog(),
                    custom=custom,
                    records=records,
                    web_search=settings.web_search,
                    tiers=tiers,
                ),
            )
            if not probe.report.ok:
                detail = "; ".join(f"{issue.where}: {issue.detail}" for issue in probe.report.issues)
                msg = f"{workflow_id} does not check out here: {detail}"
                raise RuntimeError(msg)
            # A fresh RecordingModel and a fresh registry per question -- knowledge_base.trace is
            # swapped the same way baseline.main does, so records never bleed across runs.
            for question in questions:
                knowledge_base.trace = ToolTrace()
                recording = RecordingModel(model(infrastructure, settings.models))
                registry = await build_environment(
                    recording,
                    engram=knowledge_base,
                    sandbox=sandbox,
                    external_agents=dict,
                    session_log=NoSessionLog(),
                    custom=custom,
                    records=records,
                    web_search=settings.web_search,
                    tiers=tiers,
                )
                record = await run_reuse(
                    question,
                    store=store,
                    registry=registry,
                    trace=knowledge_base.trace,
                    recording=recording,
                    workflow_id=workflow_id,
                    ledger=records.evidence,
                    custom=custom,
                )
                print(write_record(record, OUT), record.recall, record.requests)
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
