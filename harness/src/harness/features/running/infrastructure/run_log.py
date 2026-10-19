"""The run log: what a live run leaves on disk, so a restart restores its gate instead of losing it.

An observer snapshots every recorded output as it lands, and a loader turns the file back into a
memo seed. The file outlives only a process death, because every run that ends closes its run log
on the way out. The values a run's gates reviewed are kept beside the log, one file per address,
so an ending can still say what was refused.
"""

from __future__ import annotations

import json

from contextlib import suppress
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from core.ai.workflows.contracts import (
    FromNode,
    GateDecisionFact,
    JudgeGate,
    MapBatch,
    MapNode,
    OrchestratorGate,
    PersonGate,
    RetryFeedback,
    ReviewedArtifactRef,
    ReviewedArtifacts,
    RunNode,
    StepRegistry,
    Workflow,
    WorkflowArguments,
    WorkflowState,
    WorkItemFact,
    canonical_reviewed_value,
    prepare,
    reviewed_value_digest,
)
from core.files import atomic_write
from core.observability import get_logger
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError
from pydantic_core import to_jsonable_python

from harness.domain.contracts import DEFAULT_GATE_MODE, DEFAULT_SESSION_TYPE, GateMode, SessionType, session_type_of
from harness.features.running.domain.ended_run import ReviewedValue

if TYPE_CHECKING:
    from pathlib import Path

_logger = get_logger(__name__)


def reviewed_values(log_path: Path, run_id: str) -> FileReviewedArtifacts:
    """Where the run `run_id` keeps the values its gates review, beside the log at `log_path`.

    Each run gets a subtree of its own, so two runs whose logs share one folder never read each
    other's values.

    Args:
        log_path: The run log's path.
        run_id: The run whose values these are.

    Returns:
        The run's reviewed values on disk.
    """
    return FileReviewedArtifacts(run_id=run_id, root=log_path.parent / "reviewed" / run_id)


@dataclass(kw_only=True)
class FileReviewedArtifacts(ReviewedArtifacts):
    """This run's reviewed values on disk, one file per address, written before any decision.

    A refused value survives the process that saw it, so an ending can still say what was rejected.
    """

    root: Path
    """The run's own subtree, holding one file per address."""

    def persist(self, node_id: str, turn: str | None, value: object) -> ReviewedArtifactRef:
        """Capture the value, then write it under its own address, verifying an address written twice rather than overwriting it.

        Args:
            node_id: The node that produced the value, or `parameter:<name>` for a value wired from a parameter.
            turn: The turn of a map that produced it, or `None` when the wire named none.
            value: The value the gate reviews.

        Returns:
            The reference the value is held under.

        Raises:
            PydanticSerializationError: `value` holds something no JSON type covers.
            ValueError: `value` holds a NaN or an infinite float, or the address was already written and
                the file there disagrees with it.
        """
        ref = super().persist(node_id, turn, value)
        path = self._path(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        document = ReviewedValue(ref=ref, canonical=self.values[ref])
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(document.model_dump_json())
        except FileExistsError:
            self.read(ref)
        return ref

    def read(self, ref: ReviewedArtifactRef) -> JsonValue:
        """The value `ref` addresses, read back from disk and checked against the address it claims.

        Args:
            ref: The reference `persist` answered with.

        Returns:
            The value, read back from the bytes the decider saw.

        Raises:
            ValueError: `ref` names another run, names a value this run never captured, or the
                file it addresses disagrees with it.
        """
        try:
            document = ReviewedValue.model_validate_json(self._path(ref).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            msg = "The reviewed value was not recorded for this run and node."
            raise ValueError(msg) from exc
        if document.ref != ref:
            msg = "The reviewed value does not match the address it was stored under."
            raise ValueError(msg)
        self.values[ref] = document.canonical
        return super().read(ref)

    def captured(self) -> tuple[ReviewedValue, ...]:
        """Every value this run captured, read back off disk, for an ending to copy into the archive.

        Returns:
            The values. A file that cannot be read is skipped.
        """
        if not self.root.is_dir():
            return ()
        found: list[ReviewedValue] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                document = ReviewedValue.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValidationError, ValueError):
                continue
            if document.ref.run_id == self.run_id:
                found.append(document)
        return tuple(found)

    def adopt(self, document: ReviewedValue) -> ReviewedArtifactRef:
        """Re-file a value another run captured under this run's own address, byte for byte.

        The canonical text is copied, never re-serialised, so the new address carries the digest of
        the decider's own bytes. An address already written here is left as it stands.

        Args:
            document: The value another run captured.

        Returns:
            The value's address under this run.
        """
        ref = document.ref.model_copy(update={"run_id": self.run_id})
        self.values.setdefault(ref, document.canonical)
        path = self._path(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(FileExistsError), path.open("x", encoding="utf-8") as stream:
            stream.write(ReviewedValue(ref=ref, canonical=document.canonical).model_dump_json())
        return ref

    def clear(self) -> None:
        """Remove this run's captured values, and nothing any other run captured.

        A value the ending has not yet copied into the archive record is lost, and the archive
        record is where a reference into a reviewed value resolves from afterwards.
        """
        if not self.root.is_dir():
            return
        for path in self.root.glob("*.json"):
            path.unlink(missing_ok=True)
        with suppress(OSError):
            self.root.rmdir()

    def _path(self, ref: ReviewedArtifactRef) -> Path:
        """Where `ref` is kept, named by the digest of the address, refusing a `ref` of another run."""
        if ref.run_id != self.run_id:
            msg = "The reviewed value belongs to another run."
            raise ValueError(msg)
        return self.root / (reviewed_value_digest(canonical_reviewed_value(ref)) + ".json")


class _Batch(BaseModel):
    """One map batch as the run log writes it, validated on the way back in because a log is untrusted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generation: int
    ordered_digest: str
    items: tuple[str, ...]


_BATCHES: TypeAdapter[dict[str, _Batch]] = TypeAdapter(dict[str, _Batch])

DEFAULT_GATE_TIMEOUT = 900.0
"""How long a launch that names no timeout lets a gate wait for a person, in seconds."""

DEFAULT_RUNNER = "app:harness"
"""The launcher a launch that names none is recorded under."""


_GATE_MODE: TypeAdapter[GateMode] = TypeAdapter(GateMode)
_GATE_TIMEOUT: TypeAdapter[float | None] = TypeAdapter(float | None)

_PARAMS: TypeAdapter[WorkflowArguments] = TypeAdapter(WorkflowArguments)
"""What a run log's own arguments are read back as: JSON, whatever the run that wrote them held."""


@dataclass(frozen=True)
class WaitingRun:
    """A run log read back: JSON-shaped values until `revive_values` types them."""

    workflow_id: str
    spec: Workflow
    params: WorkflowArguments
    values: dict[str, object]
    retry_feedback: dict[str, list[RetryFeedback]] = field(default_factory=dict)
    """Node id -> the rejections that node owes a repair to. Empty when the log holds none."""
    gate_decisions: list[GateDecisionFact] = field(default_factory=list)
    """Every decision this run's gates received, in order. Empty when the log holds none."""
    gate_generations: dict[str, int] = field(default_factory=dict)
    """Gate id -> how many values that gate has reviewed. Empty when the log holds none."""
    work_facts: list[WorkItemFact] = field(default_factory=list)
    """Every disposition this run settled, of every generation. Empty when the log holds none,
    which reads as unknown, never as work that was never attempted."""
    batches: dict[str, MapBatch] = field(default_factory=dict)
    """Node id -> the input that node is on, and the identities it owes a disposition. Empty when
    the log holds none, and then a primary work missing here can be classified as nothing at all."""
    run_id: str = ""
    """The run whose reviewed values sit beside this log."""
    run_title: str = ""
    """What the run this log belongs to was called. `""` for a run nobody named, and a resume
    then carries the run on unnamed."""
    gate_mode: GateMode = DEFAULT_GATE_MODE
    """What the run's person gates did."""
    gate_timeout: float | None = DEFAULT_GATE_TIMEOUT
    """How long a gate waited for a person before rejecting, `None` for forever."""
    runner: str = DEFAULT_RUNNER
    """The launcher the run named."""
    session_kind: SessionType = DEFAULT_SESSION_TYPE
    """The kind of session the run lived in."""
    started_at: str = ""
    """When the launch started the run, as ISO 8601 text."""


@dataclass(frozen=True)
class CarriedState:
    """What a paused run hands the run that resumes it: what it still owes, and what it has spent.

    Narrowed to the nodes the graph being launched still has in the shape they had, so an edit that
    moved a node withdraws that node's repairs and reaches along with its recorded value.
    """

    retry_feedback: dict[str, list[RetryFeedback]] = field(default_factory=dict)
    """Node id -> the rejections that node still owes a repair to."""
    gate_decisions: list[GateDecisionFact] = field(default_factory=list)
    """Every decision the paused run's gates received, in order."""
    gate_generations: dict[str, int] = field(default_factory=dict)
    """Gate id -> reaches already spent, so a pause buys no fresh ones against `max_attempts`."""
    work_facts: list[WorkItemFact] = field(default_factory=list)
    """Every disposition the paused run settled, keyed on by identity once the run holds them."""
    batches: dict[str, MapBatch] = field(default_factory=dict)
    """Node id -> the input that node was working through and the identities it minted."""
    reviewed: tuple[ReviewedValue, ...] = ()
    """The values the paused run's gates reviewed, to be re-filed under the resuming run's own id."""


def carried_state(waiting: WaitingRun, *, kept: frozenset[str], reviewed: tuple[ReviewedValue, ...]) -> CarriedState:
    """What of `waiting`'s own state a run relaunched against a new graph may keep.

    A value and the repairs owed over it are withdrawn together.

    Args:
        waiting: The paused run's log, read back.
        kept: The node ids the new graph still has unmoved, the same filter the memo is built with.
        reviewed: The values the paused run's gates reviewed.

    Returns:
        The state narrowed to `kept`, carrying only the reviewed values a kept repair addresses.
    """
    owed = {node_id: list(items) for node_id, items in waiting.retry_feedback.items() if node_id in kept}
    addresses = {_address_of(item.reviewed) for items in owed.values() for item in items}
    return CarriedState(
        retry_feedback=owed,
        gate_decisions=[fact for fact in waiting.gate_decisions if fact.gate_id in kept],
        gate_generations={gate_id: reaches for gate_id, reaches in waiting.gate_generations.items() if gate_id in kept},
        work_facts=[fact for fact in waiting.work_facts if fact.node_id in kept],
        batches={node_id: batch for node_id, batch in waiting.batches.items() if node_id in kept},
        reviewed=tuple(document for document in reviewed if _address_of(document.ref) in addresses),
    )


def _address_of(ref: ReviewedArtifactRef) -> tuple[str, str | None, str]:
    """What makes two references name the same value: the node, the turn and the digest, never the run id."""
    return (ref.node_id, ref.turn, ref.value_digest)


def sweep_orphan_reviewed(log_path: Path) -> int:
    """Remove every reviewed-value subtree beside `log_path` that no run log there still names.

    A process death or a resume leaves such a subtree behind.

    Args:
        log_path: The path of a run log in the folder to sweep.

    Returns:
        How many subtrees were removed.
    """
    root = log_path.parent / "reviewed"
    if not root.is_dir():
        return 0
    live = {run_id for path in log_path.parent.glob("*.json") if (run_id := _logged_run_id(path))}
    removed = 0
    for subtree in root.iterdir():
        if not subtree.is_dir() or subtree.name in live:
            continue
        for value in subtree.glob("*.json"):
            value.unlink(missing_ok=True)
        with suppress(OSError):
            subtree.rmdir()
            removed += 1
    return removed


def _logged_run_id(path: Path) -> str:
    """The run a run log on disk was launched for, or `""` when the file is not one, or holds none."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(raw.get("run_id", "")) if isinstance(raw, dict) else ""


@dataclass
class RunLog:
    """A run observer that snapshots the run's recorded outputs after every step.

    It holds the run's workflow state, so the caller must run the graph with `run_log.state`.
    """

    path: Path
    workflow_id: str
    spec: Workflow
    params: WorkflowArguments
    state: WorkflowState
    run_title: str = ""
    gate_mode: GateMode = DEFAULT_GATE_MODE
    gate_timeout: float | None = DEFAULT_GATE_TIMEOUT
    runner: str = DEFAULT_RUNNER
    session_kind: SessionType = DEFAULT_SESSION_TYPE
    started_at: str = ""

    @classmethod
    def begin(  # noqa: PLR0913 (the facts a launched run is, plus its resume seed, and a holder object would rename the arity, not reduce it)
        cls,
        *,
        path: Path,
        run_id: str,
        workflow_id: str,
        spec: Workflow,
        params: WorkflowArguments,
        memo: dict[str, object] | None = None,
        carried: CarriedState | None = None,
        run_title: str = "",
        gate_mode: GateMode = DEFAULT_GATE_MODE,
        gate_timeout: float | None = DEFAULT_GATE_TIMEOUT,
        runner: str = DEFAULT_RUNNER,
        session_kind: SessionType = DEFAULT_SESSION_TYPE,
        started_at: str = "",
    ) -> RunLog:
        """A run log for one run about to start, seeded with whatever `memo` already settled.

        It clears any reviewed values already filed under `run_id`, then re-files the carried ones
        under it. Every carried rejection is re-addressed to its value's copy, so a rerun reads the
        exact bytes the person refused, and one whose value did not survive the pause is dropped.

        Args:
            path: Where the run log is written. Each run's reviewed values sit beside it in a subtree
                of their own, named for the run.
            run_id: The id the launcher minted, the one the spend log, the run events and the archive
                record key on. This run's reviewed values are addressed under it.
            workflow_id: The workflow's id.
            spec: The workflow to run.
            params: The arguments the run starts with.
            memo: Node outputs already settled, replayed rather than run again.
            carried: What a paused run handed this one: the repairs it still owes, the reaches its
                gates have spent, what its work settled, and the values its gates reviewed.
            run_title: What the launch called this run, logged so a resume or a restore carries the
                same name.
            gate_mode: What the run's person gates do, logged so a restore runs them the same way.
            gate_timeout: How long a gate waits for a person, logged so a restore waits the same.
            runner: The launcher the run names, logged so a restore names the same one.
            session_kind: The kind of session the run lives in, logged so a restore knows whether the run relaunches by itself.
            started_at: When the launch started the run, as ISO 8601 text, logged so a restore shows the same start.

        Returns:
            The run log, holding the run's workflow state.
        """
        artifacts = reviewed_values(path, run_id)
        artifacts.clear()
        carried = carried or CarriedState()
        adopted = {_address_of(document.ref): artifacts.adopt(document) for document in carried.reviewed}
        owed = {
            node_id: readdressed
            for node_id, items in carried.retry_feedback.items()
            if (
                readdressed := [
                    item.model_copy(update={"reviewed": found})
                    for item in items
                    if (found := adopted.get(_address_of(item.reviewed))) is not None
                ]
            )
        }
        state = WorkflowState(
            params=dict(params),
            memo=dict(memo or {}),
            reviewed_artifacts=artifacts,
            retry_feedback=owed,
            gate_decisions=list(carried.gate_decisions),
            gate_generations=dict(carried.gate_generations),
            batches=dict(carried.batches),
            work_facts={fact.identity: fact for fact in carried.work_facts},
        )
        return cls(
            path=path,
            workflow_id=workflow_id,
            spec=spec,
            params=dict(params),
            state=state,
            run_title=run_title,
            gate_mode=gate_mode,
            gate_timeout=gate_timeout,
            runner=runner,
            session_kind=session_kind,
            started_at=started_at,
        )

    def started(self, node_id: str) -> None:
        """Snapshot the run's recorded outputs on every start.

        The snapshot that captures a node's value is the one its successor's start triggers,
        because `finished` arrives before the step's output lands in the run state.

        Args:
            node_id: The node that started. It is not read.
        """
        del node_id
        self._snapshot()

    def finished(self, node_id: str, *, failed: bool = False) -> None:
        """Snapshot the run's recorded outputs at a step's end, unless the step failed and so recorded nothing new.

        Args:
            node_id: The node that finished. It is not read.
            failed: Whether the step failed.
        """
        del node_id
        if not failed:  # a failure recorded nothing, so the previous snapshot still stands
            self._snapshot()

    def _snapshot(self) -> None:
        values: dict[str, object] = {}
        for node_id, value in self.state.values.items():
            if node_id in self.state.rewound:
                continue
            try:
                values[node_id] = to_jsonable_python(value)
            except Exception:
                continue
        payload = {
            "workflow_id": self.workflow_id,
            "run_title": self.run_title,
            "gate_mode": self.gate_mode,
            "gate_timeout": self.gate_timeout,
            "runner": self.runner,
            "session_kind": self.session_kind,
            "started_at": self.started_at,
            "spec": self.spec.model_dump(mode="json"),
            "params": self.params,
            "values": values,
            "run_id": self.state.reviewed_artifacts.run_id,
            "retry_feedback": {node: [item.model_dump(mode="json") for item in owed] for node, owed in self.state.retry_feedback.items()},
            "gate_decisions": [fact.model_dump(mode="json") for fact in self.state.gate_decisions],
            "gate_generations": self.state.gate_generations,
            "work_facts": [fact.model_dump(mode="json") for fact in self.state.work_facts.values()],
            "batches": {node: asdict(batch) for node, batch in self.state.batches.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, json.dumps(payload))

    def reviewed(self) -> tuple[ReviewedValue, ...]:
        """Every value this run's gates reviewed, for an ending to keep past the log's own life.

        Call it before `close`, which removes this run's own captured values.

        Returns:
            The values, empty when the run captured none.
        """
        artifacts = self.state.reviewed_artifacts
        return artifacts.captured() if isinstance(artifacts, FileReviewedArtifacts) else ()

    def close(self) -> None:
        """Delete the run log of a run that ended, however it ended, so nothing is left to resume.

        This run's captured values go too, so the ending must have copied them into its archive
        record first. Only this run's own subtree is removed, and a run still in flight in another
        session keeps its own.
        """
        self.path.unlink(missing_ok=True)
        artifacts = self.state.reviewed_artifacts
        if isinstance(artifacts, FileReviewedArtifacts):
            artifacts.clear()


def load_waiting(path: Path) -> WaitingRun | None:
    """Read the run log a dead process left behind.

    It never raises. A damaged run log, or one missing a key every run log carries, is logged as a warning and read as
    nothing to resume.

    Args:
        path: The run log's path.

    Returns:
        The run as it was logged, or `None` when there is nothing to resume.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return WaitingRun(
            workflow_id=str(raw["workflow_id"]),
            spec=Workflow.model_validate(raw["spec"]),
            params=_PARAMS.validate_python(raw["params"]),
            values=dict(raw["values"]),
            retry_feedback=TypeAdapter(dict[str, list[RetryFeedback]]).validate_python(raw["retry_feedback"]),
            gate_decisions=TypeAdapter(list[GateDecisionFact]).validate_python(raw["gate_decisions"]),
            gate_generations=TypeAdapter(dict[str, int]).validate_python(raw["gate_generations"]),
            work_facts=TypeAdapter(list[WorkItemFact]).validate_python(raw["work_facts"]),
            batches={
                node: MapBatch(generation=batch.generation, ordered_digest=batch.ordered_digest, items=batch.items)
                for node, batch in _BATCHES.validate_python(raw["batches"]).items()
            },
            run_id=str(raw["run_id"]),
            run_title=str(raw["run_title"]),
            gate_mode=_GATE_MODE.validate_python(raw["gate_mode"]),
            gate_timeout=_GATE_TIMEOUT.validate_python(raw["gate_timeout"]),
            runner=str(raw["runner"]),
            session_kind=session_type_of(str(raw["session_kind"])),
            started_at=str(raw["started_at"]),
        )
    except FileNotFoundError:
        return None
    except Exception:
        _logger.warning("run log at {path} was unreadable, dropping it", path=str(path))
        return None


def revive_values(spec: Workflow, registry: StepRegistry, values: dict[str, object]) -> dict[str, object]:
    """Type each logged value against the step that produced it, and drop what fails.

    A dropped value is not an error, because its node re-runs. Values are typed against the
    prepared registry, so the workflow's inline steps revive exactly like registered ones.

    Args:
        spec: The workflow the values will seed.
        registry: The host's registered steps.
        values: The logged values, JSON-shaped, by node id.

    Returns:
        The values that typed, by node id. Empty when the workflow does not prepare.
    """
    prepared = prepare(spec, registry)
    if not prepared.report.ok:
        return {}
    revivable = _output_types(spec, prepared.registry)
    revived: dict[str, object] = {}
    for node_id, raw in values.items():
        annotation = revivable.get(node_id)
        if annotation is None:
            continue
        try:
            revived[node_id] = TypeAdapter[object](annotation).validate_python(raw)
        except ValidationError:
            continue  # the step changed since this value was logged, so the node re-runs
    return revived


def _output_types(spec: Workflow, registry: StepRegistry) -> dict[str, type]:
    """What type each node's recorded value must satisfy to be replayed, with one more entry per turn of a multi-turn map."""
    by_id = {node.id: node for node in spec.nodes}

    def type_of(node_id: str) -> type | None:
        match by_id.get(node_id):
            case RunNode() as node:
                impl = registry.get(node.work)
                return impl.output_type if impl else None
            case MapNode() as node:
                impl = registry.get(node.turns[-1])
                return list[impl.output_type] if impl else None
            case (PersonGate() as node) | (JudgeGate() as node) | (OrchestratorGate() as node):
                return type_of(node.reviews.node) if isinstance(node.reviews, FromNode) else None
            case _:
                return None

    types = {node.id: annotation for node in spec.nodes if (annotation := type_of(node.id)) is not None}
    for node in spec.nodes:
        if not isinstance(node, MapNode) or len(node.turns) == 1:
            continue
        for work in node.turns:
            impl = registry.get(work)
            if impl is not None:
                types[f"{node.id}.{work}"] = list[impl.output_type]
    return types
