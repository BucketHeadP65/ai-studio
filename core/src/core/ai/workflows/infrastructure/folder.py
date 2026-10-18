"""A workflow library kept as a folder of markdown files, with no database and no dependencies.

`FolderLibrary` is the library for code that holds one. `folder_toolset` offers the same folder as file tools,
and `FOLDER` is the `LibraryVocabulary` the contract port reaches them through. Search ranks by shared words with
a stable tie-break, so the same query always returns the same order.
"""

from __future__ import annotations

import re
import warnings

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError
from pydantic_ai.toolsets import FunctionToolset

from core.ai.workflows.contract_port import LibraryVocabulary
from core.ai.workflows.domain.document import dump_spec_document, load_spec_document
from core.ai.workflows.domain.fingerprint import WorkflowFingerprint
from core.ai.workflows.domain.intent import WorkflowIntent
from core.ai.workflows.domain.lineage import Lineage
from core.ai.workflows.domain.presentation import WorkflowPresentation, verify_presentation
from core.ai.workflows.domain.vocabulary import Workflow
from core.ai.workflows.exceptions import LibraryError, WorkflowUnknownError
from core.ai.workflows.read_models import SavedWorkflow

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

_WORD = re.compile(r"[a-z0-9]+")
_TRUSTED = frozenset({"stable", "verified", "attested"})
_MIN_TOKEN_LENGTH = 3
"""Tokens shorter than this are noise words ("a", "of", "to") and never count as overlap."""


def _tokens(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.lower()) if len(word) >= _MIN_TOKEN_LENGTH}


def _version_of(workflow_id: str, name: str) -> int | None:
    """`workflow_id`'s version when it is one of `name`'s own (`name@<n>`), else None."""
    owner, _, version = workflow_id.rpartition("@")
    return int(version) if owner == name and version.isdigit() else None


_EPOCH = datetime.fromtimestamp(0, UTC)
"""The `archived_at` a value that will not parse is read as: the oldest possible, so it sorts last among archived cards."""


def _parse_archived_at(frontmatter: Mapping[str, object], *, workflow_id: str) -> datetime | None:
    """The frontmatter's archive time (naive read as UTC), None without the key, `_EPOCH` with a warning if unparsable."""
    if "archived_at" not in frontmatter:
        return None
    recorded = frontmatter["archived_at"]
    if isinstance(recorded, str):
        try:
            parsed = datetime.fromisoformat(recorded)
        except ValueError:
            pass
        else:
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    warnings.warn(f"{workflow_id!r} has an archived_at that will not parse ({recorded!r}). Treating it as archived at the epoch.", stacklevel=2)
    return _EPOCH


def _presentation_of(frontmatter: Mapping[str, object]) -> WorkflowPresentation | None:
    """The presentation a file's frontmatter carries, or None when it has none or it no longer parses."""
    recorded = frontmatter.get("presentation")
    if recorded is None:
        return None
    try:
        return WorkflowPresentation.model_validate(recorded)
    except ValidationError:
        return None


def _intent_of(frontmatter: Mapping[str, object]) -> WorkflowIntent | None:
    """The intent a file's frontmatter carries, or None when it has none or it no longer parses."""
    recorded = frontmatter.get("intent")
    if recorded is None:
        return None
    try:
        return WorkflowIntent.model_validate(recorded)
    except ValidationError:
        return None


class FolderLibrary:
    """Workflows as `<name>-v<version>.md` files in one folder."""

    def __init__(self, root: Path, *, spec_key: str = "workflow") -> None:
        """Keep workflow files in `root`, creating the folder when it is missing.

        Args:
            root: The folder the workflow files live in.
            spec_key: The frontmatter key each file holds its workflow under.
        """
        self._root = root
        self._spec_key = spec_key
        root.mkdir(parents=True, exist_ok=True)

    async def search(self, task: str, *, limit: int = 5) -> Sequence[SavedWorkflow]:
        """Rank every parseable, live workflow against the task by the words they share, best first.

        Words shorter than three characters are ignored. A workflow sharing no word is left out.

        Args:
            task: The task description to match.
            limit: The most cards to return.

        Returns:
            The cards, most shared words first and then by id, each scored by its number of shared words.
        """
        wanted = _tokens(task)
        scored: list[tuple[int, str, SavedWorkflow]] = []
        for candidate in self._load_all():
            if candidate.archived_at is not None:
                continue
            haystack = _tokens(f"{candidate.title} {candidate.summary} {candidate.applicability}")
            overlap = len(wanted & haystack)
            if overlap:
                scored.append((overlap, candidate.id, candidate))
        scored.sort(key=lambda row: (-row[0], row[1]))
        return tuple(
            SavedWorkflow(
                id=found.id,
                title=found.title,
                summary=found.summary,
                applicability=found.applicability,
                score=float(overlap),
                provisional=found.provisional,
                metadata=found.metadata,
                presentation=found.presentation,
                intent=found.intent,
            )
            for overlap, _, found in scored[:limit]
        )

    async def catalog(self, *, limit: int = 50) -> Sequence[SavedWorkflow]:
        """Every live workflow, by title and then id, unranked.

        Args:
            limit: The most cards to return.

        Returns:
            The cards, each scored `0.0`.
        """
        live = sorted((card for card in self._load_all() if card.archived_at is None), key=lambda card: (card.title, card.id))
        return tuple(
            SavedWorkflow(
                id=card.id,
                title=card.title,
                summary=card.summary,
                applicability=card.applicability,
                score=0.0,
                provisional=card.provisional,
                metadata=card.metadata,
                presentation=card.presentation,
                intent=card.intent,
            )
            for card in live[:limit]
        )

    async def catalog_size(self) -> int:
        """Count the live workflows in this folder, the set `catalog` lists from.

        Returns:
            The number of live workflows.
        """
        return sum(1 for card in self._load_all() if card.archived_at is None)

    async def fetch_workflow(self, workflow_id: str) -> Workflow:
        """The workflow inside one file, validated on the way out.

        Args:
            workflow_id: The workflow's `name@version` id.

        Returns:
            The validated workflow.

        Raises:
            WorkflowUnknownError: If no file holds that workflow.
        """
        path = self._path_for(workflow_id)
        if not path.is_file():
            msg = f"no workflow {workflow_id!r} in {self._root}"
            raise WorkflowUnknownError(msg)
        return load_spec_document(path.read_text(encoding="utf-8"), key=self._spec_key).workflow

    async def fetch_fingerprint(self, workflow_id: str) -> WorkflowFingerprint | None:
        """The environment stamp the file carries, or None for a workflow written without one.

        Args:
            workflow_id: The workflow's `name@version` id.

        Returns:
            The fingerprint, or None when the file has none or it no longer parses.

        Raises:
            WorkflowUnknownError: If no file holds that workflow.
        """
        path = self._path_for(workflow_id)
        if not path.is_file():
            msg = f"no workflow {workflow_id!r} in {self._root}"
            raise WorkflowUnknownError(msg)
        recorded = load_spec_document(path.read_text(encoding="utf-8"), key=self._spec_key).frontmatter.get("fingerprint")
        if recorded is None:
            return None
        try:
            return WorkflowFingerprint.model_validate(recorded)
        except ValidationError:
            return None  # a stamp that no longer parses counts as none, so staleness is unknown

    async def fetch_lineage(self, workflow_id: str) -> Lineage | None:
        """The recorded diff that produced this workflow, or None when it is not an adaptation.

        Args:
            workflow_id: The workflow's `name@version` id.

        Returns:
            The lineage, or None when the file has none or it no longer parses.

        Raises:
            WorkflowUnknownError: If no file holds that workflow.
        """
        path = self._path_for(workflow_id)
        if not path.is_file():
            msg = f"no workflow {workflow_id!r} in {self._root}"
            raise WorkflowUnknownError(msg)
        recorded = load_spec_document(path.read_text(encoding="utf-8"), key=self._spec_key).frontmatter.get("lineage")
        if recorded is None:
            return None
        try:
            return Lineage.model_validate(recorded)
        except ValidationError:
            return None

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
        """Write the workflow to its own `<name>-v<version>.md` file with status `draft`.

        Every other version's file stays on disk, and saving the same version again replaces its file.
        A presentation and an intent are written only when given.

        Args:
            workflow: The workflow to keep. Its name and version choose the file.
            title: The card's title.
            summary: The card's summary.
            applicability: When to use the workflow, also written as the file's Applicability section.
            provenance: Extra frontmatter keys, such as `saved_by`, written as given.
            fingerprint: The environment stamp to keep, or None.
            lineage: The adaptation that produced this workflow, or None.
            presentation: The author's words about the workflow, or None.
            intent: What the person who wanted the workflow asked for, or None.

        Returns:
            The workflow's `name@version` id.

        Raises:
            PresentationRefusedError: If the presentation names an input or an artifact the
                workflow does not declare. Nothing reaches the disk in that case.
        """
        if presentation is not None:
            verify_presentation(workflow, presentation)
        workflow_id = f"{workflow.name}@{workflow.version}"
        path = self._path_for(workflow_id)
        frontmatter: dict[str, object] = {
            "title": title,
            "summary": summary,
            "applicability": applicability,
            "status": "draft",
            **provenance,
        }
        if fingerprint is not None:
            frontmatter["fingerprint"] = fingerprint.model_dump(mode="json")
        if lineage is not None:
            frontmatter["lineage"] = lineage.model_dump(mode="json")
        if presentation is not None:
            frontmatter["presentation"] = presentation.model_dump(mode="json")
        if intent is not None:
            frontmatter["intent"] = intent.model_dump(mode="json")
        body = f"## Applicability\n\n{applicability.strip()}\n"
        path.write_text(dump_spec_document(workflow, frontmatter=frontmatter, body=body, key=self._spec_key), encoding="utf-8")
        return workflow_id

    async def redescribe(
        self, workflow_id: str, *, title: str, summary: str, applicability: str, presentation: WorkflowPresentation | None = None
    ) -> None:
        """Rewrite the card's words in the file, leaving the workflow and every other field untouched.

        A presentation given replaces the one in the frontmatter whole, after it is checked against the
        file's own workflow. None keeps whatever the file already carries.

        Args:
            workflow_id: The workflow's `name@version` id.
            title: The new title.
            summary: The new summary.
            applicability: The new applicability, also rewritten as the file's Applicability section.
            presentation: The new author's words, or None to keep the file's own.

        Raises:
            LibraryError: If the library has no such workflow.
            PresentationRefusedError: If the presentation names an input or an artifact the
                workflow does not declare. Nothing reaches the disk in that case.
        """
        path = self._path_for(workflow_id)
        if not path.is_file():
            msg = f"no workflow {workflow_id!r} in {self._root}"
            raise LibraryError(msg)
        document = load_spec_document(path.read_text(encoding="utf-8"), key=self._spec_key)
        if presentation is not None:
            verify_presentation(document.workflow, presentation)
        frontmatter = {**document.frontmatter, "title": title, "summary": summary, "applicability": applicability}
        if presentation is not None:
            frontmatter["presentation"] = presentation.model_dump(mode="json")
        body = f"## Applicability\n\n{applicability.strip()}\n"
        path.write_text(dump_spec_document(document.workflow, frontmatter=frontmatter, body=body, key=self._spec_key), encoding="utf-8")

    async def set_archived(self, workflow_id: str, *, archived: bool) -> None:
        """Set or clear `archived_at` in the file's frontmatter, leaving the workflow and every other field untouched.

        Archiving an archived workflow keeps its first `archived_at`, and restoring a live one does nothing.

        Args:
            workflow_id: The workflow's `name@version` id.
            archived: True to archive the workflow, False to restore it.

        Raises:
            LibraryError: If the library has no such workflow.
        """
        path = self._path_for(workflow_id)
        if not path.is_file():
            msg = f"no workflow {workflow_id!r} in {self._root}"
            raise LibraryError(msg)
        document = load_spec_document(path.read_text(encoding="utf-8"), key=self._spec_key)
        frontmatter = dict(document.frontmatter)
        if archived == (frontmatter.get("archived_at") is not None):
            return
        if archived:
            frontmatter["archived_at"] = datetime.now(UTC).isoformat()
        else:
            frontmatter.pop("archived_at", None)
        body = f"## Applicability\n\n{str(frontmatter.get('applicability', '')).strip()}\n"
        path.write_text(dump_spec_document(document.workflow, frontmatter=frontmatter, body=body, key=self._spec_key), encoding="utf-8")

    async def archived(self, *, limit: int = 50) -> Sequence[SavedWorkflow]:
        """The archived cards, newest archive first.

        Args:
            limit: The most cards to return.

        Returns:
            The archived cards, each with `archived_at` set.
        """
        stamped: list[tuple[datetime, SavedWorkflow]] = [
            (card.archived_at, card) for card in self._load_all() if card.archived_at is not None
        ]
        stamped.sort(key=lambda row: row[0], reverse=True)
        return tuple(card for _, card in stamped[:limit])

    async def fetch_card(self, workflow_id: str) -> SavedWorkflow | None:
        """The card in one file's frontmatter, read without scanning the folder.

        An archived workflow is answered too, with `archived_at` set.

        Args:
            workflow_id: The workflow's `name@version` id.

        Returns:
            The card, or None when no file holds that id or the file does not parse.
        """
        path = self._path_for(workflow_id)
        if not path.is_file():
            return None
        try:
            document = load_spec_document(path.read_text(encoding="utf-8"), key=self._spec_key)
        except (ValueError, OSError):
            return None
        front = document.frontmatter
        status = str(front.get("status", "draft"))
        return SavedWorkflow(
            id=workflow_id,
            title=str(front.get("title", document.workflow.name)),
            summary=str(front.get("summary", document.workflow.description)),
            applicability=str(front.get("applicability", "")),
            provisional=status not in _TRUSTED,
            archived_at=_parse_archived_at(front, workflow_id=workflow_id),
            metadata={"path": str(path), "status": status},
            presentation=_presentation_of(front),
            intent=_intent_of(front),
        )

    async def locate(self, name: str, version: int) -> str | None:
        """The id of the file holding `name` at `version`, or None when there is no such file.

        Args:
            name: The workflow's name.
            version: The version to find.

        Returns:
            The `name@version` id, or None.
        """
        workflow_id = f"{name}@{version}"
        return workflow_id if self._path_for(workflow_id).is_file() else None

    async def current(self, workflow_id: str) -> str:
        """The newest unarchived version of the workflow `workflow_id` names, or `workflow_id` itself.

        Itself for an id that is not `name@version`, and for a name with no unarchived version.
        A folder records no supersession, so every unarchived version is live.

        Args:
            workflow_id: The workflow's `name@version` id.

        Returns:
            The `name@version` id of the newest live version, or `workflow_id`.
        """
        name, at, _ = workflow_id.partition("@")
        if not at:
            return workflow_id
        versions = [version for card in self._load_all() if card.archived_at is None and (version := _version_of(card.id, name)) is not None]
        return f"{name}@{max(versions)}" if versions else workflow_id

    def _path_for(self, workflow_id: str) -> Path:
        return self._root / f"{workflow_id.replace('@', '-v')}.md"

    def _load_all(self) -> list[SavedWorkflow]:
        found: list[SavedWorkflow] = []
        for path in sorted(self._root.glob("*.md")):
            try:
                document = load_spec_document(path.read_text(encoding="utf-8"), key=self._spec_key)
            except (ValueError, OSError):
                continue  # a file that does not parse is left out and stays on disk as it is
            front = document.frontmatter
            workflow = document.workflow
            status = str(front.get("status", "draft"))
            workflow_id = f"{workflow.name}@{workflow.version}"
            found.append(
                SavedWorkflow(
                    id=workflow_id,
                    title=str(front.get("title", workflow.name)),
                    summary=str(front.get("summary", workflow.description)),
                    applicability=str(front.get("applicability", "")),
                    provisional=status not in _TRUSTED,
                    archived_at=_parse_archived_at(front, workflow_id=workflow_id),
                    metadata={"path": str(path), "status": status},
                    presentation=_presentation_of(front),
                    intent=_intent_of(front),
                )
            )
        return found


# --- the same directory, reached through the contract port ---------------------------------------


def folder_toolset(root: Path, *, spec_key: str = "workflow") -> FunctionToolset[object]:
    """Four file tools over one folder of workflow files: `find_files`, `list_files`, `read_file` and `write_file`.

    The tools are named and shaped for files and know nothing of the contract port. `FOLDER` maps the port onto them.

    Args:
        root: The folder the workflow files live in.
        spec_key: The frontmatter key each file holds its workflow under.

    Returns:
        The toolset, which an agent of any dependency type can hold.
    """
    library = FolderLibrary(root, spec_key=spec_key)
    # `object` deps: contravariance makes this the one toolset type every agent can hold.
    toolset: FunctionToolset[object] = FunctionToolset()

    async def find_files(words: str, most: int = 5) -> dict[str, object]:
        """Files whose text overlaps `words`, best first."""
        found = await library.search(words, limit=most)
        return {"files": [{"name": card.id, "title": card.title, "summary": card.summary, "when": card.applicability} for card in found]}

    async def list_files(most: int = 50) -> dict[str, object]:
        """Every live file, by title then id, in the row shape `find_files` answers in, plus the total."""
        found = await library.catalog(limit=most)
        return {
            "total": await library.catalog_size(),
            "files": [{"name": card.id, "title": card.title, "summary": card.summary, "when": card.applicability} for card in found],
        }

    async def read_file(name: str) -> dict[str, object]:
        """One file's workflow, as data, with the environment stamp it was written under."""
        stamp = await library.fetch_fingerprint(name)
        return {
            "name": name,
            "spec": (await library.fetch_workflow(name)).model_dump(mode="json"),
            "fingerprint": stamp.model_dump(mode="json") if stamp is not None else None,
        }

    async def write_file(
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
        """Write one file and report the name it landed under."""
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
        return {"wrote": written}

    # One `add_function` call per tool, not a loop: a loop would unify the signatures and
    # publish the widened one instead of each tool's own parameters.
    toolset.add_function(find_files, takes_ctx=False)
    toolset.add_function(list_files, takes_ctx=False)
    toolset.add_function(read_file, takes_ctx=False)
    toolset.add_function(write_file, takes_ctx=False)
    return toolset


FOLDER = LibraryVocabulary(
    search_tool="find_files",
    fetch_tool="read_file",
    search_args=lambda task, limit: {"words": task, "most": limit},
    fetch_args=lambda workflow_id: {"name": workflow_id},
    to_cards=lambda raw: tuple(
        SavedWorkflow(
            id=str(row["name"]),
            title=str(row.get("title") or row["name"]),
            summary=str(row.get("summary") or ""),
            applicability=str(row.get("when") or ""),
        )
        for row in (raw.get("files") or ())
    ),
    to_spec=lambda raw: Workflow.model_validate(raw["spec"]),
    to_fingerprint=lambda raw: WorkflowFingerprint.model_validate(raw["fingerprint"]) if raw.get("fingerprint") else None,
    catalog_tool="list_files",
    catalog_args=lambda limit: {"most": limit},
    catalog_total=lambda raw: raw.get("total"),
    put_tool="write_file",
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
    to_id=lambda raw: str(raw["wrote"]),
)
"""The contract port's vocabulary for `folder_toolset`: which tool answers each library verb, and how its arguments and results map."""
