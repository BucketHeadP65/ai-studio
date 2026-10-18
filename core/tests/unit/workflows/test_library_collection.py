"""Workflow specs in an engram collection. The spec that comes back is the spec that went in.

Third store behind the same contract, and the property under test is the same one the table
pinned: a spec is data, end to end. Here the write is an `engram ingest` payload whose
frontmatter carries the spec as a document, and the read selects that document back — so the
adapter owns exactly two things worth testing without a database: the payload it builds, and
what it does with the rows engram answers. A fake records both, the way engram's own serve
tests fake their gateway.
"""

from __future__ import annotations

import json
import re

from typing import TYPE_CHECKING, Any

import pytest

from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from core.ai.workflows.contracts import (
    AgentStep,
    Briefing,
    FromNode,
    FromParameter,
    InputPresentation,
    LibraryCollection,
    LibraryError,
    Lineage,
    ParameterSpec,
    PresentationRefusedError,
    Program,
    ResultArtifactSpec,
    ResultFieldPresentation,
    RunNode,
    Shape,
    Workflow,
    WorkflowFingerprint,
    WorkflowIntent,
    WorkflowPresentation,
    WorkflowResultSpec,
    intent_issues,
)
from core.ai.workflows.domain.presentation import RecordOwnerReviewAction, SaveNoteAction
from core.ai.workflows.infrastructure.library_collection import CATALOG_COUNT_SQL, LIVE_VERSIONS_SQL, collection_toolset

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class FakeRows:
    """Records every statement and its arguments; answers with rows a test scripted."""

    def __init__(self, *, rows: Sequence[Mapping[str, Any]] = (), value: str | Mapping[str, Any] | None = None) -> None:
        self.rows = rows
        self.value = value
        self.sent: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, sql: str, /, *args: object) -> Sequence[Mapping[str, Any]]:
        self.sent.append((sql, args))
        return self.rows

    async def fetchval(self, sql: str, /, *args: object) -> str | Mapping[str, Any] | None:
        self.sent.append((sql, args))
        return self.value


class FakeIngest:
    """Records what engram would have been handed; refuses when a test says the collection would."""

    def __init__(self, *, refuse: str | None = None) -> None:
        self.refuse = refuse
        self.payloads: list[tuple[Mapping[str, Any], str]] = []

    async def __call__(self, payload: Mapping[str, Any], *, collection: str) -> None:
        if self.refuse is not None:
            raise RuntimeError(self.refuse)
        self.payloads.append((payload, collection))


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


async def test_a_stored_spec_comes_back_identical() -> None:
    """The invariant every store behind this contract must keep: nothing rendered, nothing re-parsed.

    The frontmatter document is what `api.cards` answers with, and the spec inside it validates
    back to the exact object that was saved.
    """
    spec = _spec()
    frontmatter = {"id": "wf.audit-a-claim", "type": "workflow", "spec": spec.model_dump(mode="json")}
    rows = FakeRows(value=frontmatter)
    assert await LibraryCollection(rows, FakeIngest()).fetch_workflow("wf.audit-a-claim") == spec


async def test_fetch_accepts_the_jsonb_as_text_too() -> None:
    """Asyncpg hands jsonb back as a string unless a codec is registered; both forms must work."""
    spec = _spec()
    rows = FakeRows(value=json.dumps({"spec": spec.model_dump(mode="json")}))
    assert await LibraryCollection(rows, FakeIngest()).fetch_workflow("wf.audit-a-claim") == spec


async def test_put_builds_a_page_whose_frontmatter_carries_the_spec_as_data() -> None:
    """The write is an authoring payload: the spec a frontmatter document, the applicability a section.

    No YAML fence, no rendering — engram receives the same JSON-shaped data a jsonb column stores.
    """
    ingest = FakeIngest()
    written = await LibraryCollection(FakeRows(), ingest).put(
        _spec(),
        title="Audit a claim",
        summary="One contested claim.",
        applicability="Use when. Do not use when.",
        provenance={"saved_by": "agent:test"},
    )
    assert written == "wf.audit-a-claim.v1"
    ((payload, collection),) = ingest.payloads
    assert collection == "workflows"
    frontmatter = payload["frontmatter"]
    assert frontmatter["id"] == "wf.audit-a-claim.v1"
    assert frontmatter["type"] == "workflow"
    assert frontmatter["workflow_version"] == 1
    assert frontmatter["saved_by"] == "agent:test", "provenance is recorded, never inferred"
    assert frontmatter["spec"]["name"] == "audit-a-claim", "the spec travels as a document, not as rendered prose"
    assert payload["sections"] == [{"heading": "Applicability", "body": "Use when. Do not use when."}]


async def test_a_refused_ingest_surfaces_as_a_library_error() -> None:
    """Engram refusing a page (grammar, indexed collection, schema drift) is the store refusing.

    `LibraryError` is the port's whole failure vocabulary: a caller shows its sentence, where a raw
    infrastructure exception would reach the surface as a traceback nobody can act on.
    """
    ingest = FakeIngest(refuse="collection 'research' is indexed from a knowledge base")
    with pytest.raises(LibraryError, match="indexed from a knowledge base"):
        await LibraryCollection(FakeRows(), ingest).put(_spec(), title="t", summary="s", applicability="a", provenance={"saved_by": "x"})
    assert ingest.payloads == [], "a refused write keeps nothing"


async def test_a_missing_id_raises_rather_than_returning_an_empty_spec() -> None:
    """`LibraryError`, which every store raises -- the routes catch one type, not one backend's type."""
    with pytest.raises(LibraryError) as missing:
        await LibraryCollection(FakeRows(value=None), FakeIngest()).fetch_workflow("wf.nope")
    assert "wf.nope" in str(missing.value)


async def test_a_page_without_a_spec_is_not_a_workflow_artifact() -> None:
    """A page that exists but carries no spec answers the same as no page: the store cannot serve it."""
    rows = FakeRows(value={"id": "wf.prose-only", "type": "workflow"})
    with pytest.raises(LibraryError, match="carries no spec"):
        await LibraryCollection(rows, FakeIngest()).fetch_workflow("wf.prose-only")


async def test_search_ranks_with_engram_and_reads_the_applicability_section() -> None:
    """Ranking is `api.search` over the collection — the library's own retrieval, not a reimplementation."""
    rows = FakeRows(
        rows=[
            {
                "id": "wf.audit-a-claim",
                "title": "Audit a claim",
                "summary": "One contested claim.",
                "applicability": "Use when one claim is contested. Do not use for a broad audit.",
                "status": "draft",
                "superseded": False,
                "presentation": None,
                "intent": None,
                "score": 0.033,
            }
        ]
    )
    found = await LibraryCollection(rows, FakeIngest()).search("contested claim", limit=3)
    assert [card.id for card in found] == ["wf.audit-a-claim"]
    assert found[0].provisional, "a draft is provisional; only a verified page is not"
    assert "Do not use" in found[0].applicability
    sql, args = rows.sent[0]
    assert "api.search" in sql, "engram ranks; this adapter only reads the ranking"
    assert "canon = 'applicability'" in sql
    assert args == ("contested claim", 15, None, "workflows", 3, "workflow"), (
        "k widened per level, limit applied per page, no embedder means no vector, page type scopes the library"
    )


async def test_a_verified_page_is_no_longer_provisional() -> None:
    """`verified` is the one engram status a chooser may trust; everything else stays a draft."""
    rows = FakeRows(
        rows=[
            {
                "id": "wf.a",
                "title": "A",
                "summary": "",
                "applicability": "",
                "status": "verified",
                "superseded": False,
                "presentation": None,
                "intent": None,
                "score": 0.03,
            }
        ]
    )
    found = await LibraryCollection(rows, FakeIngest()).search("a")
    assert not found[0].provisional


async def test_a_stamp_with_no_registered_steps_survives_the_store() -> None:
    """The store drops empty containers, and the stamp has to come back anyway.

    Found by saving a real workflow into the real collection and reading it straight back:
    `fetch_fingerprint` answered `None`. The stamp WAS written -- the frontmatter held
    `{"environment": {"model": "gpt-5.2"}, "grants": "1ad78e..."}` -- but `steps` was gone, because
    engram's ingest does not keep empty containers, and `steps` was a required field with no
    default. So the round trip produced a `ValidationError`, `fetch_fingerprint` mapped that to
    "no stamp", and the artifact was reported `staleness: null` for ever.

    That is not a corner case here. This app registers nothing -- every step a workflow uses, the
    workflow declares -- so `steps` is `{}` for every artifact it saves, which made every one of
    them permanently unstaleable in exactly the feature staleness exists for. An empty stamp and
    a missing stamp have to stay different things, so `steps` defaults.
    """
    written: dict[str, Any] = {}

    async def ingest_dropping_empties(payload: Mapping[str, Any], *, collection: str) -> None:
        del collection
        frontmatter = dict(payload["frontmatter"])
        stamp = {key: value for key, value in frontmatter["fingerprint"].items() if value not in ({}, [])}
        written["frontmatter"] = {**frontmatter, "fingerprint": stamp}

    store = LibraryCollection(FakeRows(), ingest_dropping_empties)
    await store.put(
        _spec(),
        title="Audit",
        summary="s",
        applicability="a",
        provenance={"saved_by": "test"},
        fingerprint=WorkflowFingerprint(steps={}, grants="1ad78e801fceeeb4", environment={"model": "gpt-5.2"}),
    )
    assert "steps" not in written["frontmatter"]["fingerprint"], "the store really did drop it"

    reading = LibraryCollection(FakeRows(value=written["frontmatter"]), FakeIngest())
    recovered = await reading.fetch_fingerprint("wf.audit-a-claim")

    assert recovered is not None, "an artifact that stamped its environment must not read back as unstamped"
    assert recovered.steps == {}
    assert recovered.grants == "1ad78e801fceeeb4"
    assert recovered.environment == {"model": "gpt-5.2"}


# --- supersession is a link, not a deletion ------------------------------------------------------


class FakePages(FakeRows):
    """A collection that already holds pages, keyed by id, so a supersede can read one back."""

    def __init__(self, pages: Mapping[str, Mapping[str, Any]]) -> None:
        super().__init__()
        self.pages = pages

    async def fetch(self, sql: str, /, *args: object) -> Sequence[Mapping[str, Any]]:
        self.sent.append((sql, args))
        if sql == LIVE_VERSIONS_SQL:
            # `like 'wf.<name>.v%'` over the live pages: no `archived_at` key, not superseded.
            prefix = str(args[1]).removesuffix("%")
            return [
                {"id": page["id"]}
                for page in self.pages.values()
                if str(page["id"]).startswith(prefix) and "archived_at" not in page["frontmatter"] and not page.get("superseded")
            ]
        page = self.pages.get(str(args[0]))
        return [page] if page is not None else []

    async def fetchval(self, sql: str, /, *args: object) -> str | Mapping[str, Any] | None:
        self.sent.append((sql, args))
        page = self.pages.get(str(args[0]))
        return page["frontmatter"] if page is not None else None


def _parent_page(name: str = "audit-a-claim", version: int = 1) -> Mapping[str, Any]:
    """A row shaped the way `api.cards` really answers, which is not how a page was authored.

    `kb.pages.frontmatter` holds the **long tail only** — the keys engram's page grammar does not
    model. Everything it does model (`id`, `type`, `title`, `summary`, `status`, `superseded_by`)
    is lifted into its own column and stripped from the jsonb. A fake that echoed the authored
    payload back would let a supersede pass here and be refused by engram for a missing page type,
    which is exactly what happened the first time this ran against the real collection.
    """
    return {
        "id": f"wf.{name}.v{version}",
        "type": "workflow",
        "title": "Audit a claim",
        "summary": "One contested claim.",
        "status": "draft",
        "frontmatter": {"workflow_version": version, "saved_by": "agent:test", "spec": _spec(name, version).model_dump(mode="json")},
        "applicability": "Use when one claim is contested.",
    }


async def test_a_second_version_lands_on_its_own_page_rather_than_over_the_first() -> None:
    """The bug this pins: `wf.<name>` meant saving v2 upserted v1's page and v1 stopped existing.

    Found by adapting an attested artifact in the running app. The child's lineage recorded
    `parent_version: 1`, and there was no id on which to fetch that parent -- the record of what
    used to be true had been overwritten by the thing that superseded it. `FolderLibrary` had it
    right all along (`name@version`, a new file per version), so the documented invariant "the
    incumbent is kept, never overwritten" was tested against the one store that kept it and
    violated by the one the app runs on.
    """
    ingest = FakeIngest()
    store = LibraryCollection(FakePages({}), ingest)

    first = await store.put(_spec(version=1), title="t", summary="s", applicability="a", provenance={"saved_by": "x"})
    second = await store.put(_spec(version=2), title="t", summary="s", applicability="a", provenance={"saved_by": "x"})

    assert first != second, "two versions are two artifacts to fetch, not one page twice"
    assert [payload["frontmatter"]["id"] for payload, _ in ingest.payloads] == [first, second]


async def test_saving_a_child_links_the_parent_forward_to_it() -> None:
    """Supersession is engram's own: the parent gets `superseded_by`, so `api.search` stops proposing it.

    The alternative -- filtering stale versions out of this adapter's SEARCH_SQL -- would be a
    second, worse copy of machinery the store already has.
    """
    ingest = FakeIngest()
    rows = FakePages({"wf.audit-a-claim.v1": _parent_page()})
    store = LibraryCollection(rows, ingest)

    child = await store.put(
        _spec(version=2),
        title="Audit a claim",
        summary="One contested claim.",
        applicability="Use when one claim is contested.",
        provenance={"saved_by": "agent:test"},
        lineage=Lineage(parent_name="audit-a-claim", parent_version=1, ops=({"op": "set_description", "to": "x"},), touched=()),
    )

    assert child == "wf.audit-a-claim.v2"
    (written_child, _), (written_parent, _) = ingest.payloads
    assert written_child["frontmatter"]["id"] == child, "the child is written first; the link is a foreign key"
    parent_front = written_parent["frontmatter"]
    assert parent_front["id"] == "wf.audit-a-claim.v1"
    assert parent_front["superseded_by"] == f"[[{child}]]"
    assert parent_front["spec"]["version"] == 1, "the parent keeps its own spec -- this is a link, not a rewrite"
    assert written_parent["sections"] == [{"heading": "Applicability", "body": "Use when one claim is contested."}]
    # The envelope engram strips into columns has to be put back, or the page it gets is typeless.
    assert (parent_front["type"], parent_front["title"], parent_front["status"]) == ("workflow", "Audit a claim", "draft")
    assert parent_front["workflow_version"] == 1, "and the long tail rides along untouched"


async def test_a_parent_the_collection_does_not_hold_is_not_invented() -> None:
    """Artifacts saved before ids carried versions have no page at the derived id, and that is fine.

    The child is stored either way. A store that raised here would make the first adaptation of
    every pre-existing artifact fail for a reason the author cannot act on.
    """
    ingest = FakeIngest()
    store = LibraryCollection(FakePages({}), ingest)

    child = await store.put(
        _spec(version=2),
        title="t",
        summary="s",
        applicability="a",
        provenance={"saved_by": "x"},
        lineage=Lineage(parent_name="audit-a-claim", parent_version=1, ops=({"op": "set_description", "to": "x"},), touched=()),
    )

    assert child == "wf.audit-a-claim.v2"
    assert len(ingest.payloads) == 1, "nothing to link forward, so nothing is written"


async def test_saving_a_higher_version_retires_every_live_lower_version_of_its_name() -> None:
    """Two versions of one name are one artifact, so a fresh save of v3 links live v1 and v2 forward to it.

    Lineage or not: the authoring path bumps a version and saves it fresh, and without this link
    the library would offer three "current" copies of one workflow. A page of another name that
    merely shares the prefix (`audit-a-claim-more`) is not that artifact and is left alone.
    """
    ingest = FakeIngest()
    rows = FakePages(
        {
            "wf.audit-a-claim.v1": _parent_page(version=1),
            "wf.audit-a-claim.v2": _parent_page(version=2),
            "wf.audit-a-claim-more.v1": _parent_page(name="audit-a-claim-more", version=1),
        }
    )
    child = await LibraryCollection(rows, ingest).put(
        _spec(version=3), title="t", summary="s", applicability="a", provenance={"saved_by": "x"}
    )
    assert child == "wf.audit-a-claim.v3"
    written = [payload["frontmatter"]["id"] for payload, _ in ingest.payloads]
    assert written == ["wf.audit-a-claim.v3", "wf.audit-a-claim.v1", "wf.audit-a-claim.v2"], "the child first, then each stale version"
    assert all(payload["frontmatter"]["superseded_by"] == f"[[{child}]]" for payload, _ in ingest.payloads[1:])
    _, args = next(sent for sent in rows.sent if sent[0] == LIVE_VERSIONS_SQL)
    assert args == ("workflows", "wf.audit-a-claim.v%"), "the scan is scoped to this collection and this name's prefix"


async def test_a_lower_version_already_retired_or_archived_is_not_relinked() -> None:
    """The scan reads the library's own live pages: a superseded or archived lower version is nobody's business again."""
    ingest = FakeIngest()
    rows = FakePages(
        {
            "wf.audit-a-claim.v1": {**_parent_page(version=1), "superseded": True},
            "wf.audit-a-claim.v2": {
                **_parent_page(version=2),
                "frontmatter": {**_parent_page(version=2)["frontmatter"], "archived_at": "2026-08-01T00:00:00+00:00"},
            },
        }
    )
    await LibraryCollection(rows, ingest).put(_spec(version=3), title="t", summary="s", applicability="a", provenance={"saved_by": "x"})
    assert [payload["frontmatter"]["id"] for payload, _ in ingest.payloads] == ["wf.audit-a-claim.v3"]


async def test_a_lower_version_saved_after_a_higher_one_stays_live() -> None:
    """Saving v1 while v3 is live is a person's own choice; neither page is linked anywhere."""
    ingest = FakeIngest()
    rows = FakePages({"wf.audit-a-claim.v3": _parent_page(version=3)})
    await LibraryCollection(rows, ingest).put(_spec(version=1), title="t", summary="s", applicability="a", provenance={"saved_by": "x"})
    assert [payload["frontmatter"]["id"] for payload, _ in ingest.payloads] == ["wf.audit-a-claim.v1"]


async def test_the_lineage_parent_that_is_also_a_lower_version_is_linked_once() -> None:
    """A child adapted from v1 of its own name links v1 through the lineage, and the name scan does not link it twice."""
    ingest = FakeIngest()
    rows = FakePages({"wf.audit-a-claim.v1": _parent_page(version=1)})
    await LibraryCollection(rows, ingest).put(
        _spec(version=2),
        title="t",
        summary="s",
        applicability="a",
        provenance={"saved_by": "x"},
        lineage=Lineage(parent_name="audit-a-claim", parent_version=1, ops=({"op": "set_description", "to": "x"},), touched=()),
    )
    assert [payload["frontmatter"]["id"] for payload, _ in ingest.payloads] == ["wf.audit-a-claim.v2", "wf.audit-a-claim.v1"]


async def test_current_names_the_newest_live_version_of_an_artifact() -> None:
    """A standing reference to v1 resolves to the newest live page of its name; the newest resolves to itself."""
    rows = FakePages(
        {
            "wf.audit-a-claim.v1": {**_parent_page(version=1), "superseded": True},
            "wf.audit-a-claim.v2": _parent_page(version=2),
            "wf.audit-a-claim.v3": {
                **_parent_page(version=3),
                "frontmatter": {**_parent_page(version=3)["frontmatter"], "archived_at": "2026-08-01T00:00:00+00:00"},
            },
            "wf.audit-a-claim.v10": _parent_page(version=10),
        }
    )
    store = LibraryCollection(rows, FakeIngest())
    assert await store.current("wf.audit-a-claim.v1") == "wf.audit-a-claim.v10", "numeric order, not text order: v10 beats v2"
    assert await store.current("wf.audit-a-claim.v10") == "wf.audit-a-claim.v10"


async def test_current_answers_itself_when_it_cannot_name_a_newer_version() -> None:
    """An id this store did not mint, or a name with no live version, resolves to the id it was given."""
    store = LibraryCollection(FakePages({"wf.audit-a-claim.v1": {**_parent_page(version=1), "superseded": True}}), FakeIngest())
    assert await store.current("audit-a-claim@1") == "audit-a-claim@1"
    assert await store.current("wf.audit-a-claim.v1") == "wf.audit-a-claim.v1"


async def test_a_bare_name_fetches_the_newest_live_version() -> None:
    """The supersession refusal says "fetch the newest version of '<name>'" -- so the bare name has to work at the door.

    Live, the agent did exactly what that message told it to and got `no workflow
    'wf.research-compound' in collection 'workflows'`: the fetch demanded the versioned id
    while nothing anywhere said what the newest version was. Both bare forms resolve -- the
    prefixed `wf.<name>` and the plain `<name>` the refusal itself prints.
    """
    rows = FakePages(
        {
            "wf.audit-a-claim.v1": {**_parent_page(version=1), "superseded": True},
            "wf.audit-a-claim.v2": _parent_page(version=2),
        }
    )
    store = LibraryCollection(rows, FakeIngest())

    assert (await store.fetch_workflow("wf.audit-a-claim")).version == 2
    assert (await store.fetch_workflow("audit-a-claim")).version == 2
    card = await store.fetch_card("audit-a-claim")
    assert card is not None
    assert card.id == "wf.audit-a-claim.v2"


async def test_a_versioned_fetch_still_answers_exactly_that_version() -> None:
    """Reading a superseded page is legitimate -- lineage points at parents -- so a versioned id is never redirected."""
    rows = FakePages(
        {
            "wf.audit-a-claim.v1": {**_parent_page(version=1), "superseded": True},
            "wf.audit-a-claim.v2": _parent_page(version=2),
        }
    )
    assert (await LibraryCollection(rows, FakeIngest()).fetch_workflow("wf.audit-a-claim.v1")).version == 1


async def test_a_bare_name_with_no_live_version_still_names_itself_in_the_refusal() -> None:
    rows = FakePages({"wf.audit-a-claim.v1": {**_parent_page(version=1), "superseded": True}})
    with pytest.raises(LibraryError, match=re.escape("wf.audit-a-claim")):
        await LibraryCollection(rows, FakeIngest()).fetch_workflow("wf.audit-a-claim")


async def test_re_saving_a_superseded_version_is_refused_with_the_reason() -> None:
    """Re-minting a version the store already superseded is refused cold, never silently done.

    The state this pins was live: a superseded v1 was re-saved, the upsert wrote frontmatter
    with no `superseded_by`, and the link was erased -- so search offered the stale v1 beside
    the current version from then on. `api.cards` exposes only the boolean, so once the link is
    overwritten nothing can read back whom it pointed at: the erase has to be refused up front,
    and the refusal has to say what to do instead.
    """
    ingest = FakeIngest()
    rows = FakePages({"wf.audit-a-claim.v1": {**_parent_page(), "superseded": True}})
    store = LibraryCollection(rows, ingest)

    with pytest.raises(LibraryError, match="superseded") as refusal:
        await store.put(_spec(version=1), title="t", summary="s", applicability="a", provenance={"saved_by": "x"})

    assert ingest.payloads == [], "refused means nothing was written"
    assert "adapt" in str(refusal.value).lower(), "the refusal names the way forward, not just the fault"


async def test_re_saving_the_current_version_is_still_an_upsert() -> None:
    """The recovery path put promises -- saving again redoes both writes -- stays open.

    Only a SUPERSEDED page refuses the upsert; the current version of anything re-saves freely,
    which is what makes a failed supersession link recoverable at all.
    """
    ingest = FakeIngest()
    rows = FakePages({"wf.audit-a-claim.v1": _parent_page()})
    written = await LibraryCollection(rows, ingest).put(
        _spec(version=1), title="t", summary="s", applicability="a", provenance={"saved_by": "x"}
    )
    assert written == "wf.audit-a-claim.v1"
    assert len(ingest.payloads) == 1


async def test_an_adaptation_that_moved_no_node_still_reads_back_as_an_adaptation() -> None:
    """The same trap as the empty stamp, one model over -- and this one was live before it was caught.

    `set_description` touches no node, so `touched` is `()`. Engram's ingest drops empty containers
    and `Lineage.touched` was required, so the child's lineage failed to validate on the way back
    and `fetch_lineage` reported `None`: an adaptation that had a parent, a diff and a supersession
    link, rendering in the app as an artifact nobody adapted.

    Which means the fix for the stamp was a fix for one *field*, not for the class of bug. Any
    container that can legitimately be empty and crosses a store has to survive coming back absent,
    and `touched == ()` is a real state -- a diff that changed the prose and left every node alone.
    `ops` deliberately stays required: an adaptation with no operations is not one, so a record
    missing them is broken and must read as broken rather than as an empty edit.
    """
    written: dict[str, Any] = {}

    async def ingest_dropping_empties(payload: Mapping[str, Any], *, collection: str) -> None:
        del collection
        frontmatter = dict(payload["frontmatter"])
        written["frontmatter"] = {**frontmatter, "lineage": {k: v for k, v in frontmatter["lineage"].items() if v not in ([], {})}}

    await LibraryCollection(FakePages({}), ingest_dropping_empties).put(
        _spec(version=2),
        title="t",
        summary="s",
        applicability="a",
        provenance={"saved_by": "x"},
        lineage=Lineage(parent_name="audit-a-claim", parent_version=1, ops=({"op": "set_description", "to": "clearer"},), touched=()),
    )
    assert "touched" not in written["frontmatter"]["lineage"], "the store really did drop it"

    reading = LibraryCollection(FakeRows(value=written["frontmatter"]), FakeIngest())
    recovered = await reading.fetch_lineage("wf.audit-a-claim.v2")

    assert recovered is not None, "an artifact that recorded its parent must not read back as parentless"
    assert recovered.parent_name == "audit-a-claim"
    assert recovered.touched == ()


async def test_a_child_that_could_not_be_linked_is_still_a_child_that_was_stored() -> None:
    """The two writes are not equal, and treating them as one reported a lie to the person saving.

    Seen live: saving an adaptation answered `ok: false` with an engram traceback
    (`ModelAPIError: Connection error` re-embedding the parent), and the child was in the store
    the whole time -- fetchable, with its lineage intact. Pressing Save again "worked", because
    the child upserted and the retried link went through.

    Losing the link degrades retrieval: both versions stay in the library until somebody re-saves.
    Losing the child loses the work. So the child's write is the save, the link is bookkeeping,
    and a bookkeeping failure warns rather than raising -- with the two versions sitting visibly
    side by side as the symptom.
    """
    calls: list[str] = []

    async def ingest(payload: Mapping[str, Any], *, collection: str) -> None:
        del collection
        page = str(payload["frontmatter"]["id"])
        calls.append(page)
        if "superseded_by" in payload["frontmatter"]:
            msg = "ModelAPIError: Connection error."
            raise RuntimeError(msg)

    store = LibraryCollection(FakePages({"wf.audit-a-claim.v1": _parent_page()}), ingest)
    with pytest.warns(UserWarning, match="wf.audit-a-claim.v1"):
        child = await store.put(
            _spec(version=2),
            title="t",
            summary="s",
            applicability="a",
            provenance={"saved_by": "x"},
            lineage=Lineage(parent_name="audit-a-claim", parent_version=1, ops=({"op": "set_description", "to": "x"},), touched=()),
        )
    assert child == "wf.audit-a-claim.v2", "the artifact is stored, and the caller is told where"
    assert calls == ["wf.audit-a-claim.v2", "wf.audit-a-claim.v1"], "the child first, then the link that failed"


async def test_one_artifact_s_card_is_readable_without_searching_for_it() -> None:
    """The prose a chooser reads, fetched by id -- `search` was the only way to it, which is a filter.

    Needed because adapting an artifact has to inherit the words it was described by, and those
    words are the library's, not the spec's.
    """
    store = LibraryCollection(FakePages({"wf.audit-a-claim.v1": _parent_page()}), FakeIngest())
    card = await store.fetch_card("wf.audit-a-claim.v1")
    assert card is not None
    assert (card.id, card.title, card.summary) == ("wf.audit-a-claim.v1", "Audit a claim", "One contested claim.")
    assert card.applicability == "Use when one claim is contested."
    assert await store.fetch_card("wf.nope") is None


async def test_a_parent_is_located_by_name_and_version_because_only_the_store_mints_ids() -> None:
    """The inverse of `put`: a lineage says name and version, and the store says which page that is.

    Asked of the store rather than assembled by the caller, because the two adapters disagree about
    slugs on purpose (`wf.x.v1` here, `x@1` in a folder) and a caller that guessed would work
    against exactly one of them.
    """
    rows = FakePages({"wf.audit-a-claim.v1": _parent_page()})
    store = LibraryCollection(rows, FakeIngest())
    assert await store.locate("audit-a-claim", 1) == "wf.audit-a-claim.v1"
    assert await store.locate("audit-a-claim", 2) is None, "a page the collection does not hold has no address"


async def test_the_collection_s_write_tool_carries_a_diff_into_the_frontmatter() -> None:
    """The other host surface behind the same contract: `parent` travels, and supersedes.

    The folder host proves the port end of the wire; this pins the collection end -- the lineage
    lands in the child page's frontmatter and the parent page gets linked forward, exactly as a
    `put` with a lineage would. One write path, whichever door called it.
    """
    ingest = FakeIngest()
    store = LibraryCollection(FakePages({"wf.audit-a-claim.v1": _parent_page()}), ingest)
    toolset = collection_toolset(store)

    ctx: RunContext[object] = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    tools = await toolset.get_tools(ctx)
    lineage = Lineage(parent_name="audit-a-claim", parent_version=1, ops=({"op": "set_description", "to": "x"},))
    answer = await toolset.call_tool(
        "write_workflow",
        {
            "title": "Audit a claim",
            "summary": "s",
            "when": "Use when.",
            "spec": _spec(version=2).model_dump(mode="json"),
            "by": "agent:test",
            "parent": lineage.model_dump(mode="json"),
        },
        ctx,
        tools["write_workflow"],
    )
    assert answer == {"saved": "wf.audit-a-claim.v2"}
    (child_payload, _), (parent_payload, _) = ingest.payloads
    assert child_payload["frontmatter"]["lineage"]["parent_version"] == 1
    assert parent_payload["frontmatter"]["superseded_by"] == "[[wf.audit-a-claim.v2]]"


# --- steps in the library ----------------------------------------------------------------------


def _step() -> Briefing:
    return Briefing(
        name="digest_source",
        instructions="Read the whole mirror and write one digest page with quoted claims.",
        inputs={"source": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema={"type": "string"}),
        tools=("fetch", "write_note"),
    )


def _code_step() -> Program:
    return Program(
        name="dedupe_sources",
        source="len(items)",
        inputs={"items": Shape(json_schema={"type": "array"})},
        output=Shape(json_schema={"type": "integer"}),
    )


def _worker_step() -> AgentStep:
    return AgentStep(
        name="scout_agent",
        agent="scout",
        inputs={"topic": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema={"type": "string"}),
    )


async def test_a_step_lands_on_its_own_page_type_and_comes_back_identical() -> None:
    ingest = FakeIngest()
    store = LibraryCollection(FakeRows(), ingest)
    step_id = await store.put_step(
        _step(),
        version=1,
        title="Digest one source",
        summary="One mirror, one digest.",
        applicability="Use per mirror.",
        provenance={"saved_by": "owner:cli"},
    )
    assert step_id == "st.digest_source.v1"
    payload = ingest.payloads[0][0]
    assert payload["frontmatter"]["type"] == "step"
    assert payload["frontmatter"]["step_version"] == 1
    assert payload["frontmatter"]["step"]["name"] == "digest_source"
    assert payload["sections"] == [{"heading": "Applicability", "body": "Use per mirror."}]


async def test_two_versions_of_a_step_are_independent_pages() -> None:
    """Unlike a workflow, a step page carries no supersession link: each version is its own write."""
    ingest = FakeIngest()
    store = LibraryCollection(FakeRows(), ingest)

    first = await store.put_step(_step(), version=1, title="t", summary="s", applicability="a", provenance={"saved_by": "x"})
    second = await store.put_step(_step(), version=2, title="t", summary="s", applicability="a", provenance={"saved_by": "x"})

    assert (first, second) == ("st.digest_source.v1", "st.digest_source.v2")
    assert [payload["frontmatter"]["id"] for payload, _ in ingest.payloads] == [first, second]
    assert len(ingest.payloads) == 2, "saving v2 writes only v2's page; v1's page is never touched"


async def test_a_code_step_round_trips_through_put_and_fetch() -> None:
    """A program, not just a briefing, comes back the way it went in."""
    ingest = FakeIngest()
    step_id = await LibraryCollection(FakeRows(), ingest).put_step(
        _code_step(), version=1, title="Dedupe sources", summary="Counts items.", applicability="Use per batch.",
        provenance={"saved_by": "owner:cli"},
    )
    frontmatter = ingest.payloads[0][0]["frontmatter"]
    recovered = await LibraryCollection(FakeRows(value=frontmatter), FakeIngest()).fetch_step(step_id)
    assert recovered == _code_step()


async def test_a_worker_step_round_trips_through_put_and_fetch() -> None:
    """A whole declared agent, not just a briefing, comes back the way it went in."""
    ingest = FakeIngest()
    step_id = await LibraryCollection(FakeRows(), ingest).put_step(
        _worker_step(), version=1, title="Scout agent", summary="Looks something up.", applicability="Use per topic.",
        provenance={"saved_by": "owner:cli"},
    )
    frontmatter = ingest.payloads[0][0]["frontmatter"]
    recovered = await LibraryCollection(FakeRows(value=frontmatter), FakeIngest()).fetch_step(step_id)
    assert recovered == _worker_step()


async def test_fetch_step_validates_the_stored_document() -> None:
    rows = FakeRows(value={"id": "st.digest_source.v1", "type": "step", "step": _step().model_dump(mode="json")})
    step = await LibraryCollection(rows, FakeIngest()).fetch_step("st.digest_source.v1")
    assert isinstance(step, Briefing)
    assert step.name == "digest_source"


async def test_fetch_step_missing_id_names_a_step_not_a_workflow() -> None:
    """The not-found message calls a missing step id a step, not the generic `fetch_workflow` wording."""
    with pytest.raises(LibraryError, match="no step") as missing:
        await LibraryCollection(FakeRows(value=None), FakeIngest()).fetch_step("st.nope.v1")
    assert "st.nope.v1" in str(missing.value)


async def test_a_page_without_a_step_is_not_a_shelf_step() -> None:
    rows = FakeRows(value={"id": "st.x.v1", "type": "step"})
    with pytest.raises(LibraryError, match=re.escape("st.x.v1")):
        await LibraryCollection(rows, FakeIngest()).fetch_step("st.x.v1")


async def test_search_steps_asks_for_step_pages_only() -> None:
    rows = FakeRows()
    await LibraryCollection(rows, FakeIngest()).search_steps("digest a mirror", limit=3)
    sql, args = rows.sent[0]
    assert "'step'" in sql or "step" in args
    assert 3 in args


async def test_put_records_which_shelf_steps_a_workflow_used() -> None:
    ingest = FakeIngest()
    await LibraryCollection(FakeRows(), ingest).put(
        _spec(), title="t", summary="s", applicability="when", provenance={"saved_by": "agent", "uses": "st.a.v1,st.b.v2"}
    )
    assert ingest.payloads[0][0]["frontmatter"]["uses"] == ["st.a.v1", "st.b.v2"]


async def test_the_collection_catalog_tool_answers_a_total_alongside_its_rows() -> None:
    """`list_workflows` gains a `total` field; the row shape `find_workflows` already answers in is untouched."""
    catalog_rows: list[Mapping[str, Any]] = [
        {"id": "wf.a.v1", "title": "A", "summary": "s", "status": "draft", "applicability": "Use when a.", "presentation": None, "intent": None},
        {"id": "wf.b.v1", "title": "B", "summary": "s", "status": "draft", "applicability": "Use when b.", "presentation": None, "intent": None},
    ]

    class _Rows:
        async def fetch(self, sql: str, /, *args: object) -> Sequence[Mapping[str, Any]]:
            if sql == CATALOG_COUNT_SQL:
                return [{"total": len(catalog_rows)}]
            return catalog_rows

        async def fetchval(self, sql: str, /, *args: object) -> str | Mapping[str, Any] | None:  # pragma: no cover - not exercised here
            msg = "fetchval should not be called by the catalog tool"
            raise AssertionError(msg)

    toolset = collection_toolset(LibraryCollection(_Rows(), FakeIngest()))
    ctx: RunContext[object] = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    tools = await toolset.get_tools(ctx)

    answer = await toolset.call_tool("list_workflows", {}, ctx, tools["list_workflows"])

    assert answer == {
        "total": 2,
        "found": [
            {"id": "wf.a.v1", "title": "A", "summary": "s", "when": "Use when a.", "score": 0.0, "draft": True},
            {"id": "wf.b.v1", "title": "B", "summary": "s", "when": "Use when b.", "score": 0.0, "draft": True},
        ],
    }


# --- the presentation a page carries --------------------------------------------------------------


def _presented_spec() -> Workflow:
    """The same workflow, with two parameters and one declared result artifact to point a presentation at."""
    return Workflow(
        name="audit-a-claim",
        version=1,
        description="Check one contested claim and report what would settle it.",
        parameters={
            "claim": ParameterSpec(description="the claim", json_schema={"type": "string"}),
            "deadline": ParameterSpec(description="when the answer is needed", json_schema={"type": "string"}),
        },
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
        result=WorkflowResultSpec(
            summary="The note the run settled on.",
            primary_work="weigh",
            artifacts=(
                ResultArtifactSpec(id="note", label="The note", source=FromNode(node="weigh"), field="note", media_type="text/markdown"),
            ),
        ),
    )


def _presentation() -> WorkflowPresentation:
    """Every field the card must carry back unchanged."""
    return WorkflowPresentation(
        purpose="Settle one contested claim against the sources you already hold.",
        produces="A note naming what would settle the claim, and what is still open.",
        owner_participation="You read the note and decide whether to keep it.",
        not_for="Auditing every claim in a long document at once.",
        inputs=(
            InputPresentation(key="claim", label="The claim to weigh", control="textarea"),
            InputPresentation(key="deadline", label="When you need the answer"),
        ),
        result_fields=(ResultFieldPresentation(label="Sources weighed", path="counts.succeeded"),),
        actions=(SaveNoteAction(id="keep_the_note", label="Save the note", artifact_id="note"),),
    )


def _page_row(presentation: WorkflowPresentation | None, intent: WorkflowIntent | None = None) -> dict[str, Any]:
    """One `api.cards` row for the stored page, carrying every column the read queries select.

    The listings select the presentation and the intent documents on their own
    (`c.frontmatter -> 'presentation'`, `c.frontmatter -> 'intent'`), the by-id reads select the
    whole frontmatter, so one row here answers both.
    """
    stored = None if presentation is None else presentation.model_dump(mode="json")
    asked_for = None if intent is None else intent.model_dump(mode="json")
    frontmatter: dict[str, Any] = {"spec": _presented_spec().model_dump(mode="json")}
    if stored is not None:
        frontmatter["presentation"] = stored
    if asked_for is not None:
        frontmatter["intent"] = asked_for
    return {
        "id": "wf.audit-a-claim.v1",
        "type": "workflow",
        "title": "Audit a claim",
        "summary": "One contested claim.",
        "applicability": "Use when one claim is contested.",
        "status": "draft",
        "superseded": False,
        "score": 0.04,
        "presentation": stored,
        "intent": asked_for,
        "frontmatter": frontmatter,
    }


async def test_put_writes_the_presentation_into_the_pages_own_frontmatter() -> None:
    ingest = FakeIngest()
    presentation = _presentation()

    await LibraryCollection(FakeRows(), ingest).put(
        _presented_spec(),
        title="Audit a claim",
        summary="One contested claim.",
        applicability="Use when one claim is contested.",
        provenance={"saved_by": "agent:test"},
        presentation=presentation,
    )

    assert ingest.payloads[0][0]["frontmatter"]["presentation"] == presentation.model_dump(mode="json")


async def test_a_presentation_comes_back_unchanged_from_every_read_surface() -> None:
    presentation = _presentation()
    library = LibraryCollection(FakeRows(rows=[_page_row(presentation)]), FakeIngest())

    assert (await library.search("contested claim"))[0].presentation == presentation
    assert (await library.catalog())[0].presentation == presentation
    assert (await library.archived())[0].presentation == presentation
    card = await library.fetch_card("wf.audit-a-claim.v1")
    assert card is not None
    assert card.presentation == presentation


async def test_redescribe_and_archiving_both_keep_the_presentation() -> None:
    """Each rewrite spreads the page's long tail first, so the presentation rides along untouched."""
    presentation = _presentation()
    stored = presentation.model_dump(mode="json")
    live = _page_row(presentation)
    archived_page = {**live, "frontmatter": {**live["frontmatter"], "archived_at": "2026-08-01T00:00:00+00:00"}}

    rewrites = FakeIngest()
    library = LibraryCollection(FakeRows(rows=[live]), rewrites)
    await library.redescribe("wf.audit-a-claim.v1", title="Audit one claim", summary="One claim.", applicability="Use when contested.")
    await library.set_archived("wf.audit-a-claim.v1", archived=True)

    restores = FakeIngest()
    await LibraryCollection(FakeRows(rows=[archived_page]), restores).set_archived("wf.audit-a-claim.v1", archived=False)

    assert [payload["frontmatter"]["presentation"] for payload, _ in (*rewrites.payloads, *restores.payloads)] == [stored, stored, stored]


async def test_a_page_written_without_a_presentation_reads_as_none() -> None:
    """A page that predates presentations carries none, and nothing invents one for it."""
    library = LibraryCollection(FakeRows(rows=[_page_row(None)]), FakeIngest())

    card = await library.fetch_card("wf.audit-a-claim.v1")
    assert card is not None
    assert card.presentation is None
    assert (card.id, card.title, card.summary, card.applicability) == (
        "wf.audit-a-claim.v1",
        "Audit a claim",
        "One contested claim.",
        "Use when one claim is contested.",
    )
    assert (await library.search("contested claim"))[0].presentation is None


async def test_put_refuses_a_presentation_naming_an_undeclared_input_before_it_writes() -> None:
    ingest = FakeIngest()
    with pytest.raises(PresentationRefusedError, match="quarter"):
        await LibraryCollection(FakeRows(), ingest).put(
            _presented_spec(),
            title="Audit a claim",
            summary="One contested claim.",
            applicability="Use when.",
            provenance={"saved_by": "agent:test"},
            presentation=WorkflowPresentation(inputs=(InputPresentation(key="quarter", label="Which quarter"),)),
        )
    assert ingest.payloads == [], "a refused presentation keeps nothing"


async def test_put_refuses_an_action_naming_an_undeclared_artifact_before_it_writes() -> None:
    ingest = FakeIngest()
    with pytest.raises(PresentationRefusedError, match="transcript"):
        await LibraryCollection(FakeRows(), ingest).put(
            _presented_spec(),
            title="Audit a claim",
            summary="One contested claim.",
            applicability="Use when.",
            provenance={"saved_by": "agent:test"},
            presentation=WorkflowPresentation(
                actions=(RecordOwnerReviewAction(id="sign_off", label="Record the review", artifact_ids=("transcript",)),)
            ),
        )
    assert ingest.payloads == [], "a refused presentation keeps nothing"


# --- the intent a page carries --------------------------------------------------------------------


def _intent() -> WorkflowIntent:
    """One fully stated intent, as a page hands it back."""
    return WorkflowIntent(
        request="Weigh every contested claim in the sources I keep",
        expected_result="A note naming what would settle the claim.",
        success_example="Last week's disputed figure came back with the two sources that disagree.",
        empty_input_behavior="Nothing was contested, so the run says so and files nothing.",
        failure_behavior="A source that cannot be read is named and the rest are still weighed.",
        trigger="scheduled",
        assumptions=("Contested means two sources disagree.",),
    )


async def test_put_writes_the_intent_into_the_pages_own_frontmatter() -> None:
    ingest = FakeIngest()
    intent = _intent()

    await LibraryCollection(FakeRows(), ingest).put(
        _presented_spec(),
        title="Audit a claim",
        summary="One contested claim.",
        applicability="Use when one claim is contested.",
        provenance={"saved_by": "agent:test"},
        intent=intent,
    )

    assert ingest.payloads[0][0]["frontmatter"]["intent"] == intent.model_dump(mode="json")


async def test_an_intent_comes_back_unchanged_from_every_read_surface() -> None:
    intent = _intent()
    library = LibraryCollection(FakeRows(rows=[_page_row(None, intent)]), FakeIngest())

    assert (await library.search("contested claim"))[0].intent == intent
    assert (await library.catalog())[0].intent == intent
    assert (await library.archived())[0].intent == intent
    card = await library.fetch_card("wf.audit-a-claim.v1")
    assert card is not None
    assert card.intent == intent


async def test_a_request_only_intent_is_kept_exactly_as_incomplete_as_it_was_stated() -> None:
    """A page may hold a half-written intent, and reading it back never fills the rest in."""
    partial = WorkflowIntent(request="Weigh every contested claim")
    library = LibraryCollection(FakeRows(rows=[_page_row(None, partial)]), FakeIngest())

    card = await library.fetch_card("wf.audit-a-claim.v1")
    assert card is not None
    assert card.intent == partial
    assert intent_issues(partial) == ("expected_result", "success_example", "empty_input_behavior", "failure_behavior")


async def test_redescribe_and_archiving_both_keep_the_intent() -> None:
    """Each rewrite spreads the page's long tail first, so what was asked for rides along untouched."""
    intent = _intent()
    stored = intent.model_dump(mode="json")
    live = _page_row(None, intent)
    archived_page = {**live, "frontmatter": {**live["frontmatter"], "archived_at": "2026-08-01T00:00:00+00:00"}}

    rewrites = FakeIngest()
    library = LibraryCollection(FakeRows(rows=[live]), rewrites)
    await library.redescribe("wf.audit-a-claim.v1", title="Audit one claim", summary="One claim.", applicability="Use when contested.")
    await library.set_archived("wf.audit-a-claim.v1", archived=True)

    restores = FakeIngest()
    await LibraryCollection(FakeRows(rows=[archived_page]), restores).set_archived("wf.audit-a-claim.v1", archived=False)

    assert [payload["frontmatter"]["intent"] for payload, _ in (*rewrites.payloads, *restores.payloads)] == [stored, stored, stored]


async def test_a_page_written_without_an_intent_reads_as_none_and_carries_no_key() -> None:
    """A page that predates intents is written and read exactly as it always was."""
    ingest = FakeIngest()
    library = LibraryCollection(FakeRows(rows=[_page_row(None)]), ingest)

    await library.put(
        _presented_spec(),
        title="Audit a claim",
        summary="One contested claim.",
        applicability="Use when one claim is contested.",
        provenance={"saved_by": "agent:test"},
    )
    card = await library.fetch_card("wf.audit-a-claim.v1")

    assert "intent" not in ingest.payloads[0][0]["frontmatter"]
    assert card is not None
    assert card.intent is None
    assert (await library.search("contested claim"))[0].intent is None


async def test_a_stored_intent_that_no_longer_parses_reads_as_absent() -> None:
    """A page is hand-editable data, so an unreadable intent is one card without one."""
    row = {**_page_row(None), "intent": {"trigger": "whenever"}}
    library = LibraryCollection(FakeRows(rows=[row]), FakeIngest())

    assert (await library.search("contested claim"))[0].intent is None


async def test_an_intent_stored_as_jsonb_text_reads_back_the_same() -> None:
    """The driver may hand jsonb back as text, the same way a presentation arrives."""
    intent = _intent()
    row = {**_page_row(None, intent), "intent": json.dumps(intent.model_dump(mode="json"))}
    library = LibraryCollection(FakeRows(rows=[row]), FakeIngest())

    assert (await library.search("contested claim"))[0].intent == intent
