"""Staleness end to end: saved with a stamp, fetched with a verdict.

Ruling: the verdict is rendered INTO the fetch result, not waiting behind a tool the model
has to remember exists. Three answers are possible and all three are honest. `[]` means
checked and fresh. A list names exactly what moved. `None` means the artifact carries no
stamp, and unknown is neither fresh nor stale.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from core.ai.workflows.contract_port import FETCH_TOOL
from core.ai.workflows.contracts import StepRegistry, Workflow, WorkflowContract, save
from core.ai.workflows.infrastructure.folder import FOLDER, FolderLibrary, folder_toolset
from tests.unit.workflows.conftest import CallLog, register_quarterly_steps

if TYPE_CHECKING:
    from pathlib import Path


async def _call(port: WorkflowContract[None], name: str, **args: object):
    toolset = port.get_toolset()
    assert toolset is not None
    ctx: RunContext[None] = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    tools = await toolset.get_tools(ctx)
    return await toolset.call_tool(name, dict(args), ctx, tools[name])


def _port(root: Path, registry: StepRegistry, *, environment: dict[str, str] | None = None) -> WorkflowContract[None]:
    return WorkflowContract[None](source=folder_toolset(root), vocabulary=FOLDER, expose=(), registry=registry, environment=environment or {})


async def _saved(root: Path, spec: Workflow, registry: StepRegistry, *, environment: dict[str, str] | None = None) -> str:
    return await save(
        spec,
        FolderLibrary(root),
        registry,
        title="Quarterly report",
        summary="Close the quarter",
        applicability="Quarterly closes only.",
        saved_by="test",
        environment=environment or {},
    )


async def test_save_then_fetch_reports_fresh(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    root = tmp_path / "workflows"
    workflow_id = await _saved(root, quarterly_report, registry)
    fetched = await _call(_port(root, registry), FETCH_TOOL, workflow_id=workflow_id)
    assert fetched["staleness"] == [], "same environment, so the check ran and found nothing moved"


async def test_a_drifted_step_marks_the_artifact_stale(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog
) -> None:
    root = tmp_path / "workflows"
    workflow_id = await _saved(root, quarterly_report, registry)

    changed = StepRegistry()
    register_quarterly_steps(changed, call_log, exclude=frozenset({"publish"}))

    async def publish(report: object, channel: str) -> str:
        del report, channel
        return "url"

    changed.register(publish)
    fetched = await _call(_port(root, changed), FETCH_TOOL, workflow_id=workflow_id)
    assert fetched["staleness"], "the publish contract moved; the fetch must say so"
    assert any("publish" in finding for finding in fetched["staleness"])
    assert "STALE" in fetched["fit"], "the verdict rides in the prose the model actually reads"


async def test_an_unstamped_artifact_reads_as_unknown(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    root = tmp_path / "workflows"
    store = FolderLibrary(root)
    workflow_id = await store.put(quarterly_report, title="t", summary="s", applicability="a", provenance={"saved_by": "old"})
    fetched = await _call(_port(root, registry), FETCH_TOOL, workflow_id=workflow_id)
    assert fetched["staleness"] is None, "no recorded stamp means unknown, and unknown is not fresh"


async def test_world_stamp_drift_is_reported(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    root = tmp_path / "workflows"
    workflow_id = await _saved(root, quarterly_report, registry, environment={"model": "gpt-5.2"})
    fetched = await _call(_port(root, registry, environment={"model": "gpt-6"}), FETCH_TOOL, workflow_id=workflow_id)
    assert any("gpt-5.2" in finding and "gpt-6" in finding for finding in fetched["staleness"])


async def test_the_stores_own_fetch_exposes_the_stamp(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    """The repository returns the aggregate's stamp too, not only through the port."""
    root = tmp_path / "workflows"
    workflow_id = await _saved(root, quarterly_report, registry)
    recorded = await FolderLibrary(root).fetch_fingerprint(workflow_id)
    assert recorded is not None
    assert set(recorded.steps) == {"fetch_sales", "fetch_costs", "reconcile", "draft_report", "publish"}


STALE_ACTION = "Tell the person what drifted before you offer or run it, and run it only if they still want it."


async def test_a_stale_workflow_that_fits_never_reads_as_runnable_as_it_stands(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    root = tmp_path / "workflows"
    workflow_id = await _saved(root, quarterly_report, registry, environment={"model": "gpt-5.2"})
    fetched = await _call(_port(root, registry, environment={"model": "gpt-6"}), FETCH_TOOL, workflow_id=workflow_id)

    assert fetched["fits_here"] is True, "every step it names is still registered here"
    assert fetched["fit"].startswith("STALE: the environment moved under this workflow: environment stamp 'model' changed")
    assert "as it stands" not in fetched["fit"]
    assert "FITS THIS RUN" not in fetched["fit"]
    assert fetched["fit"].endswith(f"Every step it names is still registered here, but what moved may change what it does. {STALE_ACTION}")


async def test_a_fresh_workflow_that_fits_still_runs_as_it_stands(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    root = tmp_path / "workflows"
    workflow_id = await _saved(root, quarterly_report, registry)
    fetched = await _call(_port(root, registry), FETCH_TOOL, workflow_id=workflow_id)

    assert fetched["fit"] == "FITS THIS RUN: every step it names is registered here, so it can be executed as it stands."


def test_a_stale_workflow_that_brings_its_own_steps_asks_the_person_first(tmp_path: Path) -> None:
    async def perform(task: object, output_type: type, /, **inputs: object) -> object:
        del task, inputs
        return output_type()

    briefed = StepRegistry(perform=perform)
    port = _port(tmp_path, briefed)

    stale, stale_fits = port._fit(("judge",), brought=("judge",), stale=True)
    fresh, fresh_fits = port._fit(("judge",), brought=("judge",))

    assert (stale_fits, fresh_fits) == (True, True)
    assert stale == (
        "The steps it names are still registered here, and the 1 step(s) it brings with it (judge) can be built from their "
        f"briefings, but what moved may change what it does. {STALE_ACTION}"
    )
    assert fresh.endswith("can be built from their briefings, so it runs as it stands.")
