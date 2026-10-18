"""The card plugins: GET carries the prose a chooser reads, PATCH rewrites it. The spec never moves.

`PATCH /api/workflow/{id}/card` calls `library.redescribe`; `GET /api/workflow/{id}` answers with
the refreshed card alongside everything it already answered -- additive, wire-compatible.
"""

from __future__ import annotations

import json

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from core.ai.workflows.contracts import (
    InputPresentation,
    LibraryError,
    ResultFieldPresentation,
    SavedWorkflow,
    StepRegistry,
    Workflow,
    WorkflowIntent,
    WorkflowPresentation,
    verify_presentation,
)
from core.ai.workflows.domain.presentation import SaveNoteAction

from harness.domain.readiness import WorkflowPresentationPatch
from harness.features.browsing.api_models import WorkflowCardPatch
from harness.features.browsing.controller import BrowsingController
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
    """Neither plugin runs the agent; the session just has to hold one."""


class FakeCardStore:
    """A library that answers `fetch_card`, and rewrites it on `redescribe`."""

    def __init__(self, specs: dict[str, Workflow], cards: dict[str, SavedWorkflow] | None = None) -> None:
        self._specs = specs
        self._cards = dict(cards or {})
        self._archived: dict[str, datetime] = {}

    async def search(self, task: str, *, limit: int = 5) -> Sequence[SavedWorkflow]:
        del task, limit
        return ()

    async def set_archived(self, workflow_id: str, *, archived: bool) -> None:
        if workflow_id not in self._specs:
            msg = f"no such workflow: {workflow_id}"
            raise LibraryError(msg)
        if archived:
            self._archived.setdefault(workflow_id, datetime.now(UTC))
        else:
            self._archived.pop(workflow_id, None)

    async def archived(self, *, limit: int = 50) -> Sequence[SavedWorkflow]:
        del limit
        cards = [await self.fetch_card(workflow_id) for workflow_id in self._archived]
        return tuple(card for card in cards if card is not None)

    async def fetch_workflow(self, workflow_id: str) -> Workflow:
        try:
            return self._specs[workflow_id]
        except KeyError as exc:
            msg = f"no such workflow: {workflow_id}"
            raise LibraryError(msg) from exc

    async def put(self, *args: object, **kwargs: object) -> str:
        del args, kwargs
        raise NotImplementedError

    async def fetch_card(self, workflow_id: str) -> SavedWorkflow | None:
        card = self._cards.get(workflow_id)
        if card is None:
            return None
        return replace(card, archived_at=self._archived.get(workflow_id))

    async def redescribe(
        self, workflow_id: str, *, title: str, summary: str, applicability: str, presentation: WorkflowPresentation | None = None
    ) -> None:
        if workflow_id not in self._specs:
            msg = f"no such workflow: {workflow_id}"
            raise LibraryError(msg)
        if presentation is not None:
            verify_presentation(self._specs[workflow_id], presentation)
        held = self._cards.get(workflow_id)
        self._cards[workflow_id] = SavedWorkflow(
            id=workflow_id,
            title=title,
            summary=summary,
            applicability=applicability,
            provisional=held.provisional if held is not None else True,
            presentation=presentation if presentation is not None else (held.presentation if held is not None else None),
            # A rewrite of the card's prose says nothing about what was asked for, so it keeps it.
            intent=held.intent if held is not None else None,
        )


class CardlessStore(FakeCardStore):
    """The same library, minus `fetch_card` -- a backend that predates the capability."""

    fetch_card = None  # type: ignore[assignment]


def _install(library: FakeCardStore, registry: StepRegistry, tmp_path: Path) -> BrowsingController:
    runtime = Runtime(registry=registry, library=library, agent=_NoAgent(), session_log=NoSessionLog(), state=a_state(tmp_path))  # type: ignore[arg-type]
    return BrowsingController(
        sessions=FakeSessionPool(runtime),
        archive=records_under(tmp_path).archive,
        spend=records_under(tmp_path).spend,
        evidence=records_under(tmp_path).evidence,
        self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"),
    )


async def _get(controller: BrowsingController, workflow_id: str) -> dict[str, Any]:
    return json.loads(bytes((await controller.stored_workflow(workflow_id)).body))


async def _patch(controller: BrowsingController, workflow_id: str, title: str, summary: str, applicability: str) -> tuple[int, dict[str, Any]]:
    response = await controller.redescribe(workflow_id, WorkflowCardPatch(title=title, summary=summary, applicability=applicability))
    return response.status_code, json.loads(bytes(response.body))


async def _archive(controller: BrowsingController, workflow_id: str) -> tuple[int, dict[str, Any]]:
    response = await controller.archive(workflow_id)
    return response.status_code, json.loads(bytes(response.body))


async def _restore(controller: BrowsingController, workflow_id: str) -> tuple[int, dict[str, Any]]:
    response = await controller.restore(workflow_id)
    return response.status_code, json.loads(bytes(response.body))


async def test_the_get_door_carries_the_card_beside_everything_it_already_answered(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    library = FakeCardStore(
        {"wf.report": quarterly_report},
        {"wf.report": SavedWorkflow(id="wf.report", title="Quarterly report", summary="Rolls up the quarter.", applicability="Use monthly.")},
    )
    controller = _install(library, registry, tmp_path)

    payload = await _get(controller, "wf.report")

    assert (payload["title"], payload["summary"], payload["applicability"]) == (
        "Quarterly report",
        "Rolls up the quarter.",
        "Use monthly.",
    )
    assert payload["provisional"] is True
    # Legacy fields an existing consumer already reads must still be there, unmoved.
    assert payload["name"] == quarterly_report.name
    assert payload["version"] == quarterly_report.version
    assert "graph" in payload
    assert payload["trust"]["level"] == "unverified"


async def test_the_get_door_carries_the_workflows_total_spend(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    library = FakeCardStore({"wf.report": quarterly_report})
    controller = _install(library, registry, tmp_path)
    ledger = records.spend
    ledger.begin(
        RunSpendHeader(
            run_id="run-1", workflow_id="wf.report", runner="app:harness", session_id="s", started_at="2026-08-26T10:00:00+00:00"
        )
    )
    ledger.record("run-1", "wf.report", rows_for("draft", [_response()]))

    payload = await _get(controller, "wf.report")

    assert payload["spend"]["runs"] == 1
    assert Decimal(payload["spend"]["cost_usd"]) > Decimal(0)


async def test_a_store_that_predates_the_card_falls_back_to_the_spec_s_own_words(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    controller = _install(CardlessStore({"wf.report": quarterly_report}), registry, tmp_path)

    payload = await _get(controller, "wf.report")

    assert payload["title"] == quarterly_report.name
    assert payload["summary"] == quarterly_report.description
    assert payload["applicability"] == ""
    assert payload["provisional"] is True


async def test_patch_rewrites_the_card_and_the_next_get_reads_it_back(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    library = FakeCardStore({"wf.report": quarterly_report})
    controller = _install(library, registry, tmp_path)

    status, body = await _patch(
        controller, "wf.report", "Quarterly report", "Rolls up the quarter.", "Use once a quarter, never for ad hoc asks."
    )

    assert status == 200
    assert body["ok"] is True
    assert (body["title"], body["summary"], body["applicability"]) == (
        "Quarterly report",
        "Rolls up the quarter.",
        "Use once a quarter, never for ad hoc asks.",
    )

    refreshed = await _get(controller, "wf.report")
    assert refreshed["title"] == "Quarterly report"
    assert refreshed["applicability"] == "Use once a quarter, never for ad hoc asks."


async def test_patch_never_touches_the_spec(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    library = FakeCardStore({"wf.report": quarterly_report})
    controller = _install(library, registry, tmp_path)

    await _patch(controller, "wf.report", "New title", "New summary", "New applicability")

    refreshed = await _get(controller, "wf.report")
    assert refreshed["name"] == quarterly_report.name
    assert refreshed["version"] == quarterly_report.version
    assert refreshed["description"] == quarterly_report.description


@pytest.mark.parametrize("blank", ["", "   "])
async def test_an_empty_title_is_refused_in_words(blank: str, quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    library = FakeCardStore({"wf.report": quarterly_report})
    controller = _install(library, registry, tmp_path)

    status, body = await _patch(controller, "wf.report", blank, "s", "a")

    assert status == 400
    assert body["ok"] is False
    assert "title" in body["message"]
    assert body["message"].strip().isascii()


async def test_patching_an_unknown_id_answers_404(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    library = FakeCardStore({"wf.report": quarterly_report})
    controller = _install(library, registry, tmp_path)

    status, body = await _patch(controller, "wf.nope", "t", "s", "a")

    assert status == 404
    assert body["ok"] is False


async def test_archiving_sets_archived_at_and_the_fake_recorded_the_call(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    library = FakeCardStore(
        {"wf.report": quarterly_report}, {"wf.report": SavedWorkflow(id="wf.report", title="Quarterly report", summary="Rolls up.")}
    )
    controller = _install(library, registry, tmp_path)

    status, body = await _archive(controller, "wf.report")

    assert status == 200
    assert body["ok"] is True
    assert body["archived_at"] is not None
    assert body["archived_at"].endswith(("+00:00", "Z")), "the SPA's zod schema requires an offset"
    assert [card.id for card in await library.archived()] == ["wf.report"]

    refreshed = await _get(controller, "wf.report")
    assert refreshed["archived_at"] is not None
    assert refreshed["archived_at"].endswith(("+00:00", "Z"))


async def test_restoring_clears_archived_at(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    library = FakeCardStore(
        {"wf.report": quarterly_report}, {"wf.report": SavedWorkflow(id="wf.report", title="Quarterly report", summary="Rolls up.")}
    )
    controller = _install(library, registry, tmp_path)
    await _archive(controller, "wf.report")

    status, body = await _restore(controller, "wf.report")

    assert status == 200
    assert body["archived_at"] is None
    refreshed = await _get(controller, "wf.report")
    assert refreshed["archived_at"] is None


async def test_archiving_an_unknown_id_answers_404(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    library = FakeCardStore({"wf.report": quarterly_report})
    controller = _install(library, registry, tmp_path)

    status, body = await _archive(controller, "wf.nope")

    assert status == 404
    assert body["ok"] is False


async def test_restoring_an_unknown_id_answers_404(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    library = FakeCardStore({"wf.report": quarterly_report})
    controller = _install(library, registry, tmp_path)

    status, body = await _restore(controller, "wf.nope")

    assert status == 404
    assert body["ok"] is False


def _presentation() -> WorkflowPresentation:
    """The author's words, as a library hands them to this route."""
    return WorkflowPresentation(
        purpose="Roll the quarter up into one report.",
        produces="The published report, and the numbers behind it.",
        owner_participation="You approve the draft before it is published.",
        not_for="An ad hoc question about one month.",
        inputs=(InputPresentation(key="quarter", label="Which quarter"),),
        result_fields=(ResultFieldPresentation(label="Net", path="values.net"),),
        actions=(SaveNoteAction(id="keep_the_report", label="Save the report", artifact_id="report"),),
    )


async def test_the_get_door_carries_the_presentation_the_library_kept(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    presentation = _presentation()
    library = FakeCardStore(
        {"wf.report": quarterly_report},
        {"wf.report": SavedWorkflow(id="wf.report", title="Quarterly report", summary="Rolls up.", presentation=presentation)},
    )
    controller = _install(library, registry, tmp_path)

    payload = await _get(controller, "wf.report")

    assert payload["presentation"] == presentation.model_dump(mode="json")


async def test_a_workflow_whose_library_kept_no_presentation_answers_null(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """Absent stays absent: this route composes no words of its own."""
    library = FakeCardStore(
        {"wf.report": quarterly_report}, {"wf.report": SavedWorkflow(id="wf.report", title="Quarterly report", summary="Rolls up.")}
    )
    controller = _install(library, registry, tmp_path)

    assert (await _get(controller, "wf.report"))["presentation"] is None


async def test_patch_archive_and_restore_all_answer_with_the_presentation_intact(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    presentation = _presentation()
    library = FakeCardStore(
        {"wf.report": quarterly_report},
        {"wf.report": SavedWorkflow(id="wf.report", title="Quarterly report", summary="Rolls up.", presentation=presentation)},
    )
    controller = _install(library, registry, tmp_path)

    _, patched = await _patch(controller, "wf.report", "Quarterly report", "Rolls up the quarter.", "Use once a quarter.")
    _, archived = await _archive(controller, "wf.report")
    _, restored = await _restore(controller, "wf.report")

    expected = presentation.model_dump(mode="json")
    assert [patched["presentation"], archived["presentation"], restored["presentation"]] == [expected, expected, expected]


# -- described readiness on the card route ---------------------------------------------

PROSE_FIELDS = ["purpose", "produces", "owner_participation", "not_for"]


async def _patch_body(controller: BrowsingController, workflow_id: str, body: WorkflowCardPatch) -> tuple[int, dict[str, Any]]:
    response = await controller.redescribe(workflow_id, body)
    return response.status_code, json.loads(bytes(response.body))


def _legacy(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> tuple[FakeCardStore, BrowsingController]:
    """A card from before anybody wrote the author's words down: prose in three fields, no presentation."""
    library = FakeCardStore(
        {"wf.report": quarterly_report},
        {"wf.report": SavedWorkflow(id="wf.report", title="Quarterly report", summary="Rolls up the quarter.", applicability="Use monthly.")},
    )
    return library, _install(library, registry, tmp_path)


def _fields(body: dict[str, Any], severity: str) -> list[str]:
    return [finding["field"] for finding in body["readiness"]["findings"] if finding["severity"] == severity]


def _refused(body: dict[str, Any]) -> list[str]:
    """The fields the refusal itself named, which is the subset this patch actually carried."""
    return [line.split(":", maxsplit=1)[0].strip() for line in body["message"].splitlines()[1:]]


async def test_a_legacy_card_takes_a_valid_title_and_still_reports_what_it_lacks(
    quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path
) -> None:
    """A partial edit is allowed to land. It is never allowed to look finished."""
    library, controller = _legacy(quarterly_report, registry, tmp_path)
    before = await _get(controller, "wf.report")

    status, body = await _patch_body(controller, "wf.report", WorkflowCardPatch(title="Quarterly close"))

    assert status == 200
    assert body["title"] == "Quarterly close"
    assert body["summary"] == "Rolls up the quarter.", "a field the patch left out is unchanged, not blanked"
    assert body["applicability"] == "Use monthly."
    assert body["readiness"]["described"] is False
    assert sorted(_fields(body, "error")) == sorted(PROSE_FIELDS)
    card = await library.fetch_card("wf.report")
    assert card is not None
    assert card.title == "Quarterly close"
    assert (await _get(controller, "wf.report"))["trust"] == before["trust"], "describing a workflow earns it nothing"


async def test_a_placeholder_title_is_refused_and_nothing_is_written(
    quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path
) -> None:
    library, controller = _legacy(quarterly_report, registry, tmp_path)

    status, body = await _patch_body(controller, "wf.report", WorkflowCardPatch(title="(not stated)"))

    assert status == 400
    assert body["ok"] is False
    assert "title" in body["message"]
    assert _refused(body) == ["title"], "only the field this patch carried refuses it"
    assert sorted(_fields(body, "error")) == sorted(["title", *PROSE_FIELDS]), "and the whole reading still rides along"
    card = await library.fetch_card("wf.report")
    assert card is not None
    assert card.title == "Quarterly report", "a refused patch leaves the card exactly as it was"


async def test_a_title_over_eighty_characters_is_refused(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    library, controller = _legacy(quarterly_report, registry, tmp_path)

    status, body = await _patch_body(controller, "wf.report", WorkflowCardPatch(title="x" * 81))

    assert status == 400
    assert _refused(body) == ["title"]
    card = await library.fetch_card("wf.report")
    assert card is not None
    assert card.title == "Quarterly report"


async def test_a_card_whose_author_wrote_every_word_reads_described(
    quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path
) -> None:
    library = FakeCardStore(
        {"wf.report": quarterly_report},
        {
            "wf.report": SavedWorkflow(
                id="wf.report",
                title="Quarterly report",
                summary="Rolls up the quarter.",
                applicability="Use monthly.",
                presentation=_presentation(),
            )
        },
    )
    controller = _install(library, registry, tmp_path)

    status, body = await _patch_body(controller, "wf.report", WorkflowCardPatch(summary="Rolls the quarter up into one report."))

    assert status == 200
    assert body["readiness"]["described"] is True
    assert body["readiness"]["findings"] == []


async def test_the_get_door_carries_the_same_readiness_the_patch_answers_with(
    quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path
) -> None:
    """One reading, wherever it is asked for, so a form never disagrees with the gate."""
    _library, controller = _legacy(quarterly_report, registry, tmp_path)

    payload = await _get(controller, "wf.report")

    assert payload["readiness"]["described"] is False
    assert sorted(_fields(payload, "error")) == sorted(PROSE_FIELDS)


# -- writing the author's words through the card route ---------------------------------

FOUR_SENTENCES = WorkflowPresentationPatch(
    purpose="Roll the quarter up into one report.",
    produces="The published report, and the numbers behind it.",
    owner_participation="You approve the draft before it is published.",
    not_for="An ad hoc question about one month.",
)


async def test_a_legacy_card_can_be_described_through_the_card_route(
    quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path
) -> None:
    """The one screen a person can answer the gate from: the four sentences land and it reads described."""
    library, controller = _legacy(quarterly_report, registry, tmp_path)

    status, body = await _patch_body(controller, "wf.report", WorkflowCardPatch(presentation=FOUR_SENTENCES))

    assert status == 200
    assert body["readiness"]["described"] is True
    assert body["presentation"]["purpose"] == "Roll the quarter up into one report."
    card = await library.fetch_card("wf.report")
    assert card is not None
    assert card.presentation is not None
    assert card.presentation.not_for == "An ad hoc question about one month."
    assert (await _get(controller, "wf.report"))["readiness"]["described"] is True


async def test_a_placeholder_sentence_is_refused_and_nothing_is_written(
    quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path
) -> None:
    library, controller = _legacy(quarterly_report, registry, tmp_path)

    status, body = await _patch_body(
        controller, "wf.report", WorkflowCardPatch(presentation=FOUR_SENTENCES.model_copy(update={"produces": "  Coming   Soon "}))
    )

    assert status == 400
    assert _refused(body) == ["produces"]
    card = await library.fetch_card("wf.report")
    assert card is not None
    assert card.presentation is None, "a refused patch writes no words at all"


async def test_one_sentence_at_a_time_keeps_the_others(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """Absent means unchanged one level down too, so a form that owns one box sends that box."""
    library, controller = _legacy(quarterly_report, registry, tmp_path)
    await _patch_body(controller, "wf.report", WorkflowCardPatch(presentation=FOUR_SENTENCES))

    status, body = await _patch_body(
        controller, "wf.report", WorkflowCardPatch(presentation=WorkflowPresentationPatch(purpose="Close the quarter in one pass."))
    )

    assert status == 200
    assert body["presentation"]["purpose"] == "Close the quarter in one pass."
    assert body["presentation"]["produces"] == "The published report, and the numbers behind it."
    card = await library.fetch_card("wf.report")
    assert card is not None
    assert card.presentation is not None
    assert card.presentation.owner_participation == "You approve the draft before it is published."


async def test_a_title_only_patch_leaves_the_words_the_library_holds(
    quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path
) -> None:
    library, controller = _legacy(quarterly_report, registry, tmp_path)
    await _patch_body(controller, "wf.report", WorkflowCardPatch(presentation=FOUR_SENTENCES))

    status, _body = await _patch_body(controller, "wf.report", WorkflowCardPatch(title="Quarterly close"))

    assert status == 200
    card = await library.fetch_card("wf.report")
    assert card is not None
    assert card.presentation is not None
    assert card.presentation.purpose == "Roll the quarter up into one report."


async def test_a_presentation_naming_what_the_spec_does_not_declare_is_refused(
    quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path
) -> None:
    """The library's own check reaches the route as a refusal, never as a crash."""
    library, controller = _legacy(quarterly_report, registry, tmp_path)

    status, body = await _patch_body(
        controller,
        "wf.report",
        WorkflowCardPatch(
            presentation=FOUR_SENTENCES.model_copy(update={"inputs": (InputPresentation(key="ghost", label="Nothing"),)})
        ),
    )

    assert status == 400
    assert "ghost" in body["message"]
    card = await library.fetch_card("wf.report")
    assert card is not None
    assert card.presentation is None


# -- what the workflow was asked for, on the card routes -------------------------------------


def _intent() -> WorkflowIntent:
    """What the person asked for, as a library hands it to this route."""
    return WorkflowIntent(
        request="Close the books at the end of every quarter",
        expected_result="The published report, and the numbers behind it.",
        success_example="Q2 closed on the first working day of July.",
        empty_input_behavior="A quarter with no entries reports that and publishes nothing.",
        failure_behavior="A ledger the run cannot read is named, and nothing is published.",
        trigger="scheduled",
        assumptions=("A quarter ends on the last day of March, June, September and December.",),
    )


async def test_the_get_door_carries_the_intent_the_library_kept(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    intent = _intent()
    library = FakeCardStore(
        {"wf.report": quarterly_report},
        {"wf.report": SavedWorkflow(id="wf.report", title="Quarterly report", summary="Rolls up.", intent=intent)},
    )
    controller = _install(library, registry, tmp_path)

    payload = await _get(controller, "wf.report")

    assert payload["intent"] == intent.model_dump(mode="json")


async def test_a_workflow_whose_library_kept_no_intent_answers_null(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """Absent stays absent: this route reads no intent off the spec."""
    library = FakeCardStore(
        {"wf.report": quarterly_report}, {"wf.report": SavedWorkflow(id="wf.report", title="Quarterly report", summary="Rolls up.")}
    )
    controller = _install(library, registry, tmp_path)

    assert (await _get(controller, "wf.report"))["intent"] is None


async def test_patch_archive_and_restore_all_answer_with_the_intent_intact(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """Rewriting the prose, archiving and restoring are card gestures. None of them touches the request."""
    intent = _intent()
    library = FakeCardStore(
        {"wf.report": quarterly_report},
        {"wf.report": SavedWorkflow(id="wf.report", title="Quarterly report", summary="Rolls up.", intent=intent)},
    )
    controller = _install(library, registry, tmp_path)

    _, patched = await _patch(controller, "wf.report", "Quarterly report", "Rolls up the quarter.", "Use once a quarter.")
    _, archived = await _archive(controller, "wf.report")
    _, restored = await _restore(controller, "wf.report")

    expected = intent.model_dump(mode="json")
    assert [patched["intent"], archived["intent"], restored["intent"]] == [expected, expected, expected]


async def test_a_half_stated_intent_reaches_the_route_exactly_as_it_was_stated(
    quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path
) -> None:
    """An incomplete intent is shown as it is. The readiness beside it still reads the card alone."""
    partial = WorkflowIntent(request="Close the books at the end of every quarter")
    library = FakeCardStore(
        {"wf.report": quarterly_report},
        {
            "wf.report": SavedWorkflow(
                id="wf.report", title="Quarterly report", summary="Rolls up.", presentation=_presentation(), intent=partial
            )
        },
    )
    controller = _install(library, registry, tmp_path)

    payload = await _get(controller, "wf.report")

    assert payload["intent"] == partial.model_dump(mode="json")
    assert payload["readiness"]["described"] is True, "the save gate reads the card, never the intent"
