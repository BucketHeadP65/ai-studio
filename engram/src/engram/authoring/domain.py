"""The authoring slice's values: a proposed note, a knowledge base a step writes into, what a reindex found, and entity names.

A `NoteDraft` is a proposal and nothing more. It names no file, no knowledge base and no act. A host
shows it to a person, the person edits it or does not, and only then does a writer turn it into a
page under an id its destination mints. A draft may not carry a field its destination assigns.
Everything else a schema declares passes through untouched, including the page's `status`.
It also holds the rule that tells a locator from a quote and the rule entity resolution matches names by.
"""

from __future__ import annotations

import re

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal, NamedTuple, Self

from core.markdown import heading_for_slug
from pydantic import BaseModel, ConfigDict, JsonValue, StringConstraints, model_validator

if TYPE_CHECKING:
    from pathlib import Path

    from engram.kb.contracts import KnowledgeBaseSchema, PageTypeSpec

ASSIGNED_FRONTMATTER: frozenset[str] = frozenset({"id", "type", "title", "summary"})
"""Frontmatter keys a draft may not carry, because its destination fills them in.

`id` is minted by the destination, and `type`, `title` and `summary` are the draft's own fields. A
draft repeating one of them in `frontmatter` is refused, never silently overruled.
"""

DraftText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
"""One short named thing in a draft: trimmed, never blank, and at most 300 characters."""


class NoteSection(BaseModel):
    """One section of a proposed note: the heading it sits under and the prose under it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    heading: DraftText
    body: str = ""


class NoteDraft(BaseModel):
    """A whole proposed note: which page type, what it says, and the fields its schema declares.

    A valid draft is not yet a valid page. The destination's own schema decides whether this page
    type exists, whether these sections are the ones it declares and whether `frontmatter` carries
    what it requires, when the page is written.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    page_type: DraftText
    title: DraftText
    summary: str = ""
    frontmatter: Mapping[str, JsonValue] = {}
    sections: tuple[NoteSection, ...] = ()

    @model_validator(mode="after")
    def _frontmatter_assigns_nothing_the_destination_owns(self) -> Self:
        """Refuse a draft whose frontmatter answers a question its destination answers."""
        claimed = sorted(ASSIGNED_FRONTMATTER & set(self.frontmatter))
        if claimed:
            named = ", ".join(claimed)
            msg = f"frontmatter carries {named}, which the destination assigns. Drop it from frontmatter"
            raise ValueError(msg)
        return self

    def payload(self, note_id: str) -> dict[str, JsonValue]:
        """This draft as the authoring payload a `NoteWriter` checks.

        The assigned fields come first and the draft's own frontmatter after them.

        Args:
            note_id: The page id the destination minted for this draft.

        Returns:
            The payload, with `frontmatter` and `sections` as its two keys.
        """
        return {
            "frontmatter": {
                "id": note_id,
                "type": self.page_type,
                "title": self.title,
                "summary": self.summary,
                **dict(self.frontmatter),
            },
            "sections": [{"heading": section.heading, "body": section.body} for section in self.sections],
        }


@dataclass(frozen=True)
class AuthoringKnowledgeBase:
    """One knowledge base a step may write into: its name, collection, checkout and schema."""

    name: str
    collection: str
    path: Path
    schema: KnowledgeBaseSchema


_MEMORY_COLLECTION_SUFFIX = "-agent-memory"


def collection_of(knowledge_base: AuthoringKnowledgeBase) -> str:
    """The collection `knowledge_base`'s agent-memory store writes to and reads from.

    The name is the knowledge base's own collection with `-agent-memory` appended, so every call
    answers the same name.

    Args:
        knowledge_base: The knowledge base whose agent-memory collection to name.

    Returns:
        The agent-memory collection name.
    """
    return f"{knowledge_base.collection}{_MEMORY_COLLECTION_SUFFIX}"


@dataclass(frozen=True)
class RegisteredKnowledgeBases:
    """Every registered knowledge base that shares its documentation-authoring tools, and whether the read succeeded.

    `reachable` is `False` only when the settings file exists but could not be read or parsed. A host
    tells the agent so rather than crashing. A settings file that does not exist, or one that lists
    no qualifying knowledge base, reports `reachable=True` with an empty `knowledge_bases`.
    """

    knowledge_bases: tuple[AuthoringKnowledgeBase, ...]
    reachable: bool


class ReindexFinding(NamedTuple):
    """One validation error a reindex found on a page other than the one a tool just wrote."""

    page_id: str
    rule: str
    message: str


REINDEX_FINDINGS_SHOWN = 5
"""How many `ReindexFinding` rows a `ReindexResult` names before folding the rest into "and N more"."""


class ReindexResult(BaseModel):
    """What one `Reindex` call found: clean, validation errors elsewhere, no index at all, or an index deferred.

    `detail` is a plain sentence a caller can use on its own. `findings` is only non-empty for
    `"validation_errors"`, capped at `REINDEX_FINDINGS_SHOWN`. `findings_total` is the true count, so
    a caller can name how many more there were beyond the cap. `"deferred"` means the write stands and
    the knowledge base is indexed once when the workflow run that made it ends, so search does not find
    it before then.
    """

    model_config = ConfigDict(frozen=True)

    outcome: Literal["clean", "validation_errors", "not_indexed", "deferred"]
    detail: str
    findings: tuple[ReindexFinding, ...] = ()
    findings_total: int = 0


_VALIDATION_ERROR_LINE = re.compile(r"^  ERROR (?P<page_id>\S+?)(?: \S+)?: (?P<rule>[\w-]+): (?P<message>.*)$", re.MULTILINE)
_VALIDATION_SUMMARY_LINE = re.compile(r"Validation: (?P<count>\d+) error")


def reindex_result_from_subprocess(knowledge_base: AuthoringKnowledgeBase, code: int, tail: str) -> ReindexResult:
    """The `ReindexResult` for a `studio index` subprocess's exit code and its captured output tail.

    Only the run's own errors count: the `ERROR` lines after the tail's `"Validation: N error(s)"` line,
    or every `ERROR` line when the tail no longer carries it. A zero exit is `"clean"`. A non-zero exit
    with at least one such line is `"validation_errors"`, its findings read from those lines and its
    count from the `"Validation"` line when present, else from how many lines survived. A non-zero exit
    whose `"Validation"` line counts zero errors is `"clean"`, because the exit reports errors standing
    on pages the run did not change. Any other non-zero exit is `"not_indexed"`, with `detail` naming
    the exit code and the tail.

    Args:
        knowledge_base: The knowledge base the subprocess indexed.
        code: The subprocess's exit code.
        tail: The last part of the subprocess's output, possibly empty.

    Returns:
        The outcome of the index run.
    """
    clean = ReindexResult(outcome="clean", detail=f"reindexed {knowledge_base.path}")
    if code == 0:
        return clean
    summaries = list(_VALIDATION_SUMMARY_LINE.finditer(tail))
    summary = summaries[-1] if summaries else None
    matches = list(_VALIDATION_ERROR_LINE.finditer(tail[summary.end() :] if summary else tail))
    if matches:
        findings = tuple(
            ReindexFinding(page_id=m["page_id"], rule=m["rule"], message=m["message"]) for m in matches[:REINDEX_FINDINGS_SHOWN]
        )
        total = int(summary["count"]) if summary else len(matches)
        return ReindexResult(
            outcome="validation_errors", detail=f"reindexed {knowledge_base.path} with {total} validation error(s)", findings=findings, findings_total=total
        )
    if summary is not None and int(summary["count"]) == 0:
        return clean
    msg = f"studio index for {knowledge_base.name} failed with exit {code}"
    return ReindexResult(outcome="not_indexed", detail=f"{msg}, output {tail}" if tail else msg)


@dataclass(frozen=True)
class WrittenNote:
    """Where a note landed."""

    id: str
    path: Path
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class CheckedNote:
    """A payload that passed every write-time check, with where it would land and nothing written."""

    id: str
    path: Path
    rendered: str
    warnings: tuple[str, ...]


_LOCATOR_PART = (
    r"(?:§\s*\d+(?:\.\d+)*"
    r"|(?:section|appendix)\s+(?:\d+|[a-z])(?:\.\d+)*"
    r"|(?:sec|app)\.\s*(?:\d+|[a-z])(?:\.\d+)*"
    r"|pp?\.\s*\d+(?:\s*[-–]\s*\d+)?"
    r"|pages?\s+\d+(?:\s*[-–]\s*\d+)?"
    r"|(?:table|figure)\s+\d+[a-z]?"
    r"|(?:tab|fig)\.\s*\d+[a-z]?)"
)
_LOCATOR = re.compile(rf"{_LOCATOR_PART}(?:\s*,\s*{_LOCATOR_PART})*", re.IGNORECASE)
"""One or more locators, comma-joined: section paths, sections, appendices, pages, tables and figures."""

_QUOTE_PAIR = 2

_PASSAGE_WORDS = 5
"""The fewest words a loc must hold to be taken for the quoted passage."""


def _bare(text: str) -> str:
    """`text` trimmed, with one pair of wrapping double quotes removed when nothing inside is a double quote."""
    trimmed = text.strip()
    if len(trimmed) >= _QUOTE_PAIR and trimmed[0] == trimmed[-1] == '"' and '"' not in trimmed[1:-1]:
        return trimmed[1:-1].strip()
    return trimmed


def is_locator(text: str) -> bool:
    """Whether `text` is only a locator, such as `§4.1`, `p. 4`, `Section 3`, `Table 2` or `§5.1, Table 2`.

    Case, the surrounding whitespace and one pair of wrapping double quotes are ignored. A phrase that
    only starts with a locator, such as `Table 2 shows the gap`, is not one.

    Args:
        text: A claim's quote as its author wrote it.

    Returns:
        True when the whole text is one or more comma-joined locators.
    """
    bare = _bare(text)
    return bool(bare) and _LOCATOR.fullmatch(bare) is not None


def swapped_locator_quotes(
    claims: Sequence[Mapping[str, object]], in_mirror: Callable[[str], bool]
) -> tuple[list[Mapping[str, object]], tuple[int, ...]]:
    """`claims` with quote and loc exchanged on each claim whose quote is only a locator and whose loc is a passage `in_mirror` finds.

    A loc is a passage when it holds at least `_PASSAGE_WORDS` words. The exchanged loc is the bare locator. A claim whose quote or loc is not a non-empty string is kept as it is.

    Args:
        claims: A payload's claims, in order.
        in_mirror: Answers whether a text appears word for word in the source mirror.

    Returns:
        The claims in the same order, and the zero-based positions of the claims exchanged.
    """
    out: list[Mapping[str, object]] = []
    positions: list[int] = []
    for position, claim in enumerate(claims):
        quote, loc = claim.get("quote"), claim.get("loc")
        if isinstance(quote, str) and isinstance(loc, str) and len(loc.split()) >= _PASSAGE_WORDS and is_locator(quote) and in_mirror(loc):
            out.append({**claim, "quote": loc, "loc": _bare(quote)})
            positions.append(position)
        else:
            out.append(claim)
    return out, tuple(positions)


class EntityName(BaseModel):
    """One name a page mentions, as entity resolution takes it: the name, its entity type, its other names and a summary."""

    model_config = ConfigDict(frozen=True, extra="forbid", use_attribute_docstrings=True)

    name: str
    """The name as the source writes it. A new entity page takes it as its title."""
    entity_type: str
    """One of the entity types the knowledge base's schema declares."""
    aliases: list[str] = []
    """Other names for the same entity, such as an abbreviation or another spelling."""
    summary: str = ""
    """One sentence saying what the entity is. A new entity page carries it as its summary and in its first section."""


_NAME_SEPARATORS = re.compile(r"[\W_]+")
"""Runs of punctuation, symbols and spaces, each of which entity resolution reads as one space."""

_LEADING_ARTICLE = "the "


def normalize_entity_name(name: str) -> str:
    """`name` as entity resolution compares it: case folded, punctuation read as spaces, and a leading "the" dropped.

    Args:
        name: A name as a page or a source writes it.

    Returns:
        The words of `name` joined by single spaces. Two names match when these are equal and not empty.
    """
    words = _NAME_SEPARATORS.sub(" ", name.casefold()).strip()
    return words.removeprefix(_LEADING_ARTICLE)


def aliases_with(title: str, aliases: Sequence[str], names: Iterable[str]) -> list[str]:
    """`aliases` followed by each of `names` that entity resolution does not already match to `title` or an earlier alias.

    Args:
        title: The entity page's title.
        aliases: The page's aliases, kept as they are and in order.
        names: The names to add, in order.

    Returns:
        The aliases with the new names appended, trimmed, none of them blank once normalized.
    """
    seen = {normalize_entity_name(name) for name in (title, *aliases)}
    merged = list(aliases)
    for name in names:
        key = normalize_entity_name(name)
        if key and key not in seen:
            seen.add(key)
            merged.append(name.strip())
    return merged


class KnownEntities:
    """Every entity page entity resolution can match a name to, by the normalized form of each of the page's names.

    A normalized name keeps the first page added under it, so add every title before any alias for a title to win.
    """

    def __init__(self) -> None:
        self._ids: dict[str, str] = {}
        self._pages: set[str] = set()

    def add(self, page_id: str, names: Iterable[str]) -> None:
        """Record `page_id` as a page and each of `names` as naming it, unless an earlier page already holds that name.

        Args:
            page_id: The entity page's id.
            names: The page's title or aliases, as written.
        """
        self._pages.add(page_id)
        for name in names:
            key = normalize_entity_name(name)
            if key:
                self._ids.setdefault(key, page_id)

    def match(self, name: str) -> str | None:
        """The id of the entity page `name` matches, or `None` when it matches none.

        Args:
            name: A name as a page or a source writes it.

        Returns:
            The page id, or `None`.
        """
        key = normalize_entity_name(name)
        return self._ids.get(key) if key else None

    def unknown(self, names: Iterable[str]) -> list[str]:
        """Each of `names` that matches no page, in order, leaving out the ones blank once normalized.

        Args:
            names: Names as a page or a source writes them.

        Returns:
            The names no page holds yet.
        """
        return [name for name in names if normalize_entity_name(name) and self.match(name) is None]

    def has_page(self, page_id: str) -> bool:
        """Whether a page with this id was added, whatever names it holds.

        Args:
            page_id: An entity page id.

        Returns:
            True when `add` recorded the id.
        """
        return page_id in self._pages


class AliasMerge(NamedTuple):
    """What merging names into one entity page's aliases did: the page's id and the names it added."""

    page_id: str
    added: tuple[str, ...]


_DEFAULT_OPENING_STATUS = "active"


def opening_status(page_type: PageTypeSpec | None) -> str:
    """The status a new page of `page_type` opens at: its first declared status, `active` when it declares none.

    Args:
        page_type: The page type, or `None` when the schema does not declare it.

    Returns:
        The status value.
    """
    return page_type.status_values[0] if page_type is not None and page_type.status_values else _DEFAULT_OPENING_STATUS


@dataclass
class NewEntityPage:
    """One entity page entity resolution is about to create, with every name one call gave it.

    `names` starts with the first name, which becomes the title, and holds no name another page holds.
    """

    id: str
    entity: EntityName
    names: list[str]

    def fold(self, names: Iterable[str]) -> None:
        """Take `names` as more names of this page.

        Args:
            names: Names of the call that resolve to this page, none of them held by another page.
        """
        self.names.extend(names)

    def payload(self, page_type: PageTypeSpec, claims_section: str | None) -> dict[str, JsonValue]:
        """This page as the authoring payload a `NoteWriter` checks.

        The status follows `opening_status`. The summary fills the page type's first canonical section other
        than `claims_section`, and no section when it declares none.

        Args:
            page_type: The page type the schema keeps its entities in.
            claims_section: The slug of the section the renderer fills from claims, never written here.

        Returns:
            The payload, with `frontmatter` and `sections` as its two keys.
        """
        title = self.entity.name.strip()
        summary = self.entity.summary.strip()
        first = next((section for section in page_type.canonical_sections if section.slug != claims_section), None)
        sections: list[JsonValue] = [{"heading": first.heading or heading_for_slug(first.slug), "body": summary}] if first is not None else []
        aliases: list[JsonValue] = list(aliases_with(title, [], self.names))
        return {
            "frontmatter": {
                "id": self.id,
                "type": page_type.page_type,
                "title": title,
                "summary": summary,
                "status": opening_status(page_type),
                "entity_type": self.entity.entity_type,
                "aliases": aliases,
            },
            "sections": sections,
        }
