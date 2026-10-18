"""Archiving an artifact: it leaves the library and search, its runs and versions stay.

`set_archived` is a library gesture -- idempotent, reversible, and orthogonal to `redescribe` and
`put`: nothing about the spec, the prose, or the lineage changes. `archived` is the other side of
the same coin: the library a chooser never sees through `search`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from core.ai.workflows.contracts import (
    Briefing,
    FromParameter,
    LibraryCollection,
    LibraryError,
    ParameterSpec,
    RunNode,
    Shape,
    Workflow,
    load_spec_document,
)
from core.ai.workflows.domain.document import dump_spec_document
from core.ai.workflows.infrastructure.folder import FolderLibrary
from core.ai.workflows.infrastructure.library_collection import ARCHIVED_SQL, CATALOG_COUNT_SQL, CATALOG_SQL, SEARCH_SQL

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


async def test_archiving_a_folder_artifact_sets_the_timestamp(tmp_path: Path) -> None:
    store = FolderLibrary(tmp_path / "workflows")
    await store.put(_spec(), title="Audit", summary="One claim.", applicability="Use when.", provenance={"saved_by": "x"})

    await store.set_archived("audit-a-claim@1", archived=True)

    card = await store.fetch_card("audit-a-claim@1")
    assert card is not None
    assert card.archived_at is not None
    assert card.archived_at.tzinfo is not None, "the timestamp is timezone-aware"


async def test_restoring_a_folder_artifact_clears_the_timestamp(tmp_path: Path) -> None:
    store = FolderLibrary(tmp_path / "workflows")
    await store.put(_spec(), title="Audit", summary="One claim.", applicability="Use when.", provenance={"saved_by": "x"})
    await store.set_archived("audit-a-claim@1", archived=True)

    await store.set_archived("audit-a-claim@1", archived=False)

    card = await store.fetch_card("audit-a-claim@1")
    assert card is not None
    assert card.archived_at is None


async def test_archiving_a_folder_artifact_twice_keeps_the_first_timestamp(tmp_path: Path) -> None:
    store = FolderLibrary(tmp_path / "workflows")
    await store.put(_spec(), title="Audit", summary="One claim.", applicability="Use when.", provenance={"saved_by": "x"})
    await store.set_archived("audit-a-claim@1", archived=True)
    first_card = await store.fetch_card("audit-a-claim@1")
    assert first_card is not None

    await store.set_archived("audit-a-claim@1", archived=True)

    second_card = await store.fetch_card("audit-a-claim@1")
    assert second_card is not None
    assert second_card.archived_at == first_card.archived_at


async def test_restoring_a_folder_artifact_that_is_not_archived_is_a_no_op(tmp_path: Path) -> None:
    store = FolderLibrary(tmp_path / "workflows")
    await store.put(_spec(), title="Audit", summary="One claim.", applicability="Use when.", provenance={"saved_by": "x"})

    await store.set_archived("audit-a-claim@1", archived=False)

    card = await store.fetch_card("audit-a-claim@1")
    assert card is not None
    assert card.archived_at is None


async def test_archiving_an_unknown_folder_artifact_raises_store_error(tmp_path: Path) -> None:
    store = FolderLibrary(tmp_path / "workflows")
    with pytest.raises(LibraryError, match="nope"):
        await store.set_archived("nope@1", archived=True)


async def test_a_folder_search_skips_an_archived_artifact(tmp_path: Path) -> None:
    store = FolderLibrary(tmp_path / "workflows")
    await store.put(_spec(), title="Audit a claim", summary="One contested claim.", applicability="a", provenance={"saved_by": "x"})
    await store.set_archived("audit-a-claim@1", archived=True)

    assert await store.search("contested claim") == ()


async def test_a_folder_fetch_card_still_answers_for_an_archived_artifact(tmp_path: Path) -> None:
    store = FolderLibrary(tmp_path / "workflows")
    await store.put(_spec(), title="Audit", summary="One claim.", applicability="Use when.", provenance={"saved_by": "x"})
    await store.set_archived("audit-a-claim@1", archived=True)

    card = await store.fetch_card("audit-a-claim@1")
    assert card is not None
    assert card.archived_at is not None


async def test_a_folder_archived_fetch_workflow_still_answers(tmp_path: Path) -> None:
    """Runs and versions stay reachable -- archiving is a library gesture, not a deletion."""
    store = FolderLibrary(tmp_path / "workflows")
    await store.put(_spec(), title="Audit", summary="One claim.", applicability="Use when.", provenance={"saved_by": "x"})
    await store.set_archived("audit-a-claim@1", archived=True)

    assert (await store.fetch_workflow("audit-a-claim@1")) == _spec()


async def test_folder_archived_lists_newest_archive_first(tmp_path: Path) -> None:
    store = FolderLibrary(tmp_path / "workflows")
    await store.put(_spec(name="a"), title="A", summary="s", applicability="a", provenance={"saved_by": "x"})
    await store.put(_spec(name="b"), title="B", summary="s", applicability="a", provenance={"saved_by": "x"})
    await store.set_archived("a@1", archived=True)
    await store.set_archived("b@1", archived=True)  # archived after "a", so it sorts first

    found = await store.archived()
    assert [card.id for card in found] == ["b@1", "a@1"]
    assert all(card.archived_at is not None for card in found)


async def test_folder_archived_excludes_live_artifacts(tmp_path: Path) -> None:
    store = FolderLibrary(tmp_path / "workflows")
    await store.put(_spec(name="a"), title="A", summary="s", applicability="a", provenance={"saved_by": "x"})
    await store.put(_spec(name="b"), title="B", summary="s", applicability="a", provenance={"saved_by": "x"})
    await store.set_archived("a@1", archived=True)

    found = await store.archived()
    assert [card.id for card in found] == ["a@1"]


async def test_folder_archived_respects_limit(tmp_path: Path) -> None:
    store = FolderLibrary(tmp_path / "workflows")
    for name in ("a", "b", "c"):
        await store.put(_spec(name=name), title=name, summary="s", applicability="a", provenance={"saved_by": "x"})
        await store.set_archived(f"{name}@1", archived=True)

    found = await store.archived(limit=2)
    assert len(found) == 2


async def test_a_folder_artifact_with_an_unparseable_archived_at_stays_archived(tmp_path: Path) -> None:
    """The key's presence is what archives an artifact; a bad value must not crash the whole scan."""
    root = tmp_path / "workflows"
    store = FolderLibrary(root)
    await store.put(_spec(), title="Audit a claim", summary="One contested claim.", applicability="a", provenance={"saved_by": "x"})
    path = root / "audit-a-claim-v1.md"  # the naming FolderLibrary._path_for uses: "@" becomes "-v"
    document = load_spec_document(path.read_text(encoding="utf-8"))
    frontmatter = {**document.frontmatter, "archived_at": "not-a-timestamp"}
    path.write_text(dump_spec_document(document.workflow, frontmatter=frontmatter, body="## Applicability\n\na\n"))

    with pytest.warns(UserWarning, match="audit-a-claim@1"):
        found = await store.search("contested claim")
    assert found == (), "still archived, even though the value would not parse"

    with pytest.warns(UserWarning, match="audit-a-claim@1"):
        listed = await store.archived()
    assert [card.id for card in listed] == ["audit-a-claim@1"]
    assert listed[0].archived_at == datetime.fromtimestamp(0, UTC)


async def test_a_folder_artifact_with_a_naive_archived_at_still_lists_archived(tmp_path: Path) -> None:
    """A hand-edited file without an offset must not take down the whole archived listing."""
    root = tmp_path / "workflows"
    store = FolderLibrary(root)
    await store.put(_spec(), title="Audit a claim", summary="One contested claim.", applicability="a", provenance={"saved_by": "x"})
    path = root / "audit-a-claim-v1.md"  # the naming FolderLibrary._path_for uses: "@" becomes "-v"
    document = load_spec_document(path.read_text(encoding="utf-8"))
    frontmatter = {**document.frontmatter, "archived_at": "2026-08-01T00:00:00"}  # no offset
    path.write_text(dump_spec_document(document.workflow, frontmatter=frontmatter, body="## Applicability\n\na\n"))

    found = await store.archived()

    assert [card.id for card in found] == ["audit-a-claim@1"]
    assert found[0].archived_at is not None
    assert found[0].archived_at.tzinfo is not None, "a naive stamp must not crash comparison against aware ones"
    assert found[0].archived_at.isoformat().endswith("+00:00")


async def test_folder_catalog_lists_non_archived_in_title_order(tmp_path: Path) -> None:
    store = FolderLibrary(tmp_path / "workflows")
    await store.put(_spec(name="zzz"), title="Zebra audit", summary="s", applicability="a", provenance={"saved_by": "x"})
    await store.put(_spec(name="aaa"), title="Aardvark audit", summary="s", applicability="a", provenance={"saved_by": "x"})

    found = await store.catalog()

    assert [card.title for card in found] == ["Aardvark audit", "Zebra audit"]
    assert all(card.score == 0.0 for card in found)


async def test_folder_catalog_skips_archived(tmp_path: Path) -> None:
    store = FolderLibrary(tmp_path / "workflows")
    await store.put(_spec(name="a"), title="A", summary="s", applicability="a", provenance={"saved_by": "x"})
    await store.put(_spec(name="b"), title="B", summary="s", applicability="a", provenance={"saved_by": "x"})
    await store.set_archived("a@1", archived=True)

    found = await store.catalog()

    assert [card.id for card in found] == ["b@1"]


async def test_folder_catalog_respects_limit(tmp_path: Path) -> None:
    store = FolderLibrary(tmp_path / "workflows")
    for name in ("a", "b", "c"):
        await store.put(_spec(name=name), title=name, summary="s", applicability="a", provenance={"saved_by": "x"})

    found = await store.catalog(limit=2)
    assert len(found) == 2


async def test_folder_catalog_size_counts_live_artifacts(tmp_path: Path) -> None:
    store = FolderLibrary(tmp_path / "workflows")
    for name in ("a", "b", "c"):
        await store.put(_spec(name=name), title=name, summary="s", applicability="a", provenance={"saved_by": "x"})

    assert await store.catalog_size() == 3


async def test_folder_catalog_size_excludes_archived(tmp_path: Path) -> None:
    store = FolderLibrary(tmp_path / "workflows")
    await store.put(_spec(name="a"), title="A", summary="s", applicability="a", provenance={"saved_by": "x"})
    await store.put(_spec(name="b"), title="B", summary="s", applicability="a", provenance={"saved_by": "x"})
    await store.set_archived("a@1", archived=True)

    assert await store.catalog_size() == 1


async def test_folder_current_names_the_newest_unarchived_version(tmp_path: Path) -> None:
    """A reference to a@1 follows the artifact to its newest unarchived version; an archived newest is skipped."""
    store = FolderLibrary(tmp_path / "workflows")
    for version in (1, 2, 10):
        await store.put(_spec(name="a", version=version), title="A", summary="s", applicability="a", provenance={"saved_by": "x"})
    await store.put(_spec(name="a-b"), title="AB", summary="s", applicability="a", provenance={"saved_by": "x"})

    assert await store.current("a@1") == "a@10", "numeric order, and a-b@1 is another artifact"
    await store.set_archived("a@10", archived=True)
    assert await store.current("a@1") == "a@2"
    assert await store.current("a@2") == "a@2"
    assert await store.current("nothing-here@1") == "nothing-here@1"
    assert await store.current("wf.a.v1") == "wf.a.v1", "not this library's id scheme, so it cannot say"


# --- LibraryCollection ----------------------------------------------------------------------------


def _listed(page: Mapping[str, Any]) -> dict[str, Any]:
    """One page as the listing queries select it: the documents they pull out of the frontmatter, beside the columns."""
    frontmatter = page.get("frontmatter", {})
    return {**page, "presentation": frontmatter.get("presentation"), "intent": frontmatter.get("intent")}


class FakeRows:
    """Records every statement; answers with rows a test scripted, keyed by the id argument."""

    def __init__(self, pages: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self.pages = pages or {}
        self.sent: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, sql: str, /, *args: object) -> Sequence[Mapping[str, Any]]:
        self.sent.append((sql, args))
        if sql == ARCHIVED_SQL:
            rows = [page for page in self.pages.values() if "archived_at" in page.get("frontmatter", {})]
            rows.sort(key=lambda page: str(page["frontmatter"]["archived_at"]), reverse=True)
            limit = args[0] if args else None
            return rows[:limit] if isinstance(limit, int) else rows
        if sql == SEARCH_SQL:
            # `c.frontmatter ? 'archived_at'` keys on presence, not parseability -- same here.
            return [
                {**_listed(page), "score": 1.0, "superseded": False}
                for page in self.pages.values()
                if "archived_at" not in page.get("frontmatter", {})
            ]
        if sql == CATALOG_SQL:
            rows = [_listed(page) for page in self.pages.values() if "archived_at" not in page.get("frontmatter", {})]
            rows.sort(key=lambda page: (str(page["title"]), str(page["id"])))
            limit = args[0] if args else None
            return rows[:limit] if isinstance(limit, int) else rows
        if sql == CATALOG_COUNT_SQL:
            live = [page for page in self.pages.values() if "archived_at" not in page.get("frontmatter", {})]
            return [{"total": len(live)}]
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


async def test_set_archived_writes_the_timestamp_into_the_long_tail() -> None:
    ingest = FakeIngest()
    store = LibraryCollection(FakeRows({"wf.audit-a-claim.v1": _page()}), ingest)

    await store.set_archived("wf.audit-a-claim.v1", archived=True)

    ((payload, collection),) = ingest.payloads
    assert collection == "workflows"
    frontmatter = payload["frontmatter"]
    assert isinstance(frontmatter["archived_at"], str)
    assert datetime.fromisoformat(frontmatter["archived_at"]).tzinfo is not None
    # The envelope engram strips into columns has to be put back, or the page it gets is typeless.
    assert (frontmatter["id"], frontmatter["type"], frontmatter["status"]) == ("wf.audit-a-claim.v1", "workflow", "draft")
    # Untouched by an archive: the spec is not the description.
    assert frontmatter["spec"]["name"] == "audit-a-claim"


async def test_restoring_clears_the_timestamp_from_the_long_tail() -> None:
    archived_page = {**_page(), "frontmatter": {**_page()["frontmatter"], "archived_at": "2026-08-01T00:00:00+00:00"}}
    ingest = FakeIngest()
    store = LibraryCollection(FakeRows({"wf.audit-a-claim.v1": archived_page}), ingest)

    await store.set_archived("wf.audit-a-claim.v1", archived=False)

    ((payload, _),) = ingest.payloads
    assert "archived_at" not in payload["frontmatter"]


async def test_archiving_a_collection_artifact_twice_keeps_the_first_timestamp() -> None:
    archived_page = {**_page(), "frontmatter": {**_page()["frontmatter"], "archived_at": "2026-08-01T00:00:00+00:00"}}
    ingest = FakeIngest()
    store = LibraryCollection(FakeRows({"wf.audit-a-claim.v1": archived_page}), ingest)

    await store.set_archived("wf.audit-a-claim.v1", archived=True)

    assert ingest.payloads == [], "already archived: no write at all, so the first timestamp cannot move"


async def test_restoring_a_collection_artifact_that_is_not_archived_is_a_no_op() -> None:
    ingest = FakeIngest()
    store = LibraryCollection(FakeRows({"wf.audit-a-claim.v1": _page()}), ingest)

    await store.set_archived("wf.audit-a-claim.v1", archived=False)

    assert ingest.payloads == []


async def test_archiving_an_unknown_collection_id_raises_store_error() -> None:
    with pytest.raises(LibraryError, match="nope"):
        await LibraryCollection(FakeRows({}), FakeIngest()).set_archived("wf.nope", archived=True)


async def test_collection_fetch_card_still_answers_for_an_archived_page() -> None:
    archived_page = {**_page(), "frontmatter": {**_page()["frontmatter"], "archived_at": "2026-08-01T00:00:00+00:00"}}
    store = LibraryCollection(FakeRows({"wf.audit-a-claim.v1": archived_page}), FakeIngest())

    card = await store.fetch_card("wf.audit-a-claim.v1")
    assert card is not None
    assert card.archived_at == datetime(2026, 8, 1, tzinfo=UTC)


async def test_collection_search_query_excludes_archived_pages() -> None:
    """`SEARCH_SQL` filters archived pages by the jsonb key test, so `limit` stays honest without over-fetching."""
    rows = FakeRows()
    await LibraryCollection(rows, FakeIngest()).search("contested claim")
    sql, _ = rows.sent[0]
    assert sql == SEARCH_SQL
    assert "archived_at" in sql


async def test_collection_archived_lists_newest_archive_first() -> None:
    older = {**_page(name="a"), "frontmatter": {**_page(name="a")["frontmatter"], "archived_at": "2026-08-01T00:00:00+00:00"}}
    newer = {**_page(name="b"), "frontmatter": {**_page(name="b")["frontmatter"], "archived_at": "2026-08-10T00:00:00+00:00"}}
    live = _page(name="c")
    rows = FakeRows({"wf.a.v1": older, "wf.b.v1": newer, "wf.c.v1": live})

    found = await LibraryCollection(rows, FakeIngest()).archived()

    assert [card.id for card in found] == ["wf.b.v1", "wf.a.v1"]
    assert all(card.archived_at is not None for card in found)


async def test_collection_archived_fetch_workflow_still_answers() -> None:
    """Runs and versions stay reachable -- archiving is a library gesture, not a deletion."""
    archived_page = {**_page(), "frontmatter": {**_page()["frontmatter"], "archived_at": "2026-08-01T00:00:00+00:00"}}
    store = LibraryCollection(FakeRows({"wf.audit-a-claim.v1": archived_page}), FakeIngest())

    assert (await store.fetch_workflow("wf.audit-a-claim.v1")) == _spec()


async def test_collection_archived_binds_the_collection_to_archived_sql() -> None:
    """`archived()` is the counterpart to a scoped `search` -- it must not list every collection's library."""
    archived_page = {**_page(), "frontmatter": {**_page()["frontmatter"], "archived_at": "2026-08-01T00:00:00+00:00"}}
    rows = FakeRows({"wf.audit-a-claim.v1": archived_page})
    store = LibraryCollection(rows, FakeIngest(), collection="research")

    await store.archived()

    _, args = next(sent for sent in rows.sent if sent[0] == ARCHIVED_SQL)
    assert args == (50, "research")


async def test_collection_archived_respects_limit() -> None:
    pages = {
        f"wf.{name}.v1": {
            **_page(name=name),
            "frontmatter": {**_page(name=name)["frontmatter"], "archived_at": f"2026-08-0{i}T00:00:00+00:00"},
        }
        for i, name in enumerate(("a", "b", "c"), start=1)
    }
    found = await LibraryCollection(FakeRows(pages), FakeIngest()).archived(limit=2)
    assert len(found) == 2


async def test_collection_search_excludes_an_archived_page_even_with_a_malformed_timestamp() -> None:
    """`c.frontmatter ? 'archived_at'` filters by key presence -- a bad value never even reaches Python."""
    bad_page = {**_page(), "frontmatter": {**_page()["frontmatter"], "archived_at": "not-a-timestamp"}}
    rows = FakeRows({"wf.audit-a-claim.v1": bad_page})

    found = await LibraryCollection(rows, FakeIngest()).search("contested claim")

    assert found == ()


async def test_collection_archived_reports_the_epoch_for_a_malformed_timestamp() -> None:
    """The key's presence is what archives an artifact; a bad value must not crash the whole scan."""
    bad_page = {**_page(), "frontmatter": {**_page()["frontmatter"], "archived_at": "not-a-timestamp"}}
    rows = FakeRows({"wf.audit-a-claim.v1": bad_page})

    with pytest.warns(UserWarning, match="wf.audit-a-claim.v1"):
        found = await LibraryCollection(rows, FakeIngest()).archived()

    assert [card.id for card in found] == ["wf.audit-a-claim.v1"]
    assert found[0].archived_at == datetime.fromtimestamp(0, UTC)


async def test_collection_catalog_lists_non_archived_in_title_order() -> None:
    archived = {**_page(name="z"), "title": "Zebra", "frontmatter": {**_page(name="z")["frontmatter"], "archived_at": "2026-08-01T00:00:00+00:00"}}
    live_a = {**_page(name="a"), "title": "Aardvark"}
    live_b = {**_page(name="b"), "title": "Bee"}
    rows = FakeRows({"wf.z.v1": archived, "wf.a.v1": live_a, "wf.b.v1": live_b})

    found = await LibraryCollection(rows, FakeIngest()).catalog()

    assert [card.title for card in found] == ["Aardvark", "Bee"]
    assert all(card.score == 0.0 for card in found)


async def test_collection_catalog_binds_the_collection_to_catalog_sql() -> None:
    """`catalog()` is scoped the same way `archived()` is -- it must not list every collection's library."""
    rows = FakeRows({"wf.audit-a-claim.v1": _page()})
    store = LibraryCollection(rows, FakeIngest(), collection="research")

    await store.catalog()

    _, args = next(sent for sent in rows.sent if sent[0] == CATALOG_SQL)
    assert args == (50, "research")


async def test_collection_catalog_respects_limit() -> None:
    pages = {f"wf.{name}.v1": _page(name=name) for name in ("a", "b", "c")}
    found = await LibraryCollection(FakeRows(pages), FakeIngest()).catalog(limit=2)
    assert len(found) == 2


async def test_collection_catalog_size_counts_live_pages() -> None:
    pages = {f"wf.{name}.v1": _page(name=name) for name in ("a", "b", "c")}
    assert await LibraryCollection(FakeRows(pages), FakeIngest()).catalog_size() == 3


async def test_collection_catalog_size_excludes_archived() -> None:
    archived = {**_page(name="z"), "frontmatter": {**_page(name="z")["frontmatter"], "archived_at": "2026-08-01T00:00:00+00:00"}}
    live = _page(name="a")
    rows = FakeRows({"wf.z.v1": archived, "wf.a.v1": live})

    assert await LibraryCollection(rows, FakeIngest()).catalog_size() == 1


async def test_collection_catalog_size_binds_the_collection_to_catalog_count_sql() -> None:
    """`catalog_size()` is scoped the same way `catalog()` and `archived()` are."""
    rows = FakeRows({"wf.audit-a-claim.v1": _page()})
    store = LibraryCollection(rows, FakeIngest(), collection="research")

    await store.catalog_size()

    _, args = next(sent for sent in rows.sent if sent[0] == CATALOG_COUNT_SQL)
    assert args == ("research",)
