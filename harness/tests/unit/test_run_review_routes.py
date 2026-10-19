"""`POST /api/runs/history/{run_id}/review`: an owner's verdict, backed by artifacts a run fixed.

Every reference in the request is resolved and confirmed against the run's own archived record
before anything is written, by the very rules `artifacts/resolve` answers with. They are reused here
through `RunArtifactResolver`, never reimplemented. The route table below is the whole refusal
surface: a status per way a reference or a repeated review id can fail, and what the store holds
afterward.
"""

from __future__ import annotations

import asyncio

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from core.ai.artifacts import ArtifactDocument, ArtifactRef, artifact_digest
from core.ai.evaluation.contracts import OwnerReviewRequest, OwnerReviewService
from core.ai.workflows.contracts import (
    FromNode,
    FromParameter,
    Outcome,
    ParameterSpec,
    PersonGate,
    ResultArtifactSpec,
    RunNode,
    Workflow,
    WorkflowResultSpec,
)

from harness.features.running.domain.ended_run import RunArchiveRecord
from harness.features.running.infrastructure.archive import FileRunArchive
from tests.support.controllers import a_client_of
from tests.support.running import FakeArtifactResolver, FakeOwnerReviewStore, a_pool, a_running_controller
from tests.support.state import KEPT_DAYS

if TYPE_CHECKING:
    from pathlib import Path

    import httpx

    from core.ai.workflows.contracts import StepRegistry

    from harness.session import SessionPool

RUN = "20260915T120000000000-abcdef12"
DRAFT = {"text": "net: 400", "net": 400}


def _spec() -> Workflow:
    """A workflow declaring two artifacts: the whole draft, and one field of it as markdown."""
    return Workflow(
        name="quarterly-report",
        version=1,
        description="Draft a report, have it approved, and declare what a caller may select.",
        parameters={"quarter": ParameterSpec(description="Fiscal quarter", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="draft_report", work="draft_report", inputs={"quarter": FromParameter(parameter="quarter")}),
            PersonGate(id="approve", reviews=FromNode(node="draft_report"), rejected_to="draft_report"),
        ),
        output=FromNode(node="approve"),
        result=WorkflowResultSpec(
            summary="the approved draft",
            primary_work="draft_report",
            artifacts=(
                ResultArtifactSpec(id="draft", label="The draft", source=FromNode(node="draft_report"), media_type="application/json"),
                ResultArtifactSpec(
                    id="prose", label="The prose", source=FromNode(node="draft_report"), field="text", media_type="text/markdown"
                ),
            ),
        ),
    )


def _archive_of(state: Path) -> FileRunArchive:
    return FileRunArchive(state / "runs" / "archive", retention_days=KEPT_DAYS)


def _seed(state: Path, *, run_id: str = RUN, values: dict[str, object] | None = None) -> None:
    now = datetime.now(UTC).isoformat()
    _archive_of(state).put(
        RunArchiveRecord(
            run_id=run_id,
            workflow_id="wf.report",
            outcome="completed",
            result=Outcome(status="unknown", summary="no result declared"),
            started_at=now,
            ended_at=now,
            runner="app:harness",
            session_id="sess-1",
            params={"quarter": "Q2"},
            spec=_spec().model_dump(mode="json"),
            values=values if values is not None else {"draft_report": DRAFT},
            events=[],
        )
    )


def _ref(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "id": "draft",
        "label": "The draft",
        "run_id": RUN,
        "node_id": "draft_report",
        "value_digest": artifact_digest(DRAFT),
        "media_type": "application/json",
    }
    return ArtifactRef.model_validate(fields | overrides).model_dump(mode="json")


def _client_over(pool: SessionPool, state: Path) -> httpx.AsyncClient:
    return a_client_of(a_running_controller(pool, state))


def _body(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "review_id": "r1",
        "actor": "owner",
        "outcome": "accepted",
        "reason": "Matches the success criterion, cites two sources.",
        "artifact_refs": [_ref()],
    }
    return fields | overrides


async def _review(client: httpx.AsyncClient, body: dict[str, object], *, run_id: str = RUN) -> httpx.Response:
    return await client.post(f"/api/runs/history/{run_id}/review", json=body)


# --- the route/service table ---------------------------------------------------------------
#
# | Request                                           | Expected response | Store            |
# |----------------------------------------------------|--------------------|------------------|
# | artifact run_id differs from path                  | 409                | unchanged        |
# | artifact id absent from declared run artifacts      | 404                | unchanged        |
# | artifact value_digest differs                       | 409                | unchanged        |
# | artifact media type unsupported                     | 415                | unchanged        |
# | artifact canonical value exceeds 1,000,000 bytes     | 413                | unchanged        |
# | two refs where the second fails                      | failing status     | neither appended |
# | exact refs and canonical digests                     | 200 + created      | one event        |


async def test_an_artifact_naming_another_run_than_the_path_is_a_409_and_the_store_is_unchanged(
    registry: StepRegistry, tmp_path: Path
) -> None:
    _seed(tmp_path)

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _review(client, _body(artifact_refs=[_ref(run_id="some-other-run")]))

        assert answered.status_code == 409
        history = await client.get(f"/api/runs/history/{RUN}")
        assert history.json()["owner_reviews"] == []


async def test_an_artifact_the_workflow_never_declared_is_a_404_and_the_store_is_unchanged(
    registry: StepRegistry, tmp_path: Path
) -> None:
    _seed(tmp_path)

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _review(client, _body(artifact_refs=[_ref(id="invented")]))

        assert answered.status_code == 404
        history = await client.get(f"/api/runs/history/{RUN}")
        assert history.json()["owner_reviews"] == []


async def test_a_digest_that_disagrees_with_the_archived_value_is_a_409_and_the_store_is_unchanged(
    registry: StepRegistry, tmp_path: Path
) -> None:
    _seed(tmp_path)

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _review(client, _body(artifact_refs=[_ref(value_digest=artifact_digest({"net": 999}))]))

        assert answered.status_code == 409
        history = await client.get(f"/api/runs/history/{RUN}")
        assert history.json()["owner_reviews"] == []


async def test_a_text_media_type_over_a_non_string_value_is_a_415_and_the_store_is_unchanged(
    registry: StepRegistry, tmp_path: Path
) -> None:
    _seed(tmp_path, values={"draft_report": {"text": {"nested": "object"}, "net": 400}})
    ref = _ref(id="prose", label="The prose", value_digest=artifact_digest({"nested": "object"}), media_type="text/markdown")

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _review(client, _body(artifact_refs=[ref]))

        assert answered.status_code == 415
        history = await client.get(f"/api/runs/history/{RUN}")
        assert history.json()["owner_reviews"] == []


async def test_a_value_past_the_canonical_ceiling_is_a_413_and_the_store_is_unchanged(registry: StepRegistry, tmp_path: Path) -> None:
    wide = "x" * 1_000_001
    _seed(tmp_path, values={"draft_report": {"text": wide, "net": 400}})
    ref = _ref(id="prose", label="The prose", value_digest=artifact_digest(wide), media_type="text/markdown")

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _review(client, _body(artifact_refs=[ref]))

        assert answered.status_code == 413
        history = await client.get(f"/api/runs/history/{RUN}")
        assert history.json()["owner_reviews"] == []


async def test_when_the_second_of_two_refs_fails_neither_is_appended(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(tmp_path)
    good = _ref()
    bad = _ref(id="prose", label="The prose", media_type="text/markdown", value_digest=artifact_digest("wrong"))

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _review(client, _body(artifact_refs=[good, bad]))

        assert answered.status_code in (404, 409, 415, 413), "a refusal status, whichever the second ref earns"
        history = await client.get(f"/api/runs/history/{RUN}")
        assert history.json()["owner_reviews"] == [], "the first ref resolving cleanly does not partially commit the review"


async def test_exact_refs_and_canonical_digests_is_a_200_with_one_event(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(tmp_path)

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _review(client, _body())

        assert answered.status_code == 200
        body = answered.json()
        assert body["created"] is True
        assert body["review"]["artifact_refs"] == [_ref()]
        assert body["review"]["review_id"] == "r1"
        history = await client.get(f"/api/runs/history/{RUN}")
        (kept,) = history.json()["owner_reviews"]
        assert kept["review_id"] == "r1"


# --- a run nothing archived, and a request that is not one -----------------------------------


async def test_reviewing_a_run_nothing_archived_is_a_404(registry: StepRegistry, tmp_path: Path) -> None:
    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _review(client, _body(), run_id="no-such-run")

        assert answered.status_code == 404


async def test_a_blank_actor_or_reason_is_a_422_before_anything_is_read(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(tmp_path)

    async with _client_over(a_pool(registry), tmp_path) as client:
        blank_actor = await _review(client, _body(actor=""))
        blank_reason = await _review(client, _body(reason=""))

        assert blank_actor.status_code == 422
        assert blank_reason.status_code == 422


async def test_an_empty_artifact_list_is_a_422(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(tmp_path)

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _review(client, _body(artifact_refs=[]))

        assert answered.status_code == 422


async def test_a_url_in_the_body_is_refused_before_anything_is_read(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(tmp_path)

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await client.post(f"/api/runs/history/{RUN}/review", json={**_body(), "url": "https://example.test/review"})

        assert answered.status_code == 422


# --- append at most once per review id --------------------------------------------------------


async def test_an_identical_retry_is_a_200_with_created_false_and_the_same_review(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(tmp_path)

    async with _client_over(a_pool(registry), tmp_path) as client:
        first = await _review(client, _body())
        retry = await _review(client, _body())

        assert first.json()["created"] is True
        assert retry.status_code == 200
        assert retry.json()["created"] is False
        assert retry.json()["review"] == first.json()["review"]
        history = await client.get(f"/api/runs/history/{RUN}")
        assert len(history.json()["owner_reviews"]) == 1


async def test_a_changed_request_under_the_same_review_id_is_a_409_and_the_first_event_survives(
    registry: StepRegistry, tmp_path: Path
) -> None:
    _seed(tmp_path)

    async with _client_over(a_pool(registry), tmp_path) as client:
        first = await _review(client, _body())
        changed = await _review(client, _body(reason="A different reason entirely"))

        assert first.status_code == 200
        assert changed.status_code == 409
        history = await client.get(f"/api/runs/history/{RUN}")
        (kept,) = history.json()["owner_reviews"]
        assert kept["reason"] == first.json()["review"]["reason"]


async def test_the_same_review_id_under_a_different_actor_is_a_409_and_the_first_actor_stands(
    registry: StepRegistry, tmp_path: Path
) -> None:
    _seed(tmp_path)

    async with _client_over(a_pool(registry), tmp_path) as client:
        first = await _review(client, _body())
        renamed = await _review(client, _body(actor="somebody else entirely"))

        assert first.status_code == 200
        assert renamed.status_code == 409
        history = await client.get(f"/api/runs/history/{RUN}")
        (kept,) = history.json()["owner_reviews"]
        assert kept["actor"] == "owner"


async def test_every_minted_review_carries_the_source_of_the_route_that_recorded_it(registry: StepRegistry, tmp_path: Path) -> None:
    """Source is the route's own, so a review can never be read as something a run assigned itself."""
    _seed(tmp_path)

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _review(client, _body())

        assert answered.json()["review"]["source"] == "app:harness"


# --- more than one review of one run ----------------------------------------------------------


async def test_a_second_review_under_its_own_id_appends_beside_the_first_and_rewrites_nothing(
    registry: StepRegistry, tmp_path: Path
) -> None:
    """A later reading is another event. The one before it stays exactly as it was minted."""
    _seed(tmp_path)
    prose = _ref(id="prose", label="The prose", media_type="text/markdown", value_digest=artifact_digest(DRAFT["text"]))

    async with _client_over(a_pool(registry), tmp_path) as client:
        accepted = await _review(client, _body())
        rejected = await _review(
            client,
            _body(
                review_id="r2",
                outcome="rejected",
                reason="The prose drops the figure the draft carries.",
                artifact_refs=[prose],
            ),
        )

        assert (accepted.status_code, rejected.status_code) == (200, 200)
        assert (accepted.json()["created"], rejected.json()["created"]) == (True, True)
        history = await client.get(f"/api/runs/history/{RUN}")
        kept = history.json()["owner_reviews"]
        assert [review["review_id"] for review in kept] == ["r1", "r2"], "both events survive, in the order they were appended"
        assert [review["outcome"] for review in kept] == ["accepted", "rejected"]
        assert kept[0] == accepted.json()["review"]


async def test_a_review_keeps_the_version_it_read_when_the_run_s_own_value_moves_on(registry: StepRegistry, tmp_path: Path) -> None:
    """Two versions of one artifact: each review names the bytes it read, and a stale reference is refused.

    The reference for the older version stays exactly as it was committed, the value moving on
    never rewrites it, and a review of that older reference after the move is refused rather than
    recorded against whatever the run holds now.
    """
    _seed(tmp_path)
    later = {"text": "net: 450", "net": 450}
    older_ref = _ref()
    later_ref = _ref(value_digest=artifact_digest(later))

    async with _client_over(a_pool(registry), tmp_path) as client:
        accepted = await _review(client, _body(artifact_refs=[older_ref]))
        _seed(tmp_path, values={"draft_report": later})
        stale = await _review(client, _body(review_id="r2", reason="Still fine by me.", artifact_refs=[older_ref]))
        fresh = await _review(
            client,
            _body(review_id="r3", outcome="rejected", reason="The new figure is not the one I asked about.", artifact_refs=[later_ref]),
        )

        assert accepted.status_code == 200
        assert stale.status_code == 409, "the older reference no longer names what the run holds, so it is refused"
        assert fresh.status_code == 200
        history = await client.get(f"/api/runs/history/{RUN}")
        kept = history.json()["owner_reviews"]
        assert [review["review_id"] for review in kept] == ["r1", "r3"], "the refused one wrote nothing"
        assert kept[0]["artifact_refs"] == [older_ref], "the first review still names the version it actually read"
        assert kept[1]["artifact_refs"] == [later_ref]


async def test_two_concurrent_identical_requests_produce_exactly_one_creation(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(tmp_path)

    async with _client_over(a_pool(registry), tmp_path) as client:
        first, second = await asyncio.gather(_review(client, _body()), _review(client, _body()))

        answers = (first, second)
        assert all(answer.status_code == 200 for answer in answers)
        created = [answer for answer in answers if answer.json()["created"]]
        retried = [answer for answer in answers if not answer.json()["created"]]
        assert len(created) == 1
        assert len(retried) == 1
        assert created[0].json()["review"] == retried[0].json()["review"]
        history = await client.get(f"/api/runs/history/{RUN}")
        assert len(history.json()["owner_reviews"]) == 1


# --- the service, driven directly with harness's own fakes ------------------------------------


async def test_an_identical_retry_never_calls_the_resolver_again_even_once_its_artifacts_have_expired() -> None:
    store = FakeOwnerReviewStore()
    ref = ArtifactRef(
        id="draft", label="The draft", run_id=RUN, node_id="draft_report", turn=None, value_digest=artifact_digest(DRAFT),
        media_type="application/json",
    )
    resolver = FakeArtifactResolver(documents={"draft": ArtifactDocument(ref=ref, value=DRAFT)})
    service = OwnerReviewService(resolver, store)
    request = OwnerReviewRequest(
        review_id="r1", actor="owner", outcome="accepted", reason="Matches the criterion.", artifact_refs=(ref,)
    )
    t1 = datetime(2026, 9, 13, 10, tzinfo=UTC)

    first = await service.review(RUN, "app:harness", request, clock=lambda: t1)
    resolver.expired = frozenset({"draft"})
    retry = await service.review(RUN, "app:harness", request, clock=lambda: t1)

    assert first.created is True
    assert retry.created is False
    assert retry.review == first.review
    assert resolver.calls == [ref], "the identical retry never called the resolver a second time"
