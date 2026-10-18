"""The studio knowledge base API with scripted answers, for tests that must not reach a running studio."""

from __future__ import annotations

import json

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self
from urllib.parse import unquote

import httpx

if TYPE_CHECKING:
    from pathlib import Path

    from engram.kb.contracts import KnowledgeBaseSchema
    from pydantic import JsonValue

LISTING_PATH = "/api/knowledge-bases"
"""The registry listing the fake answers. Every other path is read as a spec route."""


def registry_row(
    name: str, path: Path | None, *, private: bool = False, broken: bool = False, collection: str | None = None, registered: bool = True
) -> dict[str, JsonValue]:
    """One listing row the way the studio serves it, with the flags a test needs set.

    `collection` defaults to `name`, the agreeing case. `path` is `None` for a collection indexed
    in the database that no registry row names.
    """
    return {
        "name": name,
        "registered": registered,
        "indexed": False,
        "broken": broken,
        "path": None if path is None else str(path),
        "description": "",
        "pages": None,
        "kinds": [],
        "last_indexed": None,
        "private": private,
        "collection": collection if collection is not None else name,
    }


@dataclass
class FakeStudioApi:
    """Structurally a `StudioApi`, answering a scripted registry listing and one schema per name.

    `rows` are the listing's rows (`registry_row` builds one) and `specs` the schemas the spec
    route serves, keyed by registry name: a name absent from `specs` answers 404, the way a knowledge base
    carrying no schema file does. `error`, when set, is raised by every request, which is how a
    test stands in for a studio that is not running, and `listing_status` turns the listing itself
    into a failure status. `requested` records every path asked for, in order.

    Doubles as its own async context manager, so a test also hands it in where a caller opens
    `studio_http_client()` itself.

    `connection_failures` scripts a studio that is not answering *yet*: that many upcoming calls
    raise `httpx.ConnectError` before this fake falls through to its ordinary answers, one failure
    drained per call. `error`, unlike it, never drains: every call keeps raising it, standing in
    for a studio that is not running at all.
    """

    rows: list[dict[str, JsonValue]] = field(default_factory=list)
    specs: dict[str, KnowledgeBaseSchema] = field(default_factory=dict)
    error: httpx.RequestError | None = None
    listing_status: int = 200
    connection_failures: int = 0
    requested: list[str] = field(default_factory=list)
    base_url: httpx.URL = field(default_factory=lambda: httpx.URL("http://studio.test:8100"))

    async def get(self, url: str) -> httpx.Response:
        """The scripted answer for `url`: the listing, one knowledge base's schema, or the scripted failure."""
        self.requested.append(url)
        if self.connection_failures > 0:
            self.connection_failures -= 1
            msg = "[Errno 61] Connection refused"
            raise httpx.ConnectError(msg)
        if self.error is not None:
            raise self.error
        request = httpx.Request("GET", self.base_url.join(url))
        if url == LISTING_PATH:
            return httpx.Response(self.listing_status, json={"knowledge_bases": self.rows}, request=request)
        name = unquote(url.removeprefix(f"{LISTING_PATH}/").removesuffix("/schema"))
        spec = self.specs.get(name)
        if spec is None:
            return httpx.Response(404, json={"error": "spec_not_found", "detail": f"no spec for {name!r}"}, request=request)
        return httpx.Response(200, json=json.loads(spec.model_dump_json()), request=request)

    async def __aenter__(self) -> Self:
        """This fake also stands in for the client `studio_http_client()` opens."""
        return self

    async def __aexit__(self, *details: object) -> None:
        """Nothing to release. This fake never opens a connection."""
        del details


def unreachable_studio() -> FakeStudioApi:
    """A studio that is not running: every request raises the transport error httpx raises."""
    return FakeStudioApi(error=httpx.ConnectError("[Errno 61] Connection refused"))
