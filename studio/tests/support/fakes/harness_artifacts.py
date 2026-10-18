"""A stand-in harness answering for the values one run fixed, over a transport and without a network.

Answers by the same rules the real resolver route does: a reference naming another run than the
path is a 409, a run nothing archived and an artifact this run never fixed are both 404, and bytes
that disagree with the digest are a 409. Every question it is asked is recorded, so a test can
prove a refusal asked once, or never asked at all.
"""

from __future__ import annotations

import json

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

from core.ai.artifacts import ArtifactDocument, ArtifactRef, artifact_digest
from pydantic import ValidationError

if TYPE_CHECKING:
    from pydantic import JsonValue

RESOLVE_SUFFIX = "/artifacts/resolve"
"""The tail of the one path this answers on. Anything else is a 404, as it would be on the real host."""


@dataclass
class FakeHarness:
    """The values one run fixed, and how this harness behaves when asked about them.

    `values` maps an artifact id to the value that run fixed for it. `status`, `message` and
    `raw_body` script a harness that refuses or answers with something unexpected: `raw_body` is
    returned verbatim, which is how a test drives an answer that is not a document at all or is
    about a reference nobody asked about.
    """

    values: dict[str, JsonValue] = field(default_factory=dict)
    run_id: str = "run-1"
    status: int = 200
    message: str = ""
    raw_body: bytes | None = None
    asked: list[ArtifactRef] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        """A transport that answers this harness's own resolver route and nothing else."""
        return httpx.MockTransport(self.answer)

    def ref(self, artifact_id: str, *, label: str = "Proposed knowledge note", node_id: str = "present") -> ArtifactRef:
        """The reference this harness would mint for one of its own values."""
        return ArtifactRef(
            id=artifact_id,
            label=label,
            run_id=self.run_id,
            node_id=node_id,
            value_digest=artifact_digest(self.values[artifact_id]),
            media_type="application/json",
        )

    def answer(self, request: httpx.Request) -> httpx.Response:
        """What this harness says to one request, recording the reference it was asked about."""
        path = request.url.path
        if not path.startswith("/api/runs/history/") or not path.endswith(RESOLVE_SUFFIX):
            return _refused(404, "no such route")
        run_id = path.removeprefix("/api/runs/history/").removesuffix(RESOLVE_SUFFIX)
        try:
            ref = ArtifactRef.model_validate(json.loads(request.content)["ref"])
        except (ValidationError, KeyError, ValueError):
            return _refused(422, "that is not a reference")
        self.asked.append(ref)
        if ref.run_id != run_id:
            return _refused(409, "this reference belongs to another run than the one asked about")
        if run_id != self.run_id:
            return _refused(404, f"nothing archived a run {run_id!r}")
        if self.raw_body is not None:
            return httpx.Response(self.status, content=self.raw_body, headers={"content-type": "application/json"})
        if self.status != httpx.codes.OK:
            return _refused(self.status, self.message or "refused")
        if ref.id not in self.values:
            return _refused(404, f"this run fixed no artifact {ref.id!r}")
        value = self.values[ref.id]
        if artifact_digest(value) != ref.value_digest:
            return _refused(409, "the archived value no longer matches the digest this reference addresses")
        return httpx.Response(200, json=ArtifactDocument(ref=ref, value=value).model_dump(mode="json"))


def _refused(status: int, message: str) -> httpx.Response:
    """The shape this host says no in, the same one the real route uses."""
    return httpx.Response(status, json={"ok": False, "message": message})
