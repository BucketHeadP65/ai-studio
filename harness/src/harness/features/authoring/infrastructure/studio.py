"""The studio knowledge base API behind `KnowledgeBaseRefresher`: queue a source, run a refresh, watch its job, name the digest workflow."""

from __future__ import annotations

import asyncio
import time

from typing import Literal

import httpx

from httpx import AsyncClient
from pydantic import BaseModel, ValidationError

from harness.features.authoring.exceptions import DigestWorkflowUnreadableError

_POLL_SECONDS = 2.0
"""How often a refresh job is read again while the refresher waits on it."""

_REFRESH_TIMEOUT_SECONDS = 600.0
"""How long the refresher waits on a refresh job before it answers that the job is still going."""


class _EnqueuedRefresh(BaseModel):
    """The one field `POST /api/knowledge-bases/{name}/refresh` answers with (202 and 409 alike): the job to watch."""

    job_id: int


class _DigestSources(BaseModel):
    """The one field of a schema's `sources` block this adapter reads."""

    digest_workflow: str = ""


class _DigestSchema(BaseModel):
    """The part of `GET /api/knowledge-bases/{name}/schema` this adapter reads: the workflow that digests new mirrors."""

    sources: _DigestSources = _DigestSources()


class _JobStatus(BaseModel):
    """The part of `GET /api/jobs/{id}` this adapter reads: where the job stands, and why it last failed."""

    state: Literal["queued", "running", "failed", "done", "abandoned"]
    last_error: str | None = None


def _error_detail(response: httpx.Response) -> str:
    """A JSON error body's `detail` field when studio sent one, else the raw response text."""
    try:
        payload = response.json()
    except ValueError:
        return response.text
    return str(payload["detail"]) if isinstance(payload, dict) and "detail" in payload else response.text


class StudioKnowledgeBaseRefresher:
    """The source queue and the refresh jobs of the studio knowledge base API, as `digest_now` needs them.

    Every failure, from an unreachable studio to a refresh job that failed, comes back as a refusal
    in words the model reads, never as an exception.
    """

    def __init__(
        self, client: AsyncClient, *, poll_seconds: float = _POLL_SECONDS, timeout_seconds: float = _REFRESH_TIMEOUT_SECONDS
    ) -> None:
        """Hold the client and how a refresh job is watched.

        Args:
            client: This deployment's client on the studio knowledge base API.
            poll_seconds: How often a refresh job is read again while it runs.
            timeout_seconds: How long a refresh job is waited on before the answer says it is still going.
        """
        self._client = client
        self._poll_seconds = poll_seconds
        self._timeout_seconds = timeout_seconds

    async def queue_source(self, knowledge_base: str, url: str) -> str | None:
        """`POST` `url` onto `knowledge_base`'s source queue.

        Args:
            knowledge_base: The knowledge base's registered name.
            url: The source to request.

        Returns:
            `None` once the source is queued, else a refusal in words the model reads.
        """
        try:
            response = await self._client.post(f"/api/knowledge-bases/{knowledge_base}/source-queue", json={"url": url})
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return f"could not queue {url!r} on {knowledge_base!r}'s source queue: {_error_detail(exc.response)}"
        except httpx.HTTPError as exc:
            return f"could not reach studio to queue {url!r} on {knowledge_base!r}'s source queue: {exc}"
        return None

    async def sync_and_index(self, knowledge_base: str) -> str | None:
        """Enqueue `knowledge_base`'s refresh, which syncs and then indexes it, and wait for the job to land.

        A 409 names the refresh job already underway, and that job is waited on the same way. A job
        that fails or is abandoned is reported at once rather than waited out. A job still going at
        the timeout is reported as still going.

        Args:
            knowledge_base: The knowledge base's registered name.

        Returns:
            `None` when the job lands done, else a refusal in words the model reads.
        """
        try:
            response = await self._client.post(f"/api/knowledge-bases/{knowledge_base}/refresh")
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != httpx.codes.CONFLICT:
                return f"sync and index failed for {knowledge_base!r}: {_error_detail(exc.response)}"
            response = exc.response
        except httpx.HTTPError as exc:
            return f"could not reach studio to sync and index {knowledge_base!r}: {exc}"
        try:
            job_id = _EnqueuedRefresh.model_validate(response.json()).job_id
        except (ValueError, ValidationError) as exc:
            return f"studio answered oddly syncing {knowledge_base!r}: {exc}"
        return await self._await_refresh(knowledge_base, job_id)

    async def digest_workflow(self, knowledge_base: str) -> str:
        """Read `knowledge_base`'s schema from studio and answer its `sources.digest_workflow`.

        Args:
            knowledge_base: The knowledge base's registered name.

        Returns:
            The workflow name, empty when the schema names none.

        Raises:
            DigestWorkflowUnreadableError: When studio is unreachable, answers an error, or answers a body that is not a schema.
        """
        try:
            response = await self._client.get(f"/api/knowledge-bases/{knowledge_base}/schema")
            response.raise_for_status()
            return _DigestSchema.model_validate(response.json()).sources.digest_workflow
        except httpx.HTTPStatusError as exc:
            msg = f"could not read {knowledge_base!r}'s schema to find its digest workflow: {_error_detail(exc.response)}"
            raise DigestWorkflowUnreadableError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"could not reach studio to read {knowledge_base!r}'s schema: {exc}"
            raise DigestWorkflowUnreadableError(msg) from exc
        except (ValueError, ValidationError) as exc:
            msg = f"studio answered oddly reading {knowledge_base!r}'s schema: {exc}"
            raise DigestWorkflowUnreadableError(msg) from exc

    async def _read_job(self, knowledge_base: str, job_id: int) -> _JobStatus | str:
        """One `GET /api/jobs/{id}`: the job's status, or the spoken refusal the read earned."""
        try:
            response = await self._client.get(f"/api/jobs/{job_id}")
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return f"could not read refresh job {job_id} for {knowledge_base!r}: {_error_detail(exc.response)}"
        except httpx.HTTPError as exc:
            return f"could not reach studio to read refresh job {job_id} for {knowledge_base!r}: {exc}"
        try:
            return _JobStatus.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            return f"studio answered oddly about refresh job {job_id} for {knowledge_base!r}: {exc}"

    async def _await_refresh(self, knowledge_base: str, job_id: int) -> str | None:
        """Watch one refresh job until it lands. `None` when it lands `done`, else a spoken refusal."""
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            job = await self._read_job(knowledge_base, job_id)
            if isinstance(job, str):
                return job
            if job.state == "done":
                return None
            if job.state == "abandoned":
                return (
                    f"sync and index for {knowledge_base!r} is abandoned after repeated failures: "
                    f"{job.last_error or 'no error recorded'}. Job {job_id} needs a person."
                )
            if job.state == "failed":
                return (
                    f"sync and index failed for {knowledge_base!r}: {job.last_error or 'no error recorded'} "
                    f"(job {job_id}. The worker retries it on its own backoff)."
                )
            if time.monotonic() >= deadline:
                return (
                    f"sync and index for {knowledge_base!r} is still {job.state} after {int(self._timeout_seconds)}s "
                    f"(job {job_id}). Follow it with check_runs before digesting."
                )
            await asyncio.sleep(self._poll_seconds)
