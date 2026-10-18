"""The run record: one assembler for both experiment paths, so neither gets private accounting.

`build_record` is the only place a `RunRecord` is constructed -- scoring against groundtruth,
token/request accounting off the shared `RecordingModel`, and the contamination check all happen
here once, so a baseline run and a reuse run are scored by the same arithmetic. `write_record` is
the one place a record reaches disk, named so the scoreboard that reads `out/` never has to guess
which path or question a file belongs to.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

from proof.instrument import surfaced_ids

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from proof.groundtruth import ResearchQuestion
    from proof.instrument import RecordingModel, ToolTrace


async def read_only_ingest(payload: Mapping[str, Any], *, collection: str) -> None:
    """The store's write slot, filled with a refusal: neither proof path ever saves.

    The proof reads a saved artifact and grades what ran; a write from either path would mean
    the experiment mutated its own subject. Refusing loudly beats opening a writer credential
    that nothing here should hold.
    """
    del payload
    msg = f"the proof paths are read-only; nothing here may write into {collection!r}"
    raise RuntimeError(msg)


class RunRecord(BaseModel):
    """One question, one run, one path -- everything the scoreboard and the report read.

    Attributes:
        question_id: The `ResearchQuestion.id` this run answered.
        path: Which experiment path produced this record.
        surfaced: Knowledge base ids the run's tool calls returned, sorted for determinism.
        recall: Fraction of the question's cited sources `surfaced` covers.
        missed: Cited sources `surfaced` never reached, sorted for determinism.
        requests: Model requests the run spent, off the shared `RecordingModel`.
        input_tokens: Input tokens the run spent.
        output_tokens: Output tokens the run spent.
        seconds: Wall-clock duration of the run, rounded to two decimals.
        contaminated: Whether a withheld question id (`q.*`) leaked into `surfaced` despite the
            guard and the scrub -- belt-and-braces; a `True` here means both already failed.
        failed: Whether the run raised rather than completed. A failure is a row, never a retry.
        failure: The one-line reason a failed run failed (exception type and message), or `None`
            on a clean run. In the record itself because a row that says only `failed: true`
            sends the reader to a log that may not exist.
        answer: The agent's final output, or `""` when the run failed before producing one.
        workflow_id: The reuse path's matched workflow, or `None` on the baseline path.
        hit_rank: The reuse path's rank of the matched workflow in its search results, or `None`.
        drift: The reuse path's list of adaptations the matched workflow needed, or `None`.
        at: When the run was recorded, so sweeps can be ordered. `None` on records written
            before the field existed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    question_id: str
    path: Literal["baseline", "reuse"]
    surfaced: tuple[str, ...]
    recall: float
    missed: tuple[str, ...]
    requests: int
    input_tokens: int
    output_tokens: int
    seconds: float
    contaminated: bool
    failed: bool
    failure: str | None = None
    answer: str = ""
    workflow_id: str | None = None
    hit_rank: int | None = None
    drift: tuple[str, ...] | None = None
    at: datetime | None = None


def build_record(
    question: ResearchQuestion,
    *,
    path: Literal["baseline", "reuse"],
    trace: ToolTrace,
    model: RecordingModel,
    seconds: float,
    failed: bool,
    answer: str,
    failure: str | None = None,
    workflow_id: str | None = None,
    hit_rank: int | None = None,
    drift: tuple[str, ...] | None = None,
) -> RunRecord:
    """Score one run against its question's groundtruth and meter it off the shared recorder.

    The one place a `RunRecord` is assembled -- both `baseline.run_question` and the reuse
    path's equivalent call this rather than building the model themselves, so scoring and
    accounting can never drift between the two paths.
    """
    surfaced = surfaced_ids(trace)
    return RunRecord(
        question_id=question.id,
        path=path,
        surfaced=tuple(sorted(surfaced)),
        recall=question.recall(surfaced),
        missed=tuple(sorted(question.missed(surfaced))),
        requests=model.requests,
        input_tokens=model.input_tokens,
        output_tokens=model.output_tokens,
        seconds=round(seconds, 2),
        contaminated=any(page.startswith("q.") for page in surfaced),
        failed=failed,
        failure=failure,
        answer=answer,
        workflow_id=workflow_id,
        hit_rank=hit_rank,
        drift=drift,
        at=datetime.now(tz=UTC),
    )


def write_record(record: RunRecord, out_dir: Path) -> Path:
    """Write one record, named so a later sweep cannot silently erase the one it challenges.

    `{path}-{question_id}--{workflow_id}.json` on the reuse path: this harness is the store's
    standing challenger, and a rematch against a new artifact version must land BESIDE the old
    evidence, not over it. A re-sweep of the *same* version still overwrites -- the newest run
    of one artifact is that artifact's row. The baseline path has no version to discriminate by
    and keeps the short name.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"--{record.workflow_id}" if record.workflow_id is not None else ""
    target = out_dir / f"{record.path}-{record.question_id}{tag}.json"
    target.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    return target
