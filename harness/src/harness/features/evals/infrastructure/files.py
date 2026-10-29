"""The files this slice reads and writes: datasets, stamps, custody records, reports, trial logs and the trial index."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

from core.ai.evaluation.contracts import TaskDataset, require_unique
from core.files import atomic_create

from harness.features.evals.domain.benchmark import BenchmarkCustody, BenchmarkTrialRecord
from harness.features.evals.domain.outcomes import DevelopmentDatasetStamp, WorkflowAutomationDataset, require_stamped
from harness.features.evals.domain.pilot import PilotTrial
from harness.features.evals.domain.step_evaluation import OutcomeMatches, StepDataset

if TYPE_CHECKING:
    from pathlib import Path

    from harness.features.evals.domain.benchmark import OutcomeStateReport


def load_dataset(path: Path) -> StepDataset:
    """The step dataset at `path`, with `OutcomeMatches` registered as the evaluator its cases name.

    Args:
        path: The dataset file.

    Returns:
        The dataset.
    """
    return StepDataset.from_file(path, custom_evaluator_types=[OutcomeMatches])


def load_workflow_automation_dataset(path: Path) -> WorkflowAutomationDataset:
    """The workflow-automation dataset at `path`, validated whole.

    You must pass the path. This function never looks one up.

    Args:
        path: The dataset file.

    Returns:
        The dataset.
    """
    return WorkflowAutomationDataset.model_validate_json(path.read_text(encoding="utf-8"))


def load_dataset_stamp(path: Path) -> DevelopmentDatasetStamp:
    """The development dataset stamp at `path`, validated whole.

    Args:
        path: The stamp file.

    Returns:
        The stamp.
    """
    return DevelopmentDatasetStamp.model_validate_json(path.read_text(encoding="utf-8"))


def load_stamped_dataset(dataset_path: Path, stamp_path: Path) -> WorkflowAutomationDataset:
    """The dataset at `dataset_path`, loaded only if the stamp beside it covers exactly this version.

    You must pass both paths. This function never looks one up.

    Args:
        dataset_path: The dataset file.
        stamp_path: The stamp file that covers it.

    Returns:
        The dataset.

    Raises:
        ValueError: the stamp names another dataset version, a different set of cases, or other
            kinds of work than the cases declare.
    """
    dataset = load_workflow_automation_dataset(dataset_path)
    require_stamped(dataset, load_dataset_stamp(stamp_path))
    return dataset


def load_task_dataset(path: Path) -> TaskDataset:
    """The sealed task dataset at `path`, validated whole.

    Args:
        path: The dataset file a custodian filed.

    Returns:
        The dataset, every case included.
    """
    return TaskDataset.model_validate_json(path.read_text(encoding="utf-8"))


def load_benchmark_custody(path: Path) -> BenchmarkCustody:
    """The custody record at `path`, validated whole.

    Args:
        path: The custody file a custodian filed beside the sealed cases.

    Returns:
        The custody record.
    """
    return BenchmarkCustody.model_validate_json(path.read_text(encoding="utf-8"))


def _report_path(root: Path, report_id: str) -> Path:
    """The file one report is filed under."""
    return root / f"{report_id}.json"


def file_report(root: Path, report: OutcomeStateReport) -> Path:
    """Write `report` under its own id, and refuse to overwrite a report already filed there.

    Args:
        root: The folder reports are filed in. It is created when missing.
        report: The report to file.

    Returns:
        The file the report was written to.

    Raises:
        FileExistsError: a report is already filed under this id.
    """
    root.mkdir(parents=True, exist_ok=True)
    path = _report_path(root, report.report_id)
    atomic_create(path, report.model_dump_json(indent=2).encode("utf-8"))
    return path


class TrialLog:
    """Where every slot lands before any verdict is folded, one file per slot that is never rewritten."""

    def __init__(self, directory: Path) -> None:
        """Keep the trial log in `directory`, which is created when the first slot is filed.

        Args:
            directory: The folder the slot files go in.
        """
        self.directory = directory

    def file(self, record: BenchmarkTrialRecord) -> None:
        """Write one slot to its own file. A second filing of the same slot is refused, never merged.

        Args:
            record: The slot's trial record.

        Raises:
            FileExistsError: this case, repetition and variant is already on disk.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        name = f"{record.case_id}-{record.repetition}-{record.variant}.json"
        atomic_create(self.directory / name, record.model_dump_json(indent=2).encode("utf-8"))

    def filed(self) -> tuple[BenchmarkTrialRecord, ...]:
        """Every slot on disk, ordered by case, repetition and variant, as they were written.

        Returns:
            The trial records, empty when nothing has been filed.
        """
        if not self.directory.is_dir():
            return ()
        rows = [BenchmarkTrialRecord.model_validate_json(path.read_text(encoding="utf-8")) for path in sorted(self.directory.glob("*.json"))]
        return tuple(sorted(rows, key=lambda row: (row.case_id, row.repetition, row.variant)))


class FilePilotRuns:
    """The pilot trial index read from a JSON file: which run each case's pilot trial produced.

    The person who drove the trials writes the file, one `PilotTrial` per case and repetition.
    A case and repetition the file does not name is a trial that was never recorded, not an empty one.
    """

    def __init__(self, path: Path) -> None:
        """Read the trial index at `path`.

        Args:
            path: A JSON file holding a list of `PilotTrial` rows.

        Raises:
            ValueError: a row is not a valid `PilotTrial`, or two rows name the same case and repetition.
        """
        self.trials = tuple(PilotTrial.model_validate(row) for row in json.loads(path.read_text(encoding="utf-8")))
        require_unique((f"{row.case_id}-{row.repetition}" for row in self.trials), "pilot trial slots")

    async def trial_for(self, case_id: str, repetition: int) -> PilotTrial | None:
        """Look the trial up in the rows read from the file.

        Args:
            case_id: The evaluation case's id.
            repetition: The repetition of that case.

        Returns:
            The trial, or `None` when that journey never ran.
        """
        return next((row for row in self.trials if row.case_id == case_id and row.repetition == repetition), None)
