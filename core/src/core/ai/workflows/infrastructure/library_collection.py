"""A workflow library kept as pages of one knowledge base, each workflow saved as a document in its page's frontmatter.

`put` writes the workflow into `kb.pages.frontmatter` (jsonb) with the applicability as a section, and
`fetch_workflow` reads the same document back and validates it. Reads go through the read-only `kb_reader` role
and writes through the ingest callable the library is given, so this module never touches the `kb.*` tables and
holds no writer credential. Each version is its own page, `wf.<name>.v<n>`. Saving a version links every live
lower version of the name forward to it, and `search`, `catalog` and `current` read only pages nothing has superseded.
"""

from __future__ import annotations

import json
import re
import warnings

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic_ai.toolsets import FunctionToolset

from core.ai.workflows.contract_port import LibraryVocabulary
from core.ai.workflows.domain.fingerprint import WorkflowFingerprint
from core.ai.workflows.domain.intent import WorkflowIntent
from core.ai.workflows.domain.lineage import Lineage
from core.ai.workflows.domain.presentation import WorkflowPresentation, verify_presentation
from core.ai.workflows.domain.vocabulary import InlineStep, Workflow
from core.ai.workflows.exceptions import LibraryError, WorkflowUnknownError
from core.ai.workflows.read_models import SavedWorkflow

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import JsonValue

    from core.ai.embedding import QueryEmbedder
    from core.ai.workflows.internal_contracts import Ingest, Rows

PAGE_TYPE = "workflow"
ID_PREFIX = "wf."

_VERSIONED_ID = re.compile(rf"^{re.escape(ID_PREFIX)}(?P<name>[a-z0-9]+(?:-[a-z0-9]+)*)\.v(?P<version>[1-9][0-9]*)$")
"""Matches an id `page_id` mints and captures its `name` and `version`."""

STEP_PAGE_TYPE = "step"
STEP_ID_PREFIX = "st."


class SupersessionIncompleteWarning(UserWarning):
    """The new version was saved, but the link that retires an older version was not written.

    Saving the new version again writes the missing link. Until then the library offers both versions.
    """


_TRUSTED = frozenset({"verified"})
"""The one page status, of stub, draft, synthesized and verified, under which a card is not provisional."""

SEARCH_SQL = """
with hits as (
    select src_collection, page_id, max(score) as score
    from api.search($1, $2, cast($3 as text)::halfvec, collection => $4)
    group by src_collection, page_id
)
select c.id, c.title, c.summary, c.status, c.superseded, c.frontmatter -> 'presentation' as presentation,
       c.frontmatter -> 'intent' as intent, h.score,
       coalesce((select s.body from api.sections s
                 where s.collection = c.src_collection and s.page_id = c.id and s.canon = 'applicability'), '') as applicability
from hits h
join api.cards c on c.src_collection = h.src_collection and c.id = h.page_id
where c.type = $6 and not (c.frontmatter ? 'archived_at') and not c.superseded
order by h.score desc, c.id
limit $5
"""
"""Rank the live pages of one page type with `api.search`, each scored by its best hit, with its card and applicability.

`$1` is the query text, `$2` the hit limit for `api.search`, `$3` the query vector as a `halfvec_literal` or NULL
for BM25 alone, `$4` the collection, `$5` the most pages to return and `$6` the page type. Bind `$3` as text,
because binding it as a `halfvec` fails in the driver. The page type, archived and superseded filters run in SQL
after `api.search` caps its hits at `$2`, so fewer than `$5` pages can come back while more match."""

CATALOG_SQL = """
select c.id, c.title, c.summary, c.status, c.frontmatter -> 'presentation' as presentation,
       c.frontmatter -> 'intent' as intent,
       coalesce((select s.body from api.sections s
                 where s.collection = c.src_collection and s.page_id = c.id and s.canon = 'applicability'), '') as applicability
from api.cards c
where c.type = 'workflow' and not (c.frontmatter ? 'archived_at') and not c.superseded and c.src_collection = $2
order by c.title, c.id
limit $1
"""
"""Every live workflow page in collection `$2`, by title and then id, unranked, at most `$1` of them.

Archived and superseded pages are left out by the same tests `SEARCH_SQL` uses."""

CATALOG_COUNT_SQL = """
select count(*) as total
from api.cards c
where c.type = 'workflow' and not (c.frontmatter ? 'archived_at') and not c.superseded and c.src_collection = $1
"""
"""How many live workflow pages collection `$1` holds, under the same filter `CATALOG_SQL` lists by."""

LIVE_VERSIONS_SQL = """
select c.id
from api.cards c
where c.type = 'workflow' and c.src_collection = $1 and c.id like $2
  and not (c.frontmatter ? 'archived_at') and not c.superseded
"""
"""The ids of every live workflow page in collection `$1` whose id matches the `like` pattern `$2`, such as `wf.<name>.v%`.

The pattern is not escaped, so a caller must take each id apart with `_VERSIONED_ID` and keep only ids of the
exact name."""

ARCHIVED_SQL = """
select c.id, c.title, c.summary, c.status, c.frontmatter,
       coalesce((select s.body from api.sections s
                 where s.page_id = c.id and s.canon = 'applicability'), '') as applicability
from api.cards c
where c.type = 'workflow' and c.frontmatter ? 'archived_at' and c.src_collection = $2
order by c.frontmatter->>'archived_at' desc
limit $1
"""
"""Every archived workflow page in collection `$2`, newest archive first, at most `$1` of them."""


def halfvec_literal(vector: Sequence[float]) -> str:
    """Format a vector as the pgvector text literal `[f1,f2,...]`, every component at full precision.

    Args:
        vector: The embedding's components.

    Returns:
        The literal, to bind as text where `SEARCH_SQL` takes the query vector.
    """
    return "[" + ",".join(repr(component) for component in vector) + "]"


FETCH_SQL = "select frontmatter from api.cards where id = $1"
"""The frontmatter document (jsonb) of page `$1`, holding the saved workflow or reusable step as it was written."""

PAGE_SQL = """
select c.id, c.type, c.title, c.summary, c.status, c.frontmatter,
       coalesce((select s.body from api.sections s
                 where s.page_id = c.id and s.canon = 'applicability'), '') as applicability
from api.cards c
where c.id = $1
"""
"""Page `$1` as columns: its id, type, title, summary, status, remaining frontmatter and applicability.

The `frontmatter` column holds only the keys the page grammar does not model. A caller rebuilding the page to
ingest it again must add `id`, `type`, `title`, `summary` and `status` back from their columns, or the ingest
refuses it."""


SUPERSEDED_SQL = "select c.superseded from api.cards c where c.id = $1"
"""Whether page `$1` already points at a successor.

`api.cards` does not say which page superseded it, so an upsert of a superseded page would lose the link for good."""


def page_id(name: str, version: int) -> str:
    """The page id one version of a workflow lives at, `wf.<name>.v<version>`.

    Args:
        name: The workflow's name.
        version: The workflow's version.

    Returns:
        The page id.
    """
    return f"{ID_PREFIX}{name}.v{version}"


def step_page_id(name: str, version: int) -> str:
    """The page id one version of a reusable step lives at, `st.<name>.v<version>`.

    Args:
        name: The step's name.
        version: The step's version.

    Returns:
        The page id.
    """
    return f"{STEP_ID_PREFIX}{name}.v{version}"


_STEP = TypeAdapter(InlineStep)
"""Validates a reusable step's stored document back into its concrete `Briefing | Program | AgentStep`."""

_EPOCH = datetime.fromtimestamp(0, UTC)
"""The `archived_at` a value that will not parse is read as: the oldest possible, so it sorts last among archived cards."""


def _parse_archived_at(frontmatter: Mapping[str, JsonValue] | str | None, *, workflow_id: str) -> datetime | None:
    """The frontmatter's archive time (naive read as UTC), None without the key, `_EPOCH` with a warning if unparsable."""
    if frontmatter is None:
        return None
    document = frontmatter if isinstance(frontmatter, Mapping) else json.loads(frontmatter or "{}")
    if "archived_at" not in document:
        return None
    recorded = document["archived_at"]
    if isinstance(recorded, str):
        try:
            parsed = datetime.fromisoformat(recorded)
        except ValueError:
            pass
        else:
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    warnings.warn(f"{workflow_id!r} has an archived_at that will not parse ({recorded!r}). Treating it as archived at the epoch.", stacklevel=2)
    return _EPOCH


def _presentation_of(recorded: JsonValue) -> WorkflowPresentation | None:
    """One saved presentation, a mapping or its JSON text, or None when there is none or it no longer parses."""
    if recorded is None:
        return None
    try:
        return WorkflowPresentation.model_validate(json.loads(recorded) if isinstance(recorded, str) else recorded)
    except (ValidationError, json.JSONDecodeError):
        return None


def _presentation_in(frontmatter: Mapping[str, JsonValue] | str | None) -> WorkflowPresentation | None:
    """The presentation in a whole frontmatter document, a mapping or its JSON text, as the by-id reads select it."""
    if frontmatter is None:
        return None
    document = frontmatter if isinstance(frontmatter, Mapping) else json.loads(frontmatter or "{}")
    return _presentation_of(document.get("presentation"))


def _intent_of(recorded: JsonValue) -> WorkflowIntent | None:
    """One saved intent, a mapping or its JSON text, or None when there is none or it no longer parses."""
    if recorded is None:
        return None
    try:
        return WorkflowIntent.model_validate(json.loads(recorded) if isinstance(recorded, str) else recorded)
    except (ValidationError, json.JSONDecodeError):
        return None


def _intent_in(frontmatter: Mapping[str, JsonValue] | str | None) -> WorkflowIntent | None:
    """The intent in a whole frontmatter document, a mapping or its JSON text, as the by-id reads select it."""
    if frontmatter is None:
        return None
    document = frontmatter if isinstance(frontmatter, Mapping) else json.loads(frontmatter or "{}")
    return _intent_of(document.get("intent"))


class LibraryCollection:
    """A workflow library over one knowledge base's pages: search them, fetch the saved documents, ingest on save."""

    def __init__(self, rows: Rows, ingest: Ingest, *, collection: str = "workflows", embed: QueryEmbedder | None = None) -> None:
        """Read through `rows` and write through `ingest`, both within one collection.

        Args:
            rows: A read-only connection as the `kb_reader` role, the only database handle this library holds.
            ingest: The knowledge base's validated write path.
            collection: The collection name of the knowledge base the workflows live in.
            embed: Places a query in the knowledge base's vector space. None makes search lexical alone.
        """
        self._rows = rows
        self._ingest = ingest
        self._collection = collection
        self._embed = embed

    async def _search(self, task: str, *, limit: int, page_type: str) -> tuple[SavedWorkflow, ...]:
        """Rank live pages of `page_type` against `task`, best first. An embedder that raises is not caught."""
        vector = halfvec_literal(await self._embed(task)) if self._embed is not None else None
        found = await self._rows.fetch(SEARCH_SQL, task, min(50, limit * 5), vector, self._collection, limit, page_type)
        return tuple(
            SavedWorkflow(
                id=str(row["id"]),
                title=str(row["title"]),
                summary=str(row["summary"] or ""),
                applicability=str(row["applicability"] or ""),
                score=float(row["score"]) if row["score"] is not None else None,
                provisional=str(row["status"]) not in _TRUSTED,
                metadata={"status": str(row["status"]), "superseded": bool(row["superseded"])},
                presentation=_presentation_of(row["presentation"]),
                intent=_intent_of(row["intent"]),
            )
            for row in found
        )

    async def search(self, task: str, *, limit: int = 5) -> tuple[SavedWorkflow, ...]:
        """Rank live workflows against a task description, best first, archived and superseded ones left out.

        Args:
            task: The task description to match.
            limit: The most cards to return.

        Returns:
            The workflow cards, best first.
        """
        return await self._search(task, limit=limit, page_type=PAGE_TYPE)

    async def search_steps(self, task: str, *, limit: int = 5) -> tuple[SavedWorkflow, ...]:
        """Rank live reusable steps against a task description, best first.

        Args:
            task: The task description to match.
            limit: The most cards to return.

        Returns:
            The step cards, best first.
        """
        return await self._search(task, limit=limit, page_type=STEP_PAGE_TYPE)

    async def catalog(self, *, limit: int = 50) -> tuple[SavedWorkflow, ...]:
        """Every live workflow page in this collection, by title and then id, unranked.

        Args:
            limit: The most cards to return.

        Returns:
            The cards, each scored `0.0`.
        """
        found = await self._rows.fetch(CATALOG_SQL, limit, self._collection)
        return tuple(
            SavedWorkflow(
                id=str(row["id"]),
                title=str(row["title"]),
                summary=str(row["summary"] or ""),
                applicability=str(row["applicability"] or ""),
                score=0.0,
                provisional=str(row["status"]) not in _TRUSTED,
                metadata={"status": str(row["status"])},
                presentation=_presentation_of(row["presentation"]),
                intent=_intent_of(row["intent"]),
            )
            for row in found
        )

    async def catalog_size(self) -> int:
        """Count the live workflow pages in this collection, the set `catalog` lists from.

        Returns:
            The number of live workflow pages.
        """
        found = await self._rows.fetch(CATALOG_COUNT_SQL, self._collection)
        return int(found[0]["total"])

    async def _frontmatter(
        self, workflow_id: str, *, kind: str = "workflow", unknown: type[LibraryError] = LibraryError
    ) -> Mapping[str, JsonValue]:
        """One page's frontmatter document, whichever shape the connection returns jsonb in. Raises `unknown` when no page has that id."""
        stored = await self._rows.fetchval(FETCH_SQL, workflow_id)
        if stored is None:
            msg = f"no {kind} {workflow_id!r} in collection {self._collection!r}"
            raise unknown(msg)
        return stored if isinstance(stored, Mapping) else json.loads(stored)

    async def _resolved(self, workflow_id: str) -> str:
        """A versioned id unchanged, else the newest live version `<name>` or `wf.<name>` names, else `workflow_id`."""
        if _VERSIONED_ID.match(workflow_id):
            return workflow_id
        live = await self._live_versions(workflow_id.removeprefix(ID_PREFIX))
        return live[-1][1] if live else workflow_id

    async def fetch_workflow(self, workflow_id: str) -> Workflow:
        """The workflow a page holds, validated on the way out. A bare name reads its newest live version.

        A versioned id reads that version, even when a newer one exists.

        Args:
            workflow_id: A page id, `wf.<name>`, or a bare name.

        Returns:
            The validated workflow.

        Raises:
            WorkflowUnknownError: If no page has that id, or the page carries no workflow. A page that
                exists but is not a workflow is the same answer as no page at all.
        """
        frontmatter = await self._frontmatter(await self._resolved(workflow_id), unknown=WorkflowUnknownError)
        spec = frontmatter.get("spec")
        if spec is None:
            msg = f"page {workflow_id!r} carries no spec in its frontmatter, so it is not a workflow"
            raise WorkflowUnknownError(msg)
        return Workflow.model_validate(spec)

    async def fetch_step(self, step_id: str) -> InlineStep:
        """The reusable step a page holds, validated as an inline step.

        Args:
            step_id: The step's page id, `st.<name>.v<version>`.

        Returns:
            The step as a `Briefing`, `Program` or `AgentStep`.

        Raises:
            LibraryError: If no page has that id, or the page carries no step.
        """
        document = await self._frontmatter(step_id, kind="step")
        if "step" not in document:
            msg = f"{step_id!r} is a page in {self._collection!r} but not a reusable step: it carries no step document"
            raise LibraryError(msg)
        return _STEP.validate_python(document["step"])

    async def fetch_fingerprint(self, workflow_id: str) -> WorkflowFingerprint | None:
        """The environment stamp the page carries, or None for a workflow saved without one.

        Args:
            workflow_id: A page id, `wf.<name>`, or a bare name.

        Returns:
            The fingerprint, or None when the page has none or it no longer parses.

        Raises:
            WorkflowUnknownError: If no page has that id.
        """
        recorded = (await self._frontmatter(await self._resolved(workflow_id), unknown=WorkflowUnknownError)).get("fingerprint")
        if recorded is None:
            return None
        try:
            return WorkflowFingerprint.model_validate(recorded)
        except ValidationError:
            return None  # a stamp that no longer parses counts as none, so staleness is unknown

    async def fetch_lineage(self, workflow_id: str) -> Lineage | None:
        """The diff that produced this page, or None if nobody adapted anything to make it.

        Args:
            workflow_id: A page id, `wf.<name>`, or a bare name.

        Returns:
            The lineage, or None when the page has none or it no longer parses.

        Raises:
            WorkflowUnknownError: If no page has that id.
        """
        recorded = (await self._frontmatter(await self._resolved(workflow_id), unknown=WorkflowUnknownError)).get("lineage")
        if recorded is None:
            return None
        try:
            return Lineage.model_validate(recorded)
        except ValidationError:
            return None

    async def fetch_card(self, workflow_id: str) -> SavedWorkflow | None:
        """The card as `search` would return it, fetched by id instead of found by words.

        An archived page is answered too, with `archived_at` set.

        Args:
            workflow_id: A page id, `wf.<name>`, or a bare name.

        Returns:
            The card, or None when no page has that id.
        """
        workflow_id = await self._resolved(workflow_id)
        found = await self._rows.fetch(PAGE_SQL, workflow_id)
        if not found:
            return None
        page = found[0]
        status = str(page["status"])
        return SavedWorkflow(
            id=str(page["id"]),
            title=str(page["title"]),
            summary=str(page["summary"] or ""),
            applicability=str(page["applicability"] or ""),
            provisional=status not in _TRUSTED,
            archived_at=_parse_archived_at(page["frontmatter"], workflow_id=workflow_id),
            metadata={"status": status},
            presentation=_presentation_in(page["frontmatter"]),
            intent=_intent_in(page["frontmatter"]),
        )

    async def locate(self, name: str, version: int) -> str | None:
        """The id of the page holding `name` at `version`, or None when there is no such page.

        Args:
            name: The workflow's name.
            version: The version to find.

        Returns:
            The page id, or None.
        """
        workflow_id = page_id(name, version)
        return workflow_id if await self._rows.fetchval(FETCH_SQL, workflow_id) is not None else None

    async def current(self, workflow_id: str) -> str:
        """The newest live version of the workflow `workflow_id` names, or `workflow_id` itself.

        Itself in every case where the library cannot name a newer one: the id is not one this
        library minted, no version of the name is live, or this version is the newest. Live means
        neither archived nor superseded.

        Args:
            workflow_id: A page id. An id this library did not mint is returned as it is.

        Returns:
            The page id of the newest live version, or `workflow_id`.
        """
        match = _VERSIONED_ID.match(workflow_id)
        if match is None:
            return workflow_id
        live = await self._live_versions(match["name"])
        return live[-1][1] if live else workflow_id

    async def _live_versions(self, name: str) -> tuple[tuple[int, str], ...]:
        """Every live `(version, id)` of `name`, lowest version first."""
        found = await self._rows.fetch(LIVE_VERSIONS_SQL, self._collection, f"{ID_PREFIX}{name}.v%")
        versions: list[tuple[int, str]] = []
        for row in found:
            match = _VERSIONED_ID.match(str(row["id"]))
            if match is not None and match["name"] == name:
                versions.append((int(match["version"]), str(row["id"])))
        return tuple(sorted(versions))

    async def redescribe(
        self, workflow_id: str, *, title: str, summary: str, applicability: str, presentation: WorkflowPresentation | None = None
    ) -> None:
        """Rewrite the card's words on the page in place, leaving the workflow, provenance and trust history untouched.

        The keys the page's `frontmatter` column holds, such as a fingerprint or a lineage, are kept. Of the keys
        held in their own columns, only the id, type and status are written back, so a superseded page loses its
        `superseded_by` link. A presentation given replaces the kept one whole, after it is checked against the saved workflow.
        None keeps whatever words the page already carries.

        Args:
            workflow_id: The page id.
            title: The new title.
            summary: The new summary.
            applicability: The new applicability, written as the page's Applicability section.
            presentation: The new author's words, or None to keep the page's own.

        Raises:
            LibraryError: If no page has that id, or the ingest refuses the write.
            PresentationRefusedError: If the presentation names an input or an artifact the
                workflow does not declare. Nothing is ingested in that case.
        """
        found = await self._rows.fetch(PAGE_SQL, workflow_id)
        if not found:
            msg = f"no workflow {workflow_id!r} in collection {self._collection!r}"
            raise LibraryError(msg)
        page = found[0]
        if presentation is not None:
            verify_presentation(await self.fetch_workflow(workflow_id), presentation)
        stored = page["frontmatter"]
        long_tail = dict(stored if isinstance(stored, Mapping) else json.loads(stored or "{}"))
        if presentation is not None:
            long_tail["presentation"] = presentation.model_dump(mode="json")
        try:
            await self._ingest(
                {
                    "frontmatter": {
                        **long_tail,
                        "id": str(page["id"]),
                        "type": str(page["type"]),
                        "title": title,
                        "summary": summary,
                        "status": str(page["status"]),
                    },
                    "sections": [{"heading": "Applicability", "body": applicability}],
                },
                collection=self._collection,
            )
        except Exception as exc:
            msg = f"the collection refused to redescribe {workflow_id!r}: {exc}"
            raise LibraryError(msg) from exc

    async def set_archived(self, workflow_id: str, *, archived: bool) -> None:
        """Set or clear `archived_at` in the page's frontmatter, leaving the rest of its `frontmatter` column untouched.

        Of the keys held in their own columns, only the id, type, title, summary and status are written back, so a
        superseded page loses its `superseded_by` link. Archiving an archived page keeps its first `archived_at`,
        and restoring a live one does nothing.

        Args:
            workflow_id: The page id.
            archived: True to archive the workflow, False to restore it.

        Raises:
            LibraryError: If no page has that id, or the ingest refuses the write.
        """
        found = await self._rows.fetch(PAGE_SQL, workflow_id)
        if not found:
            msg = f"no workflow {workflow_id!r} in collection {self._collection!r}"
            raise LibraryError(msg)
        page = found[0]
        stored = page["frontmatter"]
        long_tail = dict(stored if isinstance(stored, Mapping) else json.loads(stored or "{}"))
        if archived == (long_tail.get("archived_at") is not None):
            return
        if archived:
            long_tail["archived_at"] = datetime.now(UTC).isoformat()
        else:
            long_tail.pop("archived_at", None)
        try:
            await self._ingest(
                {
                    "frontmatter": {
                        **long_tail,
                        "id": str(page["id"]),
                        "type": str(page["type"]),
                        "title": str(page["title"]),
                        "summary": str(page["summary"] or ""),
                        "status": str(page["status"]),
                    },
                    "sections": [{"heading": "Applicability", "body": str(page["applicability"] or "")}],
                },
                collection=self._collection,
            )
        except Exception as exc:
            msg = f"the collection refused to update the archive state of {workflow_id!r}: {exc}"
            raise LibraryError(msg) from exc

    async def archived(self, *, limit: int = 50) -> tuple[SavedWorkflow, ...]:
        """The archived workflow cards in this collection, newest archive first.

        Args:
            limit: The most cards to return.

        Returns:
            The archived cards, each with `archived_at` set.
        """
        found = await self._rows.fetch(ARCHIVED_SQL, limit, self._collection)
        return tuple(
            SavedWorkflow(
                id=str(row["id"]),
                title=str(row["title"]),
                summary=str(row["summary"] or ""),
                applicability=str(row["applicability"] or ""),
                provisional=str(row["status"]) not in _TRUSTED,
                archived_at=_parse_archived_at(row["frontmatter"], workflow_id=str(row["id"])),
                metadata={"status": str(row["status"])},
                presentation=_presentation_in(row["frontmatter"]),
                intent=_intent_in(row["frontmatter"]),
            )
            for row in found
        )

    async def put(
        self,
        workflow: Workflow,
        *,
        title: str,
        summary: str,
        applicability: str,
        provenance: Mapping[str, str],
        fingerprint: WorkflowFingerprint | None = None,
        lineage: Lineage | None = None,
        presentation: WorkflowPresentation | None = None,
        intent: WorkflowIntent | None = None,
    ) -> str:
        """Save the workflow as its own page, `wf.<name>.v<version>`, with status `draft`, and return the page id.

        After the page is written, the lineage's parent and every live lower version of the same name are
        linked forward to it with `superseded_by`, and a lower version saved after a higher one stays live.
        A link that fails leaves the new page saved and warns with `SupersessionIncompleteWarning`.
        A presentation and an intent are written only when given.

        Args:
            workflow: The workflow to save. Its name and version choose the page.
            title: The card's title.
            summary: The card's summary.
            applicability: When to use the workflow, written as the page's Applicability section.
            provenance: `saved_by` names who saved it, and `uses` lists the reusable step ids it was
                assembled from, comma separated. It is saved as the frontmatter list `uses`.
            fingerprint: The environment stamp to keep, or None.
            lineage: The adaptation that produced this workflow, or None. Its parent is linked forward.
            presentation: The author's words about the workflow, or None.
            intent: What the person who wanted the workflow asked for, or None.

        Returns:
            The page id the workflow lives under.

        Raises:
            LibraryError: If this exact version already exists and is superseded, or the ingest refuses
                the page. The current version of anything still saves again freely.
            PresentationRefusedError: If the presentation names an input or an artifact the
                workflow does not declare. Nothing is read or written in that case.
        """
        if presentation is not None:
            verify_presentation(workflow, presentation)
        workflow_id = page_id(workflow.name, workflow.version)
        existing = await self._rows.fetch(SUPERSEDED_SQL, workflow_id)
        if existing and bool(existing[0].get("superseded")):
            msg = (
                f"{workflow_id!r} is already superseded by a later version. Saving it again would offer the stale "
                f"version beside the current one and erase the supersession link. Fetch the newest version of "
                f"{workflow.name!r} and adapt that instead."
            )
            raise LibraryError(msg)
        frontmatter: dict[str, Any] = {
            "id": workflow_id,
            "type": PAGE_TYPE,
            "title": title,
            "summary": summary,
            "status": "draft",
            "workflow_version": workflow.version,
            "saved_by": provenance.get("saved_by", "unknown"),
            "spec": workflow.model_dump(mode="json"),
        }
        optional: tuple[tuple[str, BaseModel | None], ...] = (
            ("fingerprint", fingerprint),
            ("lineage", lineage),
            ("presentation", presentation),
            ("intent", intent),
        )
        frontmatter.update({key: document.model_dump(mode="json") for key, document in optional if document is not None})
        if "uses" in provenance:
            frontmatter["uses"] = [item for item in provenance["uses"].split(",") if item]
        try:
            await self._ingest(
                {"frontmatter": frontmatter, "sections": [{"heading": "Applicability", "body": applicability}]},
                collection=self._collection,
            )
        except LibraryError:
            raise
        except Exception as exc:
            # Every failure leaves this port as a `LibraryError`, whose message a caller can show.
            msg = f"the collection refused {workflow_id!r}: {exc}"
            raise LibraryError(msg) from exc
        parent_id = None if lineage is None else page_id(lineage.parent_name, lineage.parent_version)
        retired = [] if parent_id is None else [parent_id]
        retired.extend(
            older_id for version, older_id in await self._live_versions(workflow.name) if version < workflow.version and older_id != parent_id
        )
        for stale_id in retired:
            await self._supersede(stale_id, by=workflow_id)
        return workflow_id

    async def _supersede(self, parent_id: str, *, by: str) -> bool:
        """Link `parent_id` forward to `by`. False when there is no such page, it holds no workflow, or the write fails (warned)."""
        found = await self._rows.fetch(PAGE_SQL, parent_id)
        if not found:
            return False
        page = found[0]
        stored = page["frontmatter"]
        long_tail = dict(stored if isinstance(stored, Mapping) else json.loads(stored or "{}"))
        if "spec" not in long_tail:
            return False
        try:
            await self._link_forward(page, long_tail, by=by)
        except Exception as exc:
            warnings.warn(
                f"stored {by!r}, but could not link {parent_id!r} forward to it ({exc}). "
                f"Both versions will be offered until this is saved again.",
                SupersessionIncompleteWarning,
                stacklevel=3,
            )
            return False
        return True

    async def _link_forward(self, page: Mapping[str, Any], long_tail: Mapping[str, Any], *, by: str) -> None:
        """Write the parent back with `superseded_by` set, rebuilt from what the library gave us."""
        await self._ingest(
            {
                # The long tail is spread FIRST so the modelled keys always win: a stored jsonb
                # that somehow carried one would otherwise stomp the link this write exists to make.
                "frontmatter": {
                    **long_tail,
                    "id": str(page["id"]),
                    "type": str(page["type"]),
                    "title": str(page["title"]),
                    "summary": str(page["summary"] or ""),
                    "status": str(page["status"]),
                    "superseded_by": f"[[{by}]]",
                },
                "sections": [{"heading": "Applicability", "body": str(page["applicability"] or "")}],
            },
            collection=self._collection,
        )

    async def put_step(
        self, step: InlineStep, *, version: int, title: str, summary: str, applicability: str, provenance: Mapping[str, str]
    ) -> str:
        """Save one reusable step as its own page, `st.<name>.v<version>`, and return its id.

        Saving the same version again replaces its page.

        Args:
            step: The step to save. Its name and `version` choose the page.
            version: The step's version.
            title: The card's title.
            summary: The card's summary.
            applicability: When to use the step, written as the page's Applicability section.
            provenance: `saved_by` names who saved it.

        Returns:
            The step's page id.

        Raises:
            LibraryError: If the ingest refuses the page.
        """
        step_id = step_page_id(step.name, version)
        frontmatter: dict[str, Any] = {
            "id": step_id,
            "type": STEP_PAGE_TYPE,
            "title": title,
            "summary": summary,
            "status": "draft",
            "step_version": version,
            "saved_by": provenance.get("saved_by", "unknown"),
            "step": step.model_dump(mode="json"),
        }
        try:
            await self._ingest(
                {"frontmatter": frontmatter, "sections": [{"heading": "Applicability", "body": applicability}]},
                collection=self._collection,
            )
        except LibraryError:
            raise
        except Exception as exc:
            msg = f"the collection refused {step_id!r}: {exc}"
            raise LibraryError(msg) from exc
        return step_id


# --- the same collection, reached through the contract port ---------------------------------------


def collection_toolset(library: LibraryCollection) -> FunctionToolset[object]:
    """Seven tools over `library`, the host surface `WORKFLOW_COLLECTION` maps the contract port onto.

    Workflows get `find_workflows`, `list_workflows`, `read_workflow` and `write_workflow`. Reusable steps get
    `find_steps`, `read_step` and `save_step`.

    Args:
        library: The library the tools read and write.

    Returns:
        The toolset, which an agent of any dependency type can hold.
    """
    toolset: FunctionToolset[object] = FunctionToolset()

    async def find_workflows(task: str, most: int = 5) -> dict[str, object]:
        """Saved workflows ranked against a task description."""
        found = await library.search(task, limit=most)
        return {
            "found": [
                {"id": c.id, "title": c.title, "summary": c.summary, "when": c.applicability, "score": c.score, "draft": c.provisional}
                for c in found
            ]
        }

    async def list_workflows(most: int = 50) -> dict[str, object]:
        """Every live workflow, by title then id, in the row shape `find_workflows` answers in, plus the total."""
        found = await library.catalog(limit=most)
        return {
            "total": await library.catalog_size(),
            "found": [
                {"id": c.id, "title": c.title, "summary": c.summary, "when": c.applicability, "score": c.score, "draft": c.provisional}
                for c in found
            ],
        }

    async def read_workflow(workflow_id: str) -> dict[str, object]:
        """One saved workflow, as data, with the environment stamp it was saved under."""
        stamp = await library.fetch_fingerprint(workflow_id)
        return {
            "spec": (await library.fetch_workflow(workflow_id)).model_dump(mode="json"),
            "fingerprint": stamp.model_dump(mode="json") if stamp is not None else None,
        }

    async def write_workflow(
        title: str,
        summary: str,
        when: str,
        spec: dict[str, object],
        by: str,
        stamp: dict[str, object] | None = None,
        parent: dict[str, object] | None = None,
        uses: list[str] | None = None,
        words: dict[str, object] | None = None,
        asked_for: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Save one workflow as a page and report the id it now lives under."""
        written = await library.put(
            Workflow.model_validate(spec),
            title=title,
            summary=summary,
            applicability=when,
            provenance={"saved_by": by, "uses": ",".join(uses or [])},
            fingerprint=WorkflowFingerprint.model_validate(stamp) if stamp is not None else None,
            lineage=Lineage.model_validate(parent) if parent is not None else None,
            presentation=WorkflowPresentation.model_validate(words) if words is not None else None,
            intent=WorkflowIntent.model_validate(asked_for) if asked_for is not None else None,
        )
        return {"saved": written}

    async def find_steps(task: str, most: int = 5) -> dict[str, object]:
        """Reusable steps ranked against a task description. Same shape as `find_workflows`."""
        found = await library.search_steps(task, limit=most)
        return {
            "found": [
                {"id": c.id, "title": c.title, "summary": c.summary, "when": c.applicability, "score": c.score, "draft": c.provisional}
                for c in found
            ]
        }

    async def read_step(step_id: str) -> dict[str, object]:
        """One reusable step, as data."""
        return {"step": (await library.fetch_step(step_id)).model_dump(mode="json")}

    async def save_step(title: str, summary: str, when: str, step: dict[str, object], version: int, by: str) -> dict[str, object]:
        """Save one reusable step as a page and report the id it now lives under."""
        written = await library.put_step(
            _STEP.validate_python(step), version=version, title=title, summary=summary, applicability=when, provenance={"saved_by": by}
        )
        return {"saved": written}

    toolset.add_function(find_workflows, takes_ctx=False)
    toolset.add_function(list_workflows, takes_ctx=False)
    toolset.add_function(read_workflow, takes_ctx=False)
    toolset.add_function(write_workflow, takes_ctx=False)
    toolset.add_function(find_steps, takes_ctx=False)
    toolset.add_function(read_step, takes_ctx=False)
    toolset.add_function(save_step, takes_ctx=False)
    return toolset


def _to_step(raw: object) -> InlineStep:
    if not isinstance(raw, Mapping) or "step" not in raw:
        msg = f"expected a mapping with a 'step' key, got {type(raw).__name__}"
        raise TypeError(msg)
    return _STEP.validate_python(raw["step"])


WORKFLOW_COLLECTION = LibraryVocabulary(
    search_tool="find_workflows",
    fetch_tool="read_workflow",
    search_args=lambda task, limit: {"task": task, "most": limit},
    fetch_args=lambda workflow_id: {"workflow_id": workflow_id},
    to_cards=lambda raw: tuple(
        SavedWorkflow(
            id=str(row["id"]),
            title=str(row.get("title") or row["id"]),
            summary=str(row.get("summary") or ""),
            applicability=str(row.get("when") or ""),
            score=float(row["score"]) if row.get("score") else None,
            provisional=bool(row.get("draft", True)),
        )
        for row in (raw.get("found") or ())
    ),
    to_spec=lambda raw: Workflow.model_validate(raw["spec"]),
    to_fingerprint=lambda raw: WorkflowFingerprint.model_validate(raw["fingerprint"]) if raw.get("fingerprint") else None,
    catalog_tool="list_workflows",
    catalog_args=lambda limit: {"most": limit},
    catalog_total=lambda raw: raw.get("total"),
    put_tool="write_workflow",
    put_args=lambda write: {
        "title": write.title,
        "summary": write.summary,
        "when": write.applicability,
        "spec": write.workflow.model_dump(mode="json"),
        "by": write.saved_by,
        "stamp": write.fingerprint.model_dump(mode="json") if write.fingerprint is not None else None,
        "parent": write.lineage.model_dump(mode="json") if write.lineage is not None else None,
        "uses": list(write.uses) if write.uses is not None else None,
        "words": write.presentation.model_dump(mode="json") if write.presentation is not None else None,
        "asked_for": write.intent.model_dump(mode="json") if write.intent is not None else None,
    },
    to_id=lambda raw: str(raw["saved"]),
    search_steps_tool="find_steps",
    fetch_step_tool="read_step",
    put_step_tool="save_step",
    step_args=lambda step_id: {"step_id": step_id},
    to_step=_to_step,
)
"""The contract port's vocabulary for `collection_toolset`: which tool answers each library verb, and how its arguments and results map."""
