"""Redescribing an artifact: the prose a chooser reads changes, the spec never does.

A spec edit is an adaptation (`adapt`, a new version); `redescribe` is the other knob --
title, summary, applicability -- rewritten in place, on both stores behind the contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from core.ai.workflows.contracts import Briefing, FromParameter, LibraryCollection, LibraryError, ParameterSpec, RunNode, Shape, Workflow
from core.ai.workflows.infrastructure.folder import FolderLibrary

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path


def _spec(name: str = "audit-a-claim", version: int = 1) -> Workflow:
    return Workflow(
        name=name,
        version=version,
        description="Check one contested claim and report what would settle it.",
        parameters={"claim": ParameterSpec(description="the claim", json_schema={"type": "string"})},
        inline=(
            Briefing(
                name="weigh",
                instructions="Weigh the claim against what the sources say.",
                inputs={"claim": Shape(json_schema={"type": "string"})},
                output=Shape(json_schema={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}),
            ),
        ),
        nodes=(RunNode(id="weigh", work="weigh", inputs={"claim": FromParameter(parameter="claim")}),),
        output={"node": "weigh"},  # type: ignore[arg-type]
    )


# --- FolderLibrary ----------------------------------------------------------------------------------


async def test_a_folder_artifact_s_card_is_rewritten_in_place(tmp_path: Path) -> None:
    store = FolderLibrary(tmp_path / "workflows")
    await store.put(_spec(), title="Audit", summary="One claim.", applicability="Use when.", provenance={"saved_by": "x"})

    await store.redescribe("audit-a-claim@1", title="Audit a claim", summary="Weighs one claim.", applicability="Never for a broad audit.")

    card = await store.fetch_card("audit-a-claim@1")
    assert card is not None
    assert (card.title, card.summary, card.applicability) == ("Audit a claim", "Weighs one claim.", "Never for a broad audit.")
    assert (await store.fetch_workflow("audit-a-claim@1")) == _spec(), "the spec is not the description"


async def test_a_folder_redescribe_keeps_the_fingerprint_and_lineage_it_found(tmp_path: Path) -> None:
    from core.ai.workflows.contracts import WorkflowFingerprint

    store = FolderLibrary(tmp_path / "workflows")
    await store.put(
        _spec(),
        title="Audit",
        summary="s",
        applicability="a",
        provenance={"saved_by": "x"},
        fingerprint=WorkflowFingerprint(steps={}, grants="1ad78e801fceeeb4", environment={}),
    )

    await store.redescribe("audit-a-claim@1", title="t2", summary="s2", applicability="a2")

    stamp = await store.fetch_fingerprint("audit-a-claim@1")
    assert stamp is not None
    assert stamp.grants == "1ad78e801fceeeb4", "redescribing the card must not erase what save stamped"


async def test_a_folder_redescribe_of_an_unknown_id_raises_store_error(tmp_path: Path) -> None:
    store = FolderLibrary(tmp_path / "workflows")
    with pytest.raises(LibraryError, match="nope"):
        await store.redescribe("nope@1", title="t", summary="s", applicability="a")


# --- LibraryCollection ----------------------------------------------------------------------------


class FakeRows:
    """Records every statement; answers with rows a test scripted, keyed by the id argument."""

    def __init__(self, pages: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self.pages = pages or {}
        self.sent: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, sql: str, /, *args: object) -> Sequence[Mapping[str, Any]]:
        self.sent.append((sql, args))
        page = self.pages.get(str(args[0]))
        return [page] if page is not None else []

    async def fetchval(self, sql: str, /, *args: object) -> str | Mapping[str, Any] | None:
        self.sent.append((sql, args))
        page = self.pages.get(str(args[0]))
        return page["frontmatter"] if page is not None else None


class FakeIngest:
    def __init__(self) -> None:
        self.payloads: list[tuple[Mapping[str, Any], str]] = []

    async def __call__(self, payload: Mapping[str, Any], *, collection: str) -> None:
        self.payloads.append((payload, collection))


def _page(name: str = "audit-a-claim", version: int = 1) -> Mapping[str, Any]:
    return {
        "id": f"wf.{name}.v{version}",
        "type": "workflow",
        "title": "Audit a claim",
        "summary": "One contested claim.",
        "status": "draft",
        "frontmatter": {
            "workflow_version": version,
            "saved_by": "agent:test",
            "spec": _spec(name, version).model_dump(mode="json"),
        },
        "applicability": "Use when one claim is contested.",
    }


async def test_redescribe_rewrites_the_prose_and_keeps_the_long_tail() -> None:
    ingest = FakeIngest()
    store = LibraryCollection(FakeRows({"wf.audit-a-claim.v1": _page()}), ingest)

    await store.redescribe("wf.audit-a-claim.v1", title="Audit a claim", summary="Weighs one claim.", applicability="Never broadly.")

    ((payload, collection),) = ingest.payloads
    assert collection == "workflows"
    frontmatter = payload["frontmatter"]
    assert (frontmatter["title"], frontmatter["summary"]) == ("Audit a claim", "Weighs one claim.")
    assert payload["sections"] == [{"heading": "Applicability", "body": "Never broadly."}]
    # The envelope engram strips into columns has to be put back, or the page it gets is typeless.
    assert (frontmatter["id"], frontmatter["type"], frontmatter["status"]) == ("wf.audit-a-claim.v1", "workflow", "draft")
    # Untouched by a description edit: the spec is not the description.
    assert frontmatter["spec"]["name"] == "audit-a-claim"
    assert frontmatter["workflow_version"] == 1
    assert frontmatter["saved_by"] == "agent:test"


async def test_redescribe_preserves_a_supersession_link_it_did_not_ask_to_change() -> None:
    """A page already superseded keeps saying so -- redescribing its prose is not re-minting it."""
    page = {**_page(), "frontmatter": {**_page()["frontmatter"], "superseded_by": "[[wf.audit-a-claim.v2]]"}}
    ingest = FakeIngest()
    store = LibraryCollection(FakeRows({"wf.audit-a-claim.v1": page}), ingest)

    await store.redescribe("wf.audit-a-claim.v1", title="t", summary="s", applicability="a")

    ((payload, _),) = ingest.payloads
    assert payload["frontmatter"]["superseded_by"] == "[[wf.audit-a-claim.v2]]"


async def test_redescribe_of_an_unknown_id_raises_store_error() -> None:
    with pytest.raises(LibraryError, match="nope"):
        await LibraryCollection(FakeRows({}), FakeIngest()).redescribe("wf.nope", title="t", summary="s", applicability="a")
