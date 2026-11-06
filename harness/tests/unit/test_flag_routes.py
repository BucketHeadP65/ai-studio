"""The library's maintenance flags over HTTP: the list the Studio reads, and resolving one with who and why."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import FlagCard, LibraryCollection, StepRegistry, WorkflowLibrary

from harness.features.browsing.controller import BrowsingController
from harness.session import NoSessionLog, Runtime
from harness.settings import SelfAuthoringSettings
from tests.support.controllers import a_client_of
from tests.support.fakes.sessions import FakeSessionPool
from tests.support.flags import CHANGE_ID, DIVERGED, DIVERGED_ID, SETTLED_ID, UNREADABLE_MOVE, a_flag_library
from tests.support.runtime import EmptyLibrary, a_silent_agent
from tests.support.state import a_state, records_under

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    import httpx

    from tests.support.fakes.library_collection import FakeCollection


def _controller(library: WorkflowLibrary, home: Path) -> BrowsingController:
    runtime = Runtime(registry=StepRegistry(), library=library, agent=a_silent_agent(), session_log=NoSessionLog(), state=a_state(home))
    records = records_under(home)
    return BrowsingController(
        sessions=FakeSessionPool(runtime),
        archive=records.archive,
        spend=records.spend,
        evidence=records.evidence,
        self_authoring=SelfAuthoringSettings(custom_root=home / "custom"),
    )


@pytest.fixture
async def flagged(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, FakeCollection, LibraryCollection]]:
    store, library = await a_flag_library()
    async with a_client_of(_controller(library, tmp_path)) as client:
        yield client, store, library


async def test_the_open_flags_come_newest_first_with_their_evidence(flagged: tuple[httpx.AsyncClient, FakeCollection, LibraryCollection]) -> None:
    client, _, library = flagged

    response = await client.get("/api/library/flags", params={"status": "open"})

    assert response.status_code == 200
    cards = response.json()["flags"]
    assert [card["id"] for card in cards] == [DIVERGED_ID, CHANGE_ID]
    assert [FlagCard.model_validate(card) for card in cards] == list(await library.flags(status="open"))
    assert cards[0]["evidence"] == DIVERGED.evidence
    assert "+instructions: Say what changed since the last digest, in three lines." in cards[0]["evidence"]


async def test_raised_at_is_iso_8601_with_its_offset(flagged: tuple[httpx.AsyncClient, FakeCollection, LibraryCollection]) -> None:
    client, _, _ = flagged

    raised = (await client.get("/api/library/flags")).json()["flags"][0]["raised_at"]

    assert raised == "2026-11-05T11:00:00Z"
    assert datetime.fromisoformat(raised).utcoffset() is not None


async def test_a_kind_narrows_the_list_and_no_status_lists_both(flagged: tuple[httpx.AsyncClient, FakeCollection, LibraryCollection]) -> None:
    client, _, _ = flagged

    open_diverged = (await client.get("/api/library/flags", params={"status": "open", "kind": "diverged_copy"})).json()["flags"]
    every_diverged = (await client.get("/api/library/flags", params={"kind": "diverged_copy"})).json()["flags"]
    everything = (await client.get("/api/library/flags")).json()["flags"]

    assert [card["id"] for card in open_diverged] == [DIVERGED_ID]
    assert [(card["id"], card["status"]) for card in every_diverged] == [(DIVERGED_ID, "open"), (SETTLED_ID, "resolved")]
    assert [card["id"] for card in everything] == [DIVERGED_ID, CHANGE_ID, SETTLED_ID]


async def test_a_kind_the_library_does_not_know_is_refused(flagged: tuple[httpx.AsyncClient, FakeCollection, LibraryCollection]) -> None:
    client, _, _ = flagged

    response = await client.get("/api/library/flags", params={"kind": "diverged"})

    assert response.status_code == 422


async def test_resolving_answers_the_flag_resolved_and_keeps_who_and_why(flagged: tuple[httpx.AsyncClient, FakeCollection, LibraryCollection]) -> None:
    client, _, library = flagged

    response = await client.post(f"/api/library/flags/{DIVERGED_ID}/resolve", json={"by": "owner", "reason": "The shorter digest is wanted."})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    card = FlagCard.model_validate(payload["flag"])
    assert (card.id, card.status, card.evidence) == (DIVERGED_ID, "resolved", DIVERGED.evidence)
    stored = await library.fetch_flag(DIVERGED_ID)
    assert stored is not None
    assert (stored.status, stored.resolved_by, stored.decision_reason) == ("resolved", "owner", "The shorter digest is wanted.")


async def test_an_automatic_resolver_name_is_passed_through_as_given(flagged: tuple[httpx.AsyncClient, FakeCollection, LibraryCollection]) -> None:
    client, _, library = flagged

    response = await client.post(f"/api/library/flags/{CHANGE_ID}/resolve", json={"by": "library", "reason": "Applied in the checkout."})

    assert response.status_code == 200
    stored = await library.fetch_flag(CHANGE_ID)
    assert stored is not None
    assert stored.resolved_by == "library"


async def test_an_unknown_id_answers_404_naming_it(flagged: tuple[httpx.AsyncClient, FakeCollection, LibraryCollection]) -> None:
    client, store, _ = flagged
    written = len(store.payloads)

    response = await client.post("/api/library/flags/mf.diverged.nowhere.finish/resolve", json={"by": "owner", "reason": "Gone."})

    assert response.status_code == 404
    assert response.json()["ok"] is False
    assert "mf.diverged.nowhere.finish" in response.json()["message"]
    assert len(store.payloads) == written


@pytest.mark.parametrize(("by", "reason"), [("owner", ""), ("", "Kept on purpose."), ("owner", "   ")], ids=["no reason", "no name", "a blank reason"])
async def test_an_empty_field_answers_400_and_writes_nothing(
    flagged: tuple[httpx.AsyncClient, FakeCollection, LibraryCollection], by: str, reason: str
) -> None:
    client, store, library = flagged
    written = len(store.payloads)

    response = await client.post(f"/api/library/flags/{DIVERGED_ID}/resolve", json={"by": by, "reason": reason})

    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert DIVERGED_ID in response.json()["message"]
    assert len(store.payloads) == written
    stored = await library.fetch_flag(DIVERGED_ID)
    assert stored is not None
    assert stored.status == "open"


async def test_a_flag_page_that_does_not_parse_is_a_failed_read_never_an_empty_list(
    flagged: tuple[httpx.AsyncClient, FakeCollection, LibraryCollection],
) -> None:
    client, store, _ = flagged
    del store.pages[CHANGE_ID]["frontmatter"]["step_hash"]

    response = await client.get("/api/library/flags")

    assert response.status_code == 503
    assert response.json()["ok"] is False
    assert response.json()["message"].startswith(f"the maintenance flag {CHANGE_ID!r} does not parse")
    assert response.json()["message"].endswith(f". {UNREADABLE_MOVE}")


async def test_resolving_a_flag_page_that_does_not_parse_answers_503_with_the_next_move_and_writes_nothing(
    flagged: tuple[httpx.AsyncClient, FakeCollection, LibraryCollection],
) -> None:
    client, store, _ = flagged
    del store.pages[CHANGE_ID]["frontmatter"]["step_hash"]
    written = len(store.payloads)

    response = await client.post(f"/api/library/flags/{CHANGE_ID}/resolve", json={"by": "owner", "reason": "Applied."})

    assert response.status_code == 503
    assert response.json()["message"].endswith(f". {UNREADABLE_MOVE}")
    assert len(store.payloads) == written


async def test_a_library_that_keeps_no_flags_says_so_on_both_routes(tmp_path: Path) -> None:
    async with a_client_of(_controller(EmptyLibrary(), tmp_path)) as client:
        listed = await client.get("/api/library/flags")
        resolved = await client.post(f"/api/library/flags/{DIVERGED_ID}/resolve", json={"by": "owner", "reason": "Kept."})

    assert (listed.status_code, resolved.status_code) == (503, 503)
    assert listed.json() == resolved.json() == {"ok": False, "message": "this app's workflow library keeps no maintenance flags."}
