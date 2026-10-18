"""`HarnessArtifactResolver`: what it asks, where it asks it, and what it will not accept back."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import httpx
import pytest

from core.ai.artifacts import ArtifactUnavailableError, artifact_digest

from studio.knowledge_base.retention.infrastructure.artifact_resolver import MAX_ANSWER_BYTES, HarnessArtifactResolver
from tests.support.fakes.harness_artifacts import FakeHarness

if TYPE_CHECKING:
    from pydantic import JsonValue

VALUE: dict[str, JsonValue] = {"page_type": "concept", "title": "What the run concluded"}
ARTIFACT = "retention_draft"


def _harness() -> FakeHarness:
    return FakeHarness(values={ARTIFACT: dict(VALUE)})


def _reader(harness: FakeHarness, base_url: str = "http://harness.invalid") -> HarnessArtifactResolver:
    return HarnessArtifactResolver(base_url, transport=harness.transport())


async def test_the_value_comes_back_whole_for_the_reference_that_was_asked_about() -> None:
    harness = _harness()
    ref = harness.ref(ARTIFACT)
    document = await _reader(harness).resolve(ref)
    assert document.ref == ref
    assert document.value == VALUE


async def test_the_question_goes_to_the_reference_own_run_at_the_configured_host() -> None:
    harness = _harness()
    asked: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return harness.answer(request)

    reader = HarnessArtifactResolver("http://harness.invalid/", transport=httpx.MockTransport(record))
    await reader.resolve(harness.ref(ARTIFACT))
    assert [str(request.url) for request in asked] == [f"http://harness.invalid/api/runs/history/{harness.run_id}/artifacts/resolve"]


async def test_the_body_is_the_reference_under_one_key_and_carries_nothing_else() -> None:
    """The exact bytes this app sends the other one, written out rather than derived from the model it sends."""
    harness = _harness()
    asked: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return harness.answer(request)

    reader = HarnessArtifactResolver("http://harness.invalid", transport=httpx.MockTransport(record))
    await reader.resolve(harness.ref(ARTIFACT))

    assert json.loads(asked[0].content) == {
        "ref": {
            "id": ARTIFACT,
            "label": "Proposed knowledge note",
            "run_id": harness.run_id,
            "node_id": "present",
            "turn": None,
            "value_digest": artifact_digest(VALUE),
            "media_type": "application/json",
        }
    }


async def test_a_refusal_comes_back_in_the_harness_own_words() -> None:
    harness = _harness()
    harness.status = 409
    harness.message = "the archived value no longer matches the digest this reference addresses"
    with pytest.raises(ArtifactUnavailableError, match="no longer matches the digest"):
        await _reader(harness).resolve(harness.ref(ARTIFACT))


async def test_an_answer_about_a_different_reference_is_not_an_answer() -> None:
    harness = _harness()
    ref = harness.ref(ARTIFACT)
    other = ref.model_copy(update={"label": "Something else"})
    harness.raw_body = json.dumps({"ref": other.model_dump(mode="json"), "value": VALUE}).encode()
    with pytest.raises(ArtifactUnavailableError, match="a different reference"):
        await _reader(harness).resolve(ref)


async def test_bytes_that_do_not_hash_to_the_reference_are_refused() -> None:
    harness = _harness()
    ref = harness.ref(ARTIFACT)
    harness.raw_body = json.dumps({"ref": ref.model_dump(mode="json"), "value": {"different": "value"}}).encode()
    with pytest.raises(ArtifactUnavailableError, match="not the value this reference addresses"):
        await _reader(harness).resolve(ref)


async def test_an_answer_past_the_ceiling_is_refused_while_it_arrives() -> None:
    harness = _harness()
    harness.raw_body = b"[" + b"0," * ((MAX_ANSWER_BYTES // 2) + 1) + b"0]"
    with pytest.raises(ArtifactUnavailableError, match="bytes one value may weigh"):
        await _reader(harness).resolve(harness.ref(ARTIFACT))


async def test_a_harness_that_cannot_be_reached_is_a_refusal_rather_than_a_crash() -> None:
    harness = _harness()

    refused = "connection refused"

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(refused, request=request)

    reader = HarnessArtifactResolver("http://harness.invalid", transport=httpx.MockTransport(unreachable))
    with pytest.raises(ArtifactUnavailableError, match="could not be reached"):
        await reader.resolve(harness.ref(ARTIFACT))


async def test_a_value_carrying_a_nonfinite_number_addresses_nothing() -> None:
    harness = _harness()
    ref = harness.ref(ARTIFACT)
    harness.raw_body = json.dumps({"ref": ref.model_dump(mode="json"), "value": float("inf")}).encode()
    with pytest.raises(ArtifactUnavailableError):
        await _reader(harness).resolve(ref)


async def test_the_digest_a_reference_carries_is_the_one_the_value_hashes_to() -> None:
    harness = _harness()
    assert harness.ref(ARTIFACT).value_digest == artifact_digest(VALUE)
