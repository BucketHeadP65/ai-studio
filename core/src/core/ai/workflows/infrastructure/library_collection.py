"""A workflow library kept as pages of one knowledge base, each workflow saved as a document in its page's frontmatter.

`put` writes the workflow into `kb.pages.frontmatter` (jsonb) with the applicability as a section, and
`fetch_workflow` reads the same document back and validates it. Reads go through the read-only `kb_reader` role
and writes through the ingest callable the library is given, so this module never touches the `kb.*` tables and
holds no writer credential. Each version is its own page, `wf.<name>.v<n>` for a workflow and `st.<name>.v<n>` for
a reusable step. Saving a version links every live lower version of the name forward to it, and `search`, `catalog`
and `current` read only pages nothing has superseded, while `versions` lists every saved version of a name. A reusable step version holds one content: it is never saved
again with different content. A saved workflow version records, under `reusable_steps`, which saved version of each
reusable step its copies match, and the save raises a maintenance flag for each copy that matches none. Maintenance
flags are pages of their own, `mf.<short kind>.<workflow name>.<step>` or `mf.<short kind>.<step>`, written by one rule
that keeps a resolved flag resolved for the same step version. An adoption on live trial carries its marker under
`live_trial`, and a revert makes the adopted version's lineage parent live again and archives the version.
"""

from __future__ import annotations

import json
import re
import warnings

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.toolsets import FunctionToolset

from core.ai.workflows.contract_port import LibraryVocabulary
from core.ai.workflows.domain.consumers import ConsumerEntry, StepVersion, consumer_record
from core.ai.workflows.domain.events import ReusableStepSaved
from core.ai.workflows.domain.fingerprint import WorkflowFingerprint, step_content_address
from core.ai.workflows.domain.flags import FlagKind, MaintenanceFlag, diverged_copy_flag, flag_id, flag_to_write
from core.ai.workflows.domain.intent import WorkflowIntent
from core.ai.workflows.domain.lineage import Lineage
from core.ai.workflows.domain.live_trial import LiveTrialMarker
from core.ai.workflows.domain.presentation import WorkflowPresentation, verify_presentation
from core.ai.workflows.domain.vocabulary import InlineStep, Workflow
from core.ai.workflows.exceptions import FlagIdTakenError, FlagUnknownError, LibraryError, StepVersionChangedError, WorkflowUnknownError
from core.ai.workflows.read_models import ConsumerCard, FlagCard, SavedWorkflow

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from core.ai.embedding import QueryEmbedder
    from core.ai.workflows.internal_contracts import Ingest, Rows

PAGE_TYPE = "workflow"
ID_PREFIX = "wf."

_VERSIONED_ID = re.compile(rf"^{re.escape(ID_PREFIX)}(?P<name>[a-z0-9]+(?:-[a-z0-9]+)*)\.v(?P<version>[1-9][0-9]*)$")
"""Matches an id `page_id` mints and captures its `name` and `version`."""

STEP_PAGE_TYPE = "step"
STEP_ID_PREFIX = "st."

_VERSIONED_STEP_ID = re.compile(rf"^{re.escape(STEP_ID_PREFIX)}(?P<name>[a-z][a-z0-9_]*)\.v(?P<version>[1-9][0-9]*)$")
"""Matches an id `step_page_id` mints and captures its `name` and `version`."""

FLAG_PAGE_TYPE = "maintenance-flag"


class SupersessionIncompleteWarning(UserWarning):
    """The new version was saved, but the link that retires an older version was not written.

    The library offers both versions until the older one is linked forward. Saving the new version again writes the link,
    for a workflow and for a reusable step alike.
    """


class FlagWriteIncompleteWarning(UserWarning):
    """A workflow was saved or archived, but a maintenance flag that change raises or resolves was not written.

    Saving or archiving the workflow again writes the flag.
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

ALL_VERSIONS_SQL = """
select c.id
from api.cards c
where c.type = 'workflow' and c.src_collection = $1 and c.id like $2
"""
"""The ids of every workflow page in collection `$1` whose id matches the `like` pattern `$2` (`wf.<name>.v%`), live,
superseded or archived.

The pattern is not escaped, so a caller must take each id apart with `_VERSIONED_ID` and keep only ids of the exact
name."""

STEP_PAGES_SQL = """
select c.id, c.frontmatter -> 'step' as step
from api.cards c
where c.type = 'step' and c.src_collection = $1 and c.frontmatter -> 'step' ->> 'name' = any($2::text[])
"""
"""The id and the step document of every page in collection `$1` holding a reusable step named in the `text[]` `$2`.

Superseded and archived pages are answered too. The rows come in no particular order."""

LIVE_STEP_VERSIONS_SQL = """
select c.id
from api.cards c
where c.type = 'step' and c.src_collection = $1 and c.id like $2
  and not (c.frontmatter ? 'archived_at') and not c.superseded
"""
"""The ids of every live page in collection `$1` holding a reusable step, whose id matches the `like` pattern `$2` (`st.<name>.v%`).

The pattern is not escaped, and `_` is a wildcard in it, so a caller must take each id apart with `_VERSIONED_STEP_ID`
and keep only ids of the exact name."""

CONSUMERS_SQL = """
select c.id, c.frontmatter -> 'reusable_steps' as reusable_steps
from api.cards c
where c.type = 'workflow' and c.src_collection = $1
  and c.frontmatter -> 'reusable_steps' @> $2::jsonb
  and not (c.frontmatter ? 'archived_at') and not c.superseded
"""
"""The id and the consumer record of every live workflow page in collection `$1` whose record holds an entry like `$2`.

Bind `$2` as the JSON text of a list holding one object, `[{"step": "<name>"}]`, so jsonb containment matches the
pages whose record has an entry for that step. The rows come in no particular order."""

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


FLAG_SQL = """
select c.id, c.type, c.title, c.summary, c.status, c.frontmatter,
       coalesce((select s.body from api.sections s
                 where s.collection = c.src_collection and s.page_id = c.id and s.canon = 'evidence'), '') as evidence,
       coalesce((select s.body from api.sections s
                 where s.collection = c.src_collection and s.page_id = c.id and s.canon = 'proposed_change'), '') as proposed_change
from api.cards c
where c.src_collection = $1 and c.id = $2
"""
"""Page `$2` of collection `$1`, of any type, as columns: its id, type, title, summary, status, remaining frontmatter,
and the bodies of its Evidence and Proposed change sections, empty when it has none."""

FLAGS_SQL = """
select c.id, c.type, c.title, c.summary, c.status, c.frontmatter,
       coalesce((select s.body from api.sections s
                 where s.collection = c.src_collection and s.page_id = c.id and s.canon = 'evidence'), '') as evidence,
       coalesce((select s.body from api.sections s
                 where s.collection = c.src_collection and s.page_id = c.id and s.canon = 'proposed_change'), '') as proposed_change
from api.cards c
where c.type = 'maintenance-flag' and c.src_collection = $1
  and ($2::text is null or c.status = $2::text)
  and ($3::text is null or c.frontmatter ->> 'flag_kind' = $3::text)
order by (c.frontmatter ->> 'raised_at')::timestamptz desc, c.id
"""
"""Every maintenance flag page in collection `$1` with status `$2` and kind `$3`, newest raised first, in `FLAG_SQL`'s columns.

Bind `$2` or `$3` as NULL to leave that filter out."""

_FLAG_FIELDS = ("step", "step_id", "step_hash", "workflow", "raised_by", "raised_at", "resolved_by", "decision_reason")
"""The flag fields a flag page keeps in its frontmatter under their own names. Its kind is kept as `flag_kind`."""


SUPERSEDED_SQL = "select c.superseded from api.cards c where c.id = $1"
"""Whether page `$1` already points at a successor.

`api.cards` does not say which page superseded it, so an upsert of a superseded page would lose the link for good."""

SAVED_BY_SQL = """
select c.frontmatter ->> 'saved_by' as saved_by
from api.cards c
where c.type = 'workflow' and c.src_collection = $1 and c.id = $2
"""
"""Who saved workflow page `$2` of collection `$1`, as its frontmatter records it. No row when no workflow page has that id."""


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


_RECORD = TypeAdapter(list[dict[str, JsonValue]])
"""Validates a stored consumer record into its entries as mappings, before each is read as a `ConsumerEntry`."""


def _consumer_entries(stored: JsonValue, *, workflow_id: str) -> tuple[ConsumerEntry, ...]:
    """The consumer record a page keeps under `reusable_steps`, a list or its JSON text, refused naming the page when it does not parse."""
    if stored is None:
        return ()
    try:
        entries = _RECORD.validate_python(json.loads(stored) if isinstance(stored, str) else stored)
        # The ingest drops a null at every depth, so the entry of a diverged copy comes back without `version`.
        return tuple(ConsumerEntry.model_validate({"version": None, **entry}) for entry in entries)
    except (ValidationError, json.JSONDecodeError) as exc:
        msg = f"the consumer record on {workflow_id!r} does not parse: {exc}"
        raise LibraryError(msg) from exc


def _flag_of(page: Mapping[str, Any]) -> MaintenanceFlag:
    """The flag a row of `FLAG_SQL` or `FLAGS_SQL` holds, refused naming the page when it does not parse."""
    stored = page["frontmatter"]
    document = stored if isinstance(stored, Mapping) else json.loads(stored or "{}")
    try:
        return MaintenanceFlag.model_validate(
            {
                **{key: document[key] for key in _FLAG_FIELDS if key in document},
                "kind": document.get("flag_kind"),
                "title": page["title"],
                "summary": page["summary"] or "",
                "status": page["status"],
                "evidence": page["evidence"] or "",
                "proposed_change": page["proposed_change"] or "",
            }
        )
    except ValidationError as exc:
        msg = f"the maintenance flag {page['id']!r} does not parse: {exc}"
        raise LibraryError(msg) from exc


def _warn_flag_not_written(workflow_id: str, failure: str, exc: Exception, *, change: Literal["saved", "archived"]) -> None:
    """Warn that `workflow_id` was saved or archived but `failure` happened to its flags, and that doing it again writes them."""
    again = "Saving" if change == "saved" else "Archiving"
    warnings.warn(
        f"{change} {workflow_id!r}, but {failure} ({exc}). {again} the workflow again writes it.",
        FlagWriteIncompleteWarning,
        stacklevel=4,
    )


def _cases_text(cases: Mapping[str, JsonValue]) -> str:
    """A step's cases as the JSON text its page keeps, since the ingest drops the empty lists, mappings and nulls of a nested document."""
    return json.dumps(dict(cases))


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

    async def saved_by(self, workflow_id: str) -> str:
        """Who saved one workflow version, as its page records it.

        Args:
            workflow_id: The workflow version's id, `wf.<name>.v<version>`. Superseded and archived versions are answered too.

        Returns:
            The `saved_by` its save recorded, such as `owner:cli` or `agent:step-maintenance`.

        Raises:
            WorkflowUnknownError: If no workflow page has that id.
            LibraryError: If the page records no `saved_by`.
        """
        found = await self._rows.fetch(SAVED_BY_SQL, self._collection, workflow_id)
        if not found:
            msg = f"no workflow {workflow_id!r} in collection {self._collection!r}"
            raise WorkflowUnknownError(msg)
        saved_by = found[0]["saved_by"]
        if not isinstance(saved_by, str):
            msg = f"{workflow_id!r} records no saved_by, so who saved it is unknown"
            raise LibraryError(msg)
        return saved_by

    async def restamp(self, workflow_id: str, fingerprint: WorkflowFingerprint) -> None:
        """Write a new environment stamp on one workflow version, keeping every other key, the card and the body as stored.

        The lineage, the live trial marker, the consumer record and an archive mark stay as they are. A stamp equal to the
        stored one writes nothing, and so does an empty stamp on a page that holds none. It assumes one writer at a time,
        such as an import or a CLI.

        Args:
            workflow_id: The workflow version's id, `wf.<name>.v<version>`.
            fingerprint: The environment stamp to keep under `fingerprint`.

        Raises:
            WorkflowUnknownError: If no workflow page has that id. Nothing is written.
            LibraryError: If the version is superseded, or the ingest refuses the write. Nothing is written in each case.
        """
        superseded = await self._rows.fetch(SUPERSEDED_SQL, workflow_id)
        if superseded and bool(superseded[0].get("superseded")):
            msg = f"{workflow_id!r} is superseded by a later version, and writing it again would drop that link, so it is not restamped."
            raise LibraryError(msg)
        found = await self._rows.fetch(PAGE_SQL, workflow_id)
        if not found or str(found[0]["type"]) != PAGE_TYPE:
            msg = f"no workflow {workflow_id!r} in collection {self._collection!r}"
            raise WorkflowUnknownError(msg)
        page = found[0]
        stored = page["frontmatter"]
        long_tail = dict(stored if isinstance(stored, Mapping) else json.loads(stored or "{}"))
        try:
            held = WorkflowFingerprint.model_validate(long_tail.get("fingerprint") or {})
        except ValidationError:
            held = None
        if held == fingerprint:
            return
        try:
            await self._rewrite(page, {**long_tail, "fingerprint": fingerprint.model_dump(mode="json")})
        except Exception as exc:
            msg = f"the collection refused the new stamp of {workflow_id!r}: {exc}"
            raise LibraryError(msg) from exc

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

    async def versions(self, name: str) -> tuple[tuple[int, str], ...]:
        """Every saved version of one workflow, live, superseded or archived, lowest version first, read in one query.

        Args:
            name: The workflow's name.

        Returns:
            Each version's `(version, id)`. Empty when no version of `name` is saved.
        """
        return await self._version_ids(ALL_VERSIONS_SQL, f"{ID_PREFIX}{name}.v%", _VERSIONED_ID, name)

    async def _live_versions(self, name: str) -> tuple[tuple[int, str], ...]:
        """Every live `(version, id)` of the workflow `name`, lowest version first."""
        return await self._version_ids(LIVE_VERSIONS_SQL, f"{ID_PREFIX}{name}.v%", _VERSIONED_ID, name)

    async def _live_step_versions(self, name: str) -> tuple[tuple[int, str], ...]:
        """Every live `(version, id)` of the reusable step `name`, lowest version first."""
        return await self._version_ids(LIVE_STEP_VERSIONS_SQL, f"{STEP_ID_PREFIX}{name}.v%", _VERSIONED_STEP_ID, name)

    async def _version_ids(self, sql: str, pattern: str, versioned: re.Pattern[str], name: str) -> tuple[tuple[int, str], ...]:
        """The `(version, id)` of each id `sql` answers for the `like` `pattern` that `versioned` parses as `name`."""
        found = await self._rows.fetch(sql, self._collection, pattern)
        versions: list[tuple[int, str]] = []
        for row in found:
            match = versioned.match(str(row["id"]))
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
            await self._rewrite(page, long_tail, title=title, summary=summary, applicability=applicability)
        except Exception as exc:
            msg = f"the collection refused to redescribe {workflow_id!r}: {exc}"
            raise LibraryError(msg) from exc

    async def set_archived(self, workflow_id: str, *, archived: bool) -> None:
        """Set or clear `archived_at` in the page's frontmatter, leaving the rest of its `frontmatter` column untouched.

        Of the keys held in their own columns, only the id, type, title, summary and status are written back, so a
        superseded page loses its `superseded_by` link. Archiving an archived page keeps its first `archived_at`,
        and restoring a live one does nothing. Archiving, again included, resolves as `library` each open
        `diverged_copy` flag raised on this version, and a flag that is not written warns with
        `FlagWriteIncompleteWarning`, the archive standing.

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
        if archived != (long_tail.get("archived_at") is not None):
            if archived:
                long_tail["archived_at"] = datetime.now(UTC).isoformat()
            else:
                long_tail.pop("archived_at", None)
            try:
                await self._rewrite(page, long_tail)
            except Exception as exc:
                msg = f"the collection refused to update the archive state of {workflow_id!r}: {exc}"
                raise LibraryError(msg) from exc
        if archived:
            await self._resolve_open_diverged_flags(
                workflow_id, lambda flag: flag.workflow == workflow_id, reason=lambda _: f"{workflow_id} was archived.", change="archived"
            )

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
        live_trial: LiveTrialMarker | None = None,
    ) -> str:
        """Save the workflow as its own page, `wf.<name>.v<version>`, with status `draft`, and return the page id.

        The page records under `reusable_steps` the consumer record of every inline step whose name a reusable step
        has saved versions of, read against those versions and the prior record. The prior record is the one the page
        already holds when this version is saved again, else the lineage parent's, so a decline and a diverged copy's
        version carry over while the copy is unchanged. After the page is written, the lineage's parent and every
        live lower version of the same name are linked forward to it with `superseded_by`, and a lower version saved
        after a higher one stays live. A link that fails leaves the new page saved and warns with
        `SupersessionIncompleteWarning`. Then each diverged copy raises its `diverged_copy` maintenance flag, about the
        version it diverged from with the diff in its evidence, and each copy that matches a version resolves the open
        `diverged_copy` flag of its step in this workflow, as `library`. A flag that is not written leaves the page
        saved and warns with `FlagWriteIncompleteWarning`. A presentation, an intent and a live trial marker are written
        only when given, so a save without a marker drops one the page held.

        Args:
            workflow: The workflow to save. Its name and version choose the page.
            title: The card's title.
            summary: The card's summary.
            applicability: When to use the workflow, written as the page's Applicability section.
            provenance: `saved_by` names who saved it.
            fingerprint: The environment stamp to keep, or None.
            lineage: The adaptation that produced this workflow, or None. Its parent is linked forward, and its
                parent's consumer record is the prior record when this version is new.
            presentation: The author's words about the workflow, or None.
            intent: What the person who wanted the workflow asked for, or None.
            live_trial: The marker of an adoption on live trial, written under `live_trial`, or None.

        Returns:
            The page id the workflow lives under.

        Raises:
            LibraryError: If this exact version already exists and is superseded, the prior consumer record
                does not parse, or the ingest refuses the page. The current version of anything still saves again
                freely.
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
        record, versions = await self._consumer_record(workflow, workflow_id, lineage)
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
            ("live_trial", live_trial),
        )
        frontmatter.update({key: document.model_dump(mode="json") for key, document in optional if document is not None})
        if record:
            frontmatter["reusable_steps"] = [entry.model_dump(mode="json") for entry in record]
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
        await self._settle_diverged_flags(workflow, workflow_id, record, versions)
        return workflow_id

    async def _consumer_record(
        self, workflow: Workflow, workflow_id: str, lineage: Lineage | None
    ) -> tuple[tuple[ConsumerEntry, ...], Mapping[str, tuple[StepVersion, ...]]]:
        """The record a save of `workflow` at `workflow_id` writes, and the step versions read: the prior record is the page's own, else the parent's."""
        if not workflow.inline:
            return (), {}
        versions = await self.step_versions_of([step.name for step in workflow.inline])
        prior_id = workflow_id
        stored = await self._rows.fetchval(FETCH_SQL, prior_id)
        if stored is None and lineage is not None:
            prior_id = page_id(lineage.parent_name, lineage.parent_version)
            stored = await self._rows.fetchval(FETCH_SQL, prior_id)
        prior: tuple[ConsumerEntry, ...] = ()
        if stored is not None:
            document = stored if isinstance(stored, Mapping) else json.loads(stored)
            prior = _consumer_entries(document.get("reusable_steps"), workflow_id=prior_id)
        return consumer_record(workflow, versions, prior=prior), versions

    async def _settle_diverged_flags(
        self, workflow: Workflow, workflow_id: str, record: Sequence[ConsumerEntry], versions: Mapping[str, Sequence[StepVersion]]
    ) -> None:
        """Raise the flag of each diverged copy in `record`, then resolve the open one of each step this save no longer holds diverged."""
        raised_at = datetime.now(UTC)
        held = {entry.step: entry for entry in record}
        for entry in record:
            if entry.version is not None or entry.diverged_from is None:
                continue
            try:
                version = {saved.version: saved for saved in versions[entry.step]}[entry.diverged_from]
                await self.put_flag(diverged_copy_flag(workflow, workflow_id, entry, version, raised_at=raised_at))
            except Exception as exc:
                _warn_flag_not_written(
                    workflow_id, f"the maintenance flag {flag_id('diverged_copy', entry.step, workflow.name)!r} was not written", exc, change="saved"
                )

        def settled_here(flag: MaintenanceFlag) -> bool:
            raised_on = _VERSIONED_ID.match(flag.workflow)
            on_this_name = raised_on is not None and raised_on["name"] == workflow.name and int(raised_on["version"]) <= workflow.version
            still_diverged = flag.step in held and held[flag.step].version is None
            return on_this_name and not still_diverged

        def reason(flag: MaintenanceFlag) -> str:
            entry = held.get(flag.step)
            if entry is None:
                return f"{workflow_id} holds no copy of {flag.step}."
            return f"{workflow_id} holds {flag.step} v{entry.version}, a saved version, again."

        await self._resolve_open_diverged_flags(workflow_id, settled_here, reason=reason, change="saved")

    async def _resolve_open_diverged_flags(
        self,
        workflow_id: str,
        settled: Callable[[MaintenanceFlag], bool],
        *,
        reason: Callable[[MaintenanceFlag], str],
        change: Literal["saved", "archived"],
    ) -> None:
        """Resolve as `library` each open `diverged_copy` flag `settled` picks, read in one statement, warning on each failure."""
        try:
            open_flags = await self._flag_rows(status="open", kind="diverged_copy")
        except Exception as exc:
            _warn_flag_not_written(workflow_id, "its open diverged_copy flags were not read, so none was resolved", exc, change=change)
            return
        for open_id, flag in open_flags:
            if not settled(flag):
                continue
            try:
                await self._resolve(open_id, flag, by="library", reason=reason(flag))
            except Exception as exc:
                _warn_flag_not_written(workflow_id, f"the maintenance flag {open_id!r} was not written", exc, change=change)

    async def fetch_live_trial(self, workflow_id: str) -> LiveTrialMarker | None:
        """The live trial marker one workflow version carries under `live_trial`.

        Args:
            workflow_id: A page id, `wf.<name>`, or a bare name for its newest live version.

        Returns:
            The marker, or None for a version saved without one.

        Raises:
            WorkflowUnknownError: If no page has that id.
            LibraryError: If the marker on the page does not parse.
        """
        resolved = await self._resolved(workflow_id)
        recorded = (await self._frontmatter(resolved, unknown=WorkflowUnknownError)).get("live_trial")
        if recorded is None:
            return None
        try:
            return LiveTrialMarker.model_validate(recorded)
        except ValidationError as exc:
            msg = f"the live trial marker on {resolved!r} does not parse: {exc}"
            raise LibraryError(msg) from exc

    async def revert(self, workflow_id: str) -> str:
        """Undo an adoption: write its lineage parent back without `superseded_by`, then archive the version.

        The parent is written first, every other key it holds kept, so a failed archive leaves both versions live with
        this one still the newest, and the revert can be asked again. Nothing is written when the call is refused.

        Args:
            workflow_id: The workflow version to revert, `wf.<name>.v<version>`. It must be the newest live version of
                its name.

        Returns:
            The parent's id, now the newest live version.

        Raises:
            LibraryError: If `workflow_id` is not a workflow version id or not the newest live version of its name, it
                carries no lineage parent of the same name, the parent is not there, holds no workflow or is archived,
                or the ingest refuses a write. A refused archive says the parent is live again and to revert again.
        """
        match = _VERSIONED_ID.match(workflow_id)
        if match is None:
            msg = f"{workflow_id!r} is not a workflow version id. A revert names one version, wf.<name>.v<version>. Nothing was written."
            raise LibraryError(msg)
        name = match["name"]
        live = await self._live_versions(name)
        newest = live[-1][1] if live else None
        if newest != workflow_id:
            msg = (
                f"{workflow_id!r} is not the newest live version of {name!r} (the newest is {newest!r}), so it is not reverted. "
                f"Nothing was written."
            )
            raise LibraryError(msg)
        lineage = await self.fetch_lineage(workflow_id)
        if lineage is None or lineage.parent_name != name:
            msg = f"{workflow_id!r} carries no lineage parent of the same name to go back to, so it is not reverted. Nothing was written."
            raise LibraryError(msg)
        parent_id = page_id(name, lineage.parent_version)
        found = await self._rows.fetch(PAGE_SQL, parent_id)
        if not found:
            msg = f"the lineage parent {parent_id!r} of {workflow_id!r} is not in collection {self._collection!r}, so it is not reverted. Nothing was written."
            raise LibraryError(msg)
        parent = found[0]
        stored = parent["frontmatter"]
        long_tail = dict(stored if isinstance(stored, Mapping) else json.loads(stored or "{}"))
        if "spec" not in long_tail:
            msg = f"the lineage parent {parent_id!r} of {workflow_id!r} holds no workflow, so it is not reverted. Nothing was written."
            raise LibraryError(msg)
        if long_tail.get("archived_at") is not None:
            msg = (
                f"the lineage parent {parent_id!r} of {workflow_id!r} is archived. Restore it, then revert {workflow_id!r} again. "
                f"Nothing was written."
            )
            raise LibraryError(msg)
        try:
            await self._rewrite(parent, long_tail)
        except Exception as exc:
            msg = f"the collection refused to make {parent_id!r} live again, so {workflow_id!r} is not reverted: {exc}"
            raise LibraryError(msg) from exc
        try:
            await self.set_archived(workflow_id, archived=True)
        except LibraryError as exc:
            msg = (
                f"{parent_id} is live again, and archiving {workflow_id} failed, so both are live and {workflow_id} is still "
                f"the newest. Revert {workflow_id} again to finish: {exc}"
            )
            raise LibraryError(msg) from exc
        return parent_id

    async def consumers_of(self, step_name: str) -> tuple[ConsumerCard, ...]:
        """Every live workflow version whose consumer record names the reusable step, by workflow name and then version.

        Live means neither archived nor superseded.

        Args:
            step_name: The reusable step's name.

        Returns:
            One card per workflow version, carrying its record's entry for the step. Empty when no live version holds a copy.

        Raises:
            LibraryError: The record on one of those pages does not parse.
        """
        found = await self._rows.fetch(CONSUMERS_SQL, self._collection, json.dumps([{"step": step_name}]))
        cards: list[ConsumerCard] = []
        for row in found:
            workflow_id = str(row["id"])
            match = _VERSIONED_ID.match(workflow_id)
            record = _consumer_entries(row["reusable_steps"], workflow_id=workflow_id)
            entry = next((held for held in record if held.step == step_name), None)
            if match is None or entry is None:
                continue
            cards.append(ConsumerCard(workflow_id=workflow_id, name=match["name"], version=int(match["version"]), entry=entry))
        return tuple(sorted(cards, key=lambda card: (card.name, card.version)))

    async def fetch_consumer_record(self, workflow_id: str) -> tuple[ConsumerEntry, ...]:
        """The consumer record one workflow version was saved with, one entry per copy of a reusable step, by step name.

        Args:
            workflow_id: A page id, `wf.<name>`, or a bare name for its newest live version.

        Returns:
            The entries. Empty for a workflow that holds no copy of a reusable step.

        Raises:
            WorkflowUnknownError: If no page has that id.
            LibraryError: If the record on the page does not parse.
        """
        resolved = await self._resolved(workflow_id)
        document = await self._frontmatter(resolved, unknown=WorkflowUnknownError)
        return _consumer_entries(document.get("reusable_steps"), workflow_id=resolved)

    async def record_decline(self, workflow_id: str, *, step: str, step_hash: str, because: Literal["kept", "reverted"]) -> None:
        """Record on a workflow version nothing has superseded that it keeps its copy of a step instead of one step version.

        An archived version takes a decline too. The page is written back with that entry declined and every other key
        and entry as stored. A decline already recorded writes nothing.

        Args:
            workflow_id: The page id.
            step: The reusable step's name.
            step_hash: The content address of the step version declined.
            because: `kept` when step maintenance kept the copy, `reverted` when a live trial of that version reverted.

        Raises:
            LibraryError: If no page has that id, it is superseded, its record holds no copy of `step`, or the ingest
                refuses the write. Nothing is written in each case.
        """
        superseded = await self._rows.fetch(SUPERSEDED_SQL, workflow_id)
        if superseded and bool(superseded[0].get("superseded")):
            msg = (
                f"{workflow_id!r} is superseded by a later version, and writing it again would drop that link. "
                f"Record the decline on the newest live version of the workflow."
            )
            raise LibraryError(msg)
        found = await self._rows.fetch(PAGE_SQL, workflow_id)
        if not found:
            msg = f"no workflow {workflow_id!r} in collection {self._collection!r}"
            raise LibraryError(msg)
        page = found[0]
        stored = page["frontmatter"]
        long_tail = dict(stored if isinstance(stored, Mapping) else json.loads(stored or "{}"))
        record = _consumer_entries(long_tail.get("reusable_steps"), workflow_id=workflow_id)
        held = next((entry for entry in record if entry.step == step), None)
        if held is None:
            msg = f"{workflow_id!r} holds no copy of the reusable step {step!r}, so it has no copy to keep."
            raise LibraryError(msg)
        declined = held.model_copy(update={"declined": step_hash, "declined_because": because})
        if declined == held:
            return
        await self._write_record(page, long_tail, tuple(declined if entry is held else entry for entry in record))

    async def _write_record(self, page: Mapping[str, Any], long_tail: Mapping[str, Any], record: Sequence[ConsumerEntry]) -> None:
        """Write a live page back with `record` as its consumer record, every other kept key as `long_tail` holds it."""
        kept = {key: value for key, value in long_tail.items() if key != "reusable_steps"}
        if record:
            kept["reusable_steps"] = [entry.model_dump(mode="json") for entry in record]
        try:
            await self._rewrite(page, kept)
        except LibraryError:
            raise
        except Exception as exc:
            msg = f"the collection refused the consumer record of {page['id']!r}: {exc}"
            raise LibraryError(msg) from exc

    async def put_flag(self, flag: MaintenanceFlag) -> str | None:
        """Write a maintenance flag under its stable id by the rule every writer follows, read from the page first.

        No page writes the flag open. An open page takes the new flag, and one that already holds it apart from
        `raised_at` writes nothing. A resolved page keeps its decision while the flag's `step_hash` is the same, and
        opens again for another. An empty field is left out of the page.

        Args:
            flag: The flag to raise. A flag about one consumer names its workflow version id, `wf.<name>.v<version>`.

        Returns:
            The id written, or None when the page was left as it is.

        Raises:
            FlagIdTakenError: If the id holds a page of another type. Nothing is written.
            LibraryError: If the flag's workflow is not a workflow version id, the page under the id does not parse,
                or the ingest refuses the page.
        """
        workflow_name = ""
        if flag.workflow:
            match = _VERSIONED_ID.match(flag.workflow)
            if match is None:
                msg = f"the flag names the workflow {flag.workflow!r}. Name the consumer by its workflow version id, wf.<name>.v<version>."
                raise LibraryError(msg)
            workflow_name = match["name"]
        written_id = flag_id(flag.kind, flag.step, workflow_name)
        found = await self._rows.fetch(FLAG_SQL, self._collection, written_id)
        if found and str(found[0]["type"]) != FLAG_PAGE_TYPE:
            raise FlagIdTakenError(written_id, page_type=str(found[0]["type"]))
        to_write = flag_to_write(_flag_of(found[0]) if found else None, flag)
        if to_write is None:
            return None
        await self._write_flag(written_id, to_write)
        return written_id

    async def flags(self, *, status: Literal["open", "resolved"] | None = None, kind: FlagKind | None = None) -> tuple[FlagCard, ...]:
        """Every maintenance flag in this collection with that status and kind, newest raised first.

        Args:
            status: Only flags with this status. None lists both.
            kind: Only flags of this kind. None lists every kind.

        Returns:
            The cards, newest raised first.

        Raises:
            LibraryError: If a flag page does not parse.
        """
        return tuple(FlagCard.from_flag(listed_id, flag) for listed_id, flag in await self._flag_rows(status=status, kind=kind))

    async def _flag_rows(self, *, status: Literal["open", "resolved"] | None, kind: FlagKind | None) -> list[tuple[str, MaintenanceFlag]]:
        """Each flag `FLAGS_SQL` answers for that status and kind, with its id, newest raised first."""
        return [(str(page["id"]), _flag_of(page)) for page in await self._rows.fetch(FLAGS_SQL, self._collection, status, kind)]

    async def fetch_flag(self, flag_id: str) -> MaintenanceFlag | None:
        """The maintenance flag a page in this collection holds.

        Args:
            flag_id: The flag's page id.

        Returns:
            The flag, or None when no maintenance flag has that id, a page of another type included.

        Raises:
            LibraryError: If the flag page does not parse.
        """
        found = await self._rows.fetch(FLAG_SQL, self._collection, flag_id)
        if not found or str(found[0]["type"]) != FLAG_PAGE_TYPE:
            return None
        return _flag_of(found[0])

    async def resolve_flag(self, flag_id: str, *, by: str, reason: str) -> None:
        """Resolve a maintenance flag: write `status: resolved`, who resolved it and why, and keep every other field.

        A resolved flag resolved again takes the new name and reason.

        Args:
            flag_id: The flag's page id.
            by: Who resolved it. Must not be empty.
            reason: Why. Must not be empty.

        Raises:
            ValueError: If `by` or `reason` is empty. Nothing is written.
            FlagUnknownError: If no maintenance flag has that id. Nothing is written.
            LibraryError: If the flag page does not parse, or the ingest refuses the page.
        """
        for name, given in (("by", by), ("reason", reason)):
            if not given.strip():
                msg = f"resolving {flag_id} needs a {name}: say who resolved it and why, then resolve it again."
                raise ValueError(msg)
        stored = await self.fetch_flag(flag_id)
        if stored is None:
            msg = f"no maintenance flag {flag_id!r} in collection {self._collection!r}. List the flags to find its id."
            raise FlagUnknownError(msg)
        await self._resolve(flag_id, stored, by=by, reason=reason)

    async def _resolve(self, resolved_id: str, stored: MaintenanceFlag, *, by: str, reason: str) -> None:
        """Write the flag `stored` holds back under `resolved_id`, resolved by `by` for `reason`, every other field kept."""
        await self._write_flag(resolved_id, stored.model_copy(update={"status": "resolved", "resolved_by": by, "decision_reason": reason}))

    async def _write_flag(self, written_id: str, flag: MaintenanceFlag) -> None:
        """Ingest `flag` as the page `written_id`, its empty fields left out, its two sections always written."""
        frontmatter: dict[str, Any] = {
            "id": written_id,
            "type": FLAG_PAGE_TYPE,
            "title": flag.title,
            "summary": flag.summary,
            "status": flag.status,
            "flag_kind": flag.kind,
            **{key: value for key in _FLAG_FIELDS if (value := getattr(flag, key))},
        }
        frontmatter["raised_at"] = flag.raised_at.isoformat()
        sections = [{"heading": "Evidence", "body": flag.evidence}, {"heading": "Proposed change", "body": flag.proposed_change}]
        try:
            await self._ingest({"frontmatter": frontmatter, "sections": sections}, collection=self._collection)
        except LibraryError:
            raise
        except Exception as exc:
            msg = f"the collection refused the maintenance flag {written_id!r}: {exc}"
            raise LibraryError(msg) from exc

    async def _supersede(self, parent_id: str, *, by: str) -> bool:
        """Link `parent_id` forward to `by`. False when there is no such page, it holds no workflow or step, or the write fails (warned)."""
        found = await self._rows.fetch(PAGE_SQL, parent_id)
        if not found:
            return False
        page = found[0]
        stored = page["frontmatter"]
        long_tail = dict(stored if isinstance(stored, Mapping) else json.loads(stored or "{}"))
        if "spec" not in long_tail and "step" not in long_tail:
            return False
        try:
            await self._rewrite(page, long_tail, superseded_by=by)
        except Exception as exc:
            warnings.warn(
                f"stored {by!r}, but could not link {parent_id!r} forward to it ({exc}). "
                f"Both versions will be offered until {parent_id!r} is linked forward. Saving it again writes the link.",
                SupersessionIncompleteWarning,
                stacklevel=3,
            )
            return False
        return True

    async def _rewrite(
        self,
        page: Mapping[str, Any],
        long_tail: Mapping[str, Any],
        *,
        title: str | None = None,
        summary: str | None = None,
        applicability: str | None = None,
        superseded_by: str | None = None,
    ) -> None:
        """Ingest a page read with `PAGE_SQL` again: `long_tail` as its kept keys, and each modelled key as stored unless given."""
        frontmatter: dict[str, Any] = {
            # The long tail is spread FIRST so the modelled keys always win: a stored jsonb that
            # somehow carried one would otherwise stomp them, the supersession link included.
            **long_tail,
            "id": str(page["id"]),
            "type": str(page["type"]),
            "title": str(page["title"]) if title is None else title,
            "summary": str(page["summary"] or "") if summary is None else summary,
            "status": str(page["status"]),
        }
        if superseded_by is not None:
            frontmatter["superseded_by"] = f"[[{superseded_by}]]"
        body = str(page["applicability"] or "") if applicability is None else applicability
        payload = {"frontmatter": frontmatter, "sections": [{"heading": "Applicability", "body": body}]}
        await self._ingest(payload, collection=self._collection)

    async def put_step(
        self,
        step: InlineStep,
        *,
        version: int,
        title: str,
        summary: str,
        applicability: str,
        provenance: Mapping[str, str],
        cases: Mapping[str, JsonValue] | None = None,
    ) -> str:
        """Save one reusable step version as its own page, `st.<name>.v<version>`, and return its id.

        A version holds one step content and refuses a different one. Saving an unchanged step writes nothing, except
        that a live version takes new cases in place, with no new version and no move of its content address. A new
        version links every live lower version of the name forward to it, and a link that fails warns with
        `SupersessionIncompleteWarning`. Saving the newest live version again links forward any live lower version a
        failed link left behind. A version above every saved version of the name, live, superseded or archived,
        hands the ingest one `ReusableStepSaved` with its page, so the event lands in the page's transaction or not at
        all. No other save raises it. It assumes one writer at a time, such as an import or a CLI.

        Args:
            step: The step to save. Its name and `version` choose the page.
            version: The step's version.
            title: The card's title. A version already saved keeps its own.
            summary: The card's summary. A version already saved keeps its own.
            applicability: When to use the step, written as the page's Applicability section. A version already
                saved keeps its own.
            provenance: `saved_by` names who saved it.
            cases: The step's cases, read back with `fetch_step_cases`. An empty mapping removes them, and None keeps
                whatever cases the version holds.

        Returns:
            The step's page id.

        Raises:
            StepVersionChangedError: If this version already holds a different step. Nothing is written.
            LibraryError: If the ingest refuses the page or its new cases.
        """
        step_id = step_page_id(step.name, version)
        saved = await self._rows.fetch(PAGE_SQL, step_id)
        if saved:
            await self._keep_saved_version(saved[0], step, cases)
            live = await self._live_step_versions(step.name)
            if live and live[-1][1] == step_id:
                for lower_id in (lower_id for lower, lower_id in live if lower < version):
                    await self._supersede(lower_id, by=step_id)
            return step_id
        saved_by = provenance.get("saved_by", "unknown")
        retired = tuple(lower_id for lower, lower_id in await self._live_step_versions(step.name) if lower < version)
        events: tuple[ReusableStepSaved, ...] = ()
        if version > await self._newest_step_version(step.name):
            step_hash = step_content_address(step)
            saved_event = ReusableStepSaved(
                step=step.name, version=version, step_id=step_id, step_hash=step_hash, saved_by=saved_by, superseded=retired
            )
            events = (saved_event,)
        frontmatter: dict[str, Any] = {
            "id": step_id,
            "type": STEP_PAGE_TYPE,
            "title": title,
            "summary": summary,
            "status": "draft",
            "step_version": version,
            "saved_by": saved_by,
            "step": step.model_dump(mode="json"),
        }
        if cases:
            frontmatter["cases"] = _cases_text(cases)
        try:
            await self._ingest(
                {"frontmatter": frontmatter, "sections": [{"heading": "Applicability", "body": applicability}]},
                collection=self._collection,
                events=events,
            )
        except LibraryError:
            raise
        except Exception as exc:
            msg = f"the collection refused {step_id!r}: {exc}"
            raise LibraryError(msg) from exc
        for lower_id in retired:
            await self._supersede(lower_id, by=step_id)
        return step_id

    async def _newest_step_version(self, name: str) -> int:
        """The highest version of the reusable step `name` any page holds, live, superseded or archived, else 0."""
        versions = [
            int(match["version"])
            for row in await self._rows.fetch(STEP_PAGES_SQL, self._collection, [name])
            if (match := _VERSIONED_STEP_ID.match(str(row["id"]))) is not None and match["name"] == name
        ]
        return max(versions, default=0)

    async def _keep_saved_version(self, page: Mapping[str, Any], step: InlineStep, cases: Mapping[str, JsonValue] | None) -> None:
        """Refuse a different step at a saved version, else bring a live version's cases in place up to `cases` when given."""
        step_id = str(page["id"])
        stored = page["frontmatter"]
        long_tail = dict(stored if isinstance(stored, Mapping) else json.loads(stored or "{}"))
        if "step" not in long_tail or step_content_address(_STEP.validate_python(long_tail["step"])) != step_content_address(step):
            raise StepVersionChangedError(step_id)
        if cases is None:
            return
        held = long_tail.get("cases")
        if (held == _cases_text(cases)) if cases else (held is None):
            return
        superseded = await self._rows.fetch(SUPERSEDED_SQL, step_id)
        if superseded and bool(superseded[0].get("superseded")):
            return
        long_tail.pop("cases", None)
        if cases:
            long_tail["cases"] = _cases_text(cases)
        try:
            await self._rewrite(page, long_tail)
        except LibraryError:
            raise
        except Exception as exc:
            msg = f"the collection refused to update the cases of {step_id!r}: {exc}"
            raise LibraryError(msg) from exc

    async def step_versions(self, name: str) -> tuple[StepVersion, ...]:
        """Every saved version of one reusable step, live, superseded or archived, lowest version first.

        Args:
            name: The reusable step's name.

        Returns:
            The versions, each with its id and content address. Empty when no version of `name` is saved.
        """
        return (await self.step_versions_of([name]))[name]

    async def step_versions_of(self, names: Sequence[str]) -> Mapping[str, tuple[StepVersion, ...]]:
        """Every saved version of each named reusable step, read in one query, each name's versions lowest first.

        The content address of each version is taken from the step its page holds.

        Args:
            names: The reusable step names.

        Returns:
            One entry per name, an empty tuple for a name with no saved version.
        """
        wanted = list(dict.fromkeys(names))
        if not wanted:
            return {}
        found: dict[str, list[StepVersion]] = {name: [] for name in wanted}
        for row in await self._rows.fetch(STEP_PAGES_SQL, self._collection, wanted):
            match = _VERSIONED_STEP_ID.match(str(row["id"]))
            if match is None or match["name"] not in found:
                continue
            stored = row["step"]
            step = _STEP.validate_python(stored if isinstance(stored, Mapping) else json.loads(stored))
            found[match["name"]].append(
                StepVersion(
                    name=match["name"],
                    version=int(match["version"]),
                    step_id=str(row["id"]),
                    step_hash=step_content_address(step),
                    step=step,
                )
            )
        return {name: tuple(sorted(versions, key=lambda saved: saved.version)) for name, versions in found.items()}

    async def fetch_step_cases(self, step_id: str) -> Mapping[str, JsonValue] | None:
        """The cases one reusable step version carries, as the save that wrote them gave them.

        A live, superseded or archived version is answered alike.

        Args:
            step_id: The step version's page id, `st.<name>.v<version>`.

        Returns:
            The cases, or None when the version holds none.

        Raises:
            LibraryError: If no page has that id, the page carries no reusable step, or its cases are not the JSON
                text of an object.
        """
        document = await self._frontmatter(step_id, kind="step")
        if "step" not in document:
            msg = f"{step_id!r} is a page in {self._collection!r} but not a reusable step: it carries no step document"
            raise LibraryError(msg)
        stored = document.get("cases")
        if stored is None:
            return None
        try:
            cases = json.loads(stored) if isinstance(stored, str) else None
        except json.JSONDecodeError as exc:
            msg = f"the cases on {step_id!r} are not JSON text ({exc}), the shape put_step writes"
            raise LibraryError(msg) from exc
        if not isinstance(cases, Mapping):
            msg = f"the cases on {step_id!r} are not the JSON text of an object, the shape put_step writes"
            raise LibraryError(msg)
        return cases


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
        words: dict[str, object] | None = None,
        asked_for: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Save one workflow as a page and report the id it now lives under."""
        written = await library.put(
            Workflow.model_validate(spec),
            title=title,
            summary=summary,
            applicability=when,
            provenance={"saved_by": by},
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
        """Save one reusable step as a page and report the id it now lives under.

        A version that already holds a different step is refused. Save the step again with a higher version.
        """
        try:
            written = await library.put_step(
                _STEP.validate_python(step), version=version, title=title, summary=summary, applicability=when, provenance={"saved_by": by}
            )
        except StepVersionChangedError as exc:
            msg = f"{exc.step_id} already holds different content. Save it again with a higher version."
            raise ModelRetry(msg) from exc
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
