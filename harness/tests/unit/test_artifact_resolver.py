"""The one read-only route a reference resolves through, and everything it refuses.

The route takes a reference and answers with the value that reference addresses, out of the archived
run's own immutable record. It never takes a URL, never resolves against live state, and never
answers with bytes whose digest disagrees with the reference asking for them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from core.ai.artifacts import ArtifactRef, artifact_digest
from core.ai.workflows.contracts import (
    FromNode,
    FromParameter,
    MapNode,
    ParameterSpec,
    PersonGate,
    ResultArtifactSpec,
    RunNode,
    Workflow,
    WorkflowResultSpec,
    canonical_reviewed_value,
    reviewed_value_digest,
)

from harness.features.running.domain.ended_run import ReviewedValue, RunArchiveRecord
from harness.features.running.infrastructure.archive import FileRunArchive
from tests.support.controllers import a_client_of
from tests.support.running import a_pool, a_running_controller
from tests.support.state import KEPT_DAYS

if TYPE_CHECKING:
    from pathlib import Path

    import httpx

    from core.ai.workflows.contracts import ReviewedArtifactRef, StepRegistry

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
                ResultArtifactSpec(
                    id="quarter", label="The quarter asked for", source=FromParameter(parameter="quarter"), media_type="text/plain"
                ),
            ),
        ),
    )


def _archive_of(state: Path) -> FileRunArchive:
    return FileRunArchive(state / "runs" / "archive", retention_days=KEPT_DAYS)


def _seed(state: Path, *, run_id: str = RUN, values: dict[str, object] | None = None, reviewed: tuple[ReviewedValue, ...] = ()) -> None:
    now = datetime.now(UTC).isoformat()
    _archive_of(state).put(
        RunArchiveRecord(
            run_id=run_id,
            workflow_id="wf.report",
            outcome="completed",
            started_at=now,
            ended_at=now,
            runner="app:harness",
            session_id="sess-1",
            params={"quarter": "Q2"},
            spec=_spec().model_dump(mode="json"),
            values=values if values is not None else {"draft_report": DRAFT},
            events=[],
            reviewed=reviewed,
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


async def _resolve(client: httpx.AsyncClient, ref: dict[str, object], *, run_id: str = RUN) -> httpx.Response:
    return await client.post(f"/api/runs/history/{run_id}/artifacts/resolve", json={"ref": ref})


async def test_a_declared_artifact_resolves_to_the_exact_value_the_run_archived(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(tmp_path)

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _resolve(client, _ref())

        assert answered.status_code == 200
        body = answered.json()
        assert body["value"] == DRAFT
        assert body["ref"] == _ref(), "the answer carries back the very reference that was asked for"


async def test_a_declared_field_selects_one_top_level_property_and_nothing_deeper(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(tmp_path)
    ref = _ref(id="prose", label="The prose", value_digest=artifact_digest(DRAFT["text"]), media_type="text/markdown")

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _resolve(client, ref)

        assert answered.status_code == 200
        assert answered.json()["value"] == "net: 400"


async def test_an_artifact_wired_from_a_parameter_resolves_against_the_recorded_arguments(
    registry: StepRegistry, tmp_path: Path
) -> None:
    _seed(tmp_path)
    ref = _ref(id="quarter", label="The quarter asked for", node_id="parameter:quarter", value_digest=artifact_digest("Q2"),
               media_type="text/plain")

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _resolve(client, ref)

        assert answered.status_code == 200
        assert answered.json()["value"] == "Q2"


async def test_a_reference_naming_another_run_than_the_path_is_refused(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(tmp_path)

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _resolve(client, _ref(run_id="some-other-run"))

        assert answered.status_code == 409
        assert "run" in answered.json()["message"]


async def test_a_run_nothing_archived_is_a_404(registry: StepRegistry, tmp_path: Path) -> None:
    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _resolve(client, _ref(run_id="no-such-run"), run_id="no-such-run")

        assert answered.status_code == 404


async def test_an_artifact_the_workflow_never_declared_is_a_404(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(tmp_path)

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _resolve(client, _ref(id="invented"))

        assert answered.status_code == 404
        assert "declare" in answered.json()["message"]


@pytest.mark.parametrize(("field", "value"), [("node_id", "reconcile"), ("turn", "polish")])
async def test_a_reference_addressing_a_node_or_turn_the_declaration_does_not_name_is_refused(
    field: str, value: str, registry: StepRegistry, tmp_path: Path
) -> None:
    _seed(tmp_path)

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _resolve(client, _ref(**{field: value}))

        assert answered.status_code == 409


async def test_a_declared_field_the_archived_value_does_not_carry_is_a_404(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(tmp_path, values={"draft_report": {"net": 400}})
    ref = _ref(id="prose", label="The prose", value_digest=artifact_digest("net: 400"), media_type="text/markdown")

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _resolve(client, ref)

        assert answered.status_code == 404


async def test_a_node_the_run_never_recorded_a_value_for_is_a_404(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(tmp_path, values={})

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _resolve(client, _ref())

        assert answered.status_code == 404


async def test_a_digest_that_no_longer_matches_the_archived_value_is_refused(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(tmp_path, values={"draft_report": {"text": "net: 500", "net": 500}})

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _resolve(client, _ref())

        assert answered.status_code == 409
        assert "digest" in answered.json()["message"]


async def test_text_media_over_a_value_that_is_not_a_string_is_refused(registry: StepRegistry, tmp_path: Path) -> None:
    """The declaration says markdown; the archived value under it is an object, so nothing can render it."""
    _seed(tmp_path, values={"draft_report": {"text": {"nested": "object"}, "net": 400}})
    ref = _ref(id="prose", label="The prose", value_digest=artifact_digest({"nested": "object"}), media_type="text/markdown")

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _resolve(client, ref)

        assert answered.status_code == 415


async def test_a_value_past_the_canonical_ceiling_is_refused(registry: StepRegistry, tmp_path: Path) -> None:
    wide = "x" * 1_000_001
    _seed(tmp_path, values={"draft_report": {"text": wide, "net": 400}})
    ref = _ref(id="prose", label="The prose", value_digest=artifact_digest(wide), media_type="text/markdown")

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _resolve(client, ref)

        assert answered.status_code == 413


def _captured(node_id: str, value: object) -> tuple[ReviewedValue, ReviewedArtifactRef]:
    """One value a gate reviewed, as the run captured it beside its log."""
    from core.ai.workflows.contracts import ReviewedArtifactRef as Ref

    canonical = canonical_reviewed_value(value)
    ref = Ref(run_id=RUN, node_id=node_id, turn=None, value_digest=reviewed_value_digest(canonical))
    return ReviewedValue(ref=ref, canonical=canonical), ref


async def test_a_value_a_gate_reviewed_resolves_from_the_copy_the_ending_kept(registry: StepRegistry, tmp_path: Path) -> None:
    """The run log is gone, and the bytes the gate saw are still readable from the archived record."""
    refused = {"text": "net: 300", "net": 300}
    captured, address = _captured("draft_report", refused)
    _seed(tmp_path, reviewed=(captured,))
    ref = _ref(id="what-was-refused", label="What was refused", value_digest=address.value_digest)

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _resolve(client, ref)

        assert answered.status_code == 200
        assert answered.json()["value"] == refused, "the reviewed bytes, not whatever the node produced afterwards"


async def test_a_reviewed_address_asked_for_at_the_wrong_digest_is_refused(registry: StepRegistry, tmp_path: Path) -> None:
    captured, _ = _captured("reconcile", {"net": 300})
    _seed(tmp_path, reviewed=(captured,))
    ref = _ref(id="what-was-refused", label="What was refused", node_id="reconcile", value_digest=artifact_digest({"net": 999}))

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _resolve(client, ref)

        assert answered.status_code == 409


async def test_the_reference_is_carried_under_one_key_and_a_bare_reference_is_refused(registry: StepRegistry, tmp_path: Path) -> None:
    """The exact body another application sends, written out rather than built by the helper the other tests share."""
    _seed(tmp_path)

    async with _client_over(a_pool(registry), tmp_path) as client:
        enveloped = await client.post(f"/api/runs/history/{RUN}/artifacts/resolve", json={"ref": _ref()})
        bare = await client.post(f"/api/runs/history/{RUN}/artifacts/resolve", json=_ref())

        assert enveloped.status_code == 200
        assert bare.status_code == 422, "the reference travels under `ref`, never spread across the body"


async def test_the_route_answers_a_reference_and_never_a_url(registry: StepRegistry, tmp_path: Path) -> None:
    _seed(tmp_path)

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _resolve(client, _ref())
        refused = await client.post(f"/api/runs/history/{RUN}/artifacts/resolve", json={"url": "https://example.test/draft"})

        assert set(answered.json()) == {"ref", "value"}, "a document is its reference and its value, and carries no link"
        assert refused.status_code == 422, "a body that is not a reference is not a request this route understands"


async def test_a_reference_that_is_not_a_reference_is_refused_before_anything_is_read(
    registry: StepRegistry, tmp_path: Path
) -> None:
    _seed(tmp_path)

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await client.post(
            f"/api/runs/history/{RUN}/artifacts/resolve", json={"ref": {**_ref(), "value_digest": "not-a-digest"}}
        )

        assert answered.status_code == 422


# --- one turn of a map, addressed exactly ------------------------------------------------------


_POLISHED = {"text": "net: 400, polished", "net": 400}


def _turned_spec() -> Workflow:
    """A map taking two turns per item, declaring the middle turn's own output as an artifact."""
    return Workflow(
        name="quarterly-reports",
        version=1,
        description="Draft and polish one report per quarter, declaring what a caller may select.",
        parameters={"quarters": ParameterSpec(description="Fiscal quarters", json_schema={"type": "array", "items": {"type": "string"}})},
        nodes=(
            MapNode(id="reports", turns=("draft_report", "polish"), over=FromParameter(parameter="quarters"), each="quarter"),
        ),
        output=FromNode(node="reports"),
        result=WorkflowResultSpec(
            summary="the polished reports",
            primary_work="reports",
            artifacts=(
                ResultArtifactSpec(
                    id="polished",
                    label="The polished draft",
                    source=FromNode(node="reports", turn="polish"),
                    media_type="application/json",
                ),
            ),
        ),
    )


async def test_a_reference_naming_one_turn_resolves_to_that_turns_own_recorded_value(registry: StepRegistry, tmp_path: Path) -> None:
    now = datetime.now(UTC).isoformat()
    _archive_of(tmp_path).put(
        RunArchiveRecord(
            run_id=RUN,
            workflow_id="wf.reports",
            outcome="completed",
            started_at=now,
            ended_at=now,
            runner="app:harness",
            session_id="sess-1",
            params={"quarters": ["Q2"]},
            spec=_turned_spec().model_dump(mode="json"),
            values={"reports": [DRAFT], "reports.polish": _POLISHED},
            events=[],
        )
    )
    ref = ArtifactRef(
        id="polished",
        label="The polished draft",
        run_id=RUN,
        node_id="reports",
        turn="polish",
        value_digest=artifact_digest(_POLISHED),
        media_type="application/json",
    ).model_dump(mode="json")

    async with _client_over(a_pool(registry), tmp_path) as client:
        answered = await _resolve(client, ref)

        assert answered.status_code == 200
        body = answered.json()
        assert body["value"] == _POLISHED, "the turn's own value, not the map's collected list"
        assert body["ref"]["turn"] == "polish"
