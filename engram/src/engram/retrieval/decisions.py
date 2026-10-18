"""Decisions on disk: list a knowledge base's open decisions, and move a page between its page_type's declared statuses.

`list_decisions` reads every page type whose spec declares a `DecisionSpec`.
"""

from __future__ import annotations

import json
import re

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from core.files import atomic_write
from pydantic import BaseModel, ConfigDict

from engram.kb import contracts as kb

if TYPE_CHECKING:
    from collections.abc import Sequence

    from engram.kb.contracts import KnowledgeBaseSchema, PageTypeSpec

_STATUS_LINE = re.compile(r"^status:\s*\S+\s*$", re.MULTILINE)
# The whole `decision_reason:` entry: its own line plus every following line indented with a space or tab,
# so a folded or block-scalar reason is replaced whole.
_DECISION_REASON_LINE = re.compile(r"^decision_reason:[^\n]*(?:\n[ \t]+[^\n]*)*", re.MULTILINE)


class DecidedPage(BaseModel):
    """One page a decision was (or can be) made on, as the owner reads it."""

    model_config = ConfigDict(frozen=True)

    id: str
    page_type: str
    title: str
    summary: str
    status: str
    links: Mapping[str, tuple[str, ...]]
    path: str


class DecisionsListing(BaseModel):
    """Every open-decision page that parsed cleanly, plus the filenames of any that did not."""

    model_config = ConfigDict(frozen=True)

    rows: tuple[DecidedPage, ...] = ()
    skipped: tuple[str, ...] = ()


def page_type_paths(knowledge_base_path: Path, page_type: PageTypeSpec) -> list[Path]:
    """Every markdown file under one page type's declared folders and explicit files, deduplicated.

    Reads `page_type.folder` and every entry of `page_type.folders` recursively, relative to the
    knowledge base root, plus every entry of `page_type.files`. A page type declaring no folder yields
    only its files. A file reached more than one way is returned once, in first-seen order.

    Args:
        knowledge_base_path: The knowledge base root.
        page_type: The page type whose folders and files are read.

    Returns:
        The existing markdown files, in first-seen order.
    """
    seen: dict[str, Path] = {}
    for raw in (page_type.folder or "", *page_type.folders):
        folder = raw.strip("/")
        if not folder:
            continue
        base = knowledge_base_path / folder
        if base.is_dir():
            for path in sorted(base.glob("**/*.md")):
                if path.is_file():
                    seen.setdefault(path.as_posix(), path)
    for rel in page_type.files:
        path = knowledge_base_path / rel
        if path.is_file():
            seen.setdefault(path.as_posix(), path)
    return list(seen.values())


def _link_targets(front: kb.BaseFrontmatter, key: str) -> tuple[str, ...]:
    """The `UnitLink` targets of one link-list field in `front`, empty when the field carries none."""
    raw = (front.model_extra or {}).get(key, ())
    if not isinstance(raw, (tuple, list)):
        return ()
    return tuple(link.target for link in raw if isinstance(link, kb.UnitLink))


def _read_row(path: Path, spec: KnowledgeBaseSchema, page_type: PageTypeSpec) -> DecidedPage | None:
    """Parse one page of `page_type` into a `DecidedPage`, or `None` when it can't be read, doesn't parse, or is a different page_type."""
    try:
        text = path.read_text(encoding="utf-8")
        note = kb.parse(text, source_path=path.name, spec=spec)
    except (kb.NoteGrammarError, OSError, UnicodeDecodeError):
        return None
    front = note.frontmatter
    if front.type != page_type.page_type:
        return None
    links = {field: _link_targets(front, field) for field in sorted(page_type.link_list_edge_fields)}
    return DecidedPage(
        id=front.id,
        page_type=front.type,
        title=front.title,
        summary=front.summary or "",
        status=front.status,
        links=links,
        path=str(path),
    )


def _find_page(knowledge_base_path: Path, spec: KnowledgeBaseSchema, page_id: str) -> tuple[DecidedPage, PageTypeSpec] | None:
    """The first page carrying `page_id` across every page type's folders, with its page type."""
    for page_type in spec.page_types:
        for path in page_type_paths(knowledge_base_path, page_type):
            row = _read_row(path, spec, page_type)
            if row is not None and row.id == page_id:
                return row, page_type
    return None


def list_decisions(knowledge_base_path: Path, spec: KnowledgeBaseSchema) -> DecisionsListing:
    """Every page of a decision page type (`spec.decision_page_types`) currently at that page_type's open status, newest file first.

    A file that cannot be read, is not UTF-8, or fails the note grammar is skipped, never raised. Its
    filename lands in the result's `skipped` tuple.

    Args:
        knowledge_base_path: The knowledge base root.
        spec: The knowledge base's schema, which declares the decision page types.

    Returns:
        The open-decision pages, newest file first, and the filenames that were skipped.
    """
    rows: list[tuple[float, DecidedPage]] = []
    skipped: list[str] = []
    for page_type in spec.decision_page_types:
        decision = page_type.decision
        if decision is None:  # narrows for pyright, spec.decision_page_types only holds page types with one
            continue
        for path in page_type_paths(knowledge_base_path, page_type):
            row = _read_row(path, spec, page_type)
            if row is None:
                skipped.append(path.name)
                continue
            if row.status == decision.open:
                rows.append((path.stat().st_mtime, row))
    rows.sort(key=lambda dated: dated[0], reverse=True)
    return DecisionsListing(rows=tuple(row for _, row in rows), skipped=tuple(skipped))


def _decision_reason_line(reason: str) -> str:
    """`decision_reason: <reason>` as one physical YAML-safe line, newlines turned into spaces."""
    normalized = " ".join(reason.splitlines())
    return yaml.safe_dump(
        {"decision_reason": normalized}, default_flow_style=False, sort_keys=False, allow_unicode=True, width=1_000_000
    ).rstrip("\n")


def _normalized_targets(targets: Sequence[str]) -> tuple[str, ...]:
    """Page ids out of `targets`, each accepted as `id` or `[[id]]`, blanks dropped."""
    ids = (target.strip().removeprefix("[[").removesuffix("]]").strip() for target in targets)
    return tuple(page_id for page_id in ids if page_id)


def _field_line(field: str, targets: tuple[str, ...]) -> str:
    """`<field>: ["[[id]]", ...]` as one flow-style line the field's block-list regex finds again."""
    return f"{field}: [" + ", ".join(json.dumps(f"[[{page_id}]]") for page_id in targets) + "]"


def _field_block_pattern(field: str) -> re.Pattern[str]:
    """The whole `<field>:` entry: its own line plus any block-list item lines (`- "[[id]]"`) indented under it."""
    return re.compile(rf"^{re.escape(field)}:[^\n]*(?:\n[ \t]*-[ \t][^\n]*)*", re.MULTILINE)


def _field_targets(held: tuple[str, ...], given: Sequence[str], *, add: bool) -> tuple[str, ...]:
    """The ids a field holds after a write: `given` alone, or with `add` the `held` ids then the new given ones, each once."""
    normalized = _normalized_targets(given)
    return tuple(dict.fromkeys((*held, *normalized))) if add else normalized


def _with_status_and_reason(head: str, status: str, reason: str) -> str:
    """`head` with its status line set to `status` and its `decision_reason:` line set to `reason`, each only when not empty."""
    if status:
        head = _STATUS_LINE.sub(f"status: {status}", head, count=1)
    if reason:
        line = _decision_reason_line(reason)
        if _DECISION_REASON_LINE.search(head):
            head = _DECISION_REASON_LINE.sub(lambda _match: line, head, count=1)
        else:
            head = _STATUS_LINE.sub(lambda m: f"{m.group(0)}\n{line}", head, count=1)
    return head


def decide_page(  # noqa: PLR0913 (status, links, reason and add are the parts of one decision, bundling them would hide the call contract)
    knowledge_base_path: Path,
    spec: KnowledgeBaseSchema,
    page_id: str,
    status: str,
    *,
    links: Mapping[str, Sequence[str]] | None = None,
    reason: str = "",
    add: bool = False,
) -> DecidedPage:
    """Move one page to `status`, write the given link fields, and return the page as it now reads.

    `page_id` is searched for across every page type's folders, not only the decision page types.
    `status` and `links` are checked against the found page's own page type, so a page type with no
    `decision` block accepts any status in its effective status set. An empty `status` keeps the page's
    current status, so a call can write only `links` and `reason`.

    Args:
        knowledge_base_path: The knowledge base root.
        spec: The knowledge base's schema.
        page_id: The id of the page to move.
        status: The status to set, one of the page type's effective statuses. Empty keeps the current one.
        links: Maps a link-list `edge_fields` name to the ids that field should now hold, bare or as
            `[[id]]`. Each named field's whole entry is replaced, unless `add` is set. A field left out keeps its entry.
        reason: When non-empty, replaces the `decision_reason:` line, or inserts one after `status:`.
            When empty, any existing line is left untouched.
        add: Whether each named field keeps the ids it holds and gains the given ones after them, each id once,
            instead of being replaced.

    Returns:
        The page as it now reads.

    Raises:
        LookupError: No page under any of `spec`'s page types' folders carries `page_id` (absent,
            or present but unparseable), or its frontmatter has no `status:` line the rewrite can find.
        ValueError: A non-empty `status` is outside the page's page_type's effective status set, or a `links` key
            names a field that is not one of that page_type's link-list `edge_fields`.
    """
    given_links = links or {}
    found = _find_page(knowledge_base_path, spec, page_id)
    if found is None:
        msg = f"no page {page_id!r} under {knowledge_base_path}"
        raise LookupError(msg)
    row, page_type = found

    allowed_status = page_type.effective_status_values
    if status and status not in allowed_status:
        msg = f"status {status!r} is not one of page_type {page_type.page_type!r}'s statuses: {sorted(allowed_status)}"
        raise ValueError(msg)
    allowed_fields = page_type.link_list_edge_fields
    unknown_fields = set(given_links) - allowed_fields
    if unknown_fields:
        msg = f"link field(s) {sorted(unknown_fields)} are not on page_type {page_type.page_type!r} (allowed: {sorted(allowed_fields)})"
        raise ValueError(msg)

    path = Path(row.path)
    text = path.read_text(encoding="utf-8")
    head, sep, body = text.partition("\n---\n")
    if not sep:
        msg = f"{path} has no frontmatter block"
        raise LookupError(msg)
    if not _STATUS_LINE.search(head):
        msg = f"{path} has no status: line to rewrite"
        raise LookupError(msg)
    head = _with_status_and_reason(head, status, reason)
    new_links = dict(row.links)
    for field, targets in given_links.items():
        normalized = _field_targets(row.links.get(field, ()), targets, add=add)
        field_line = _field_line(field, normalized)
        block = _field_block_pattern(field)
        if block.search(head):
            head = block.sub(lambda _match, line=field_line: line, head, count=1)
        else:
            head = _STATUS_LINE.sub(lambda m, line=field_line: f"{m.group(0)}\n{line}", head, count=1)
        new_links[field] = normalized
    atomic_write(path, head + sep + body)
    return row.model_copy(update={"status": status or row.status, "links": new_links})
