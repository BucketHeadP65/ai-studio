"""Maintenance flags are pages in the library's own knowledge base: written by one rule, listed, read and resolved.

Every library here runs over `FakeCollection` with `prune` on, so a stored page loses its nulls and empty containers
as engram's ingest drops them, and the ingest writes into the same pages the rows read from.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.ai.workflows.contracts import (
    Briefing,
    FlagCard,
    FlagIdTakenError,
    FlagUnknownError,
    FromNode,
    FromParameter,
    LibraryCollection,
    LibraryError,
    MaintenanceFlag,
    MaintenanceFlags,
    ParameterSpec,
    RunNode,
    Shape,
    Workflow,
    step_content_address,
)
from core.ai.workflows.infrastructure.library_collection import FLAG_SQL, FLAGS_SQL, FlagWriteIncompleteWarning
from tests.support.fakes.library_collection import FakeCollection

TEXT = Shape(json_schema={"type": "string"})
RAISED = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


def _digest(*lines: str) -> Briefing:
    return Briefing(name="digest", instructions="\n".join(lines), inputs={"source": TEXT}, output=TEXT)


DIGEST = {
    1: _digest("Read the whole source.", "Write one digest page.", "Quote nothing."),
    2: _digest("Read the whole source.", "Write one digest page.", "Quote every claim."),
    3: _digest("Read the whole source.", "Write one digest page with a section per claim.", "Quote every claim and name its section."),
}
EDITED = _digest("Read the whole source.", "Write one digest page.", "Quote every claim, briefly.")
"""A copy of `digest` that matches no saved version and is closest by line to v2."""

AGAIN = _digest("Read the whole source.", "Write one digest page.", "Quote every claim, in full.")
"""Another copy of `digest` that matches no saved version and is closest by line to v2."""

RELATE = Briefing(name="relate", instructions="Link the digest page to every earlier page it builds on.", inputs={"source": TEXT}, output=TEXT)


def _workflow(step: Briefing, *, name: str = "digest-sources", version: int = 1) -> Workflow:
    return Workflow(
        name=name,
        version=version,
        description="Digest one source.",
        parameters={"source": ParameterSpec(description="the source", json_schema={"type": "string"})},
        inline=(step,),
        nodes=(RunNode(id=step.name, work=step.name, inputs={"source": FromParameter(parameter="source")}),),
        output=FromNode(node=step.name),
    )


def _flag(**changes: object) -> MaintenanceFlag:
    fields: dict[str, object] = {
        "kind": "consumer_flag",
        "step": "digest",
        "step_id": "st.digest.v3",
        "step_hash": "a" * 64,
        "workflow": "wf.digest-sources.v2",
        "title": "Flagged consumer: digest in digest-sources",
        "summary": "Decide whether digest-sources adopts st.digest.v3.",
        "evidence": "The contract check failed on the output type.",
        "raised_by": "agent:step-maintenance",
        "raised_at": RAISED,
    }
    fields.update(changes)
    return MaintenanceFlag.model_validate(fields)


async def _library(store: FakeCollection) -> LibraryCollection:
    """A library holding `digest` v1 to v3."""
    library = LibraryCollection(store, store.ingest, collection=store.collection)
    for version, step in DIGEST.items():
        await library.put_step(step, version=version, title="Digest", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})
    return library


async def _put(library: LibraryCollection, workflow: Workflow) -> str:
    return await library.put(workflow, title="Digest sources", summary="s", applicability="Use per source.", provenance={"saved_by": "agent:test"})


def _flag_payloads(store: FakeCollection) -> list[dict[str, object]]:
    return [dict(payload) for payload in store.payloads if payload["frontmatter"]["type"] == "maintenance-flag"]


def test_the_collection_library_answers_the_flags_port() -> None:
    store = FakeCollection()
    assert isinstance(LibraryCollection(store, store.ingest), MaintenanceFlags)


async def test_put_flag_on_a_free_id_writes_an_open_flag_page_with_its_two_sections() -> None:
    store = FakeCollection(prune=True)
    library = LibraryCollection(store, store.ingest)

    written = await library.put_flag(_flag(proposed_change="Change the output type to a list."))

    assert written == "mf.consumer.digest-sources.digest"
    (payload,) = store.payloads
    assert payload["frontmatter"] == {
        "id": "mf.consumer.digest-sources.digest",
        "type": "maintenance-flag",
        "title": "Flagged consumer: digest in digest-sources",
        "summary": "Decide whether digest-sources adopts st.digest.v3.",
        "status": "open",
        "flag_kind": "consumer_flag",
        "step": "digest",
        "step_id": "st.digest.v3",
        "step_hash": "a" * 64,
        "workflow": "wf.digest-sources.v2",
        "raised_by": "agent:step-maintenance",
        "raised_at": "2026-11-05T09:00:00+00:00",
    }
    assert payload["sections"] == [
        {"heading": "Evidence", "body": "The contract check failed on the output type."},
        {"heading": "Proposed change", "body": "Change the output type to a list."},
    ]
    assert await library.fetch_flag(written) == _flag(proposed_change="Change the output type to a list.")


async def test_a_flag_about_a_step_has_an_id_without_a_workflow_and_reads_back_with_its_empty_fields() -> None:
    store = FakeCollection(prune=True)
    library = LibraryCollection(store, store.ingest)
    about_the_step = _flag(kind="plan_rejected", workflow="", evidence="The judge rejected the plan three times.")

    written = await library.put_flag(about_the_step)

    assert written == "mf.rejected.digest"
    assert "workflow" not in store.written(written), "an empty field is left out of the page"
    assert await library.fetch_flag(written) == about_the_step


async def test_put_flag_over_a_resolved_page_with_the_same_step_hash_writes_nothing() -> None:
    store = FakeCollection(prune=True)
    library = LibraryCollection(store, store.ingest)
    flag_id = await library.put_flag(_flag())
    assert flag_id is not None
    await library.resolve_flag(flag_id, by="owner", reason="The output type is right for this workflow.")
    store.payloads.clear()

    again = await library.put_flag(_flag(evidence="The same failure, found by the next run.", raised_at=RAISED + timedelta(days=1)))

    assert again is None
    assert store.payloads == []
    stored = await library.fetch_flag(flag_id)
    assert stored is not None
    assert (stored.status, stored.evidence) == ("resolved", "The contract check failed on the output type.")


async def test_put_flag_over_a_resolved_page_with_another_step_hash_opens_it_again() -> None:
    store = FakeCollection(prune=True)
    library = LibraryCollection(store, store.ingest)
    flag_id = await library.put_flag(_flag())
    assert flag_id is not None
    await library.resolve_flag(flag_id, by="owner", reason="Keep the old version.")

    newer = _flag(step_id="st.digest.v4", step_hash="b" * 64, evidence="The contract check failed again on v4.")
    assert await library.put_flag(newer) == flag_id

    assert await library.fetch_flag(flag_id) == newer


async def test_put_flag_on_an_id_that_holds_a_workflow_page_is_refused_and_writes_nothing() -> None:
    store = FakeCollection(prune=True)
    library = LibraryCollection(store, store.ingest)
    store.pages["mf.consumer.digest-sources.digest"] = {
        "id": "mf.consumer.digest-sources.digest",
        "type": "workflow",
        "title": "t",
        "summary": "s",
        "status": "draft",
        "superseded": False,
        "frontmatter": {"workflow_version": 1},
        "sections": {"applicability": "a"},
        "applicability": "a",
    }

    with pytest.raises(FlagIdTakenError, match=r"mf\.consumer\.digest-sources\.digest already holds a workflow page") as refused:
        await library.put_flag(_flag())

    assert refused.value.flag_id == "mf.consumer.digest-sources.digest"
    assert store.payloads == []
    assert await library.fetch_flag("mf.consumer.digest-sources.digest") is None, "no maintenance flag lives there"


async def test_put_flag_refuses_a_workflow_that_is_not_a_workflow_version_id() -> None:
    store = FakeCollection(prune=True)
    library = LibraryCollection(store, store.ingest)

    with pytest.raises(LibraryError, match=r"wf\.<name>\.v<version>"):
        await library.put_flag(_flag(workflow="digest-sources"))

    assert store.payloads == []


async def test_resolve_flag_writes_the_resolution_and_keeps_every_other_field() -> None:
    store = FakeCollection(prune=True)
    library = LibraryCollection(store, store.ingest)
    flag = _flag(proposed_change="Change the output type to a list.")
    flag_id = await library.put_flag(flag)
    assert flag_id is not None

    await library.resolve_flag(flag_id, by="owner", reason="The output type is right for this workflow.")

    assert await library.fetch_flag(flag_id) == flag.model_copy(
        update={"status": "resolved", "resolved_by": "owner", "decision_reason": "The output type is right for this workflow."}
    )
    frontmatter = store.written(flag_id)
    assert (frontmatter["status"], frontmatter["resolved_by"]) == ("resolved", "owner")


@pytest.mark.parametrize(("by", "reason", "missing"), [("owner", " ", "reason"), ("", "Keep it.", "by")])
async def test_resolve_flag_refuses_an_empty_name_or_reason_and_writes_nothing(by: str, reason: str, missing: str) -> None:
    store = FakeCollection(prune=True)
    library = LibraryCollection(store, store.ingest)
    flag_id = await library.put_flag(_flag())
    assert flag_id is not None
    store.payloads.clear()

    with pytest.raises(ValueError, match=missing):
        await library.resolve_flag(flag_id, by=by, reason=reason)

    assert store.payloads == []


async def test_resolve_flag_refuses_an_id_no_flag_has() -> None:
    store = FakeCollection(prune=True)
    library = LibraryCollection(store, store.ingest)

    with pytest.raises(FlagUnknownError, match=r"mf\.consumer\.nothing\.digest"):
        await library.resolve_flag("mf.consumer.nothing.digest", by="owner", reason="Keep it.")

    assert store.payloads == []


async def test_a_flag_page_that_does_not_parse_is_reported_never_read_as_absent() -> None:
    store = FakeCollection(prune=True)
    library = LibraryCollection(store, store.ingest)
    flag_id = await library.put_flag(_flag())
    assert flag_id is not None
    del store.pages[flag_id]["frontmatter"]["step_hash"]

    with pytest.raises(LibraryError, match=r"mf\.consumer\.digest-sources\.digest"):
        await library.fetch_flag(flag_id)


async def test_flags_sends_the_collection_status_and_kind_and_answers_cards_newest_first() -> None:
    store = FakeCollection(prune=True)
    library = LibraryCollection(store, store.ingest)
    await library.put_flag(_flag(workflow="wf.old-sources.v1", raised_at=RAISED - timedelta(days=2)))
    await library.put_flag(_flag(workflow="wf.new-sources.v4", raised_at=RAISED, proposed_change="Retype the output."))
    await library.put_flag(_flag(kind="plan_rejected", workflow="", raised_at=RAISED - timedelta(days=1)))
    resolved = await library.put_flag(_flag(workflow="wf.done-sources.v1", raised_at=RAISED + timedelta(days=1)))
    assert resolved is not None
    await library.resolve_flag(resolved, by="owner", reason="Done.")
    store.sent.clear()

    cards = await library.flags(status="open")

    assert store.sent == [(FLAGS_SQL, ("workflows", "open", None))]
    assert cards == (
        FlagCard(
            id="mf.consumer.new-sources.digest",
            kind="consumer_flag",
            status="open",
            step="digest",
            workflow="wf.new-sources.v4",
            title="Flagged consumer: digest in digest-sources",
            summary="Decide whether digest-sources adopts st.digest.v3.",
            raised_at=RAISED,
            evidence="The contract check failed on the output type.",
            proposed_change="Retype the output.",
        ),
        FlagCard(
            id="mf.rejected.digest",
            kind="plan_rejected",
            status="open",
            step="digest",
            workflow="",
            title="Flagged consumer: digest in digest-sources",
            summary="Decide whether digest-sources adopts st.digest.v3.",
            raised_at=RAISED - timedelta(days=1),
            evidence="The contract check failed on the output type.",
            proposed_change="",
        ),
        FlagCard(
            id="mf.consumer.old-sources.digest",
            kind="consumer_flag",
            status="open",
            step="digest",
            workflow="wf.old-sources.v1",
            title="Flagged consumer: digest in digest-sources",
            summary="Decide whether digest-sources adopts st.digest.v3.",
            raised_at=RAISED - timedelta(days=2),
            evidence="The contract check failed on the output type.",
            proposed_change="",
        ),
    )
    assert [card.id for card in await library.flags(kind="plan_rejected")] == ["mf.rejected.digest"]
    assert next(card.id for card in await library.flags()) == "mf.consumer.done-sources.digest", "no filter lists resolved flags too"
    assert cards[0].model_dump(mode="json")["raised_at"] == "2026-11-05T09:00:00Z", "the card's time carries its offset"


def test_a_flag_card_carries_the_flags_id_and_every_field_a_list_shows() -> None:
    flag = _flag(status="resolved", resolved_by="owner", decision_reason="Kept.", proposed_change="Retype the output.")

    card = FlagCard.from_flag("mf.consumer.digest-sources.digest", flag)

    assert card == FlagCard(
        id="mf.consumer.digest-sources.digest",
        kind="consumer_flag",
        status="resolved",
        step="digest",
        workflow="wf.digest-sources.v2",
        title="Flagged consumer: digest in digest-sources",
        summary="Decide whether digest-sources adopts st.digest.v3.",
        raised_at=RAISED,
        evidence="The contract check failed on the output type.",
        proposed_change="Retype the output.",
    )


async def test_flags_lists_each_card_as_from_flag_maps_its_page() -> None:
    store = FakeCollection(prune=True)
    library = LibraryCollection(store, store.ingest)
    written = await library.put_flag(_flag(proposed_change="Retype the output."))
    assert written is not None
    stored = await library.fetch_flag(written)
    assert stored is not None

    assert await library.flags() == (FlagCard.from_flag(written, stored),)


async def test_put_of_a_workflow_holding_a_diverged_copy_writes_its_flag_with_the_diff_in_evidence() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)

    workflow_id = await _put(library, _workflow(EDITED))

    flag = await library.fetch_flag("mf.diverged.digest-sources.digest")
    assert flag is not None
    assert (flag.kind, flag.status, flag.workflow, flag.raised_by) == ("diverged_copy", "open", workflow_id, "library")
    assert (flag.step, flag.step_id, flag.step_hash) == ("digest", "st.digest.v2", step_content_address(DIGEST[2])), "the version it diverged from"
    assert "st.digest.v2" in flag.evidence
    assert "closest to it by text" in flag.evidence
    assert "```diff\n--- st.digest.v2\n+++ wf.digest-sources.v1\n" in flag.evidence
    assert "\n-  Quote every claim.\n+  Quote every claim, briefly.\n" in flag.evidence
    assert flag.proposed_change == ""


async def test_saving_the_same_diverged_copy_again_writes_no_flag() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)
    await _put(library, _workflow(EDITED))
    store.payloads.clear()

    await _put(library, _workflow(EDITED))

    assert _flag_payloads(store) == []


async def test_a_later_save_whose_copy_matches_a_version_resolves_the_diverged_flag_as_the_library() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)
    await _put(library, _workflow(EDITED))
    diverged = await library.fetch_flag("mf.diverged.digest-sources.digest")

    matched = await _put(library, _workflow(DIGEST[3], version=2))

    flag = await library.fetch_flag("mf.diverged.digest-sources.digest")
    assert diverged is not None
    assert flag == diverged.model_copy(
        update={"status": "resolved", "resolved_by": "library", "decision_reason": f"{matched} holds digest v3, a saved version, again."}
    )


async def test_a_save_whose_copy_matches_resolves_nothing_when_no_flag_is_open() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)
    await _put(library, _workflow(EDITED))
    await library.resolve_flag("mf.diverged.digest-sources.digest", by="owner", reason="The copy stays.")
    store.payloads.clear()

    await _put(library, _workflow(DIGEST[3], version=2))
    await _put(library, _workflow(DIGEST[3], name="pasted-sources"))

    assert _flag_payloads(store) == []
    flag = await library.fetch_flag("mf.diverged.digest-sources.digest")
    assert flag is not None
    assert (flag.resolved_by, flag.decision_reason) == ("owner", "The copy stays.")


async def test_a_resolved_diverged_flag_stays_resolved_while_the_copy_diverges_from_the_same_version() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)
    await _put(library, _workflow(EDITED))
    await library.resolve_flag("mf.diverged.digest-sources.digest", by="owner", reason="The copy stays.")
    store.payloads.clear()

    await _put(library, _workflow(EDITED, version=2))

    assert _flag_payloads(store) == []
    flag = await library.fetch_flag("mf.diverged.digest-sources.digest")
    assert flag is not None
    assert flag.status == "resolved"


async def test_a_flag_write_the_ingest_refuses_warns_and_the_workflow_stays_saved() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)
    store.refused_types = frozenset({"maintenance-flag"})

    with pytest.warns(FlagWriteIncompleteWarning, match=r"mf\.diverged\.digest-sources\.digest"):
        workflow_id = await _put(library, _workflow(EDITED))

    assert workflow_id == "wf.digest-sources.v1"
    assert "wf.digest-sources.v1" in store.pages
    assert "mf.diverged.digest-sources.digest" not in store.pages


async def test_a_resolution_the_ingest_refuses_warns_and_the_workflow_stays_saved() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)
    await _put(library, _workflow(EDITED))
    store.refused_types = frozenset({"maintenance-flag"})

    with pytest.warns(FlagWriteIncompleteWarning, match=r"mf\.diverged\.digest-sources\.digest"):
        workflow_id = await _put(library, _workflow(DIGEST[3], version=2))

    assert workflow_id == "wf.digest-sources.v2"
    flag = await library.fetch_flag("mf.diverged.digest-sources.digest")
    assert flag is not None
    assert flag.status == "open", "the next save writes the resolution again"


async def test_resolve_flag_reads_the_flag_page_once() -> None:
    store = FakeCollection(prune=True)
    library = LibraryCollection(store, store.ingest)
    flag_id = await library.put_flag(_flag())
    assert flag_id is not None
    store.sent.clear()

    await library.resolve_flag(flag_id, by="owner", reason="Keep it.")

    assert store.sent == [(FLAG_SQL, ("workflows", flag_id))]


async def test_a_flag_the_library_resolved_opens_again_when_the_copy_diverges_again_and_a_persons_resolution_stays() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)
    await _put(library, _workflow(EDITED))
    await _put(library, _workflow(DIGEST[2], version=2))
    matched = await library.fetch_flag("mf.diverged.digest-sources.digest")
    assert matched is not None
    assert (matched.status, matched.resolved_by) == ("resolved", "library")

    again = await _put(library, _workflow(AGAIN, version=3))

    reopened = await library.fetch_flag("mf.diverged.digest-sources.digest")
    assert reopened is not None
    assert (reopened.status, reopened.workflow, reopened.step_id) == ("open", again, "st.digest.v2"), "the same step version, opened again"
    await library.resolve_flag("mf.diverged.digest-sources.digest", by="owner", reason="This copy stays.")
    store.payloads.clear()

    await _put(library, _workflow(AGAIN, version=4))

    assert _flag_payloads(store) == []
    kept = await library.fetch_flag("mf.diverged.digest-sources.digest")
    assert kept is not None
    assert (kept.status, kept.resolved_by) == ("resolved", "owner")


async def test_a_later_save_without_the_copy_resolves_the_diverged_flag_as_the_library() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)
    await library.put_step(RELATE, version=1, title="Relate", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})
    await _put(library, _workflow(EDITED))

    without = await _put(library, _workflow(RELATE, version=2))

    flag = await library.fetch_flag("mf.diverged.digest-sources.digest")
    assert flag is not None
    assert (flag.status, flag.resolved_by, flag.decision_reason) == ("resolved", "library", f"{without} holds no copy of digest.")


async def test_a_save_reads_the_open_flags_once_and_resolves_without_reading_again() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)
    await _put(library, _workflow(EDITED))
    store.sent.clear()

    await _put(library, _workflow(DIGEST[2], version=2))

    flag_reads = [(sql, args) for sql, args in store.sent if sql in (FLAG_SQL, FLAGS_SQL)]
    assert flag_reads == [(FLAGS_SQL, ("workflows", "open", "diverged_copy"))]


async def test_a_lower_version_saved_after_a_higher_one_leaves_the_higher_ones_flag_open() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)
    await library.put_step(RELATE, version=1, title="Relate", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})
    await _put(library, _workflow(EDITED, version=2))

    await _put(library, _workflow(RELATE, version=1))

    flag = await library.fetch_flag("mf.diverged.digest-sources.digest")
    assert flag is not None
    assert (flag.status, flag.workflow) == ("open", "wf.digest-sources.v2")


async def test_archiving_a_version_resolves_its_open_diverged_flags_and_no_other() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)
    await _put(library, _workflow(EDITED))
    await _put(library, _workflow(AGAIN, name="brief-sources", version=2))

    await library.set_archived("wf.digest-sources.v1", archived=True)

    archived = await library.fetch_flag("mf.diverged.digest-sources.digest")
    other = await library.fetch_flag("mf.diverged.brief-sources.digest")
    assert archived is not None
    assert other is not None
    assert (archived.status, archived.resolved_by, archived.decision_reason) == ("resolved", "library", "wf.digest-sources.v1 was archived.")
    assert other.status == "open"


async def test_an_archive_whose_resolution_fails_warns_and_archiving_again_resolves_it() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)
    await _put(library, _workflow(EDITED))
    store.refused_types = frozenset({"maintenance-flag"})

    with pytest.warns(FlagWriteIncompleteWarning, match=r"mf\.diverged\.digest-sources\.digest"):
        await library.set_archived("wf.digest-sources.v1", archived=True)

    assert "archived_at" in store.pages["wf.digest-sources.v1"]["frontmatter"], "the archive stands"
    store.refused_types = frozenset()
    await library.set_archived("wf.digest-sources.v1", archived=True)
    flag = await library.fetch_flag("mf.diverged.digest-sources.digest")
    assert flag is not None
    assert flag.status == "resolved"


async def test_a_diverged_copy_holding_double_percent_pairs_gets_its_flag_with_each_pair_split() -> None:
    store = FakeCollection(prune=True)
    library = LibraryCollection(store, store.ingest)
    percent = _digest("Report coverage as 50%% of sources.", "Quote every claim.", "End with 100%% done.")
    edited = _digest("Report coverage as 50%% of sources.", "Quote every claim, briefly.", "End with 100%% done.")
    await library.put_step(percent, version=1, title="Digest", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})

    await _put(library, _workflow(edited))

    flag = await library.fetch_flag("mf.diverged.digest-sources.digest")
    assert flag is not None
    assert "%%" not in flag.evidence
    assert "50% % of sources" in flag.evidence
    assert "writes each double percent sign as % %" in flag.evidence
