"""Resolving one value a harness run fixed, over the one harness endpoint this app was configured with.

The address comes from this app's own settings and never from a request.
The answer is either the exact document the reference names or an `ArtifactUnavailableError`.
"""

from __future__ import annotations

import httpx

from core.ai.artifacts import MAX_CANONICAL_BYTES, ArtifactDocument, ArtifactRef, ArtifactUnavailableError
from core.observability import get_logger
from pydantic import BaseModel, ConfigDict, ValidationError

_logger = get_logger(__name__)

_RESOLVE_PATH = "/api/runs/history/{run_id}/artifacts/resolve"
"""The one route a run's values are resolved at."""

MAX_ANSWER_BYTES = MAX_CANONICAL_BYTES + 8192
"""The most one answer may weigh on the wire: the canonical ceiling plus room for the reference.

An answer is refused the moment it passes this, before it is held whole.
"""


class _RefusalBody(BaseModel):
    """The harness's refusal body: its own sentence about why."""

    model_config = ConfigDict(extra="ignore")

    message: str = ""


class HarnessArtifactResolver:
    """`ArtifactResolver` over the harness's read-only resolve route at one configured base URL.

    Every answer is validated as an `ArtifactDocument`, which checks the digest, the media type and the size.
    The reference the answer carries must be the one that was asked about.
    """

    def __init__(self, base_url: str, *, timeout_seconds: float = 10.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """Hold where to ask, how long to wait, and the transport to ask over.

        Args:
            base_url: The harness this deployment reads runs from. Configuration, never a request.
            timeout_seconds: How long one request may take before it is refused.
            transport: The transport that carries the request. `None` lets httpx open its own.
        """
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def resolve(self, ref: ArtifactRef) -> ArtifactDocument:
        """Ask the harness for the document `ref` addresses, out of the run `ref` names.

        Args:
            ref: The reference to resolve.

        Returns:
            The document, whose reference is `ref`.

        Raises:
            ArtifactUnavailableError: The harness refused, could not be reached, answered something
                that is not a document, or answered about a different reference.
        """
        url = f"{self._base_url}{_RESOLVE_PATH.format(run_id=ref.run_id)}"
        body = await self._answer(url, ref)
        try:
            document = ArtifactDocument.model_validate_json(body)
        except ValidationError as error:
            msg = f"the harness answered for artifact {ref.id!r} with something that is not the value this reference addresses: {error}"
            raise ArtifactUnavailableError(msg) from error
        if document.ref != ref:
            msg = f"the harness answered about a different reference than artifact {ref.id!r} of run {ref.run_id!r}"
            raise ArtifactUnavailableError(msg)
        return document

    async def _answer(self, url: str, ref: ArtifactRef) -> bytes:
        """The bytes of one answer, refused on a bad status, an unreachable harness, or an oversized body."""
        payload = {"ref": ref.model_dump(mode="json")}
        try:
            async with (
                httpx.AsyncClient(timeout=self._timeout_seconds, transport=self._transport) as client,
                client.stream("POST", url, json=payload) as response,
            ):
                body = await _bounded_body(response)
                if response.status_code != httpx.codes.OK:
                    raise ArtifactUnavailableError(_refusal(ref, response.status_code, body))
                return body
        except httpx.HTTPError as error:
            _logger.warning("artifact_resolve_unreachable: artifact {artifact!r} of run {run_id!r}: {reason}", artifact=ref.id, run_id=ref.run_id, reason=str(error))
            msg = f"the harness could not be reached to resolve artifact {ref.id!r} of run {ref.run_id!r}: {error}"
            raise ArtifactUnavailableError(msg) from error


async def _bounded_body(response: httpx.Response) -> bytes:
    """Everything `response` carries, raising `ArtifactUnavailableError` the moment it passes `MAX_ANSWER_BYTES`."""
    chunks: list[bytes] = []
    weight = 0
    async for chunk in response.aiter_bytes():
        weight += len(chunk)
        if weight > MAX_ANSWER_BYTES:
            msg = f"the harness answered with more than the {MAX_ANSWER_BYTES} bytes one value may weigh"
            raise ArtifactUnavailableError(msg)
        chunks.append(chunk)
    return b"".join(chunks)


def _refusal(ref: ArtifactRef, status: int, body: bytes) -> str:
    """The harness's own words about why it would not answer, or the status when it said nothing readable."""
    said = _message(body)
    where = f"artifact {ref.id!r} of run {ref.run_id!r}"
    return f"the harness refused {where}: {said}" if said else f"the harness refused {where} with status {status}"


def _message(body: bytes) -> str:
    """The `message` a refusal body carries, empty when the body is not one this app can read."""
    try:
        return _RefusalBody.model_validate_json(body).message
    except ValidationError:
        return ""
