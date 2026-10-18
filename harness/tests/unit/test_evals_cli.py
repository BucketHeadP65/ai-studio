"""`harness evals run` forwards `--artifacts` to `evals_run_in_session`, defaulting to `harness/workflows`.

Same technique `test_import_cli.py` uses for `harness save`: the session-opening call is heavy (it
builds the whole environment), so the CLI wrapper is exercised with that call swapped for a fake that
records what it was asked for.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
import typer

from core.ai.evaluation.contracts import EvaluationLimits, StateRootIdentity
from typer.testing import CliRunner

from harness.cli import _BenchmarkFlags, app, stated_limits
from harness.features.evals.domain.benchmark import (
    BenchmarkInputs,
    BenchmarkRefusalNote,
    DevelopmentReplay,
    OutcomeStateReport,
    SealedComparison,
    freeze_benchmark,
)

if TYPE_CHECKING:
    from pathlib import Path


class _FakeReport:
    def print(self) -> None:
        return None


def test_defaults_artifacts_to_harness_workflows(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_evals_run_in_session(step_id: str, *, knowledge_base: str, artifacts: str) -> _FakeReport:
        calls.append({"step_id": step_id, "knowledge_base": knowledge_base, "artifacts": artifacts})
        return _FakeReport()

    import harness.cli as cli_module
    import harness.features.evals.contracts as runner_module

    monkeypatch.setattr(cli_module, "evals_run_in_session", fake_evals_run_in_session)
    monkeypatch.setattr(runner_module, "report_failed", lambda report: False)

    result = CliRunner().invoke(app, ["evals", "run", "st.check_adrs.v1", "--knowledge-base", "repo-docs"])

    assert result.exit_code == 0, result.output
    assert calls == [{"step_id": "st.check_adrs.v1", "knowledge_base": "repo-docs", "artifacts": "harness/workflows"}]


def test_an_explicit_artifacts_directory_overrides_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_evals_run_in_session(step_id: str, *, knowledge_base: str, artifacts: str) -> _FakeReport:
        calls.append({"step_id": step_id, "knowledge_base": knowledge_base, "artifacts": artifacts})
        return _FakeReport()

    import harness.cli as cli_module
    import harness.features.evals.contracts as runner_module

    monkeypatch.setattr(cli_module, "evals_run_in_session", fake_evals_run_in_session)
    monkeypatch.setattr(runner_module, "report_failed", lambda report: False)

    result = CliRunner().invoke(
        app,
        [
            "evals",
            "run",
            "st.check_adrs.v1",
            "--knowledge-base",
            "repo-docs",
            "--artifacts",
            "templates/repo-docs/workflows",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [{"step_id": "st.check_adrs.v1", "knowledge_base": "repo-docs", "artifacts": "templates/repo-docs/workflows"}]


# -- `harness evals outcomes`: the flags, and what they carry into the benchmark -------


BENCH_START = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
ROOT_IDENTITY = StateRootIdentity(canonical_state_root="/a/shared/budget/root", device_id=1)
INPUTS = BenchmarkInputs(
    model="deterministic",
    model_version="1",
    workflow="wf.workflow-automation.v1",
    prompt="Design the automation the person asked for.",
    tools=("search",),
    sources=("repo-docs@snapshot-1",),
    grader="the custodian's own grader",
)


def _blocked_report() -> OutcomeStateReport:
    """A report that measured nothing, which is what the verbs print when a benchmark is refused."""
    return OutcomeStateReport(
        report_id="report-1",
        objective="task-success",
        empirical_status="blocked",
        model_kind="unbuilt",
        started_at=BENCH_START,
        dataset="workflow-automation-development",
        dataset_version="v1",
        dataset_fingerprint="a" * 64,
        freeze=freeze_benchmark(INPUTS, root=ROOT_IDENTITY, limits=None),
        refusals=(BenchmarkRefusalNote(refusal="development_dataset_only", detail="the set declares itself author-visible"),),
    )


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Swap the session opener for a recorder: the flags are the thing under test, not the benchmark."""
    import harness.cli as cli_module

    asked: list[object] = []

    async def fake_run_benchmark_in_session(flags: object) -> OutcomeStateReport:
        asked.append(flags)
        return _blocked_report()

    monkeypatch.setattr(cli_module, "run_benchmark_in_session", fake_run_benchmark_in_session)
    return asked


def test_the_replay_verb_reads_the_development_set_and_states_no_thresholds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A replay names the author-visible set and carries no stop thresholds, because nothing spends."""
    asked = _capture(monkeypatch)

    result = CliRunner().invoke(
        app,
        [
            "evals", "outcomes", "replay", "cases.json", "cases.stamp.json",
            "--inputs", "inputs.json", "--report-root", str(tmp_path / "reports"), "--shared-budget-root", str(tmp_path / "budget"),
        ],
    )

    assert result.exit_code == 0, result.output
    flags = asked[0]
    assert isinstance(flags, _BenchmarkFlags)
    assert isinstance(flags.cases, DevelopmentReplay)
    assert flags.cases.dataset_path.name == "cases.json"
    assert flags.cases.stamp_path.name == "cases.stamp.json"
    assert flags.limits is None, "a replay opens no cycle, so it states no thresholds"
    assert flags.journey is None
    assert flags.objective == "task-success"
    assert "blocked" in result.output
    assert "recommends nothing" in result.output


def test_the_compare_verb_carries_every_stated_threshold_and_the_journey(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The five thresholds arrive as the limits the cycle opens under, exactly as they were typed."""
    asked = _capture(monkeypatch)

    result = CliRunner().invoke(
        app,
        [
            "evals", "outcomes", "compare", "sealed.json", "custody.json",
            "--journey", "journey.json", "--inputs", "inputs.json",
            "--report-root", str(tmp_path / "reports"), "--shared-budget-root", str(tmp_path / "budget"),
            "--objective", "measured-generative-model-cost",
            "--cycle-cost-stop-usd", "10", "--trial-cost-stop-usd", "2",
            "--trial-deadline-seconds", "180", "--trial-request-limit", "16", "--trial-attempt-limit", "3",
        ],
    )

    assert result.exit_code == 0, result.output
    flags = asked[0]
    assert isinstance(flags, _BenchmarkFlags)
    assert isinstance(flags.cases, SealedComparison)
    assert flags.cases.custody_path.name == "custody.json"
    assert flags.journey is not None
    assert flags.journey.name == "journey.json"
    assert flags.objective == "measured-generative-model-cost"
    assert flags.limits == EvaluationLimits(
        cycle_cost_stop_usd=Decimal(10),
        trial_cost_stop_usd=Decimal(2),
        trial_deadline_seconds=180,
        trial_request_limit=16,
        trial_attempt_limit=3,
    )


def test_four_of_the_five_thresholds_is_refused_rather_than_completed_from_a_default() -> None:
    """A benchmark under four thresholds has one dimension nobody bounded, and that is not filled in."""
    with pytest.raises(typer.Exit) as exited:
        stated_limits("10", "2", 180, 16, None)

    assert exited.value.exit_code == 1
    assert stated_limits(None, None, None, None, None) is None, "stating none of them is a benchmark that opens no cycle"


def test_a_threshold_that_is_not_positive_is_refused() -> None:
    """A limit of zero stops a trial before it starts rather than bounding one, so it is not a limit."""
    with pytest.raises(typer.Exit) as exited:
        stated_limits("10", "0", 180, 16, 3)

    assert exited.value.exit_code == 1
