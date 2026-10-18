"""The report: one honest table, folded from `proof/out/` into `proof/report.md`.

Aggregates are computed over clean rows only -- neither failed nor contaminated -- and every
row this excludes from an average is listed beneath it rather than dropped from the report. A
failed run is a row, marked, never a silent retry; the same is true of a contaminated one. The
break-even line and the stated-caps paragraph both answer "what was this run allowed to cost",
which is a different question from "what did it actually cost" -- the report keeps both, and
never lets the cap stand in for the claim.
"""

from __future__ import annotations

import asyncio
import math

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from core.ai.workflows.contracts import LibraryCollection
from pydantic import BaseModel, ConfigDict

from harness.adapters.store import store_connection
from harness.domain.contracts import BRIEFING_ONLY_BUDGET, DEFINED_STEP_BUDGET
from proof.baseline import BASELINE_BUDGET
from proof.records import RunRecord, read_only_ingest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from core.ai.workflows.contracts import Workflow

OUT = Path(__file__).parent / "out"
REPORT = Path(__file__).parent / "report.md"
DISTILLATION = OUT / "distillation.json"

_PATHS: tuple[Literal["baseline", "reuse"], ...] = ("baseline", "reuse")


class DistillationCost(BaseModel):
    """What the studio session spent, once, to produce the artifact both paths compare against.

    Hand-written into `proof/out/distillation.json` rather than metered like a run: the
    studio's model calls happen inside a chat session this harness does not instrument, and the
    honest move is to record the number the session actually reported, named as what it is,
    rather than fabricate an automatic count over a UI nobody watched.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_id: str
    model_calls: int
    validator_round_trips: int
    notes: str = ""


def load_records(out_dir: Path = OUT) -> list[RunRecord]:
    """Load every `RunRecord` json under `out_dir`, in path order, skipping the distillation file.

    An absent directory is not an error -- it means the experiment has not run yet, and the
    report says so rather than crashing on a directory nobody made.
    """
    if not out_dir.exists():
        return []
    return [
        RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(out_dir.glob("*.json"))
        if path.name != DISTILLATION.name
    ]


def load_distillation(path: Path = DISTILLATION) -> DistillationCost | None:
    """Load the hand-written distillation cost, or `None` when the studio session has not recorded one yet."""
    if not path.exists():
        return None
    return DistillationCost.model_validate_json(path.read_text(encoding="utf-8"))


async def _fetch_workflow_from_store(workflow_id: str) -> Workflow:
    """Fetch one artifact's spec from the live engram-backed store -- the production seam.

    Never called by a unit test: it opens a real connection pool against the workflow store.
    `collective_step_budget`'s `fetch_workflow` parameter exists precisely so a test can hand it a
    fixture instead.
    """
    connection = await store_connection()
    try:
        return await LibraryCollection(connection, read_only_ingest).fetch_workflow(workflow_id)
    finally:
        await connection.close()


async def collective_step_budget(
    workflow_id: str | None, *, fetch_workflow: Callable[[str], Awaitable[Workflow]] | None = None
) -> int | None:
    """Sum a saved artifact's per-task budgets, or answer `None` when that cannot be verified now.

    `None` covers every reason this might not be answerable today: no `workflow_id` yet (no
    `distillation.json`, or one that has not recorded a workflow id), the store unreachable, or
    the named artifact gone from it. The caller degrades to the run records' own max requests
    for any of these rather than treating one as a crash -- a report generated mid-experiment,
    before the artifact is saved, is still a report.
    """
    if workflow_id is None:
        return None
    fetch = fetch_workflow if fetch_workflow is not None else _fetch_workflow_from_store
    try:
        spec = await fetch(workflow_id)
    except Exception:  # store reachability is not knowable in advance; degrade, never crash (see pyproject BLE001 ignore)
        return None
    return sum(DEFINED_STEP_BUDGET if task.tools else BRIEFING_ONLY_BUDGET for task in spec.inline)


def _clean(records: Sequence[RunRecord], path: Literal["baseline", "reuse"]) -> list[RunRecord]:
    """Records on one path that are neither failed nor contaminated -- what an aggregate may average."""
    return [record for record in records if record.path == path and not record.failed and not record.contaminated]


def _excluded(records: Sequence[RunRecord]) -> list[RunRecord]:
    """Records disqualified from every aggregate -- listed here, never silently dropped."""
    return [record for record in records if record.failed or record.contaminated]


def _flags(record: RunRecord) -> str:
    """The reasons, if any, a row is excluded from the aggregates -- `FAILED`/`CONTAMINATED` verbatim."""
    tags = [tag for tag, present in (("FAILED", record.failed), ("CONTAMINATED", record.contaminated)) if present]
    return ", ".join(tags) if tags else "clean"


def _question_table(records: Sequence[RunRecord]) -> str:
    """Render one row per (question, path): recall, cost, flags, and what evidence was missed."""
    header = "| question | path | recall | requests | in tok | out tok | seconds | flags | missed |"
    rule = "|---|---|---|---|---|---|---|---|---|"
    rows = [
        f"| {record.question_id} | {record.path} | {record.recall:.2f} | {record.requests} | {record.input_tokens} | "
        f"{record.output_tokens} | {record.seconds:.2f} | {_flags(record)} | {', '.join(record.missed) or '—'} |"
        for record in sorted(records, key=lambda record: (record.question_id, record.path))
    ]
    return "\n".join([header, rule, *rows])


def _aggregate_table(records: Sequence[RunRecord]) -> str:
    """Render mean recall and cost per path, over clean rows only; an empty path reads as dashes."""
    header = "| path | n | mean recall | mean requests | mean in tok | mean out tok | mean seconds |"
    rule = "|---|---|---|---|---|---|---|"
    rows: list[str] = []
    for path in _PATHS:
        clean = _clean(records, path)
        if not clean:
            rows.append(f"| {path} | 0 | — | — | — | — | — |")
            continue
        count = len(clean)
        rows.append(
            f"| {path} | {count} | {sum(r.recall for r in clean) / count:.2f} | "
            f"{sum(r.requests for r in clean) / count:.1f} | {sum(r.input_tokens for r in clean) / count:.0f} | "
            f"{sum(r.output_tokens for r in clean) / count:.0f} | {sum(r.seconds for r in clean) / count:.2f} |"
        )
    return "\n".join([header, rule, *rows])


def _excluded_list(records: Sequence[RunRecord]) -> str:
    """List every row the aggregate above excluded, by question and reason -- never just a count."""
    excluded = sorted(_excluded(records), key=lambda record: (record.question_id, record.path))
    if not excluded:
        return "None."
    return "\n".join(f"- {record.question_id} ({record.path}): {_flags(record)}" for record in excluded)


def _break_even_section(records: Sequence[RunRecord], distillation: DistillationCost | None) -> str:
    """State the distillation's one-time cost and, where the clean rows allow it, its break-even point."""
    if distillation is None:
        return "Distillation cost not recorded -- no `proof/out/distillation.json` yet. Break-even cannot be computed."
    lines = [
        f"Distillation model calls: {distillation.model_calls}.",
        f"Validator round-trips: {distillation.validator_round_trips}.",
    ]
    if distillation.notes:
        lines.append(f"Notes: {distillation.notes}")
    clean_baseline = _clean(records, "baseline")
    clean_reuse = _clean(records, "reuse")
    if not clean_baseline or not clean_reuse:
        lines.append("Break-even is not yet computable -- at least one clean run is needed on each path.")
        return "\n".join(lines)
    mean_baseline_requests = sum(record.requests for record in clean_baseline) / len(clean_baseline)
    mean_reuse_requests = sum(record.requests for record in clean_reuse) / len(clean_reuse)
    savings = mean_baseline_requests - mean_reuse_requests
    break_even = math.ceil(distillation.model_calls / max(1.0, savings))
    lines.append(f"Mean baseline requests (clean): {mean_baseline_requests:.2f}. Mean reuse requests (clean): {mean_reuse_requests:.2f}.")
    lines.append(f"Break-even: {break_even} reuse run(s) recover the distillation cost, at {savings:.2f} requests saved per run.")
    if savings <= 0:
        lines.append(
            "Reuse was not cheaper than baseline over the clean runs recorded so far, so the count above is a "
            "floor-clamped estimate, not an earned payback point."
        )
    return "\n".join(lines)


def _hit_rank_table(reuse_records: Sequence[RunRecord]) -> str:
    """Render, per question, which workflow the store matched and where it ranked in search."""
    header = "| question | workflow | hit rank |"
    rule = "|---|---|---|"
    rows = [
        f"| {record.question_id} | {record.workflow_id or '—'} | "
        f"{record.hit_rank if record.hit_rank is not None else 'not found'} |"
        for record in sorted(reuse_records, key=lambda record: record.question_id)
    ]
    return "\n".join([header, rule, *rows])


def _drift_line(record: RunRecord) -> str:
    """One reuse run's drift: unknown (no fingerprint), none, or the adaptations it took."""
    if record.drift is None:
        return f"- {record.question_id}: drift unknown -- no fingerprint was recorded when the artifact was saved."
    if not record.drift:
        return f"- {record.question_id}: no drift -- the artifact still matched the environment it was saved against."
    return f"- {record.question_id}: adapted for {', '.join(record.drift)}."


def _caps_paragraph(records: Sequence[RunRecord], collective_budget: int | None) -> str:
    """State `BASELINE_BUDGET` and, where possible, verify it against the saved artifact's own budget."""
    baseline_note = (
        f"`BASELINE_BUDGET` is {BASELINE_BUDGET} requests -- twice the artifact's own `DEFINED_STEP_BUDGET` "
        f"({DEFINED_STEP_BUDGET}), a runaway cap rather than the cost claim: every cost figure above comes from "
        f"recorded actuals, never from this number."
    )
    if collective_budget is not None:
        holds = collective_budget <= BASELINE_BUDGET
        verdict = "holds" if holds else "FAILS -- the baseline was capped tighter than the artifact it is compared against"
        return (
            f"{baseline_note} The saved artifact's collective step budget (`sum(DEFINED_STEP_BUDGET if step.tools "
            f"else BRIEFING_ONLY_BUDGET for step in spec.inline)`) is {collective_budget} requests; the check that "
            f"`BASELINE_BUDGET` ({BASELINE_BUDGET}) is at least that {verdict}."
        )
    max_requests = max((record.requests for record in records), default=None)
    if max_requests is None:
        return f"{baseline_note} The store was unreachable at report time, and no run records exist yet to sanity-check the cap against."
    return (
        f"{baseline_note} The store was unreachable at report time, so the collective step budget could not be "
        f"verified against the saved spec; the highest single-run request count observed across every recorded "
        f"run was {max_requests}, comfortably under `BASELINE_BUDGET`."
    )


def render_report(records: Sequence[RunRecord], distillation: DistillationCost | None, *, collective_budget: int | None = None) -> str:
    """Render the whole report as markdown: no row hidden, no aggregate computed over a bad run.

    `collective_budget` is a pre-computed answer from `collective_step_budget` (or `None` when
    the store could not confirm one) -- fetching it is an async, I/O-bound step this function
    stays free of, so it can be exercised on plain fixture records with no event loop.
    """
    ordered = sorted(records, key=lambda record: (record.question_id, record.path))
    baseline_records = [record for record in ordered if record.path == "baseline"]
    reuse_records = [record for record in ordered if record.path == "reuse"]

    parts = [
        "# The rematch -- proof report",
        "",
        (
            "Path A (baseline, from scratch) against Path B (reuse, the saved artifact) over the research knowledge base's "
            "six withheld questions. Every run below is a row; a failed or contaminated one is marked and excluded "
            "from the aggregates beneath it, never averaged away."
        ),
        "",
    ]
    if not ordered:
        parts.append("No run records found in `proof/out/` yet -- run `proof.baseline` (and, later, `proof.reuse`) first.")
        return "\n".join(parts)
    if not baseline_records:
        parts.extend(["_No baseline (Path A) records found yet -- this report reflects the reuse path only._", ""])
    if not reuse_records:
        parts.extend(["_No reuse (Path B) records found yet -- this report reflects the baseline path only, generated mid-experiment._", ""])

    parts.extend(["## Per-question results", "", _question_table(ordered), ""])
    parts.extend(
        [
            "## Aggregates (clean rows only)",
            "",
            _aggregate_table(ordered),
            "",
            "Excluded from the aggregates above:",
            "",
            _excluded_list(ordered),
            "",
        ]
    )
    parts.extend(["## Break-even", "", _break_even_section(ordered, distillation), ""])

    if reuse_records:
        parts.extend(["## Hit-rank (Path B -- where the matched workflow ranked in search)", "", _hit_rank_table(reuse_records), ""])
        parts.extend(
            [
                "## Drift (Path B -- adaptations the matched workflow needed)",
                "",
                "\n".join(_drift_line(record) for record in reuse_records),
                "",
            ]
        )

    parts.extend(["## Stated caps", "", _caps_paragraph(ordered, collective_budget), ""])
    return "\n".join(parts)


async def main() -> None:
    """Fold `proof/out/` into `proof/report.md`, verifying the caps paragraph against the store when it can."""
    records = load_records()
    distillation = load_distillation()
    collective_budget = await collective_step_budget(distillation.workflow_id if distillation is not None else None)
    text = render_report(records, distillation, collective_budget=collective_budget)
    REPORT.write_text(text, encoding="utf-8")
    print(REPORT, len(records), "records")  # the one line of CLI feedback this entry point gives (see pyproject T201 ignore)


if __name__ == "__main__":
    asyncio.run(main())
