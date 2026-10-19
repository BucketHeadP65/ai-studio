"""The library: every candidate with its trust level and whether this run could execute it.

Untested until a live failure pointed at it, and the failure was not in this route at all --
`InterfaceError: cannot perform operation: another operation is in progress`, because the app
held one asyncpg *connection* and a connection cannot multiplex. The listing asks two questions
of every artifact at once, so the panel broke the moment the library held two of them.

The fix was a pool in the composition root, which no test here can reach. What these tests pin
is the requirement that made a pool necessary: **the listing reads concurrently**. Serialising it
would have made the same symptom go away and left the next concurrent reader -- a second tab, or
the agent fetching a spec while the browser polls -- to rediscover it, with 2N round trips to a
remote database as the consolation prize.
"""

from __future__ import annotations

import asyncio
import json

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from core.ai.workflows.contracts import (
    Approval,
    FromNode,
    FromParameter,
    Grade,
    InputPresentation,
    JudgeGate,
    LibraryError,
    Outcome,
    ParameterSpec,
    PersonGate,
    RunNode,
    SavedWorkflow,
    StepRegistry,
    Workflow,
    WorkflowPresentation,
    spec_identity,
)

from harness.features.browsing.controller import BrowsingController
from harness.features.running.domain.ended_run import RunArchiveRecord
from harness.features.running.spend import RunSpendHeader, rows_for
from harness.session import NoSessionLog, Runtime
from harness.settings import SelfAuthoringSettings
from tests.support.fakes.sessions import FakeSessionPool
from tests.support.state import a_state, records_under
from tests.unit.test_run_spend import _response

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class _NoAgent:
    """The listing never runs the agent; the session just has to hold one."""


def _controller(runtime: Runtime, tmp_path: Path) -> BrowsingController:
    """A controller over `runtime`, with a fake standing in for its one collaborator."""
    return BrowsingController(
        sessions=FakeSessionPool(runtime),
        archive=records_under(tmp_path).archive,
        spend=records_under(tmp_path).spend,
        evidence=records_under(tmp_path).evidence,
        self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"),
    )


class WatchfulStore:
    """Serves two specs and records how many reads were in flight at once."""

    def __init__(self, specs: dict[str, Workflow]) -> None:
        self._specs = specs
        self.in_flight = 0
        self.peak = 0
        self._archived: dict[str, datetime] = {}

    async def search(self, task: str, *, limit: int = 5) -> Sequence[SavedWorkflow]:
        del task, limit
        return tuple(SavedWorkflow(id=key, title=key, summary="") for key in self._specs)

    async def set_archived(self, workflow_id: str, *, archived: bool) -> None:
        if archived:
            self._archived.setdefault(workflow_id, datetime.now(UTC))
        else:
            self._archived.pop(workflow_id, None)

    async def archived(self, *, limit: int = 50) -> Sequence[SavedWorkflow]:
        del limit
        return tuple(SavedWorkflow(id=key, title=key, summary="", archived_at=stamp) for key, stamp in self._archived.items())

    async def fetch_workflow(self, workflow_id: str) -> Workflow:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0)  # a real read yields here; a serial route never overlaps
            return self._specs[workflow_id]
        finally:
            self.in_flight -= 1


@pytest.fixture
def library(quarterly_report: Workflow) -> WatchfulStore:
    return WatchfulStore({"wf.a": quarterly_report, "wf.b": quarterly_report})


@pytest.fixture
def controller(library: WatchfulStore, registry: StepRegistry, tmp_path: Path) -> BrowsingController:
    runtime = Runtime(registry=registry, library=library, agent=_NoAgent(), session_log=NoSessionLog(), state=a_state(tmp_path))  # type: ignore[arg-type]
    return _controller(runtime, tmp_path)


async def _cards(controller: BrowsingController, *, archived: bool = False) -> list[dict[str, Any]]:
    return json.loads(bytes((await controller.library_listing(archived=archived)).body))


class CatalogStore:
    """A library that can list everything, distinct from what a search by word would find.

    Proof that the listing goes through `catalog()` rather than a search for the word "workflow".
    """

    def __init__(self) -> None:
        self.catalog_calls = 0
        self.search_calls: list[str] = []

    async def search(self, task: str, *, limit: int = 5) -> Sequence[SavedWorkflow]:
        del limit
        self.search_calls.append(task)
        return (SavedWorkflow(id="wf.search-hit", title="search hit", summary=""),)

    async def catalog(self, *, limit: int = 50) -> Sequence[SavedWorkflow]:
        del limit
        self.catalog_calls += 1
        return (
            SavedWorkflow(id="wf.catalog-a", title="a", summary="", score=0.0),
            SavedWorkflow(id="wf.catalog-b", title="b", summary="", score=0.0),
        )

    async def archived(self, *, limit: int = 50) -> Sequence[SavedWorkflow]:
        del limit
        return ()

    async def fetch_workflow(self, workflow_id: str) -> Workflow:
        msg = f"no workflow {workflow_id!r} here"
        raise LibraryError(msg)


async def test_the_shelf_answers_a_card_per_artifact_with_its_tier(controller: BrowsingController) -> None:
    """Trust is on the card because the alternative is clicking through to find out."""
    cards = await _cards(controller)
    assert [card["id"] for card in cards] == ["wf.a", "wf.b"]
    assert all(card["trust"]["level"] == "unverified" for card in cards), "nothing has run these"
    assert all(card["missing"] == [] for card in cards), "this registry has the steps they name"
    assert all(card["archived_at"] is None for card in cards), "nothing on this library is archived"


async def test_archived_true_lists_only_the_archived_cards(library: WatchfulStore, controller: BrowsingController) -> None:
    await library.set_archived("wf.a", archived=True)

    cards = await _cards(controller, archived=True)

    assert [card["id"] for card in cards] == ["wf.a"]
    assert cards[0]["archived_at"] is not None
    assert cards[0]["archived_at"].endswith(("+00:00", "Z")), "the SPA's zod schema requires an offset"


async def test_a_card_says_which_version_it_is_rather_than_leaving_it_in_the_id(controller: BrowsingController) -> None:
    """Now that a page is a version, the library can hold two of the same artifact side by side.

    Which version is a fact about the artifact, so the route says it. The alternative was the
    browser reading it out of the id -- and the id's shape is the library's business (`wf.x.v4`
    here, `x@4` in a folder), so a card that parsed one would be wrong about the other and would
    label this one "lit check decision record.v4".
    """
    cards = await _cards(controller)
    assert all(card["version"] == 3 for card in cards), "the fixture is at v3, and the card says so"


async def test_the_shelf_reads_every_artifact_at_once(library: WatchfulStore, controller: BrowsingController) -> None:
    """Which is why the library handle has to be a pool, and the whole of what this route requires.

    Two questions per artifact against a remote database: serial, a library of twelve is
    twenty-four round trips to Azure before the panel draws anything.
    """
    await _cards(controller)
    assert library.peak > 1, "the reads were serialised -- a pool is pointless and the panel is slow"


async def test_a_card_carries_its_grades_beside_its_tier(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """The card already answers *reliable*. It must not silently drop *right* when there is one.

    wf.b is a different version, because two workflows equal as values share their evidence.
    """
    library = WatchfulStore({"wf.a": quarterly_report, "wf.b": quarterly_report.model_copy(update={"version": quarterly_report.version + 1})})
    runtime = Runtime(registry=registry, library=library, agent=_NoAgent(), session_log=NoSessionLog(), state=a_state(tmp_path))  # type: ignore[arg-type]
    controller = _controller(runtime, tmp_path)
    records = records_under(tmp_path)
    await records.evidence.append(
        Grade(
            workflow_id="wf.a",
            spec_hash=spec_identity(quarterly_report),
            workflow_version=quarterly_report.version,
            judge_kind="structural",
            judge="recall/groundtruth-research-v1",
            measure="evidence-recall",
            score=0.83,
        )
    )
    cards = {card["id"]: card for card in await _cards(controller)}
    assert cards["wf.a"]["trust"]["structural_grades"] == 1
    assert cards["wf.a"]["trust"]["structural_mean"] == 0.83
    assert cards["wf.a"]["trust"]["structural_last"] == 0.83
    assert cards["wf.b"]["trust"]["structural_grades"] == 0, "no grade was recorded for this one"


async def test_a_card_keeps_the_trust_a_workflow_earned_before_it_moved(controller: BrowsingController, quarterly_report: Workflow, tmp_path: Path) -> None:
    """The approval was filed under the id the workflow had before a re-key, and it still counts."""
    records = records_under(tmp_path)
    await records.evidence.append(
        Approval(workflow_id="wf.before-the-move", spec_hash=spec_identity(quarterly_report), workflow_version=quarterly_report.version, by="owner")
    )

    cards = {card["id"]: card for card in await _cards(controller)}

    assert cards["wf.a"]["trust"]["level"] == "approved"


async def test_a_card_reports_the_trust_the_workflow_page_reports(controller: BrowsingController, quarterly_report: Workflow, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    await records.evidence.append(
        Approval(workflow_id="wf.before-the-move", spec_hash=spec_identity(quarterly_report), workflow_version=quarterly_report.version, by="owner")
    )
    await records.evidence.append(Approval(workflow_id="wf.a", spec_hash="another", workflow_version=quarterly_report.version, by="owner"))

    cards = {card["id"]: card for card in await _cards(controller)}

    for workflow_id in ("wf.a", "wf.b"):
        page = json.loads(bytes((await controller.stored_workflow(workflow_id)).body))
        assert list(cards[workflow_id]["trust"].items()) == list(page["trust"].items()), workflow_id


async def test_a_card_whose_workflow_cannot_be_read_reports_an_empty_history(tmp_path: Path) -> None:
    class _BrokenStore:
        async def search(self, task: str, *, limit: int = 5) -> Sequence[SavedWorkflow]:
            del task, limit
            return (SavedWorkflow(id="wf.broken", title="wf.broken", summary=""),)

        async def fetch_workflow(self, workflow_id: str) -> Workflow:
            msg = f"no workflow {workflow_id!r} here"
            raise LibraryError(msg)

    runtime = Runtime(registry=StepRegistry(), library=_BrokenStore(), agent=_NoAgent(), session_log=NoSessionLog(), state=a_state(tmp_path))  # type: ignore[arg-type]
    await records_under(tmp_path).evidence.append(Approval(workflow_id="wf.broken", spec_hash="abc", by="owner"))

    cards = await _cards(_controller(runtime, tmp_path))

    assert cards[0]["trust"] == {
        "level": "unverified",
        "runs_with_evidence": 0,
        "failed_runs": 0,
        "approved_by": [],
        "structural_grades": 0,
        "structural_mean": None,
        "structural_last": None,
    }


async def test_a_card_reports_its_shape_from_the_graph(registry: StepRegistry, tmp_path: Path) -> None:
    """One step, one person gate, one agent gate -- counted from the same graph the workflow plugin draws."""

    async def draft_step(topic: str) -> str:
        return f"draft about {topic}"

    async def judge_plainly(text: str) -> bool:
        return True

    registry.register(draft_step)
    registry.register(judge_plainly)
    spec = Workflow(
        name="dual-gate",
        version=1,
        description="one step, one person gate, one agent gate",
        parameters={"topic": ParameterSpec(description="topic", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="draft", work="draft_step", inputs={"topic": FromParameter(parameter="topic")}),
            PersonGate(id="approve", reviews=FromNode(node="draft"), rejected_to="draft"),
            JudgeGate(id="quality", work="judge_plainly", reviews=FromNode(node="approve"), rejected_to="draft"),
        ),
        output=FromNode(node="quality"),
    )
    library = WatchfulStore({"wf.dual": spec})
    runtime = Runtime(registry=registry, library=library, agent=_NoAgent(), session_log=NoSessionLog(), state=a_state(tmp_path))  # type: ignore[arg-type]

    cards = await _cards(_controller(runtime, tmp_path))

    assert cards[0]["shape"] == {"steps": 1, "person_gates": 1, "agent_gates": 1}


async def test_a_card_whose_spec_cannot_be_read_reports_zero_shape(tmp_path: Path) -> None:
    class _BrokenStore:
        async def search(self, task: str, *, limit: int = 5) -> Sequence[SavedWorkflow]:
            del task, limit
            return (SavedWorkflow(id="wf.broken", title="wf.broken", summary=""),)

        async def fetch_workflow(self, workflow_id: str) -> Workflow:
            msg = f"no workflow {workflow_id!r} here"
            raise LibraryError(msg)

    runtime = Runtime(registry=StepRegistry(), library=_BrokenStore(), agent=_NoAgent(), session_log=NoSessionLog(), state=a_state(tmp_path))  # type: ignore[arg-type]

    cards = await _cards(_controller(runtime, tmp_path))

    assert cards[0]["shape"] == {"steps": 0, "person_gates": 0, "agent_gates": 0}


async def test_a_card_reports_its_newest_run(controller: BrowsingController, tmp_path: Path) -> None:
    """`runs.last` is the newest by `ended_at`, not insertion order."""
    records = records_under(tmp_path)
    now = datetime.now(UTC)
    older = RunArchiveRecord(
        run_id="run-older",
        workflow_id="wf.a",
        outcome="completed",
        result=Outcome(status="unknown", summary="no result declared"),
        started_at=now.isoformat(),
        ended_at=(now - timedelta(hours=2)).isoformat(),
        runner="app:harness",
        session_id="",
        params={},
        spec={},
        values={},
        events=[],
    )
    newer = RunArchiveRecord(
        run_id="run-newer",
        workflow_id="wf.a",
        outcome="failed",
        result=Outcome(status="unknown", summary="no result declared"),
        started_at=now.isoformat(),
        ended_at=(now - timedelta(minutes=5)).isoformat(),
        runner="app:harness",
        session_id="",
        params={},
        spec={},
        values={},
        events=[],
    )
    records.archive.put(older)
    records.archive.put(newer)

    cards = {card["id"]: card for card in await _cards(controller)}

    assert cards["wf.a"]["runs"] == {
        "last": {"outcome": "failed", "started_at": newer.started_at, "ended_at": newer.ended_at},
    }
    assert cards["wf.b"]["runs"] == {"last": None}, "a workflow that never ran reports null"


async def test_a_card_carries_the_spend_across_its_runs(controller: BrowsingController, tmp_path: Path) -> None:
    """The two runs are wf.a's own. wf.b never ran and reports a zero spend, not a missing one."""
    records = records_under(tmp_path)
    ledger = records.spend
    for run_id in ("r-1", "r-2"):
        ledger.begin(
            RunSpendHeader(
                run_id=run_id, workflow_id="wf.a", runner="app:harness", session_id="s", started_at=f"2026-08-2{run_id[-1]}T10:00:00+00:00"
            )
        )
        ledger.record(run_id, "wf.a", rows_for("draft", [_response()]))

    cards = {card["id"]: card for card in await _cards(controller)}

    assert cards["wf.a"]["spend"]["runs"] == 2
    assert Decimal(cards["wf.a"]["spend"]["cost_usd"]) > Decimal(0)
    assert cards["wf.b"]["spend"] == {"cost_usd": "0", "unpriced": 0, "runs": 0}
    assert cards["wf.a"]["spend"]["unpriced"] == 0, "every response of a priced run has a figure"


async def test_the_shelf_lists_through_the_catalog_when_the_store_has_one(registry: StepRegistry, tmp_path: Path) -> None:
    """Without a `task`, the library is a listing -- not a search for the word "workflow"."""
    library = CatalogStore()
    runtime = Runtime(registry=registry, library=library, agent=_NoAgent(), session_log=NoSessionLog(), state=a_state(tmp_path))  # type: ignore[arg-type]

    cards = await _cards(_controller(runtime, tmp_path))

    assert [card["id"] for card in cards] == ["wf.catalog-a", "wf.catalog-b"]
    assert library.catalog_calls == 1
    assert library.search_calls == [], "the catalog answered; no search for the word 'workflow' was made"


async def test_a_real_task_still_searches_even_when_the_store_can_catalog(registry: StepRegistry, tmp_path: Path) -> None:
    """`?task=...` names a real search, catalog or not."""
    library = CatalogStore()
    runtime = Runtime(registry=registry, library=library, agent=_NoAgent(), session_log=NoSessionLog(), state=a_state(tmp_path))  # type: ignore[arg-type]

    cards = json.loads(bytes((await _controller(runtime, tmp_path).library_listing(task="biography of einstein")).body))

    assert [card["id"] for card in cards] == ["wf.search-hit"]
    assert library.search_calls == ["biography of einstein"]
    assert library.catalog_calls == 0


class PresentingStore(WatchfulStore):
    """The same two specs, one of them with the author's words on its card."""

    presentation = WorkflowPresentation(
        purpose="Roll the quarter up into one report.",
        produces="The published report.",
        inputs=(InputPresentation(key="quarter", label="Which quarter"),),
    )

    async def search(self, task: str, *, limit: int = 5) -> Sequence[SavedWorkflow]:
        del task, limit
        return (
            SavedWorkflow(id="wf.a", title="a", summary="", presentation=self.presentation),
            SavedWorkflow(id="wf.b", title="b", summary=""),
        )

    async def archived(self, *, limit: int = 50) -> Sequence[SavedWorkflow]:
        del limit
        return (SavedWorkflow(id="wf.a", title="a", summary="", archived_at=datetime.now(UTC), presentation=self.presentation),)


async def test_a_listed_card_carries_the_presentation_its_library_kept(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """One card has the author's words, the other has none, and the listing says so both times."""
    library = PresentingStore({"wf.a": quarterly_report, "wf.b": quarterly_report})
    runtime = Runtime(registry=registry, library=library, agent=_NoAgent(), session_log=NoSessionLog(), state=a_state(tmp_path))  # type: ignore[arg-type]

    cards = await _cards(_controller(runtime, tmp_path))

    assert [card["presentation"] for card in cards] == [PresentingStore.presentation.model_dump(mode="json"), None]


async def test_an_archived_card_keeps_its_presentation_too(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    library = PresentingStore({"wf.a": quarterly_report, "wf.b": quarterly_report})
    runtime = Runtime(registry=registry, library=library, agent=_NoAgent(), session_log=NoSessionLog(), state=a_state(tmp_path))  # type: ignore[arg-type]

    cards = await _cards(_controller(runtime, tmp_path), archived=True)

    assert [card["presentation"] for card in cards] == [PresentingStore.presentation.model_dump(mode="json")]
