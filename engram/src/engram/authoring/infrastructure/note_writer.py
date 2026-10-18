"""The authoring slice's page writer: it checks a payload against a knowledge base's grammar and writes it as a page file.

Before the checks, resolvable page ids and claim anchors in section bodies are written as wikilinks.
"""

from __future__ import annotations

import re

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import yaml

from core.files import atomic_create, atomic_write
from core.markdown import split_frontmatter
from pydantic import ValidationError

from engram.authoring.domain import AuthoringKnowledgeBase, CheckedNote, WrittenNote, swapped_locator_quotes
from engram.authoring.exceptions import NoteRefusedError
from engram.authoring.infrastructure.mirrors import mirror_text
from engram.indexing.contracts import validate_note
from engram.kb import contracts as kb

if TYPE_CHECKING:
    from collections.abc import Callable

    from engram.indexing.contracts import ValidationIssue
    from engram.kb.contracts import ClaimBlock, KnowledgeBaseNote, KnowledgeBaseSchema, PageTypeSpec


def _refuse_unsafe_id(page_id: str) -> None:
    """Refuse an id that is not safely a single file name once `.md` is appended to it."""
    # The id minus its prefix is the whole file name, so a separator or `..` would escape the folder.
    if "/" in page_id or "\\" in page_id:
        msg = f"id {page_id!r} contains a path separator. An id names one file, it is never a path"
        raise NoteRefusedError(msg)
    if ".." in page_id:
        msg = f"id {page_id!r} contains '..'. An id must not carry a directory-traversal segment"
        raise NoteRefusedError(msg)


def _stamped_created(payload: Mapping[str, object]) -> Mapping[str, object]:
    """`payload` with today's date as the frontmatter's `created` when the author left it out."""
    frontmatter = payload.get("frontmatter")
    if not isinstance(frontmatter, Mapping) or frontmatter.get("created"):
        return payload
    return {**payload, "frontmatter": {**frontmatter, "created": datetime.now(tz=UTC).date().isoformat()}}


def _id_aliased(payload: Mapping[str, object]) -> Mapping[str, object]:
    """`payload` with the page's own id leading the frontmatter's `aliases`."""
    # Obsidian resolves a wikilink by file name or alias only, and the file name drops the id's prefix.
    frontmatter = payload.get("frontmatter")
    if not isinstance(frontmatter, Mapping):
        return payload
    page_id = frontmatter.get("id")
    aliases = frontmatter.get("aliases", [])
    if not isinstance(page_id, str) or page_id == "" or not isinstance(aliases, list) or page_id in aliases:
        return payload
    return {**payload, "frontmatter": {**frontmatter, "aliases": [page_id, *aliases]}}


_HEADING_LINE = r"^ {0,3}#{1,6}(?=[ \t]|$)[^\n]*"
"""A markdown heading line written inside a section body."""

_PROTECTED_SPAN = re.compile(rf"```.*?```|`[^`\n]*`|\[\[[^\[\]]*\]\]|{_HEADING_LINE}", re.DOTALL | re.MULTILINE)
"""Body regions the bare-id pass never rewrites: fenced code, inline code, well-formed wikilinks and heading lines."""

_UNTOUCHED_SPAN = re.compile(rf"```.*?```|`[^`\n]*`|{_HEADING_LINE}", re.DOTALL | re.MULTILINE)
"""Body regions no linkifier pass ever rewrites: fenced code, inline code and heading lines."""

_WIKILINK = re.compile(r"\[\[([^\[\]|]+)(?:\|([^\[\]]+))?\]\]")
"""A well-formed wikilink: target, optional display text."""


def _id_pattern(schema: KnowledgeBaseSchema) -> re.Pattern[str] | None:
    """One pattern matching any of `schema`'s page-id shapes, optional brackets and anchor included, or `None` with no prefixes."""
    prefixes = sorted((page_type.id_prefix for page_type in schema.page_types if page_type.id_prefix), key=len, reverse=True)
    if not prefixes:
        return None
    alternation = "|".join(re.escape(prefix) for prefix in prefixes)
    return re.compile(
        rf"(?<![\w/.@-])(?P<open>\[{{1,2}})?(?P<id>(?:{alternation})[a-z0-9][a-z0-9-]*)(?P<anchor>#[\w^-]+(?:\.[\w^-]+)*)?(?P<close>\]{{1,2}})?"
    )


def _resolved_stem(page_id: str, *, schema: KnowledgeBaseSchema, knowledge_base_path: Path, own_id: str) -> str | None:
    """The file stem `page_id` resolves to, or `None` when no page of this knowledge base carries it."""
    # The page being written resolves before its file exists. Any other id needs `<stem>.md` under its declared roots.
    page_types = sorted((pt for pt in schema.page_types if pt.id_prefix), key=lambda pt: len(pt.id_prefix), reverse=True)
    page_type = next((pt for pt in page_types if page_id.startswith(pt.id_prefix)), None)
    if page_type is None:
        return None
    stem = _file_stem(page_id, page_type.id_prefix)
    if page_id == own_id:
        return stem
    for root in _declared_roots(page_type):
        root_path = (knowledge_base_path / root).resolve()
        if (root_path / f"{stem}.md").is_file() or (root_path.is_dir() and any(root_path.rglob(f"{stem}.md"))):
            return stem
    return None


def _linkified_text(text: str, pattern: re.Pattern[str], notes: list[str], resolve_stem: Callable[[str], str | None]) -> str:
    """`text` with every resolvable bare or mis-bracketed page id written as `[[stem|id]]`, each decision appended to `notes`."""

    def repl(match: re.Match[str]) -> str:
        opened, closed = match.group("open") or "", match.group("close") or ""
        if (opened, closed) == ("[[", "]]") or ((opened, closed) == ("[", "]") and match.string[match.end() : match.end() + 1] == "("):
            return match.group(0)
        page_id = match.group("id")
        stem = resolve_stem(page_id)
        if stem is None:
            notes.append(f"'{page_id}' reads as a page id but no page resolves. Left as written")
            return match.group(0)
        anchor = match.group("anchor") or ""
        linked = f"[[{stem}{anchor}|{page_id}{anchor}]]"
        notes.append(f"wrote '{match.group(0)}' as '{linked}'")
        return linked

    return pattern.sub(repl, text)


def _retargeted_links(text: str, notes: list[str], resolve_stem: Callable[[str], str | None]) -> str:
    """`text` with every wikilink aimed at a resolvable full page id retargeted to that page's file stem."""

    def repl(match: re.Match[str]) -> str:
        target, display = match.group(1), match.group(2)
        base, sep, anchor = target.partition("#")
        stem = resolve_stem(base)
        if stem is None or stem == base:
            return match.group(0)
        linked = f"[[{stem}{sep}{anchor}|{display or target}]]"
        notes.append(f"retargeted '{match.group(0)}' as '{linked}'")
        return linked

    return _WIKILINK.sub(repl, text)


def _outside_spans(body: str, spans: re.Pattern[str], rewrite: Callable[[str], str]) -> str:
    """`body` with `rewrite` applied between `spans` matches, which pass through byte-for-byte."""
    pieces: list[str] = []
    last = 0
    for protected in spans.finditer(body):
        pieces.append(rewrite(body[last : protected.start()]))
        pieces.append(protected.group(0))
        last = protected.end()
    pieces.append(rewrite(body[last:]))
    return "".join(pieces)


def _linkified_body(body: str, pattern: re.Pattern[str], notes: list[str], resolve_stem: Callable[[str], str | None]) -> str:
    """`body` after both passes: bare ids linkified outside code, links and heading lines, then full-id link targets rewritten to stems."""
    wrapped = _outside_spans(body, _PROTECTED_SPAN, lambda text: _linkified_text(text, pattern, notes, resolve_stem))
    return _outside_spans(wrapped, _UNTOUCHED_SPAN, lambda text: _retargeted_links(text, notes, resolve_stem))


_ANCHOR_TOKEN = re.compile(r"\^c\d+")
"""A claim-anchor mention: the token a claim line defines and prose cites."""

_ANCHOR_DEFINITION = re.compile(r"\^c\d+\s*$")
"""A line whose tail is an anchor definition. Such a line is never rewritten."""

_LINE_PROTECTED = re.compile(r"\[\[[^\[\]]*\]\]|`[^`\n]*`")
"""Within one line, the spans the anchor pass never rewrites: wikilinks and inline code."""


def _anchor_definitions(text: str) -> set[str]:
    """Every anchor `text` defines, by the end-of-line convention (`... ^c4`)."""
    return {match.group(0).strip() for line in text.split("\n") if (match := _ANCHOR_DEFINITION.search(line))}


class _AnchorIndex:
    """Answers whether a page (by file stem) defines a claim anchor, reading each page at most once."""

    def __init__(self, knowledge_base_path: Path, own_stem: str, own_anchors: set[str]) -> None:
        self._knowledge_base_path = knowledge_base_path
        self._own_stem = own_stem
        self._cache: dict[str, set[str]] = {own_stem: own_anchors} if own_stem else {}

    def defines(self, stem: str, anchor: str) -> bool:
        if stem not in self._cache:
            found = next((p for p in self._knowledge_base_path.rglob(f"{stem}.md") if not any(part.startswith(".") for part in p.parts)), None)
            self._cache[stem] = _anchor_definitions(found.read_text(encoding="utf-8")) if found is not None else set()
        return anchor in self._cache[stem]

    @property
    def own_stem(self) -> str:
        return self._own_stem


def _anchored_line(line: str, notes: list[str], index: _AnchorIndex) -> str:
    """`line` with each bare claim anchor linked to the page that defines it, or reported when none does."""
    if _ANCHOR_DEFINITION.search(line):
        return line
    pieces: list[str] = []
    last = 0
    stems_before: list[str] = []
    for protected in _LINE_PROTECTED.finditer(line):
        pieces.append(_anchored_gap(line[last : protected.start()], notes, index, stems_before))
        pieces.append(protected.group(0))
        if protected.group(0).startswith("[["):
            stems_before = [protected.group(0)[2:].partition("]]")[0].partition("|")[0].partition("#")[0], *stems_before]
        last = protected.end()
    pieces.append(_anchored_gap(line[last:], notes, index, stems_before))
    return "".join(pieces)


def _anchored_gap(text: str, notes: list[str], index: _AnchorIndex, stems_before: list[str]) -> str:
    """One between-links stretch of a line, every bare anchor in it attributed or reported."""
    # Attribution tries each earlier wikilink target on the line, nearest first, then the page being written.

    def repl(match: re.Match[str]) -> str:
        anchor = match.group(0)
        stem = next((s for s in (*stems_before, index.own_stem) if s and index.defines(s, anchor)), None)
        if stem is None:
            notes.append(f"bare claim anchor '{anchor}' cites no page that defines it. Write it as [[<page-stem>#{anchor}]]")
            return anchor
        linked = f"[[{stem}#{anchor}|{anchor}]]"
        notes.append(f"wrote '{anchor}' as '{linked}'")
        return linked

    return _ANCHOR_TOKEN.sub(repl, text)


def _anchored_body(body: str, notes: list[str], index: _AnchorIndex) -> str:
    """`body` with the anchor pass applied outside code and heading lines, line by line."""
    return _outside_spans(body, _UNTOUCHED_SPAN, lambda text: "\n".join(_anchored_line(line, notes, index) for line in text.split("\n")))


def _linkified(payload: Mapping[str, object], *, schema: KnowledgeBaseSchema, knowledge_base_path: Path) -> tuple[Mapping[str, object], tuple[str, ...]]:
    """`payload` with resolvable page ids in section bodies written as `[[stem|id]]` wikilinks, plus a note per rewrite."""
    pattern = _id_pattern(schema)
    sections = payload.get("sections")
    frontmatter = payload.get("frontmatter")
    own_id = frontmatter.get("id") if isinstance(frontmatter, Mapping) else None
    if pattern is None or not isinstance(sections, list):
        return payload, ()

    def resolve_stem(page_id: str) -> str | None:
        return _resolved_stem(page_id, schema=schema, knowledge_base_path=knowledge_base_path, own_id=own_id if isinstance(own_id, str) else "")

    own_stem = (resolve_stem(own_id) or "") if isinstance(own_id, str) else ""
    own_anchors: set[str] = set()
    for section in sections:
        if isinstance(section, Mapping) and isinstance(body := section.get("body"), str):
            own_anchors |= _anchor_definitions(body)
    index = _AnchorIndex(knowledge_base_path, own_stem, own_anchors)

    notes: list[str] = []
    rewritten: list[object] = []
    changed = False
    for section in sections:
        body = section.get("body") if isinstance(section, Mapping) else None
        if not isinstance(section, Mapping) or not isinstance(body, str) or body == "":
            rewritten.append(section)
            continue
        new_body = _anchored_body(_linkified_body(body, pattern, notes, resolve_stem), notes, index)
        changed = changed or new_body != body
        rewritten.append({**section, "body": new_body} if new_body != body else section)
    return ({**payload, "sections": rewritten} if changed else payload), tuple(dict.fromkeys(notes))


def _vocabulary_index(knowledge_base: AuthoringKnowledgeBase) -> kb.VocabularyIndex:
    """`knowledge_base`'s vocabulary, read fresh from its `_meta/vocabulary` pages."""
    vocabulary_dir = knowledge_base.path / "_meta" / "vocabulary"
    documents = [(p.name, p.read_text(encoding="utf-8")) for p in sorted(vocabulary_dir.glob("*.md"))] if vocabulary_dir.is_dir() else []
    return kb.parse_vocabulary(documents).index



def _file_stem(page_id: str, prefix: str) -> str:
    """The file name a page lands under: its id minus the page type's prefix, refused when nothing is left."""
    stem = page_id.removeprefix(prefix) if prefix else page_id
    if not stem:
        msg = f"id {page_id!r} is only its page_type's prefix, add the page's own name after it"
        raise NoteRefusedError(msg)
    return stem


def _digested_source(path: Path, link_field: str) -> str | None:
    """The source the page file at `path` names through `link_field`, empty when it names none, None when no file sits there."""
    if not path.is_file():
        return None
    try:
        frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return ""
    source = frontmatter.get(link_field)
    return source if isinstance(source, str) else ""


_WIKI_ROOT = "Wiki"


def _declared_roots(page_type: PageTypeSpec | None) -> tuple[str, ...]:
    """The roots an explicit `write` `path` may resolve under for one page type, `Wiki` when it declares none."""
    if page_type is None:
        return (_WIKI_ROOT,)
    roots = tuple(dict.fromkeys((*((page_type.folder,) if page_type.folder else ()), *page_type.folders)))
    return roots or (_WIKI_ROOT,)


_QUOTE_NOT_IN_SOURCE = "quote-not-in-source"
"""The index's rule for a quote its mirror lacks."""


def _with_locator_quotes_swapped(payload: Mapping[str, object], mirror: str) -> tuple[Mapping[str, object], tuple[int, ...]]:
    """`payload` with each locator-quote in its `claims` list swapped with its loc when the loc is in `mirror`."""
    claims = payload.get("claims")
    if not isinstance(claims, list) or not all(isinstance(claim, Mapping) for claim in claims):
        return payload, ()
    swapped, positions = swapped_locator_quotes(claims, lambda text: kb.quotes_in_mirror(text, mirror))
    return ({**payload, "claims": swapped} if positions else payload), positions


def _swap_warning(claim: ClaimBlock) -> str:
    """The warning that names one swapped claim, read off the claim as it now stands."""
    return f"claim {claim.anchor}: its quote held the locator {claim.loc!r} and its loc held the source's words, so the two were swapped"


_ONE_PARAGRAPH_RULE = "Keep each claim's text to one paragraph, with no blank line inside it, and each field value on one line"


def _unread_claim_line(unread: kb.UnreadClaim) -> str:
    """One refusal line for a claim that does not read back: the claim and the part that differs."""
    if unread.read_back is None:
        return f"claim {unread.anchor} does not read back from the page at all ({unread.written[:80]!r})"
    return f"claim {unread.anchor} does not read back as written: its {unread.part} {unread.written[:80]!r} reads back as {unread.read_back[:80]!r}"


def _refusal_line(issue: ValidationIssue, note: KnowledgeBaseNote) -> str:
    """One refusal line: the issue's message led by the claim it names, and for a quote the mirror lacks, the quote and the fix."""
    if issue.anchor is None:
        return issue.message
    line = f"claim {issue.anchor}: {issue.message}"
    if issue.rule == _QUOTE_NOT_IN_SOURCE:
        quote = next((claim.quote for claim in note.claims if claim.anchor == issue.anchor), None)
        line += f" ({quote!r}). Copy the quote word for word from one chunk read_source answers, and set loc to that chunk's loc"
    return line


class NoteWriter:
    """Validate an authoring payload against one knowledge base and write it under its page type's folder as `<id minus its prefix>.md`."""

    def __init__(self, knowledge_base: AuthoringKnowledgeBase) -> None:
        """Hold the knowledge base this writer validates and writes against.

        Args:
            knowledge_base: The knowledge base whose grammar checks each payload and whose folders receive each page.
        """
        self._target = knowledge_base

    def write(self, payload: Mapping[str, object], path: str = "") -> WrittenNote:
        """Render, parse back, validate and write the page. A page id already on disk is replaced, unless it digests another source.

        Args:
            payload: The authoring payload, with frontmatter, sections and claims.
            path: Where to place the page, relative to the knowledge base root and ending in `.md`. It must
                resolve under one of the page type's declared roots. Left empty, the page goes in the page
                type's own folder.

        Returns:
            The page's id, where it landed and any warnings about it.

        Raises:
            NoteRefusedError: The payload fails the grammar, a claim does not read back from the rendered page as
                written, its id is not a safe file name, the validation reports an error, a quote is not in the
                mirror the page names, `path` fails its own placement rule, or the page would replace the digest of
                a different source.
        """
        checked = self.check(payload, path)
        checked.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(checked.path, checked.rendered)
        return WrittenNote(id=checked.id, path=checked.path, warnings=checked.warnings)

    def create(self, checked: CheckedNote) -> WrittenNote:
        """Publish a checked note at a name nothing holds yet, and refuse outright if something does.

        The bytes published are the ones `checked` already carries, never a second rendering. Use
        `write` to replace an existing page.

        Args:
            checked: The note `check` passed.

        Returns:
            The page's id, where it landed and any warnings about it.

        Raises:
            FileExistsError: Something already sits at `checked.path`. It is left exactly as it is,
                whether it is an earlier copy of this same note or somebody's own page.
        """
        checked.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_create(checked.path, checked.rendered.encode("utf-8"))
        return WrittenNote(id=checked.id, path=checked.path, warnings=checked.warnings)

    def check(self, payload: Mapping[str, object], path: str = "") -> CheckedNote:
        """Run every check `write` runs (render, parse back, validation, placement) with nothing written.

        Every claim must read back from the rendered page as written, each part read the way the parser reads it. A
        claim whose text holds a blank line, or whose field value holds a line break, does not, and the page is
        refused naming every such claim.

        Resolvable page ids sitting bare (or mis-bracketed) in section bodies are written as
        `[[stem|id]]` wikilinks, and full-id link targets are retargeted to their file stems. Each
        rewrite or unresolved candidate is named in `warnings`. A heading line inside a body is never rewritten.

        When the knowledge base verifies quotes and the page names its mirror through the schema's `sources.key_field`,
        every quote must appear in that mirror. A claim in the payload's `claims` whose quote is only a locator and
        whose loc is the mirror's words has the two swapped first, named in `warnings`.

        Args:
            payload: The authoring payload, with frontmatter, sections and claims.
            path: Where to place the page, relative to the knowledge base root and ending in `.md`. Left
                empty, the page goes in the page type's own folder.

        Returns:
            The page's id, where it would land, its rendered text and any warnings about it.

        Raises:
            NoteRefusedError: The payload fails the grammar, a claim does not read back from the rendered page as
                written, its id is not a safe file name, the validation reports an error, a quote is not in the
                mirror the page names, `path` fails its own placement rule, or the page would replace the digest of
                a different source.
        """
        schema = self._target.schema
        prepared, link_notes = _linkified(_id_aliased(_stamped_created(payload)), schema=schema, knowledge_base_path=self._target.path)
        mirror = self._cited_mirror(prepared)
        swapped: tuple[int, ...] = ()
        if mirror is not None:
            prepared, swapped = _with_locator_quotes_swapped(prepared, mirror)
        try:
            built = kb.AuthoringPayload.model_validate({**prepared, "knowledge_base_schema": schema})
            rendered = kb.render(built, spec=schema)
            note = kb.parse(rendered, source_path="note.md", spec=schema)
        except (ValidationError, kb.NoteGrammarError, ValueError) as error:
            raise NoteRefusedError(str(error)) from error
        if unread := kb.claims_not_read_back(built, note):
            raise NoteRefusedError("\n".join((*(_unread_claim_line(claim) for claim in unread), _ONE_PARAGRAPH_RULE)))
        _refuse_unsafe_id(note.frontmatter.id)
        report = validate_note(
            note, self._registry(), mirror_text=mirror, spec=schema, path_root=self._path_root(), path_root_source=str(self._target.path)
        )
        if report.n_errors > 0:
            raise NoteRefusedError("\n".join(_refusal_line(issue, note) for issue in report.errors))
        page_type = schema.page_type(note.frontmatter.type)
        location = self._placed(page_type, note.frontmatter.id, path)
        self._refuse_replacing_another_digest(note.frontmatter.id, location, rendered)
        swap_warnings = tuple(_swap_warning(note.claims[position]) for position in swapped if position < len(note.claims))
        page_warnings = (*link_notes, *swap_warnings, *(issue.message for issue in report.warnings if issue.page_id == note.frontmatter.id))
        return CheckedNote(id=note.frontmatter.id, path=location, rendered=rendered, warnings=page_warnings)

    def digest_id_for(self, page_type: str, page_id: str, source_id: str) -> str:
        """`page_id`, numbered `-2`, `-3` and on until no other page sits where the digest would land.

        A digest of `source_id` itself keeps its id, so a source digested again replaces its own page.

        Args:
            page_type: The page type the digest is written as.
            page_id: The id the digest would take, its page type's prefix included.
            source_id: The source the digest names through the schema's `sources.digest_link_field`.

        Returns:
            The first id whose file is absent or already digests `source_id`.

        Raises:
            NoteRefusedError: `page_id` is only its page type's prefix.
        """
        link_field = self._target.schema.sources.digest_link_field
        spec = self._target.schema.page_type(page_type)
        candidate, number = page_id, 1
        while (held_by := _digested_source(self._placed(spec, candidate, ""), link_field)) is not None and held_by != source_id:
            number += 1
            candidate = f"{page_id}-{number}"
        return candidate

    def _refuse_replacing_another_digest(self, page_id: str, location: Path, rendered: str) -> None:
        """Refuse a page that would replace the digest of a different source sitting at `location`."""
        link_field = self._target.schema.sources.digest_link_field
        held_by = _digested_source(location, link_field)
        source = split_frontmatter(rendered)[0].get(link_field)
        if held_by and isinstance(source, str) and source and source != held_by:
            msg = (
                f"{page_id} is already the digest of {held_by!r}, and this page digests {source!r}. Writing it would replace "
                "that page. Give this digest its own id: digest_skeleton names a free one"
            )
            raise NoteRefusedError(msg)

    def _placed(self, page_type: PageTypeSpec | None, page_id: str, path: str) -> Path:
        """Where the page lands: `path` resolved under the page type's declared roots when given, else the page type's own folder."""
        if path:
            return self._explicit_path(page_type, path)
        folder = self._target.path / (page_type.folder if page_type is not None and page_type.folder else _WIKI_ROOT)
        stem = _file_stem(page_id, page_type.id_prefix if page_type is not None else "")
        return folder / f"{stem}.md"

    def _explicit_path(self, page_type: PageTypeSpec | None, path: str) -> Path:
        """`path` resolved under one of the page type's declared roots, refused otherwise."""
        if not path.endswith(".md"):
            msg = f"path {path!r} must end in .md"
            raise NoteRefusedError(msg)
        if PurePosixPath(path).is_absolute():
            msg = f"path {path!r} must be relative to the knowledge base root"
            raise NoteRefusedError(msg)
        knowledge_base_root = self._target.path
        resolved = (knowledge_base_root / path).resolve()
        roots = _declared_roots(page_type)
        for root in roots:
            if resolved.is_relative_to((knowledge_base_root / root).resolve()):
                return resolved
        named = ", ".join(roots)
        msg = f"path {path!r} does not resolve under this page_type's declared roots ({named})"
        raise NoteRefusedError(msg)

    def _registry(self) -> kb.VocabularyIndex:
        return _vocabulary_index(self._target)

    def _path_root(self) -> Path:
        return self._target.path if self._target.schema.path_root == "knowledge_base" else self._target.path.parent

    def _cited_mirror(self, payload: Mapping[str, object]) -> str | None:
        """The text of the mirror `payload` names through the schema's `sources.key_field`, when the knowledge base verifies quotes."""
        sources = self._target.schema.sources
        frontmatter = payload.get("frontmatter")
        if not sources.quote_verify or not sources.key_field or not isinstance(frontmatter, Mapping):
            return None
        key = frontmatter.get(sources.key_field)
        if not isinstance(key, str) or not key.strip():
            return None
        return mirror_text(self._target.path, key.strip())
