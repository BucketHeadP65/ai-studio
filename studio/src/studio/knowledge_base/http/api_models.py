"""The request and response bodies of the knowledge base HTTP API, and its one failure body.

Every failure on this API is `{error, detail}` at its status, never a traceback and never a shape a caller has to branch on.
"""

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Literal

from engram.identity import KnowledgeBaseIdentity
from engram.kb.contracts import KnowledgeBaseSchema
from engram.retrieval.contracts import DecidedPage, ValidationRow
from engram.sources.contracts import SourceQueueEntry, WatchEntry
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from studio.knowledge_base.refresh.contracts import RefreshRun
from studio.knowledge_base.settings import Schedule


def error_response(status: int, error: str, detail: str, *, extra: Mapping[str, JsonValue] | None = None) -> JSONResponse:
    """The uniform `{error, detail}` JSON body every failure on this API answers with.

    A failure that carries more than one sentence puts its other fields in `extra`, and they follow `error` and `detail` in the body.

    Args:
        status: The HTTP status.
        error: The machine-readable error code.
        detail: The sentence a person reads.
        extra: The other fields of the body, in their order. It must not hold `error` or `detail`.

    Returns:
        The response.
    """
    content: dict[str, JsonValue] = {"error": error, "detail": detail}
    if extra is not None:
        content.update(extra)
    return JSONResponse(status_code=status, content=content)


class KnowledgeBaseListEntry(BaseModel):
    """One name in `GET /api/knowledge-bases`, from the registry of knowledge bases, the indexed database, or both.

    `registered` and `indexed` are independent: a knowledge base can be registered and never indexed, indexed and not registered, or both.
    `broken` marks a registered knowledge base that fails validation, such as a moved folder or a malformed shape, and its `path` then comes from the unvalidated settings file.
    `private` is true when the registered knowledge base or its own schema declares it private.
    A host that reads several knowledge bases at once leaves a private one out.
    `collection` is the schema's `retrieval.collection`, and `None` when the schema cannot be read.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    registered: bool
    indexed: bool
    broken: bool = False
    path: Path | None = None
    description: str = ""
    pages: int | None = None
    page_types: tuple[str, ...] = ()
    last_indexed: datetime | None = None
    private: bool = False
    collection: str | None = None


class KnowledgeBaseListResult(BaseModel):
    """The `GET /api/knowledge-bases` response: every registered or indexed name, sorted by name."""

    model_config = ConfigDict(frozen=True)

    knowledge_bases: tuple[KnowledgeBaseListEntry, ...] = ()


class KnowledgeBaseCreateRequest(BaseModel):
    """The `POST /api/knowledge-bases` body: a name, a folder, and the knowledge base's schema or a template.

    Exactly one of `schema` and `template` is given.
    An invalid `schema` answers the uniform `{error, detail}` body at 422.
    """

    model_config = ConfigDict(use_attribute_docstrings=True, frozen=True, populate_by_name=True)

    name: str = Field(min_length=1)
    """The registered name. It must not be empty, and it must equal the schema's `retrieval.collection`."""
    path: Path
    """The folder the knowledge base is created in. It may hold no hidden folder, and it must be empty unless `force` is set."""
    knowledge_base_schema: KnowledgeBaseSchema | None = Field(default=None, alias="schema")
    """The knowledge base's schema, under the wire key `schema`. It is given in place of `template`."""
    template: str | None = None
    """A slug from `GET /api/templates`, given in place of `schema`.

    The template's schema becomes the knowledge base's schema, and its `_meta/*.md` guidance pages are copied into `path` before the scaffold.
    """
    description: str = ""
    force: bool = False
    """Whether the scaffold may write into a folder that is not empty."""


class TemplateOut(BaseModel):
    """One entry in `GET /api/templates`: a template's slug, title and folder."""

    model_config = ConfigDict(frozen=True)

    slug: str
    title: str
    path: Path


class TemplatesResult(BaseModel):
    """The `GET /api/templates` response: every template in the templates folder, or in `ENGRAM_TEMPLATES` when it is set."""

    model_config = ConfigDict(frozen=True)

    templates: tuple[TemplateOut, ...] = ()


class DecidedPageOut(BaseModel):
    """One decision page over the wire, without its path on the host."""

    model_config = ConfigDict(frozen=True)

    id: str
    page_type: str
    title: str
    summary: str
    status: str
    links: Mapping[str, tuple[str, ...]] = {}


def decided_page_out(row: DecidedPage) -> DecidedPageOut:
    """One decision page in its wire shape, without its path on the host.

    Args:
        row: The decision page as the knowledge base reads it.

    Returns:
        The page over the wire.
    """
    return DecidedPageOut(
        id=row.id, page_type=row.page_type, title=row.title, summary=row.summary, status=row.status, links=row.links
    )


class OutcomeOut(BaseModel):
    """One status a decision page type may move a page to, with the link field it asks for."""

    model_config = ConfigDict(frozen=True)

    status: str
    link: str | None = None


class DecisionsResult(BaseModel):
    """The `GET /api/knowledge-bases/{name}/decisions` response: every page waiting for a decision, and each decision page type's outcomes.

    `rows` are the pages at their page type's open status, newest file first.
    `skipped` names the files that failed to parse.
    `outcomes_by_page_type` maps each decision page type to the statuses a page of that type may move to, each with the link field it asks for.
    """

    model_config = ConfigDict(frozen=True)

    rows: tuple[DecidedPageOut, ...] = ()
    skipped: tuple[str, ...] = ()
    outcomes_by_page_type: Mapping[str, tuple[OutcomeOut, ...]] = {}


class PageDecisionOut(DecidedPageOut):
    """The `PATCH /api/knowledge-bases/{name}/pages/{page_id}` response: the page as it now reads, and the refresh that indexes it.

    The new status is on the page at once, and search shows it once the knowledge base's refresh has indexed it.
    `job_id` is that refresh (`GET /api/jobs/{id}`).
    `index` is `queued` when this call created the refresh, and `in_progress` when one was already queued or running.
    A refresh already past its file scan leaves the change to the next one.
    """

    job_id: int
    index: Literal["queued", "in_progress"]


class KnowledgeBaseDetail(KnowledgeBaseListEntry):
    """The `GET /api/knowledge-bases/{name}` response: every listing field, and everything else AI Factory Studio shows about one knowledge base.

    `sources_connector` is kept for old callers, and it must agree with the first of `sources_connectors`.
    `sources_connector` is `None` and `sources_connectors` is empty when the schema declares no sources or cannot be read.
    `validation` holds the index's standing issues, and it is empty when the knowledge base was never indexed.
    `source_queue` is filled only when the source-queue connector is declared.
    `last_refresh_run` is `None` before the first refresh, and `schedule` is `None` when no schedule is set.
    `identity` is `None` unless the knowledge base is registered and its schema can be read, from its folder or else from the index.
    The open decisions are their own route, `GET /api/knowledge-bases/{name}/decisions`.
    """

    sources_connector: str | None = None
    sources_connectors: tuple[str, ...] = ()
    watch_list: tuple[WatchEntry, ...] = ()
    validation: tuple[ValidationRow, ...] = ()
    source_queue: tuple[SourceQueueEntry, ...] = ()
    last_refresh_run: RefreshRun | None = None
    schedule: Schedule | None = None
    identity: KnowledgeBaseIdentity | None = None


class SourceQueueAddRequest(BaseModel):
    """The `POST /api/knowledge-bases/{name}/source-queue` body: one URL to put on the knowledge base's source queue."""

    model_config = ConfigDict(use_attribute_docstrings=True, frozen=True)

    url: str = Field(min_length=1)
    """The URL of the source. It must not be empty."""


class WatchEntryAddRequest(BaseModel):
    """The `POST /api/knowledge-bases/{name}/watch-list` body: an entry to add to the knowledge base's watch list."""

    model_config = ConfigDict(use_attribute_docstrings=True, frozen=True)

    scanner: str = Field(min_length=1)
    """The scanner that reads the entry: `xml` for an Atom or RSS document at `ref`, or one an installed package registers.

    It must not be empty, and the route refuses a scanner this machine does not have.
    """
    ref: str = Field(min_length=1)
    """What the scanner reads, such as a document URL. It must not be empty."""
    max_items: int = 15
    """The most items one scan pulls for the entry, used only when this call adds it."""


class PageDecisionRequest(BaseModel):
    """The `PATCH /api/knowledge-bases/{name}/pages/{page_id}` body: the page's new status, its link fields, and a reason."""

    model_config = ConfigDict(use_attribute_docstrings=True, frozen=True)

    status: str
    """The status to move the page to. It must be one its page type allows."""
    links: Mapping[str, tuple[str, ...]] = {}
    """Each link-list field to replace, mapped to the page ids it should now hold, bare or as `[[id]]`.

    An outcome's declared `link` names the field to fill for that status. A field left out keeps its value.
    """
    reason: str = ""
    """Why the page was decided, written onto the page. Empty leaves any earlier reason on the page."""


class RefreshRunsResult(BaseModel):
    """The `GET /api/knowledge-bases/{name}/refresh-runs` response: the knowledge base's refresh runs, newest first."""

    model_config = ConfigDict(frozen=True)

    refresh_runs: tuple[RefreshRun, ...] = ()


class ScheduleReport(BaseModel):
    """The `PUT` and `DELETE /api/knowledge-bases/{name}/schedule` response: the registered knowledge base's schedule as it now stands."""

    model_config = ConfigDict(frozen=True)

    name: str
    schedule: Schedule | None


class RegisterKnowledgeBaseRequest(BaseModel):
    """The `PUT /api/knowledge-bases/{name}/registration` body: an existing knowledge base folder to register.

    Registering writes only the registry of knowledge bases. It never creates a file in the folder.
    """

    model_config = ConfigDict(use_attribute_docstrings=True, frozen=True)

    path: Path
    """The knowledge base's folder. It may hold no hidden folder, and it must hold a `knowledge-base-schema.json` or the older `Units/` and `_meta/` pair."""
    description: str = ""


class RegistrationReport(BaseModel):
    """The registration routes' answer: the registered knowledge base as it now stands, or as it stood before its removal."""

    model_config = ConfigDict(frozen=True)

    name: str
    path: Path
    description: str = ""
    registered: bool
