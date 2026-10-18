"""The write side of a knowledge base for a step that acts: a note, a source request, the list of what is undigested, and the mirror read."""

from __future__ import annotations

import asyncio
import json
import os
import re

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit
from uuid import uuid4

from core.markdown import heading_for_slug, split_frontmatter
from pydantic import JsonValue, ValidationError
from pydantic_ai import ModelRetry

from engram.agent_memory.contracts import page_id_of
from engram.authoring.domain import EntityName, NewEntityPage, opening_status
from engram.authoring.exceptions import NoteRefusedError
from engram.authoring.infrastructure.entity_pages import EntityPages
from engram.authoring.infrastructure.note_writer import NoteWriter, _vocabulary_index
from engram.indexing.contracts import section_loc
from engram.kb.contracts import (
    NOTES_DIR,
    NOTES_SUFFIX,
    KnowledgeBaseSchema,
    LinkClass,
    NoteGrammarError,
    NotesSelector,
    PagesSelector,
    SourceQueuePolicy,
    SourcesSelector,
    SourcesSpec,
    add_reply,
    load_sheet,
    notes_home,
    open_threads,
    payload_of,
    read_policy,
    save_sheet,
    set_status,
    sidecar_for,
)
from engram.kb.contracts import write_policy as write_policy_page
from engram.retrieval import contracts as retrieval
from engram.sources.contracts import (
    ConnectorNotInstalledError,
    SourceQueueDecision,
    SourceQueueStatus,
    SourceType,
    arxiv_id_of,
    claimants_for,
    classify_url,
    connector_class,
    doi_of,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from core.ai.retrieval.contracts import SqlGateway

    from engram.agent_memory.contracts import EngramMemoryStore
    from engram.authoring.domain import AuthoringKnowledgeBase, CheckedNote, KnownEntities, ReindexResult
    from engram.authoring.internal_contracts import Reindex
    from engram.kb.contracts import ClaimGrammarSpec, DerivationEntry, Note, PageTypeSpec, RelationDecl
    from engram.sources.contracts import ConnectorSource, SourceQueue, SourceQueueEntry


def _undigested_sql(sources: SourcesSpec, *, picked: bool = False) -> str:
    """The `list_undigested` query through `sources`'s own field names, narrowed to `:source_ids` with no limit when `picked`."""
    narrowed = "\n  AND sr.source_id = ANY(:source_ids)" if picked else ""
    limited = "" if picked else "\nLIMIT :limit"
    return f"""
SELECT sr.source_id, sr.provider, sr.title, coalesce(sr.meta->>'item_type', '') AS item_type
FROM kb.sources_raw sr
WHERE sr.collection = :collection{narrowed}
  AND NOT EXISTS (
    SELECT 1 FROM kb.pages p
    WHERE p.collection = sr.collection
      AND (p.frontmatter->>'{sources.digest_link_field}' = sr.source_id OR p.frontmatter->>'{sources.key_field}' = sr.provider_key)
  )
ORDER BY sr.synced_at DESC NULLS LAST{limited}
"""  # noqa: S608 (the two interpolated names are frontmatter JSONB keys already restricted to [a-z][a-z0-9_]* by SourcesSpec's own field validators, the two clauses are fixed text, never raw input, and every value stays a bound parameter)


def _recent_digests_sql(sources: SourcesSpec) -> str:
    """The `list_recent_digests` query, naming a digest through `sources.digest_link_field`."""
    return f"""
SELECT p.id AS digest_id, p.page_type, p.title
FROM kb.pages p
WHERE p.collection = :collection
  AND p.frontmatter ? '{sources.digest_link_field}'
  AND p.status <> 'skipped'
  AND p.created >= (now() - make_interval(hours => :since_hours))::date
ORDER BY p.created DESC, p.id
LIMIT :limit
"""  # noqa: S608 (the interpolated name is a frontmatter JSONB key already restricted to [a-z][a-z0-9_]* by SourcesSpec's own field validator, never raw input, and every value stays a bound parameter)

_SKIPPED = "skipped"
"""The status of a skipped digest page, the one the compounding batch and the recent-digests list leave out."""

_SOURCE_DIGESTS_CAP = 500


def _page_source_sql(sources: SourcesSpec) -> str:
    """The `skip_digest` lookup of one page: its id and the source it names through `sources.digest_link_field`."""
    return f"""
SELECT p.id, coalesce(p.frontmatter->>'{sources.digest_link_field}', '') AS source_id
FROM kb.pages p
WHERE p.collection = :collection AND p.id = :page_id
"""  # noqa: S608 (the interpolated name is a frontmatter JSONB key already restricted to [a-z][a-z0-9_]* by SourcesSpec's own field validator, never raw input, and every value stays a bound parameter)


def _source_digests_sql(sources: SourcesSpec) -> str:
    """Every page naming one source through `sources.digest_link_field`, with its page type, by id."""
    return f"""
SELECT p.id, p.page_type
FROM kb.pages p
WHERE p.collection = :collection AND p.frontmatter->>'{sources.digest_link_field}' = :source_id
ORDER BY p.id
"""  # noqa: S608 (the interpolated name is a frontmatter JSONB key already restricted to [a-z][a-z0-9_]* by SourcesSpec's own field validator, never raw input, and every value stays a bound parameter)

_COMPOUNDING_PAGES_DERIVED_CLAUSE = """
  AND NOT EXISTS (
    SELECT 1 FROM kb.edges e
    JOIN kb.pages q ON q.collection = :collection AND q.id = CASE WHEN e.src_id = p.id THEN e.dst_id ELSE e.src_id END
    WHERE e.src_collection = :collection AND e.dst_collection = :collection
      AND (e.src_id = p.id OR e.dst_id = p.id)
      AND q.page_type = ANY(:to_kinds)
  )"""
"""A page already joined by an edge, in either direction, to a page whose page type is in the entry's `to` is derived.

`kb.edges` carries unit types (`page`, `section`), so the other endpoint's page type comes from
`kb.pages`."""

_COMPOUNDING_PAGES_WITHIN_HOURS_CLAUSE = " AND p.created >= (now() - make_interval(hours => :within_hours))::date"

_COMPOUNDING_PAGES_STATUS_CLAUSE = " AND p.status = :status"

_COMPOUNDING_PAGES_ORDER_CLAUSE = " ORDER BY p.created DESC NULLS LAST, p.id LIMIT :limit"

_PAGES_OF_TYPE_SQL = """
SELECT p.id AS page_id, p.page_type, p.title, p.status
FROM kb.pages p
WHERE p.collection = :collection AND p.page_type = :page_type
"""

_PAGES_OF_TYPE_STATUS_CLAUSE = " AND p.status = :status"

_PAGES_OF_TYPE_LACKING_LINK_CLAUSE = """
  AND NOT EXISTS (
    SELECT 1 FROM kb.edges e
    WHERE e.src_collection = :collection AND e.src_id = p.id AND e.rel = :lacking_link
  )"""
"""A page with an outgoing edge of the relation `lacking_link` in its own collection is left out."""

_PAGES_OF_TYPE_ORDER_CLAUSE = " ORDER BY p.id LIMIT :limit"

_PAGE_PATH_SQL = """
SELECT m.path
FROM kb.index_manifest m
WHERE m.collection = :collection AND m.page_id = :page_id
ORDER BY m.path
LIMIT 1
"""
"""The knowledge-base-relative file one indexed page was read from."""


def _compounding_pages_select(sources: SourcesSpec) -> str:
    """The head of every pages-plane compounding query: each page with its source, its file and its stored errors."""
    return f"""
SELECT p.id AS page_id, p.page_type, p.title, p.status, p.created,
       coalesce(p.frontmatter->>'{sources.digest_link_field}', '') AS source_id,
       coalesce((SELECT m.path FROM kb.index_manifest m
                 WHERE m.collection = p.collection AND m.page_id = p.id ORDER BY m.path LIMIT 1), '') AS path,
       coalesce((SELECT array_agg(DISTINCT v.rule ORDER BY v.rule) FROM kb.validation_store v
                 WHERE v.collection = p.collection AND v.page_id = p.id AND v.severity = 'error'), ARRAY[]::text[]) AS findings
FROM kb.pages p
WHERE p.collection = :collection AND p.status <> 'skipped'
"""  # noqa: S608 (the interpolated name is a frontmatter JSONB key already restricted to [a-z][a-z0-9_]* by SourcesSpec's own field validator, never raw input, and every value stays a bound parameter)


def _compounding_pages_sql(selector: PagesSelector, sources: SourcesSpec) -> str:
    """The pages-plane compounding query for one page type, composed from constant fragments."""
    sql = _compounding_pages_select(sources) + " AND p.page_type = :page_type" + _COMPOUNDING_PAGES_DERIVED_CLAUSE
    if selector.within_hours is not None:
        sql += _COMPOUNDING_PAGES_WITHIN_HOURS_CLAUSE
    if selector.status is not None:
        sql += _COMPOUNDING_PAGES_STATUS_CLAUSE
    return sql + _COMPOUNDING_PAGES_ORDER_CLAUSE


def _compounding_named_pages_sql(sources: SourcesSpec) -> str:
    """The pages-plane query for named pages: exactly those ids among the entry's page types, newest first then by id."""
    return _compounding_pages_select(sources) + " AND p.page_type = ANY(:page_types) AND p.id = ANY(:page_ids) ORDER BY p.created DESC NULLS LAST, p.id"


def _page_row(row: Mapping[str, object]) -> dict[str, object]:
    """One pages-plane row as `compounding_work` answers it, `findings` as sorted distinct rule names."""
    findings = row.get("findings")
    rules = sorted({str(rule) for rule in findings}) if isinstance(findings, (list, tuple)) else []
    shaped: dict[str, object] = {key: str(row.get(key) or "") for key in ("page_id", "page_type", "title", "status", "source_id", "path")}
    shaped["findings"] = rules
    return shaped


def _open_note_count(sidecar: Path) -> int:
    """How many of `sidecar`'s open, unarchived note threads it carries (0 for a missing or malformed sidecar)."""
    return len(open_threads(load_sheet(sidecar)))


def _compounding_notes_rows(knowledge_base: AuthoringKnowledgeBase, selector: NotesSelector, cap: int) -> list[dict[str, object]]:
    """Files under `selector.folder` whose sidecar carries an open note thread, in path order, capped at `cap`."""
    home = notes_home(knowledge_base.path)
    notes_dir = home / NOTES_DIR
    scope = (knowledge_base.path / selector.folder) if selector.folder else knowledge_base.path
    found: list[tuple[str, int]] = []
    if notes_dir.is_dir():
        for sidecar in notes_dir.rglob(f"*{NOTES_SUFFIX}"):
            if not sidecar.is_file():
                continue
            under_notes = sidecar.relative_to(notes_dir)
            annotated_path = home / under_notes.with_name(under_notes.name[: -len(NOTES_SUFFIX)])
            if not annotated_path.is_relative_to(scope):
                continue
            open_notes = _open_note_count(sidecar)
            if open_notes == 0:
                continue
            found.append((str(annotated_path.relative_to(knowledge_base.path)), open_notes))
    found.sort(key=lambda item: item[0])
    rows: list[dict[str, object]] = []
    for path, count in found[:cap]:
        rows.append({"path": path, "open_notes": count})
    return rows


_SOURCE_SQL = """
SELECT sr.source_id, sr.provider, sr.title, coalesce(sr.meta->>'item_type', '') AS item_type,
       rc.id AS chunk_id, rc.section_path::text AS section_path, rc.heading, rc.seq, rc.body,
       count(rc.id) OVER () AS chunk_count,
       count(rc.id) FILTER (WHERE rc.opens_section) OVER () AS section_count
FROM kb.sources_raw sr
LEFT JOIN (
    SELECT c.collection, c.source_id, c.id, c.section_path, c.heading, c.seq, c.body,
           lag(c.seq) OVER w IS NULL
           OR c.heading IS DISTINCT FROM lag(c.heading) OVER w
           OR c.section_path IS DISTINCT FROM lag(c.section_path) OVER w AS opens_section
    FROM kb.raw_chunks c
    WHERE c.collection = :collection AND c.source_id = :source_id
    WINDOW w AS (ORDER BY c.seq)
) rc ON rc.collection = sr.collection AND rc.source_id = sr.source_id
WHERE sr.collection = :collection AND sr.source_id = :source_id
ORDER BY rc.seq NULLS FIRST
"""

_SOURCE_CHUNK_CAP = 2000
"""The most chunks one mirror read returns. A mirror is read whole, and no mirror chunks past this.

`chunk_count` and `section_count` in the query count every chunk row the WHERE clause matched, before
the fetch trims to this cap, so they stay true even when `chunks` in the answer is truncated. A chunk
opens a heading section when its heading or section path differs from the chunk before it.
"""

_SOURCE_BY_IDENTITY_SQL = """
SELECT sr.source_id, sr.provider
FROM kb.sources_raw sr
WHERE sr.collection = :collection
  AND (sr.meta->>'url' = :url
       OR (:arxiv_id <> '' AND sr.meta->>'arxiv_id' = :arxiv_id)
       OR (:doi <> '' AND sr.meta->>'doi' = :doi))
ORDER BY sr.provider, sr.source_id
LIMIT 1
"""

_SOURCE_IDENTITY_SQL = """
SELECT sr.source_id, sr.provider, sr.title, sr.source_hash,
       coalesce(sr.meta->>'item_type', '') AS item_type, sr.meta->>'year' AS year, sr.meta AS meta
FROM kb.sources_raw sr
WHERE sr.collection = :collection AND sr.source_id = :source_id
"""

_TRACE_RESOLVE_SQL = """
SELECT sr.source_id, sr.provider, sr.provider_key, sr.title, coalesce(sr.meta->>'url', '') AS url
FROM kb.sources_raw sr
WHERE sr.collection = :collection
  AND (sr.source_id = :q OR sr.title = :q OR sr.meta->>'url' = :q)
LIMIT 2
"""

_TRACE_DIGESTS_SQL = """
SELECT p.id, p.page_type, p.title, coalesce(m.path, '') AS path
FROM kb.pages p
LEFT JOIN kb.index_manifest m ON m.collection = p.collection AND m.page_id = p.id
WHERE p.collection = :collection AND p.source_id = :source_id
"""

_TRACE_DIGESTS_CAP = 500

_TRACE_INBOUND_SQL = """
SELECT DISTINCT lr.src_page_id AS page_id, p.title, coalesce(m.path, '') AS path, lr.rel
FROM kb.link_refs lr
JOIN kb.pages p ON p.collection = lr.collection AND p.id = lr.src_page_id
LEFT JOIN kb.index_manifest m ON m.collection = p.collection AND m.page_id = p.id
WHERE lr.collection = :collection AND lr.dst_id = :page_id
"""

_TRACE_INBOUND_CAP = 500

_TRACE_MIRROR_PATH_SQL = """
SELECT path
FROM kb.index_manifest
WHERE collection = :collection AND page_type = 'source' AND source_key = :provider_key
"""
"""`index_manifest.source_key` is the indexing stage's own column, never a frontmatter field a schema declares.

Every indexed mirror file's manifest row carries it under this fixed name, whatever the schema's
`sources.key_field` says."""

def _link_rule(page_type: PageTypeSpec | None, claims: ClaimGrammarSpec | None) -> str:
    """The digest rule naming the link shape, `page_type`'s own link fields, and a claim's edges when `claims` declares edge relations."""
    fields = page_type.frontmatter_fields if page_type is not None else ()
    edge_fields = frozenset(page_type.edge_fields) if page_type is not None else frozenset()
    lists = [field.name for field in fields if field.name in edge_fields and field.type == "link-list"]
    singles = [field.name for field in fields if field.name in edge_fields and field.type != "link-list"]
    named = [f"{_joined(lists)} {'holds' if len(lists) == 1 else 'hold'} a list of those"] if lists else []
    named += [f"{_joined(singles)} {'holds' if len(singles) == 1 else 'hold'} one"] if singles else []
    has_claim_edges = page_type is not None and page_type.claims_enabled and claims is not None and bool(claims.edge_relations)
    claim_clause = ["a claim's edges are [relation, '[[page.id]]'] pairs"] if has_claim_edges else []
    clauses = ", and ".join(clause for clause in (", ".join(named), *claim_clause) if clause)
    return (
        f"A link to another page is the string '[[page.id]]', brackets included{f': {clauses}' if clauses else ''}. "
        "A bare id, a 'page:' prefix, or markdown link syntax is refused as not a unit link."
    )


def _mapped_digest_fields(sources: SourcesSpec, meta: object) -> dict[str, object]:
    """Each field `sources.digest_fields` maps from a key the mirror's metadata `meta` holds, with that key's value."""
    if not isinstance(meta, Mapping):
        return {}
    return {field: meta[key] for key, field in sources.digest_fields.items() if meta.get(key) is not None}


_CLAIM_STUB_KEYS = ("text", "kind", "quote", "loc")


def _claim_text_fields(sources: SourcesSpec, claims: ClaimGrammarSpec | None) -> tuple[str, ...]:
    """The declared text fields that are also text scalar fields, none unless `sources.digest_claim_text_fields` is on."""
    if not sources.digest_claim_text_fields or claims is None:
        return ()
    scalars = frozenset(claims.text_scalar_fields)
    return tuple(name for name in claims.text_fields if name in scalars and name not in _CLAIM_STUB_KEYS)


def _claim_rule(text_fields: tuple[str, ...]) -> str:
    """The digest rule for one claims entry, naming `text_fields` as the keys a claim also fills when the source states them."""
    rule = "One claims entry per finding: text, kind from claim.kind, quote copied verbatim from a chunk, loc copied from that chunk."
    if not text_fields:
        return rule
    return f"{rule} Also fill {_joined(list(text_fields))} from the source when it states a value, else leave the key empty."


def _part_field_fill(sources: SourcesSpec) -> dict[str, str]:
    """`part_field` naming the field a section page sets to its lead page's id, empty when the schema never splits a digest."""
    return {"part_field": sources.digest_part_field} if sources.digest_sections_over is not None else {}


def _joined(names: list[str]) -> str:
    """`names` as prose: `a`, `a and b`, or `a, b and c`."""
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


_SKELETON_SLUG_LENGTH = 60


def _id_slug(title: str) -> str:
    """`title` as the hyphenated, lowercase tail of a page id, at most `_SKELETON_SLUG_LENGTH` characters."""
    words = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (words[:_SKELETON_SLUG_LENGTH].rstrip("-")) or "untitled"


_PURPOSE_LENGTH = 2000
"""The most characters of a purpose page `digest_skeleton` hands over whole."""

_PURPOSE_SCOPE_HEADINGS = ("mission", "in scope", "out of scope")
"""The headings a purpose page written by `engram init` uses for what the knowledge base is for, what belongs in it and what stays out."""


def _purpose_text(kb: AuthoringKnowledgeBase) -> str:
    """The text of the knowledge base's purpose page, `""` when it has none."""
    purpose_path = kb.path / "_meta" / "purpose.md"
    return purpose_path.read_text(encoding="utf-8") if purpose_path.is_file() else ""


def _purpose_for_digest(text: str) -> str:
    """A purpose page short enough to hand a digesting model: its `summary` and the sections that say what belongs and what stays out."""
    frontmatter, rest = split_frontmatter(text)
    summary = str(frontmatter.get("summary") or "").strip()
    body = rest.strip()
    if len(body) <= _PURPOSE_LENGTH:
        return body
    kept = [
        block.strip()
        for block in re.split(r"(?m)^(?=## )", body)
        if block.startswith("## ") and block[3:].split("\n", 1)[0].strip().lower() in _PURPOSE_SCOPE_HEADINGS
    ]
    scoped = "\n\n".join([summary, *kept] if summary else kept)
    return (scoped or body)[:_PURPOSE_LENGTH]


_DECISION_PAGE_TYPE_PAGES_SQL = """
SELECT id, page_type, title, status
FROM kb.pages
WHERE collection = :collection AND page_type = ANY(:page_types)
ORDER BY page_type, id
"""

_DECISION_PAGE_TYPE_PAGES_CAP = 500

_BELIEF_CATEGORY = "project"
"""The `engram.agent_memory.CATEGORIES` bucket `remember_belief` files under."""


def source_type_of(connector: str, item_type: str, *, source: ConnectorSource | None = None) -> SourceType:
    """Fold one mirror's `item_type` into the four source types a digest step routes on, through the connector that wrote it.

    The name is resolved against the registry and the connector's own `source_type_of` answers. A
    connector whose items are already one of the four source types returns it unchanged. One with a
    vocabulary of its own maps every type it knows, and anything it does not recognise, onto a
    source type.

    Args:
        connector: The connector name the mirror carries (`sources_raw.provider`), naming whose
            vocabulary `item_type` speaks.
        item_type: The mirror's own type string, in any case, possibly empty.
        source: The same injectable registry seam `engram.sources.registry.connector_class`
            takes. Left `None`, it resolves against the real installed entry points.

    Returns:
        The source type, always one of `video`, `post`, `article`, `paper`.

    Raises:
        ValueError: `connector` names no connector engram registers.
    """
    try:
        return connector_class(connector, source=source).source_type_of(item_type)
    except ConnectorNotInstalledError as error:
        msg = f"no registered connector named {connector!r} to fold its item type {item_type!r} ({error})"
        raise ValueError(msg) from error


def _reindex_clause(result: ReindexResult) -> str:
    """The reply fragment a tool appends after its own sentence, one plain fragment per outcome."""
    if result.outcome == "clean":
        return "indexed clean"
    if result.outcome == "not_indexed":
        return f"the knowledge base did not index ({result.detail})"
    if result.outcome == "deferred":
        return "indexed once this run ends, so search does not find it yet"
    named = ", ".join(f"{finding.page_id} ({finding.rule})" for finding in result.findings)
    extra = result.findings_total - len(result.findings)
    more = f" and {extra} more" if extra > 0 else ""
    noun = "error" if result.findings_total == 1 else "errors"
    return f"indexed with {result.findings_total} validation {noun} on other pages, {named}{more}"


def _already_reply(row: SourceQueueEntry, url: str) -> str:
    """The answer for a URL already on the queue: its own status word, first."""
    if row.status is SourceQueueStatus.REMOVED:
        return f"removed {url}: {row.reason or 'removed by the owner'}"
    return f"already {row.status.value} {url}"


def _requested_reply(url: str, because: str) -> str:
    """The answer for a URL this call put on the queue, `requested` first, then why."""
    return f"requested {url}, {because}"


_REPOSITORY_HOSTS = frozenset({"github.com", "gitlab.com", "bitbucket.org", "sourceforge.net", "codeberg.org"})
_SOCIAL_POST_HOSTS = frozenset({"facebook.com", "instagram.com", "reddit.com", "tiktok.com", "threads.net"})
_ENCYCLOPEDIA_HOST_SUFFIX = ".wikipedia.org"


_PROFILE_PATH_SEGMENTS = 1


def _link_class_of(url: str) -> LinkClass | None:
    """Which class a policy's `never` list names `url` falls in by its host and path shape alone, or `None` when it fits none."""
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    if not host:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    if not segments:
        return "homepage"
    return _hosted_link_class(host, segments)


def _hosted_link_class(host: str, segments: list[str]) -> LinkClass | None:
    """`_link_class_of`'s lookup once a host and a non-empty path are in hand."""
    if host in _REPOSITORY_HOSTS:
        return "profile" if len(segments) == _PROFILE_PATH_SEGMENTS else "repository"
    if host == "wikipedia.org" or host.endswith(_ENCYCLOPEDIA_HOST_SUFFIX):
        return "encyclopedia"
    if host == "linkedin.com" and segments[0] in {"in", "company"}:
        return "profile"
    if host in {"twitter.com", "x.com"} and len(segments) == _PROFILE_PATH_SEGMENTS:
        return "profile"
    if host in _SOCIAL_POST_HOSTS:
        return "social-post"
    return None


def _rendered(value: object) -> str:
    """One policy rule's value as words a person reads: a tuple as its items comma joined, `None` as `none`."""
    if isinstance(value, tuple):
        return ", ".join(str(item) for item in value) or "none"
    if value is None:
        return "none"
    return str(value)


def _knowledge_base_relative(path: Path, root: Path) -> str:
    """`path` as read from `root`: its relative form when it sits under `root`, else the relative form through `..`."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return os.path.relpath(path, root)


def _decision_page_type_paths(knowledge_base_path: Path, schema: KnowledgeBaseSchema) -> list[Path]:
    """Every markdown file of any of `schema`'s declared decision page types, deduplicated in first-seen order."""
    seen: dict[str, Path] = {}
    for page_type in schema.decision_page_types:
        for path in retrieval.page_type_paths(knowledge_base_path, page_type):
            seen.setdefault(path.as_posix(), path)
    return list(seen.values())


def _thread_view(note: Note) -> dict[str, object]:
    """One open note thread as `read_notes` answers it: its id, status, anchor text, body, timestamp, and replies."""
    return {
        "note_id": note.id,
        "status": note.status,
        "anchor": note.anchor.exact,
        "text": note.text,
        "created_at": note.created_at.isoformat(),
        "replies": [{"author": reply.author, "text": reply.text, "at": reply.at.isoformat()} for reply in note.replies],
    }


@dataclass(frozen=True)
class _ReferenceRequest:
    """One `request_source` call for a judged reference: the URL, the source it was found via, the knowledge base, and when."""

    url: str
    found_via: str
    knowledge_base: str
    found_at: datetime


def _cap_reply(request: _ReferenceRequest, *, count: int, cap: int) -> str:
    """The `seen` answer for a source that already has `cap` rows on the queue under `request.found_via`."""
    noun = "reference" if count == 1 else "references"
    return f"seen {request.url}, {request.found_via} already has {count} {noun} on {request.knowledge_base}'s queue, past the cap of {cap}"


@dataclass(frozen=True)
class _Pass:
    """One run's pass over one knowledge base: the run's id and how many references it has queued there so far."""

    run: str
    queued: int


def _pass_capped_reply(queue: SourceQueue, request: _ReferenceRequest, current: _Pass, *, cap: int) -> str:
    """Records `request.url` as `seen` because `current` already queued `cap` references, and answers why."""
    noun = "reference" if current.queued == 1 else "references"
    reason = f"run {current.run} already queued {current.queued} {noun} on {request.knowledge_base}'s queue, past the ceiling of {cap}"
    row, added = queue.add_seen(request.url, found_via=request.found_via, reason=reason, found_at=request.found_at)
    if not added:
        return _already_reply(row, request.url)
    return f"seen {request.url}, {reason}, ask for it with source_queue_decide"


def _queue_refusal(queue: SourceQueue, request: _ReferenceRequest, policy: SourceQueuePolicy, current: _Pass | None) -> str | None:
    """The `seen` answer when `request.url` is held back by the source cap, the hop depth, or `current`'s own ceiling, else `None`."""
    count = queue.count_of(request.found_via)
    if count >= policy.per_source_cap:
        return _cap_reply(request, count=count, cap=policy.per_source_cap)
    depth = queue.depth_of(request.found_via) + 1
    if depth > policy.hop_depth:
        return _seen_reply(queue, request, depth, policy)
    if policy.per_pass_cap is not None and current is not None and current.queued >= policy.per_pass_cap:
        return _pass_capped_reply(queue, request, current, cap=policy.per_pass_cap)
    return None


def _hard_rule_refusal(url: str, source_type: SourceType, policy: SourceQueuePolicy) -> str | None:
    """The `dropped` answer when `url` fails one of `policy`'s hard rules, checked never list first, else `None`."""
    link_class = _link_class_of(url)
    if link_class is not None and link_class in policy.never:
        return f"dropped {url}, class {link_class} is on the never list"
    if policy.source_types and source_type.value not in policy.source_types:
        return f"dropped {url}, source type {source_type.value} is not in source_types"
    return None


def _seen_reply(queue: SourceQueue, request: _ReferenceRequest, depth: int, policy: SourceQueuePolicy) -> str:
    """Records `request.url` as `seen` past `policy.hop_depth` and answers with the chain it was found through."""
    chain = " via ".join(queue.chain_of(request.found_via))
    seen_reason = f"beyond hop depth {policy.hop_depth} (found via {chain})"
    row, added = queue.add_seen(request.url, found_via=request.found_via, reason=seen_reason, found_at=request.found_at)
    if not added:
        return _already_reply(row, request.url)
    return (
        f"seen {request.url}, {depth} hops from a source the owner added and {request.knowledge_base}'s policy fetches "
        f"{policy.hop_depth}, ask for it with source_queue_decide"
    )


def _bounded_reply(queue: SourceQueue, request: _ReferenceRequest, reason: str, policy: SourceQueuePolicy) -> tuple[str, bool]:
    """Queues `request.url` under `policy.per_source_cap`: the answer (`requested`, or `seen` when the cap was hit on write) and whether it queued."""
    row, added = queue.add_bounded(request.url, found_via=request.found_via, cap=policy.per_source_cap, reason=reason, found_at=request.found_at)
    if row is None:
        return _cap_reply(request, count=policy.per_source_cap, cap=policy.per_source_cap), False
    if not added:
        return _already_reply(row, request.url), False
    return _requested_reply(request.url, reason), True


def _chunk_loc(row: Mapping[str, object]) -> str:
    """The `loc::` a claim cites for one mirror chunk: its `§` section path, else its heading, else its id."""
    path = row.get("section_path")
    loc = section_loc(path) if isinstance(path, str) else None
    return loc or str(row.get("heading") or row.get("chunk_id") or "")


def _aware(found_at: datetime | None) -> datetime | None:
    """`found_at` as a UTC-aware datetime: a naive one (a hand-edited queue row) is read as UTC rather than compared raw."""
    if found_at is None or found_at.tzinfo is not None:
        return found_at
    return found_at.replace(tzinfo=UTC)


def _page_type_digest(page_type: PageTypeSpec, relations: Mapping[str, RelationDecl]) -> dict[str, object]:
    """One page type's ceremony and edges: its guidance, each section's slug, heading and guidance, and each edge field with its relation."""
    return {
        "page_type": page_type.page_type,
        "guidance": page_type.guidance,
        "sections": [
            {"slug": section.slug, "heading": section.heading or heading_for_slug(section.slug), "guidance": section.guidance}
            for section in page_type.canonical_sections
        ],
        "edge_fields": [_edge_field_digest(name, page_type, relations.get(name)) for name in page_type.edge_fields],
    }


def _edge_field_digest(name: str, page_type: PageTypeSpec, relation: RelationDecl | None) -> dict[str, str]:
    """One edge field, whether it holds one link or a list, and its relation's domain, range and description, empty when none is declared."""
    return {
        "field": name,
        "holds": "list" if name in page_type.link_list_edge_fields else "one",
        "domain": relation.domain if relation else "",
        "range": relation.range if relation else "",
        "description": relation.description if relation else "",
    }


def _claim_link_fields(claims: ClaimGrammarSpec | None) -> list[dict[str, str]]:
    """A claim's built-in `subject` and each link field `claims` declares, one link or a list, never `disputed_by` or `edges`, none without a grammar."""
    if claims is None:
        return []
    declared = [{"field": name, "holds": "one" if name in claims.link_scalar_names else "list"} for name in claims.link_fields]
    return [{"field": "subject", "holds": "one"}, *declared]


class AuthoringTools:
    """Tools over every knowledge base: `tools()`'s twenty, plus grounding tools a caller mounts individually.

    `tools()` carries `write_note`, `check_note`, `read_notes`, `resolve_note`, `list_pages`, `read_page`,
    `request_source`, `list_undigested`, `read_source`, `trace_source`, `remove_source_artifacts`,
    `list_recent_digests`, `compounding_work`, `digest_skeleton`, `source_queue_policy`, `write_policy`,
    `source_queue_rows`, `source_queue_decide`, `decide_page`, and `resolve_entities`. Every step that acts on a knowledge
    base gets all twenty. `knowledge_base_profile`, `skeleton`, `recall_beliefs`, `remember_belief`, and `skip_digest`
    are not in that set. A step mounts them one at a time when it needs grounding or, for `skip_digest`, when it is
    granted the right to skip a digest, and most steps do not.
    """

    def __init__(
        self,
        *,
        knowledge_bases: Mapping[str, AuthoringKnowledgeBase],
        reindex: Reindex,
        admin: SqlGateway,
        queue_for: Callable[[AuthoringKnowledgeBase], SourceQueue],
        memory_for: Callable[[AuthoringKnowledgeBase], EngramMemoryStore] | None = None,
        connectors: ConnectorSource | None = None,
        current_run: Callable[[], str | None] | None = None,
        entity_pages_for: Callable[[AuthoringKnowledgeBase], EntityPages] | None = None,
    ) -> None:
        """Store the knowledge bases by name and every dependency the tools reach through.

        Args:
            knowledge_bases: Every knowledge base the tools may act on, by name.
            reindex: Indexes a knowledge base again after a tool wrote to it.
            admin: The SQL gateway the tools read the `kb` schema through.
            queue_for: Returns a knowledge base's source queue.
            memory_for: Opens, or returns an already-open, agent-memory store for a knowledge base. Left
                `None`, `recall_beliefs` and `remember_belief` answer that agent memory is not mounted
                rather than raising.
            connectors: The injectable seam `engram.sources.registry.connector_class` and
                `engram.sources.contracts.claimants_for` take (a mapping or a zero-arg loader). Left `None`,
                URL classification and item-type folding resolve against the real installed entry points.
            current_run: Answers the id of the workflow run the calling step works for, read on every
                `request_source` call, or `None` when no run is making the call. A policy's `per_pass_cap`
                bounds only the references one run queues, counted in this process. Left `None`, no call
                is in a run, so `per_pass_cap` holds nothing back.
            entity_pages_for: Returns a knowledge base's entity pages, read and created by `resolve_entities`. Left
                `None`, they are the pages on disk.
        """
        self._knowledge_bases = knowledge_bases
        self._reindex = reindex
        self._admin = admin
        self._queue_for = queue_for
        self._memory_for = memory_for
        self._connectors = connectors
        self._current_run = current_run
        self._entity_pages_for = entity_pages_for or EntityPages
        self._entity_locks: dict[str, asyncio.Lock] = {}
        self._queued_by_pass: dict[tuple[str, str], int] = {}

    async def write_note(self, knowledge_base: str, payload: dict[str, object], path: str = "") -> str:
        """Write one page into `knowledge_base` from an authoring payload (frontmatter, sections, claims), then reindex.

        The payload is validated against the knowledge base's own grammar. A refusal names every finding so the next
        attempt can fix them all at once. A claim whose text holds a blank line, or whose field value holds a line
        break, is refused, because it does not read back from the page as written. Writing an id that already exists
        replaces that page, unless that page is the digest of a different source, which is refused. An explicit `path`
        (relative to the knowledge base root, ending in `.md`) places the page there instead of under its page type's
        own folder, so long as it resolves under one of that page type's declared roots. A digest whose source link
        names no source mirror in the knowledge base is refused.

        When the knowledge base verifies quotes and the page names its source mirror through the schema's key field, every
        claim's quote must appear word for word in that mirror. A claim whose quote is only a locator, such as `§7` or
        `p. 4`, and whose loc holds the mirror's words has the two swapped, and the answer's warnings name it. Any other
        quote the mirror lacks refuses the page, naming the claim.

        Args:
            knowledge_base: The knowledge base to write the page into.
            payload: The page to write: its frontmatter, sections and claims.
            path: Where to place the page, relative to the knowledge base root. Left empty, the page type's own folder.

        Returns:
            Where the page was written and whether it indexed clean, with any warnings.

        Raises:
            ModelRetry: The knowledge base's grammar refused the page. The message names every finding.
                Also raised when `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        if unlinked := await self._unlinked_digest(kb, payload):
            msg = f"the page was refused: {unlinked}"
            raise ModelRetry(msg)
        try:
            written = NoteWriter(kb).write(payload, path)
        except NoteRefusedError as error:
            msg = f"the page was refused by {knowledge_base}'s grammar:\n{error}"
            raise ModelRetry(msg) from error
        result = await self._reindex(kb)
        said = f"{written.id} written to {_knowledge_base_relative(written.path, kb.path)}, {_reindex_clause(result)}"
        if written.warnings:
            return f"{said} (warnings: {', '.join(written.warnings)})"
        return said

    async def _unlinked_digest(self, kb: AuthoringKnowledgeBase, payload: Mapping[str, object]) -> str | None:
        """The refusal for a digest whose source link names no source mirror in `kb`, or `None` when it names one or links nothing."""
        link_field = kb.schema.sources.digest_link_field
        frontmatter = payload.get("frontmatter")
        if not link_field or not isinstance(frontmatter, Mapping):
            return None
        source_id = frontmatter.get(link_field)
        if not isinstance(source_id, str) or not source_id.strip():
            return None
        found = await self._admin.rows(_SOURCE_IDENTITY_SQL, {"collection": kb.collection, "source_id": source_id}, cap=1)
        if found.rows:
            return None
        return (
            f"{link_field} {source_id!r} names no source mirror in {kb.name!r}. It must be the source_id of the mirror this page "
            "digests, the value digest_skeleton fills, never the source's URL or title. list_undigested names the mirrors"
        )

    async def check_note(self, knowledge_base: str, payload: dict[str, object], path: str = "") -> str:
        """Run every check `write_note` would run on this payload, write nothing, and say what it found.

        The findings come back as the answer, never a retry: fix them, check again, and only then
        write. A payload that passes answers with where the page would land, warnings included.

        The quote check and the locator swap run here too, so a swap shows in the answer's warnings.

        Args:
            knowledge_base: The knowledge base whose grammar checks the page.
            payload: The page payload to check: its frontmatter, sections and claims.
            path: Where the page would be placed, relative to the knowledge base root. Left empty, the page type's own folder.

        Returns:
            The findings, or where the page would land with any warnings.

        Raises:
            ModelRetry: `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        if unlinked := await self._unlinked_digest(kb, payload):
            return f"the page would be refused: {unlinked}"
        try:
            checked = NoteWriter(kb).check(payload, path)
        except NoteRefusedError as error:
            return f"the page would be refused by {knowledge_base}'s grammar:\n{error}"
        said = f"{checked.id} passes {knowledge_base}'s grammar, would land at {_knowledge_base_relative(checked.path, kb.path)}"
        if checked.warnings:
            return f"{said} (warnings: {'; '.join(checked.warnings)})"
        return said

    async def read_notes(self, knowledge_base: str, path: str) -> dict[str, object]:
        """One file of `knowledge_base` with its open note threads: `path`, `exists`, `text`, `threads`.

        A missing file answers `exists: False`, `text: ""`, `threads: []`, never an exception.
        Each thread carries its `note_id`, `status`, `anchor`, `text`, `created_at`, and `replies`.

        Args:
            knowledge_base: The knowledge base the file belongs to.
            path: The file to read, relative to the knowledge base root.

        Returns:
            The file's text and its open owner note threads.

        Raises:
            ModelRetry: `path` is absolute, or resolves outside the knowledge base root.
                Also raised when `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        file = self._resolved_note_path(kb, path)
        exists = file.is_file()
        text = file.read_text(encoding="utf-8") if exists else ""
        sheet = load_sheet(sidecar_for(notes_home(kb.path), file))
        threads = [_thread_view(note) for note in open_threads(sheet)]
        return {"path": path, "exists": exists, "text": text, "threads": threads}

    async def resolve_note(self, knowledge_base: str, path: str, note_id: str, reply: str = "") -> str:
        """Mark one thread on `path` resolved, after appending `reply` as the agent when it is not empty.

        Args:
            knowledge_base: The knowledge base the file belongs to.
            path: The file carrying the thread, relative to the knowledge base root.
            note_id: The owner note thread to mark resolved.
            reply: An optional reply to add first. Left empty, no reply is added.

        Returns:
            Which thread was resolved on which file, and whether a reply went with it.

        Raises:
            ModelRetry: `path` is absolute or resolves outside the knowledge base root, or `path` carries
                no sidecar with `note_id` on it. The message lists the thread ids the file does have.
                Also raised when `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        file = self._resolved_note_path(kb, path)
        sidecar = sidecar_for(notes_home(kb.path), file)
        sheet = load_sheet(sidecar)
        reply_text = reply.strip()
        try:
            if reply_text:
                sheet, _ = add_reply(sheet, note_id, author="agent", text=reply_text)
            sheet, _ = set_status(sheet, note_id, "resolved")
        except KeyError as error:
            known = ", ".join(note.id for note in sheet.notes)
            msg = f"no note {note_id!r} on {path}, this file's threads are ({known})"
            raise ModelRetry(msg) from error
        save_sheet(sidecar, sheet)
        if reply_text:
            return f"replied and resolved {note_id} on {path}"
        return f"resolved {note_id} on {path}"

    def _resolved_note_path(self, knowledge_base: AuthoringKnowledgeBase, path: str) -> Path:
        """`path` resolved under `knowledge_base.path`, refusing an absolute path or one that escapes the knowledge base root."""
        if PurePosixPath(path).is_absolute():
            msg = f"path {path!r} must be relative to the knowledge base root"
            raise ModelRetry(msg)
        root = knowledge_base.path.resolve()
        resolved = (knowledge_base.path / path).resolve()
        if not resolved.is_relative_to(root):
            msg = f"path {path!r} resolves outside the knowledge base root"
            raise ModelRetry(msg)
        return resolved

    async def list_pages(
        self, knowledge_base: str, page_type: str, status: str = "", limit: int = 500, lacking_link: str = ""
    ) -> list[dict[str, str]]:
        """Every page of `page_type` in `knowledge_base` by id, each as `page_id`, `page_type`, `title`, `status`, capped at `limit`.

        `status` narrows the listing to the pages at that status when it is not empty. `lacking_link` narrows it to the
        pages with no outgoing link of that relation when it is not empty.

        Args:
            knowledge_base: The knowledge base to list pages from.
            page_type: The page type to list.
            status: The status to list pages at. Left empty, every status.
            limit: The most pages to list.
            lacking_link: The relation a listed page must have no outgoing link of. Left empty, every page.

        Returns:
            The pages, in id order.

        Raises:
            ModelRetry: `page_type` is not a page type of `knowledge_base`. The message names the page types it has.
                Also raised when `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        if kb.schema.page_type(page_type) is None:
            page_types = ", ".join(page_type.page_type for page_type in kb.schema.page_types)
            msg = f"no page_type {page_type!r} in knowledge base {knowledge_base!r}, the page_types are {page_types}"
            raise ModelRetry(msg)
        sql = (
            _PAGES_OF_TYPE_SQL
            + (_PAGES_OF_TYPE_STATUS_CLAUSE if status else "")
            + (_PAGES_OF_TYPE_LACKING_LINK_CLAUSE if lacking_link else "")
            + _PAGES_OF_TYPE_ORDER_CLAUSE
        )
        params: dict[str, object] = {"collection": kb.collection, "page_type": page_type, "limit": limit}
        if status:
            params["status"] = status
        if lacking_link:
            params["lacking_link"] = lacking_link
        result = await self._admin.rows(sql, params, cap=limit)
        return [{key: str(row.get(key, "")) for key in ("page_id", "page_type", "title", "status")} for row in result.rows]

    async def read_page(self, knowledge_base: str, page_id: str) -> dict[str, JsonValue]:
        """One indexed page of `knowledge_base` by its id, as `page_id`, `path` and a ready write_note `payload`.

        The payload is the file on disk now, parsed by the knowledge base's own grammar: `frontmatter`, `sections` and
        `claims` exactly as write_note takes them. Passed back with `path`, it rewrites the page with the same
        frontmatter values and the same body, byte for byte. A section the page type does not declare can come inside the
        body of the section before it, heading included. Leave it there, or the page changes. The path is the one
        the index last read the page from, relative to the knowledge base root. A page written since the knowledge base
        last indexed is not found.

        Args:
            knowledge_base: The knowledge base the page belongs to.
            page_id: The page's id.

        Returns:
            The page's id, its path and its payload.

        Raises:
            ModelRetry: No indexed page carries `page_id`, its file no longer exists, or the file does not parse into a
                payload that renders it back. Also raised when `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        found = await self._admin.rows(_PAGE_PATH_SQL, {"collection": kb.collection, "page_id": page_id}, cap=1)
        if not found.rows:
            msg = f"no indexed page {page_id!r} in knowledge base {knowledge_base!r}, list_pages and search answer the page ids it holds"
            raise ModelRetry(msg)
        path = str(found.rows[0].get("path", ""))
        file = self._resolved_note_path(kb, path)
        if not file.is_file():
            msg = f"{page_id} was indexed from {path}, which no longer exists in {knowledge_base!r}"
            raise ModelRetry(msg)
        try:
            payload = payload_of(file.read_text(encoding="utf-8"), source_path=path, spec=kb.schema)
        except (NoteGrammarError, ValueError) as error:
            msg = f"{page_id} at {path} cannot be read as a payload: {error}"
            raise ModelRetry(msg) from error
        return {"page_id": page_id, "path": path, "payload": payload}

    async def request_source(self, knowledge_base: str, url: str, found_via: str = "", reason: str = "") -> str:
        """Ask `knowledge_base` to queue `url` for its next sync, under the knowledge base's ingestion policy. Idempotent by URL.

        The answer's first word names the outcome: `requested` when it is queued, `seen` when it is recorded but
        held back (past the policy's `hop_depth`, the source is at its `per_source_cap`, or this pass is at its
        `per_pass_cap`), `dropped` when a hard rule refuses it (its class is on the policy's `never` list, or its
        source type is not in `source_types`), `no_url` when nothing needs fetching (the knowledge base already mirrors it, under any
        connector, matched by URL or by any identifier the mirror carries), or `already <status>` when the queue already holds this URL
        under its own status.

        A request with no `found_via` is the owner's own: it only checks whether the URL is already known. A
        reference (`found_via` set) needs a one-sentence `reason` naming why the source it was found via builds on
        it, and is judged against the knowledge base's ingestion policy before it reaches the queue.

        Args:
            knowledge_base: The knowledge base whose next sync should fetch the URL.
            url: The URL to queue.
            found_via: The source the URL was found via. Left empty, the request is the owner's own.
            reason: One sentence naming why the source it was found via builds on it. A reference needs one.

        Returns:
            The outcome, its first word naming it.

        Raises:
            ModelRetry: `found_via` is set and `reason` is empty.
                Also raised when `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        held = await self._mirror_holding(kb, url)
        if held is not None:
            return f"no_url already on file as {held} in {knowledge_base}, read_source it"
        queue = self._queue_for(kb)
        now = datetime.now(tz=UTC)
        if not found_via:
            row, added = queue.add_once(url, found_via=None, found_at=now)
            if not added:
                return _already_reply(row, url)
            return _requested_reply(url, f"queued for {knowledge_base}'s next sync")
        request = _ReferenceRequest(url=url, found_via=found_via, knowledge_base=knowledge_base, found_at=now)
        return self._request_reference(kb, queue, request, reason)

    def _request_reference(self, knowledge_base: AuthoringKnowledgeBase, queue: SourceQueue, request: _ReferenceRequest, reason: str) -> str:
        """`request_source`'s `found_via`-set branch: judge `request.url` against the ingestion policy before it reaches the queue."""
        if not reason.strip():
            msg = "a reference needs a reason, say in one sentence why the source it was found via builds on it"
            raise ModelRetry(msg)
        policy_page = read_policy(knowledge_base.path, knowledge_base.schema)
        if policy_page.parse_error is not None:
            return f"dropped {request.url}, {request.knowledge_base}'s ingestion policy page could not be read, fix it with write_policy"
        policy = policy_page.rules
        source_type, key = classify_url(request.url, connectors=claimants_for(knowledge_base.schema.sources.connectors, source=self._connectors))
        existing = queue.get(key)
        if existing is not None:
            return _already_reply(existing, request.url)
        refusal = _hard_rule_refusal(request.url, source_type, policy)
        if refusal is not None:
            return refusal
        run = self._current_run() if self._current_run is not None else None
        current = _Pass(run=run, queued=self._queued_by_pass.get((request.knowledge_base, run), 0)) if run is not None else None
        held_back = _queue_refusal(queue, request, policy, current)
        if held_back is not None:
            return held_back
        reply, queued = _bounded_reply(queue, request, reason.strip(), policy)
        if queued and current is not None:
            self._queued_by_pass[request.knowledge_base, current.run] = current.queued + 1
        return reply

    async def source_queue_policy(self, knowledge_base: str) -> dict[str, object]:
        """`knowledge_base`'s ingestion policy as the judge reads it: `rules`, `guidance`, `purpose`, `unknown_rules`, and whether the page is on disk.

        `unknown_rules` names any frontmatter key on the page that is not one of the policy's own fields, so the
        agent can tell the owner their edit did not take.

        Args:
            knowledge_base: The knowledge base whose ingestion policy to read.

        Returns:
            The policy's hard rules, its guidance, the knowledge base's purpose, and the unknown rules.

        Raises:
            ModelRetry: The page could not be read as rules at all (invalid YAML, frontmatter that is not a
                mapping, or a rule value outside its closed set). The message names the failure and points at
                write_policy.
                Also raised when `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        page = read_policy(kb.path, kb.schema)
        if page.parse_error is not None:
            msg = f"{knowledge_base}'s ingestion policy page is not valid, fix it with write_policy: {page.parse_error}"
            raise ModelRetry(msg)
        purpose = _purpose_text(kb)
        return {
            "rules": page.rules.model_dump(mode="json"),
            "guidance": page.guidance,
            "purpose": purpose,
            "present": page.present,
            "unknown_rules": list(page.unknown_rules),
        }

    async def write_policy(self, knowledge_base: str, rules: dict[str, object], guidance: str) -> str:
        """Replace `knowledge_base`'s ingestion policy page with `rules` merged over the current ones and say what changed.

        Call it after the owner said the words, never on your own initiative. `rules` may carry any subset of the
        policy's fields, the rest keep their current values. An empty `guidance` keeps the page's current
        guidance, clearing it is a page edit, never this call. A page that could not be read as rules is replaced
        wholesale from the knowledge base's own defaults, and the answer says so.

        Args:
            knowledge_base: The knowledge base whose ingestion policy to replace.
            rules: The rules to merge over the current ones.
            guidance: The policy's new guidance. Left empty, the current guidance stays.

        Returns:
            What changed, rule by rule.

        Raises:
            ModelRetry: A rule is outside its closed set or of the wrong type. The message names the field.
                Also raised when `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        before = read_policy(kb.path, kb.schema)
        base = before.rules if before.parse_error is None else kb.schema.sources.source_queue
        try:
            merged = SourceQueuePolicy.model_validate({**base.model_dump(), **rules})
        except ValidationError as error:
            msg = f"the policy was refused: {error}"
            raise ModelRetry(msg) from error
        new_guidance = guidance.strip() or before.guidance
        write_policy_page(kb.path, merged, new_guidance)
        changed = [
            f"{name} now {_rendered(getattr(merged, name))} (was {_rendered(getattr(base, name))})"
            for name in SourceQueuePolicy.model_fields
            if getattr(base, name) != getattr(merged, name)
        ]
        guidance_note = "guidance unchanged" if new_guidance == before.guidance else "guidance replaced"
        if before.parse_error is not None:
            summary = ", ".join(changed) or "rules at their defaults"
            return f"{knowledge_base}'s ingestion policy page could not be read, replaced wholesale: {summary}, {guidance_note}"
        return f"policy written for {knowledge_base}: {', '.join(changed) or 'rules unchanged'}, {guidance_note}"

    async def source_queue_rows(self, knowledge_base: str, status: str = "", limit: int = 40) -> dict[str, object]:
        """`knowledge_base`'s queue, its true state first: counts per status, the digest backlog, then the rows.

        `counts` covers the whole queue whatever `status` filters. `undigested_mirrors` is how
        many fetched mirrors still have no digest page. `resolved` alone says fetched, never
        digested, and judging the backlog from it misleads. `rows` are newest first, all of them
        or only those in `status`, each with its reason and what it was found via.

        Args:
            knowledge_base: The knowledge base whose source queue to read.
            status: The status of the rows to list. Left empty, every row.
            limit: The most rows to list.

        Returns:
            The counts per status, the undigested-mirror backlog, and the rows newest first.

        Raises:
            ModelRetry: `status` is not a queue status. The message lists them.
                Also raised when `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        rows = self._queue_for(kb).entries()
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.status.value] = counts.get(row.status.value, 0) + 1
        if status:
            known = sorted(member.value for member in SourceQueueStatus)
            if status not in known:
                msg = f"no queue status {status!r}, one of: {', '.join(known)}"
                raise ModelRetry(msg)
            rows = [row for row in rows if row.status.value == status]
        floor = datetime.min.replace(tzinfo=UTC)
        newest = sorted(rows, key=lambda row: _aware(row.found_at) or floor, reverse=True)[:limit]
        return {
            "counts": counts,
            "undigested_mirrors": len(await self.list_undigested(knowledge_base, limit=500)),
            "note": "resolved means fetched and mirrored, not digested. undigested_mirrors is the digest backlog, list_undigested names it.",
            "rows": [
                {
                    "key": row.key,
                    "url": row.url,
                    "source_type": row.source_type.value,
                    "status": row.status.value,
                    "reason": row.reason or "",
                    "found_via": row.found_via or "",
                    "found_at": row.found_at.isoformat() if row.found_at is not None else "",
                }
                for row in newest
            ],
        }

    async def source_queue_decide(self, knowledge_base: str, key: str, action: SourceQueueDecision, reason: str = "") -> str:
        """Move one queue row on the owner's word: `fetch` makes a seen, failed or skipped row requested, `drop` makes a requested or seen row skipped.

        An empty `reason` on a `drop` keeps the row's existing reason, give one to replace it.

        Args:
            knowledge_base: The knowledge base whose source queue holds the row.
            key: The source queue row to move.
            action: `fetch` or `drop`.
            reason: Why, on a `drop`. Left empty, the row's existing reason stays.

        Returns:
            The row's new status, or that there was nothing to do.

        Raises:
            ModelRetry: `key` is not on the queue.
                Also raised when `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        queue = self._queue_for(kb)
        row = queue.get(key)
        if row is None:
            msg = f"no queue row {key!r} in {knowledge_base}, source_queue_rows lists them"
            raise ModelRetry(msg)
        moved = queue.decide(key, action, reason=reason.strip() or None)
        if moved is None:
            return f"{key} is {row.status.value}, nothing to {action}"
        return f"{key} is now {moved.status.value} in {knowledge_base}"

    async def decide_page(
        self,
        knowledge_base: str,
        page_id: str,
        status: str = "",
        links: Mapping[str, Sequence[str]] | None = None,
        reason: str = "",
        *,
        add: bool = False,
    ) -> str:
        """Move one page to one of its page type's declared statuses, or write only its link fields, then reindex so searches see it.

        A status move is the owner's decision. An empty status keeps the page's current status, so a step whose
        instructions ask for it can write only link fields and a reason. `links` maps a link-list `edge_fields` name to the ids that field should now hold (bare or
        as `[[id]]`), replacing that field's whole entry. An outcome's own `link` (the page type's declared
        decision outcome) names which field to give when resolving to it. A field left out of `links`
        keeps its existing entry untouched. With `add`, each named field keeps the ids it holds and gains the
        given ones after them, each id once, so a call adds links and never removes one.

        Args:
            knowledge_base: The knowledge base the page belongs to.
            page_id: The page to move.
            status: One of the page type's declared statuses. Left empty, the page keeps its current status.
            links: The link fields an outcome asks for, each mapped to the ids it should now hold.
            reason: An optional reason, noted on the page.
            add: Whether the given ids join what each named field holds instead of replacing it.

        Returns:
            The page's status, new or kept, and whether it indexed clean, with any reason and links written. With
            `add`, it names everything each named field now holds.

        Raises:
            ModelRetry: No page carries `page_id`, a non-empty `status` is not one of its page type's declared
                statuses, or a `links` key is not one of that page type's link-list edge fields.
                Also raised when `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        try:
            row = retrieval.decide_page(kb.path, kb.schema, page_id, status, links=links, reason=reason, add=add)
        except (LookupError, ValueError) as error:
            raise ModelRetry(str(error)) from error
        result = await self._reindex(kb)
        noted = ", reason noted" if reason else ""
        written = "".join(
            f", {field} now holds {', '.join(row.links[field])}" if add else f", {field}: {', '.join(ids)}" for field, ids in (links or {}).items()
        )
        moved = f"is now {row.status}" if status else f"stays {row.status}"
        return f"{row.id} ({row.page_type}) {moved} in {knowledge_base}, {_reindex_clause(result)}{noted}{written}"

    async def resolve_entities(self, knowledge_base: str, entities: list[EntityName]) -> str:
        """Resolve each name to the id of its one entity page, creating a page for each name that matches none (entity resolution).

        A name matches an entity page when it equals the page's title or one of its aliases once case, punctuation and a
        leading "the" are set aside. A name that matches no page resolves to the page its first matching alias names,
        and joins that page as an alias. A new page takes the name as its title, with its entity type, its aliases and
        its summary. Names in one call that make the same page id share one page, and a name whose page id an existing
        page already carries joins that page. A page only ever takes names no other page holds: a matched page takes the
        new ones as aliases, and so does a page another writer created first. Every name is checked before anything is
        written, and the knowledge base is indexed again once when anything was. Calls on one knowledge base through one
        `AuthoringTools` instance on one event loop resolve one at a time, from the read of its entity pages to the last
        write, so calls at once never make two pages for one entity.

        Args:
            knowledge_base: The knowledge base whose entity pages the names resolve to.
            entities: Every name to resolve, each with its entity type, its other names and a one-sentence summary.

        Returns:
            JSON `{"entities": [{"name", "id", "created"}]}` in input order. `created` is true on every row whose page
            this call created.

        Raises:
            ModelRetry: The schema has no `entities` block, a name is blank, an entity type is not one the schema
                declares, a name that matches no page makes no page id, or the grammar refuses a new page. Each names
                every offender, and nothing is written then. Also raised when the file a name's page sits in, new or
                matched, cannot be read as an entity page, naming the pages already written, and when `knowledge_base`
                names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        entities_spec = kb.schema.entities
        page_type = kb.schema.page_type(entities_spec.page_type) if entities_spec is not None else None
        if page_type is None:
            msg = f"{knowledge_base} keeps no entity pages: its schema has no entities block naming the page type they are written as"
            raise ModelRetry(msg)
        if blank := [str(position) for position, entity in enumerate(entities, start=1) if not entity.name.strip()]:
            msg = f"blank name at position{'s' if len(blank) > 1 else ''} {', '.join(blank)} of entities, counted from 1. Give every entity the name the source writes"
            raise ModelRetry(msg)
        declared = [entry.value for entry in kb.schema.entity_types]
        if undeclared := list(dict.fromkeys(entity.entity_type for entity in entities if entity.entity_type not in declared)):
            named = ", ".join(repr(entity_type) for entity_type in undeclared)
            msg = f"{knowledge_base} declares no entity_type {named}. The entity types are {', '.join(declared)}"
            raise ModelRetry(msg)
        pages = self._entity_pages_for(kb)
        prefix = kb.schema.prefix_by_page_type.get(page_type.page_type, f"{page_type.page_type}.")
        written: set[str] = set()
        try:
            async with self._entity_locks.setdefault(kb.name, asyncio.Lock()):
                resolved, new_pages, additions = self._planned_entities(await asyncio.to_thread(pages.known), entities, prefix)
                checked = self._checked_entity_pages(kb, page_type, new_pages)
                created, merged = await self._published_entity_pages(pages, checked, new_pages, additions, written)
        finally:
            if written:
                await self._reindex(kb)
        rows = [{"name": name, "id": merged.get(page_id, page_id), "created": page_id in created} for name, page_id in resolved]
        return json.dumps({"entities": rows}, ensure_ascii=False)

    @staticmethod
    def _planned_entities(
        known: KnownEntities, entities: Sequence[EntityName], prefix: str
    ) -> tuple[list[tuple[str, str]], dict[str, NewEntityPage], dict[str, list[str]]]:
        """Each name paired with its page id, the new pages those ids need, and the new names each existing page takes, none held by another page."""
        resolved: list[tuple[str, str]] = []
        new_pages: dict[str, NewEntityPage] = {}
        additions: dict[str, list[str]] = {}
        unnamed: list[str] = []
        for entity in entities:
            names = [entity.name, *entity.aliases]
            page_id = next((page for page in map(known.match, names) if page is not None), None)
            if page_id is None:
                slug = _id_slug(entity.name)
                if slug == "untitled":
                    unnamed.append(entity.name)
                    continue
                page_id = f"{prefix}{slug}"
            fresh = known.unknown(names)
            if page_id in new_pages:
                new_pages[page_id].fold(fresh)
            elif known.has_page(page_id):
                if fresh:
                    additions.setdefault(page_id, []).extend(fresh)
            else:
                new_pages[page_id] = NewEntityPage(id=page_id, entity=entity, names=fresh)
            known.add(page_id, names)
            resolved.append((entity.name, page_id))
        if unnamed:
            named = ", ".join(repr(name) for name in unnamed)
            msg = f"these names make no page id of their own: {named}. Pass a Latin-script form of each as name, and keep the original among its aliases"
            raise ModelRetry(msg)
        return resolved, new_pages, additions

    @staticmethod
    def _checked_entity_pages(kb: AuthoringKnowledgeBase, page_type: PageTypeSpec, new_pages: Mapping[str, NewEntityPage]) -> dict[str, CheckedNote]:
        """Every new page checked against `kb`'s grammar by id, refused whole when one fails."""
        writer = NoteWriter(kb)
        claims_section = kb.schema.claims.section if kb.schema.claims is not None else None
        checked: dict[str, CheckedNote] = {}
        for page_id, new_page in new_pages.items():
            try:
                checked[page_id] = writer.check(new_page.payload(page_type, claims_section))
            except NoteRefusedError as error:
                msg = f"the entity page {page_id} was refused by {kb.name}'s grammar, and nothing was written:\n{error}"
                raise ModelRetry(msg) from error
        return checked

    @staticmethod
    async def _published_entity_pages(
        pages: EntityPages,
        checked: Mapping[str, CheckedNote],
        new_pages: Mapping[str, NewEntityPage],
        additions: Mapping[str, Sequence[str]],
        written: set[str],
    ) -> tuple[set[str], dict[str, str]]:
        """Create each checked page or merge into the page another writer created first, add each existing page's new names, and record each page written in `written`."""
        created: set[str] = set()
        merged: dict[str, str] = {}
        try:
            for page_id, note in checked.items():
                if await asyncio.to_thread(pages.create, note):
                    created.add(page_id)
                    written.add(page_id)
                    continue
                merge = await asyncio.to_thread(pages.merge_aliases, note.path, new_pages[page_id].names)
                merged[page_id] = merge.page_id
                if merge.added:
                    written.add(merge.page_id)
            for page_id, names in additions.items():
                merge = await asyncio.to_thread(pages.merge_aliases, pages.path_of(page_id), names)
                if merge.added:
                    written.add(merge.page_id)
        except NoteRefusedError as error:
            msg = f"{error}. Pages already written: {', '.join(sorted(written)) or 'none'}"
            raise ModelRetry(msg) from error
        return created, merged

    async def skip_digest(self, knowledge_base: str, page_id: str, reason: str) -> str:
        """Skip one source's digest: every digest page of the source `page_id` digests is set to skipped, `reason` noted on each.

        Use it for a digest that falls outside the knowledge base's purpose or repeats the work a digest of
        another source already covers. The pages and the mirror stay, the source still counts as digested, and the owner can move a page
        back. A skipped page leaves the flag batch and the recent-digests list.

        Args:
            knowledge_base: The knowledge base the page belongs to.
            page_id: Any digest page of the source, its lead page or one of its section pages.
            reason: One sentence saying why, noted on every page as its decision reason.

        Returns:
            Which pages are now skipped, and whether the knowledge base indexed clean.

        Raises:
            ModelRetry: `reason` is empty, no page carries `page_id`, the page names no source so it is not a digest,
                or a page of that source has a page type declaring no skipped status. Nothing changes then. Also raised
                when one page cannot be rewritten, naming the pages already skipped, and when `knowledge_base` names no
                knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        if not reason.strip():
            msg = "give a one-sentence reason, it is noted on every page you skip"
            raise ModelRetry(msg)
        sources = kb.schema.sources
        found = await self._admin.rows(_page_source_sql(sources), {"collection": kb.collection, "page_id": page_id}, cap=1)
        if not found.rows:
            msg = f"no page {page_id!r} in {knowledge_base!r}. Pass a digest page's full id"
            raise ModelRetry(msg)
        source_id = str(found.rows[0].get("source_id") or "")
        if not source_id:
            msg = f"{page_id} names no source through {sources.digest_link_field}, so it is not a digest. Only a digest can be skipped"
            raise ModelRetry(msg)
        digests = await self._admin.rows(_source_digests_sql(sources), {"collection": kb.collection, "source_id": source_id}, cap=_SOURCE_DIGESTS_CAP)
        pages = [(str(row.get("id") or ""), str(row.get("page_type") or "")) for row in digests.rows]
        for digest_id, page_type in pages:
            spec = kb.schema.page_type(page_type)
            statuses = sorted(spec.effective_status_values) if spec is not None else []
            if _SKIPPED not in statuses:
                msg = f"{digest_id} is a {page_type} page, whose statuses are {statuses}, none of them skipped. Nothing was changed"
                raise ModelRetry(msg)
        skipped: list[str] = []
        for digest_id, _page_type in pages:
            try:
                retrieval.decide_page(kb.path, kb.schema, digest_id, _SKIPPED, reason=reason)
            except (LookupError, ValueError) as error:
                await self._reindex(kb)
                done = ", ".join(skipped) or "none"
                msg = f"{digest_id} could not be skipped ({error}). Already skipped: {done}"
                raise ModelRetry(msg) from error
            skipped.append(digest_id)
        result = await self._reindex(kb)
        return f"{len(skipped)} digest pages of {source_id} are now skipped in {knowledge_base}: {', '.join(skipped)}, reason noted, {_reindex_clause(result)}"

    async def _mirror_holding(self, knowledge_base: AuthoringKnowledgeBase, url: str) -> str | None:
        """`"<source_id> (<provider>)"` of the mirror in `knowledge_base` that already is the paper or page behind `url`, else `None`."""
        params = {"collection": knowledge_base.collection, "url": url, "arxiv_id": arxiv_id_of(url) or "", "doi": doi_of(url) or ""}
        result = await self._admin.rows(_SOURCE_BY_IDENTITY_SQL, params, cap=1)
        if not result.rows:
            return None
        row = result.rows[0]
        return f"{row.get('source_id')} ({row.get('provider')})"

    async def list_undigested(self, knowledge_base: str, limit: int = 40, source_ids: list[str] | None = None) -> list[dict[str, str]]:
        """Source mirrors in `knowledge_base` that no digest links through `sources.digest_link_field`, newest first, at most `limit`.

        Every row's `item_type` is one of `video`, `paper`, `post`, `article`, whatever vocabulary
        the mirror's own connector wrote. With `source_ids`, it answers exactly the undigested mirrors among
        those ids, however old, and ignores `limit`. An id that names no undigested mirror is left out.

        Args:
            knowledge_base: The knowledge base whose source mirrors to list.
            limit: The most mirrors to list when `source_ids` is not given.
            source_ids: The source ids to look up. Left out, the newest undigested mirrors are listed.

        Returns:
            The mirrors that have no digest yet, newest synced first.

        Raises:
            ModelRetry: `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        if source_ids is None:
            result = await self._admin.rows(_undigested_sql(kb.schema.sources), {"collection": kb.collection, "limit": limit}, cap=limit)
        elif not source_ids:
            return []
        else:
            params = {"collection": kb.collection, "source_ids": source_ids}
            result = await self._admin.rows(_undigested_sql(kb.schema.sources, picked=True), params, cap=len(source_ids))
        return [
            {
                **{key: str(row.get(key, "")) for key in ("source_id", "provider", "title")},
                "item_type": source_type_of(str(row.get("provider", "")), str(row.get("item_type", "")), source=self._connectors).value,
            }
            for row in result.rows
        ]

    async def list_recent_digests(self, knowledge_base: str, since_hours: int = 26, limit: int = 40) -> list[dict[str, str]]:
        """Digest pages in `knowledge_base` indexed within the last `since_hours` hours, newest first, at most `limit`.

        A digest is any page whose frontmatter names a source through `sources.digest_link_field`.
        A page with status `skipped` is left out. "Written" is the page's `created` date, which
        `write_note` stamps when the author leaves it out, so the window is read in whole days and
        a page with no `created` date never counts as recent. Each row carries `digest_id`, `page_type`,
        and `title`.

        Args:
            knowledge_base: The knowledge base whose digest pages to list.
            since_hours: How many hours back to look.
            limit: The most digest pages to list.

        Returns:
            The digest pages, newest first.

        Raises:
            ModelRetry: `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        params = {"collection": kb.collection, "since_hours": since_hours, "limit": limit}
        result = await self._admin.rows(_recent_digests_sql(kb.schema.sources), params, cap=limit)
        return [{key: str(row.get(key, "")) for key in ("digest_id", "page_type", "title")} for row in result.rows]

    async def compounding_work(self, knowledge_base: str, name: str, page_ids: list[str] | None = None) -> dict[str, object]:
        """The pending inputs for `knowledge_base`'s compounding entry `name`, with the destination page types and the status to write.

        `entry` echoes the declared rule: `name`, `to`, `cap_per_run`, `gate`, `guidance`.
        `opening_status` is the first `status_values` of `to`'s first page type, `"active"` when that
        page type declares none. It is the status a fresh page of this derivation's output opens at.
        `rows` are the pending inputs themselves, shaped by the entry's own plane. A `sources` entry
        lists undigested mirrors exactly like `list_undigested`. A `pages` entry lists pages of its
        declared page types within its window and status, newest first. Each pages row also carries `source_id`
        (the page's `sources.digest_link_field` value, empty when it names none), `path` (its file, empty when the
        index holds none) and `findings` (the sorted rule names of its stored validation errors). A `notes` entry
        lists files under its folder that carry at least one open note thread.

        Args:
            knowledge_base: The knowledge base whose compounding policy declares the entry.
            name: The derivation entry to read.
            page_ids: Exactly these pages, for an entry over pages: each page of the entry's page types carrying one of
                these ids, whatever its age and whether a page of the entry's `to` already links it, never a skipped
                page and never capped. Left empty, the entry's own window, status and cap decide.

        Returns:
            The entry, the opening status and the pending inputs.

        Raises:
            ModelRetry: `name` names no entry this knowledge base declares. The message lists every entry it does.
                Also raised when `knowledge_base` names no knowledge base these tools write into, and when
                `page_ids` is given for an entry over sources or notes.
        """
        kb = self._knowledge_base(knowledge_base)
        entry = kb.schema.compounding.entry(name)
        if entry is None:
            declared = ", ".join(e.name for e in kb.schema.compounding.entries)
            msg = f"no derivation {name!r} in knowledge base {knowledge_base!r}, the entries are {declared}"
            raise ModelRetry(msg)
        wanted = list(dict.fromkeys(page_id.strip() for page_id in page_ids or () if page_id.strip()))
        rows = await self._compounding_rows(kb, entry, wanted)
        return {
            "entry": {
                "name": entry.name,
                "to": list(entry.to),
                "cap_per_run": entry.cap_per_run,
                "gate": entry.gate,
                "guidance": entry.guidance,
            },
            "opening_status": opening_status(kb.schema.page_type(entry.to[0])),
            "rows": rows,
        }

    async def _compounding_rows(self, knowledge_base: AuthoringKnowledgeBase, entry: DerivationEntry, page_ids: list[str]) -> list[dict[str, object]]:
        """`compounding_work`'s pending inputs, dispatched on `entry.source`'s own plane, or the named pages."""
        selector = entry.source
        if page_ids and not isinstance(selector, PagesSelector):
            msg = f"page_ids narrows only an entry over pages, and {entry.name!r} reads {selector.plane}"
            raise ModelRetry(msg)
        if isinstance(selector, SourcesSelector):
            return await self._compounding_source_rows(knowledge_base, entry.cap_per_run)
        if isinstance(selector, PagesSelector):
            if page_ids:
                return await self._compounding_named_pages_rows(knowledge_base, selector, page_ids)
            return await self._compounding_pages_rows(knowledge_base, selector, entry.to, entry.cap_per_run)
        return _compounding_notes_rows(knowledge_base, selector, entry.cap_per_run)

    async def _compounding_named_pages_rows(
        self, knowledge_base: AuthoringKnowledgeBase, selector: PagesSelector, page_ids: list[str]
    ) -> list[dict[str, object]]:
        """The pages of `selector.page_types` carrying one of `page_ids`, with no window, status filter or cap."""
        params: dict[str, object] = {"collection": knowledge_base.collection, "page_types": list(selector.page_types), "page_ids": page_ids}
        result = await self._admin.rows(_compounding_named_pages_sql(knowledge_base.schema.sources), params, cap=len(page_ids))
        return [_page_row(row) for row in result.rows]

    async def _compounding_source_rows(self, knowledge_base: AuthoringKnowledgeBase, cap: int) -> list[dict[str, object]]:
        """Undigested mirrors for a sources-plane compounding entry, the `list_undigested` rows at `cap`."""
        return [dict(row) for row in await self.list_undigested(knowledge_base.name, limit=cap)]

    async def _compounding_pages_rows(
        self, knowledge_base: AuthoringKnowledgeBase, selector: PagesSelector, to_page_types: tuple[str, ...], cap: int
    ) -> list[dict[str, object]]:
        """Pages of `selector.page_types` not yet joined to one of `to_page_types`, newest first then by id, capped at `cap`."""
        sql = _compounding_pages_sql(selector, knowledge_base.schema.sources)
        candidates: list[Mapping[str, object]] = []
        for page_type in selector.page_types:
            params: dict[str, object] = {"collection": knowledge_base.collection, "page_type": page_type, "limit": cap, "to_kinds": list(to_page_types)}
            if selector.within_hours is not None:
                params["within_hours"] = selector.within_hours
            if selector.status is not None:
                params["status"] = selector.status
            result = await self._admin.rows(sql, params, cap=cap)
            candidates.extend(result.rows)
        by_id = sorted(candidates, key=lambda row: str(row.get("page_id", "")))
        newest_first = sorted(by_id, key=lambda row: str(row.get("created") or ""), reverse=True)
        return [_page_row(row) for row in newest_first[:cap]]

    async def digest_skeleton(self, knowledge_base: str, source_id: str) -> dict[str, object]:
        """A ready `write_note` payload for `source_id`'s digest in `knowledge_base`: fill the blanks, keep the rest.

        The frontmatter arrives with the digest page type for the mirror's item type, an id under that
        page type's prefix (numbered when another page already holds it), the schema's
        `sources.digest_link_field` set to `source_id`, and every identity field the page type declares,
        required or optional, already copied from the mirror (`key_field`, `hash_field`, and `year` when
        the mirror knows it). Every field `sources.digest_fields` maps from a key the mirror's metadata holds
        arrives filled with that key's value. A required field the mirror cannot supply is present and empty. The page type comes
        from the schema's `sources.digest_types`, `sections` holds that page type's canonical sections
        (the schema's claims section left out when the page type carries claims, and the renderer fills it from
        `claims`) with empty bodies, and `claims` one
        empty claim naming the fields a finding carries when the page type carries claims, else none. That
        claim also names the claim grammar's declared text scalar fields, and the claim rule names them too, when the
        schema sets `sources.digest_claim_text_fields`. A built-in claim field, an edge relation or a link field is never named.
        `fill` lists the registered values for every required choice-bound field, the claim types under
        `claim.kind` when the page type carries claims, the knowledge base's `purpose` (its
        summary and what belongs in it and what stays out, `""` when it has none), `part_field` naming the
        field a section page of a split digest sets to its lead page's id when the schema sets
        `sources.digest_sections_over`, and the rules a page must keep, which name the page type's own link fields.

        Args:
            knowledge_base: The knowledge base the digest is written into.
            source_id: The source mirror to digest.

        Returns:
            The payload, with `frontmatter`, `sections`, `claims` and `fill`.

        Raises:
            ModelRetry: No such mirror, and the answer points at `list_undigested`. Or the schema declares
                no digest page type for the mirror's item type.
                Also raised when `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        params = {"collection": kb.collection, "source_id": source_id}
        result = await self._admin.rows(_SOURCE_IDENTITY_SQL, params, cap=1)
        if not result.rows:
            msg = f"no source mirror {source_id!r} in {knowledge_base!r}. list_undigested names the mirrors you can digest"
            raise ModelRetry(msg)
        row = result.rows[0]
        item_type = source_type_of(str(row.get("provider", "")), str(row.get("item_type", "")), source=self._connectors).value
        page_type = kb.schema.sources.digest_types.get(item_type)
        if page_type is None:
            declared = ", ".join(f"{item} -> {page}" for item, page in kb.schema.sources.digest_types.items()) or "nothing"
            msg = f"{knowledge_base!r} declares no digest page_type for item type {item_type!r}. Its spec's sources.digest_types maps {declared}"
            raise ModelRetry(msg)
        title = str(row.get("title") or source_id)
        prefix = kb.schema.prefix_by_page_type.get(page_type, f"{page_type}.")
        sources_schema = kb.schema.sources
        known: dict[str, object] = {
            sources_schema.key_field: source_id,
            sources_schema.digest_link_field: source_id,
            sources_schema.hash_field: str(row.get("source_hash") or ""),
        }
        year = row.get("year")
        if isinstance(year, int) or (isinstance(year, str) and year.strip().isdigit()):
            known["year"] = int(year)
        known = {**_mapped_digest_fields(sources_schema, row.get("meta")), **known}
        frontmatter: dict[str, object] = {
            "id": NoteWriter(kb).digest_id_for(page_type, f"{prefix}{_id_slug(title)}", source_id),
            "type": page_type,
            "title": title,
            "summary": "",
            "status": "active",
            sources_schema.digest_link_field: source_id,
        }
        vocabulary = _vocabulary_index(kb)
        fill: dict[str, object] = {}
        page_type_schema = kb.schema.page_type(page_type)
        unfilled: list[str] = []
        for field in page_type_schema.frontmatter_fields if page_type_schema is not None else ():
            if field.name not in known and not field.required:
                continue
            value = known.get(field.name, "")
            frontmatter[field.name] = value
            if not field.required:
                continue
            choices = list(field.choices) or sorted(vocabulary.facets.get(field.name, {}))
            if choices:
                fill[field.name] = choices
            elif value == "":
                unfilled.append(field.name)
        has_claims = page_type_schema is not None and page_type_schema.claims_enabled
        if has_claims:
            fill["claim.kind"] = sorted(vocabulary.claim_types)
        fill["purpose"] = _purpose_for_digest(_purpose_text(kb))
        fill.update(_part_field_fill(sources_schema))
        payload_keys = "frontmatter, sections and claims as its three keys" if has_claims else "frontmatter and sections as its keys"
        claim_text_fields = _claim_text_fields(sources_schema, kb.schema.claims)
        claim_rule = [_claim_rule(claim_text_fields)] if has_claims else []
        fill["rules"] = [
            *(f"{name} is empty and the mirror cannot supply it: fill it from the source itself." for name in unfilled),
            "Keep every value given here and fill the empty ones: a required field left empty is refused.",
            f"Pass this whole object to write_note as payload, with {payload_keys}, never the frontmatter dict alone.",
            "Section bodies are markdown strings, never lists or objects.",
            *claim_rule,
            _link_rule(page_type_schema, kb.schema.claims),
            "Write the page once, complete. Never write a placeholder to see what the grammar accepts.",
        ]
        claims_slug = kb.schema.claims.section if kb.schema.claims is not None and has_claims else None
        canonical = page_type_schema.canonical_sections if page_type_schema is not None else ()
        sections = [
            {"heading": section.heading or heading_for_slug(section.slug), "body": ""} for section in canonical if section.slug != claims_slug
        ]
        return {
            "frontmatter": frontmatter,
            "sections": sections,
            "claims": [dict.fromkeys((*_CLAIM_STUB_KEYS, *claim_text_fields), "")] if has_claims else [],
            "fill": fill,
        }

    async def skeleton(self, knowledge_base: str, page_type: str) -> dict[str, object]:
        """A blank `write_note` payload for one page type in `knowledge_base`: its own sections and ceremony, nothing prefilled from any source.

        `frontmatter` carries `type`, an empty `title` and `summary`, `id` set to the page type's own
        prefix (the agent completes it with a slug of the title), and `status` set to the first of
        the page type's `status_values`, `"active"` when it declares none. `sections` seeds every
        canonical section in order (the claims section left out, the renderer fills it from a
        page's `claims`), each with its declared or derived heading, an empty body, and the
        section's own authoring guidance. `fill` lists the registered choices for a required field
        that has a closed value set, a required field with none is left empty and named in `rules`.
        `guidance` is the page type's own note on when and where a page of this page type is written, `""`
        when the page type carries none.

        Args:
            knowledge_base: The knowledge base the page is written into.
            page_type: The page type to seed a payload for.

        Returns:
            The payload, with `frontmatter`, `sections`, `fill`, `guidance` and `rules`.

        Raises:
            ModelRetry: `page_type` names no page type this knowledge base declares. The message lists every page type it does.
                Also raised when `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        schema = kb.schema
        page_type_schema = schema.page_type(page_type)
        if page_type_schema is None:
            page_types = ", ".join(entry.page_type for entry in schema.page_types)
            msg = f"no page_type {page_type!r} in knowledge base {knowledge_base!r}, the page_types are {page_types}"
            raise ModelRetry(msg)
        prefix = schema.prefix_by_page_type.get(page_type, f"{page_type}.")
        status = opening_status(page_type_schema)
        frontmatter: dict[str, object] = {"id": prefix, "type": page_type, "title": "", "summary": "", "status": status}
        vocabulary = _vocabulary_index(kb)
        fill: dict[str, object] = {}
        unfilled: list[str] = []
        for field in page_type_schema.frontmatter_fields:
            if not field.required:
                continue
            frontmatter[field.name] = ""
            choices = list(field.choices) or sorted(vocabulary.facets.get(field.name, {}))
            if choices:
                fill[field.name] = choices
            else:
                unfilled.append(field.name)
        claims_slug = schema.claims.section if schema.claims is not None else None
        sections = [
            {"heading": section.heading or heading_for_slug(section.slug), "body": "", "guidance": section.guidance}
            for section in page_type_schema.canonical_sections
            if section.slug != claims_slug
        ]
        rules = [
            *(f"{name} is empty and has no registered values. Fill it before writing." for name in unfilled),
            "Keep every value given here and fill the empty ones. A required field left empty is refused.",
            "Section bodies are markdown strings, never lists or objects.",
            "Complete the id with a short slug of the title, after the prefix already given.",
            "A section's guidance describes what to write there. It is never part of the written page.",
            "Write the page once, complete. Never write a placeholder to see what the grammar accepts.",
        ]
        return {"frontmatter": frontmatter, "sections": sections, "fill": fill, "guidance": page_type_schema.guidance, "rules": rules}

    async def read_source(self, knowledge_base: str, source_id: str) -> dict[str, object]:
        """One source mirror in `knowledge_base`, whole: its identity and its chunks in reading order.

        Each chunk carries `loc` (its `§` section locator, `§4.1`, or its heading where the mirror
        numbers no section), `heading` and `text`. A claim quotes a chunk's `text` verbatim and cites
        that chunk's `loc`. `chunk_count` is the mirror's total chunk count and `section_count` its
        heading sections, both true even when `chunks` is capped short of them. A long heading section
        spans several chunks that share its heading. When `knowledge_base`'s schema sets a
        `digest_sections_over` threshold, the answer also carries that threshold and
        `split_recommended` (`section_count` over it): a large mirror should digest as a lead page
        plus one page per major section instead of one page.

        Args:
            knowledge_base: The knowledge base holding the mirror.
            source_id: The source mirror to read.

        Returns:
            The mirror's identity, its chunks, its chunk count and its heading section count.

        Raises:
            ModelRetry: No such mirror. The answer points at `list_undigested`.
                Also raised when `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        params = {"collection": kb.collection, "source_id": source_id}
        result = await self._admin.rows(_SOURCE_SQL, params, cap=_SOURCE_CHUNK_CAP)
        if not result.rows:
            msg = f"no source mirror {source_id!r} in {knowledge_base!r}. list_undigested names the mirrors you can read"
            raise ModelRetry(msg)
        first = result.rows[0]
        chunks = [
            {"loc": _chunk_loc(row), "heading": str(row.get("heading") or ""), "text": str(row.get("body") or "")}
            for row in result.rows
            if row.get("chunk_id") is not None
        ]
        raw_count = first.get("chunk_count")
        chunk_count = raw_count if isinstance(raw_count, int) else 0
        raw_sections = first.get("section_count")
        section_count = raw_sections if isinstance(raw_sections, int) else 0
        answer: dict[str, object] = {
            **{key: str(first.get(key, "")) for key in ("source_id", "provider", "title")},
            "item_type": source_type_of(str(first.get("provider", "")), str(first.get("item_type", "")), source=self._connectors).value,
            "chunks": chunks,
            "chunk_count": chunk_count,
            "section_count": section_count,
        }
        threshold = kb.schema.sources.digest_sections_over
        if threshold is not None:
            answer["split_recommended"] = section_count > threshold
            answer["digest_sections_over"] = threshold
        return answer

    async def trace_source(self, knowledge_base: str, source: str) -> dict[str, object]:
        """The removal closure for one mirror in `knowledge_base`: what only it owns, and what else cites it.

        `source` resolves by its `source_id`, its exact title, or its URL. `only_x` is everything
        this source alone accounts for: its digest pages, its indexed mirror file, the
        `_meta/source-queue-cache` files it left, the decision pages naming it (every page type the knowledge base
        declares a `decision` block for), and its source queue status. `expected_paths`
        is every knowledge-base-relative file a removal would delete, sorted. Hand it to
        `remove_source_artifacts` unchanged. `candidates` is every other page that
        cites one of the digest pages, or, transitively, a page already found this way: a
        removal leaves each one with a dangling link, so a human looks at them first. Each
        candidate's `via` names every digest or candidate page it links through, merged rather
        than repeated when more than one path reaches it.

        Args:
            knowledge_base: The knowledge base holding the mirror.
            source: The mirror's `source_id`, exact title or URL.

        Returns:
            The source, what only it accounts for, the candidates and the expected paths.

        Raises:
            ModelRetry: `source` matches no mirror, or matches more than one.
                Also raised when `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        params = {"collection": kb.collection, "q": source}
        resolved = await self._admin.rows(_TRACE_RESOLVE_SQL, params, cap=2)
        matches = list(resolved.rows)
        if not matches:
            msg = f"no source {source!r} in {knowledge_base!r}. Give its source_id, exact title, or URL, list_undigested and search can re-ground it"
            raise ModelRetry(msg)
        if len(matches) > 1:
            ids = ", ".join(str(candidate.get("source_id")) for candidate in matches)
            msg = f"{source!r} matches more than one source in {knowledge_base!r} ({ids}). Give the exact source_id"
            raise ModelRetry(msg)
        row = matches[0]
        source_id = str(row.get("source_id") or "")
        provider_key = str(row.get("provider_key") or "")
        url = str(row.get("url") or "")

        digests_params = {"collection": kb.collection, "source_id": source_id}
        digests = await self._admin.rows(_TRACE_DIGESTS_SQL, digests_params, cap=_TRACE_DIGESTS_CAP)
        digest_pages = [{key: str(digest.get(key, "")) for key in ("id", "page_type", "title", "path")} for digest in digests.rows]
        candidates = await self._trace_inbound_closure(kb, [page["id"] for page in digest_pages])

        mirror_path = await self._trace_mirror_path(kb, provider_key)
        cache_paths = self._trace_cache_paths(kb, source_id)
        decision_pages = self._trace_decision_pages(kb, source_id)
        source_queue_status = self._trace_source_queue_status(kb, url)

        digest_paths = [str(page["path"]) for page in digest_pages if page["path"]]
        expected_paths = sorted({*([mirror_path] if mirror_path else []), *cache_paths, *digest_paths, *decision_pages})
        return {
            "source": {"source_id": source_id, "title": str(row.get("title") or ""), "provider": str(row.get("provider") or ""), "url": url},
            "only_x": {
                "digest_pages": digest_pages,
                "mirror_path": mirror_path,
                "cache_paths": cache_paths,
                "decision_pages": decision_pages,
                "source_queue_status": source_queue_status,
            },
            "candidates": candidates,
            "expected_paths": expected_paths,
        }

    async def _trace_inbound_closure(self, knowledge_base: AuthoringKnowledgeBase, only_x_ids: list[str]) -> list[dict[str, object]]:
        """Every page reachable by following inbound links from `only_x_ids`, transitively, excluding `only_x_ids` itself."""
        seen = set(only_x_ids)
        candidates: dict[str, dict[str, object]] = {}
        frontier = list(only_x_ids)
        while frontier:
            next_frontier: list[str] = []
            for seed_id in frontier:
                params = {"collection": knowledge_base.collection, "page_id": seed_id}
                inbound = await self._admin.rows(_TRACE_INBOUND_SQL, params, cap=_TRACE_INBOUND_CAP)
                for citer in inbound.rows:
                    page_id = str(citer.get("page_id") or "")
                    via = f"links {seed_id}"
                    existing = candidates.get(page_id)
                    if existing is not None:
                        vias = existing["via"]
                        if isinstance(vias, list) and via not in vias:
                            vias.append(via)
                        continue
                    if page_id in seen:
                        continue
                    seen.add(page_id)
                    candidates[page_id] = {
                        "page_id": page_id,
                        "title": str(citer.get("title") or ""),
                        "path": str(citer.get("path") or ""),
                        "via": [via],
                    }
                    next_frontier.append(page_id)
            frontier = next_frontier
        return list(candidates.values())

    async def _trace_mirror_path(self, knowledge_base: AuthoringKnowledgeBase, provider_key: str) -> str:
        """The indexed mirror file's path in the knowledge base for `provider_key`, `""` when it is already gone."""
        if not provider_key:
            return ""
        params = {"collection": knowledge_base.collection, "provider_key": provider_key}
        result = await self._admin.rows(_TRACE_MIRROR_PATH_SQL, params, cap=1)
        if not result.rows:
            return ""
        return str(result.rows[0].get("path") or "")

    def _trace_cache_paths(self, knowledge_base: AuthoringKnowledgeBase, source_id: str) -> list[str]:
        """The `_meta/source-queue-cache` files `source_id` left behind that still exist, relative to the knowledge base."""
        cache_dir = knowledge_base.path / "_meta" / "source-queue-cache"
        candidates = (cache_dir / f"{source_id}.md", cache_dir / f"{source_id}.meta.json")
        return [_knowledge_base_relative(candidate, knowledge_base.path) for candidate in candidates if candidate.is_file()]

    def _trace_decision_pages(self, knowledge_base: AuthoringKnowledgeBase, source_id: str) -> list[str]:
        """Paths of every decision page, at any status, whose text names `source_id` as a whole word."""
        pattern = re.compile(rf"\b{re.escape(source_id)}\b")
        found: list[str] = []
        for path in _decision_page_type_paths(knowledge_base.path, knowledge_base.schema):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if pattern.search(text):
                found.append(_knowledge_base_relative(path, knowledge_base.path))
        return found

    def _trace_source_queue_status(self, knowledge_base: AuthoringKnowledgeBase, url: str) -> str:
        """`url`'s row status on `knowledge_base`'s source queue, `""` when it has none (or `url` is empty)."""
        if not url:
            return ""
        _, key = classify_url(url, connectors=claimants_for(knowledge_base.schema.sources.connectors, source=self._connectors))
        entry = self._queue_for(knowledge_base).get(key)
        return entry.status.value if entry is not None else ""

    async def remove_source_artifacts(self, knowledge_base: str, source_id: str, expected_paths: list[str], reason: str = "") -> str:
        """Delete every file `trace_source(knowledge_base, source_id)` names, block the source's queue row, then reindex, all or nothing.

        `expected_paths` must be that trace's own `expected_paths`, unchanged: this tool recomputes
        the closure itself and refuses on any disagreement, so a plan drawn up before a later edit
        landed never deletes something it did not account for. Every path must also resolve under
        the knowledge base root and exist on disk, checked before the first file is unlinked. The source's
        queue row (matched by its URL when the trace found one, else `source_id`) moves to
        `removed`, a terminal status no later `request_source` or watch-list scan can revive.

        A file that fails to unlink does not stop the rest: every file in the plan is attempted, the
        queue is left untouched, and the knowledge base is reindexed best-effort so a later `trace_source`
        sees the files actually gone rather than counting them as still expected.

        Args:
            knowledge_base: The knowledge base holding the source.
            source_id: The source whose artifacts to delete.
            expected_paths: The `expected_paths` of that source's `trace_source`, unchanged.
            reason: Why the source is removed, recorded on its queue row. Left empty, `owner decision`.

        Returns:
            How many files are gone, that the queue row is blocked, and whether it indexed clean.

        Raises:
            ModelRetry: the recomputed closure disagrees with `expected_paths`, a path resolves
                outside the knowledge base root, a path in the plan no longer exists, one or more files
                could not be unlinked (naming what was removed, what failed and why, and whether the
                reindex ran), or every file was removed and the queue blocked but the final
                reindex itself failed (naming the count removed, the error, and the fix).
                Also raised when `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        trace = await self.trace_source(knowledge_base, source_id)
        recomputed = cast("list[str]", trace["expected_paths"])
        if set(recomputed) != set(expected_paths):
            diff = sorted(set(recomputed) ^ set(expected_paths))
            msg = f"the removal plan drifted: {diff}, re-run trace_source and bring the fresh plan"
            raise ModelRetry(msg)
        root = kb.path.resolve()
        paths = sorted(set(expected_paths))
        resolved: list[Path] = []
        for rel in paths:
            candidate = (kb.path / rel).resolve()
            if not candidate.is_relative_to(root):
                msg = f"{rel!r} resolves outside the root of {knowledge_base}, nothing deleted"
                raise ModelRetry(msg)
            resolved.append(candidate)
        missing = [rel for rel, candidate in zip(paths, resolved, strict=True) if not candidate.is_file()]
        if missing:
            msg = f"the removal plan drifted: {missing} no longer exist, re-run trace_source and bring the fresh plan"
            raise ModelRetry(msg)
        removed_files: list[str] = []
        failures: list[tuple[str, str]] = []
        for rel, candidate in zip(paths, resolved, strict=True):
            try:
                candidate.unlink()
            except OSError as error:
                failures.append((rel, str(error)))
            else:
                removed_files.append(rel)
        if failures:
            heal = (await self._reindex(kb)).outcome
            removed_desc = ", ".join(removed_files) or "none"
            failed_desc = ", ".join(f"{rel} ({error})" for rel, error in failures)
            if heal == "deferred":
                reindex_desc = "the knowledge base indexes once this run ends, so until then trace_source still lists the removed files"
                retry = "once this run has ended re-run"
            else:
                reindex_desc = "the reindex also failed" if heal == "not_indexed" else "the knowledge base was reindexed so trace_source sees the partial state"
                retry = "re-run"
            msg = (
                f"removal of {source_id} failed partway through: removed {removed_desc}. Could not remove {failed_desc}. "
                f"{reindex_desc}. The queue was not touched. Fix those files (permissions, locks), then {retry} "
                "trace_source and call remove_source_artifacts again with the fresh plan"
            )
            raise ModelRetry(msg)
        source_row = cast("dict[str, str]", trace["source"])
        queue = self._queue_for(kb)
        now = datetime.now(tz=UTC)
        queue.remove(
            source_row["url"] or source_id,
            reason=f"removed {now.date().isoformat()}: {reason or 'owner decision'}",
            found_at=now,
        )
        detail = await self._reindex_after_removal(kb, knowledge_base, source_id, len(paths))
        return f"removed {source_id} from {knowledge_base}: {len(paths)} file(s) gone, queue blocked, {detail}"

    async def _reindex_after_removal(self, knowledge_base: AuthoringKnowledgeBase, name: str, source_id: str, removed: int) -> str:
        """Reindex once every file is gone and the queue is blocked, or say plainly that only the reindex failed."""
        result = await self._reindex(knowledge_base)
        if result.outcome == "not_indexed":
            msg = (
                f"removed {source_id} from {name}, {removed} file(s) gone, queue blocked, the reindex "
                f"did not run ({result.detail}), run studio index for {name} to bring the knowledge base's own state back in sync"
            )
            raise ModelRetry(msg)
        return _reindex_clause(result)

    async def knowledge_base_profile(self, knowledge_base: str) -> dict[str, object]:
        """`knowledge_base`'s ground truth in one answer: its purpose, its schema vocabulary, its page types, its decision history, and its compounding map.

        `purpose` is `<knowledge_base>/_meta/purpose.md`'s text, `""` when the file is absent. `vocabulary`
        is a compact digest of the knowledge base's schema: each facet's key and seed values, each relation's
        slug and description, the claim-type vocabulary, each claim field a writer fills with a link (`claim_link_fields`,
        each a `field` and whether it `holds` `one` link or a `list`, leaving out `disputed_by`, which validation sets,
        and a claim's `edges` pairs), and the entity types. `entity_page_type`
        is the page type entity pages are written as, `""` when the knowledge base keeps none. `page_types` lists
        every page type this knowledge base declares: its own authoring guidance, and each canonical section's
        slug, declared or derived heading, and guidance, the ceremony `skeleton` seeds one page from, read here for
        every page type at once. Each page type's `edge_fields` lists the frontmatter fields its links to other
        pages go in, each as `field`, whether it `holds` `one` link or a `list`, and the `domain`, `range` and
        `description` of the relation of that name, all `""` when the schema declares no such relation.
        `decisions` lists each of the knowledge base's declared decision page types (its
        `open` status, its `outcomes`) together with every one of its pages (id, title, status), page type
        then id order, so a step can look back over past decisions without re-proposing one already
        resolved. A knowledge base declaring no decision page type gets an empty list, no query issued.
        `compounding` lists every derivation entry this knowledge base declares, each dumped by its wire
        shape (`name`, `from`, `to`, `cap_per_run`, `gate`, `guidance`), the map `compounding_work`
        reads one entry's pending inputs against. A step that needs the base's own ground truth (a
        novelty check, a contradiction check, that lookback) gets it handed in rather than having to
        search for it.

        Args:
            knowledge_base: The knowledge base to describe.

        Returns:
            The purpose, vocabulary, entity page type, page types, decisions and compounding map.

        Raises:
            ModelRetry: `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        purpose = _purpose_text(kb)
        schema = kb.schema
        vocabulary = {
            "facets": [{"key": facet.key, "seed": list(facet.seed)} for facet in schema.facets],
            "relations": [{"slug": relation.slug, "description": relation.description} for relation in schema.relations],
            "claim_types": [{"value": entry.value, "definition": entry.definition} for entry in schema.claim_types],
            "claim_link_fields": _claim_link_fields(schema.claims),
            "entity_types": [{"value": entry.value, "definition": entry.definition} for entry in schema.entity_types],
        }
        entity_page_type = schema.entities.page_type if schema.entities is not None else ""
        relations = {relation.slug: relation for relation in schema.relations}
        page_types = [_page_type_digest(page_type, relations) for page_type in schema.page_types]
        decisions_out = await self._decisions_digest(kb)
        compounding = [entry.model_dump() for entry in schema.compounding.entries]
        return {
            "purpose": purpose,
            "vocabulary": vocabulary,
            "entity_page_type": entity_page_type,
            "page_types": page_types,
            "decisions": decisions_out,
            "compounding": compounding,
        }

    async def _decisions_digest(self, knowledge_base: AuthoringKnowledgeBase) -> list[dict[str, object]]:
        """Each of `knowledge_base`'s declared decision page types with its open status, outcomes, and its pages (id, title, status)."""
        decision_page_types = knowledge_base.schema.decision_page_types
        if not decision_page_types:
            return []
        page_type_names = [page_type.page_type for page_type in decision_page_types]
        result = await self._admin.rows(
            _DECISION_PAGE_TYPE_PAGES_SQL, {"collection": knowledge_base.collection, "page_types": page_type_names}, cap=_DECISION_PAGE_TYPE_PAGES_CAP
        )
        pages_by_page_type: dict[str, list[dict[str, str]]] = {name: [] for name in page_type_names}
        for row in result.rows:
            page_type_name = str(row.get("page_type", ""))
            pages_by_page_type.setdefault(page_type_name, []).append({key: str(row.get(key, "")) for key in ("id", "title", "status")})
        digests: list[dict[str, object]] = []
        for page_type in decision_page_types:
            decision = page_type.decision
            if decision is None:  # narrows for pyright. decision_page_types only ever holds page types with one.
                continue
            digests.append(
                {
                    "page_type": page_type.page_type,
                    "open": decision.open,
                    "outcomes": [outcome.model_dump() for outcome in decision.outcomes],
                    "pages": pages_by_page_type.get(page_type.page_type, []),
                }
            )
        return digests

    async def recall_beliefs(self, knowledge_base: str) -> list[dict[str, str]] | str:
        """`knowledge_base`'s live agent-memory cards (the same listing `MEMORY.md` renders) as `{id, text}` entries.

        `text` is a card's summary (the distilled belief itself), falling back to its title when a
        card has no summary. Answers a plain sentence, not an exception, when this deployment has
        not wired agent memory for `knowledge_base` (its tools were built with `memory_for=None`).

        Args:
            knowledge_base: The knowledge base whose agent memory to read.

        Returns:
            The cards, or a sentence saying agent memory is not mounted.

        Raises:
            ModelRetry: `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        if self._memory_for is None:
            return f"agent memory is not mounted for {knowledge_base!r} in this deployment"
        store = self._memory_for(kb)
        rows = await store.cards()
        return [{"id": page_id, "text": summary or title} for page_id, title, summary in rows]

    async def remember_belief(self, knowledge_base: str, text: str) -> str:
        """Write one distilled belief note into `knowledge_base`'s agent memory, filed under the `project` category.

        Each call writes a fresh note (the store keeps one distilled belief per note). It never
        overwrites a prior belief. Answers a plain sentence, not an exception, when this deployment
        has not wired agent memory for `knowledge_base`.

        Args:
            knowledge_base: The knowledge base whose agent memory to write.
            text: The distilled belief.

        Returns:
            The new belief's page id, or a sentence saying agent memory is not mounted.

        Raises:
            ModelRetry: `knowledge_base` names no knowledge base these tools write into.
        """
        kb = self._knowledge_base(knowledge_base)
        if self._memory_for is None:
            return f"agent memory is not mounted for {knowledge_base!r} in this deployment"
        store = self._memory_for(kb)
        path = f"{kb.collection}/beliefs/{_BELIEF_CATEGORY}/belief-{uuid4().hex[:12]}.md"
        await store.write(path, f"{text}\n", expected_version=None)
        return f"remembered for {knowledge_base}: {page_id_of(path)}"

    def tools(self) -> tuple[tuple[Callable[..., Awaitable[object]], str, str], ...]:
        """The callables with their tool names and descriptions, for a capability to mount.

        `knowledge_base_profile`, `skeleton`, `recall_beliefs`, `remember_belief`, and `skip_digest` are not in this
        set. A step mounts them individually when it needs grounding or is granted the right to skip a digest.

        Returns:
            One `(callable, name, description)` triple per tool, in a fixed order.
        """
        return (
            (
                self.write_note,
                "write_note",
                (
                    "Write one validated page into a knowledge base and index it. In section prose, cite a page as "
                    "[[its-file-stem|its.full.id]] and a claim as [[its-file-stem#^cN|^cN]]. A bare page id or claim anchor "
                    "that provably resolves is rewritten into that link for you, and every rewrite or unresolvable citation "
                    "is named in the answer's warnings. A claim's quote must appear word for word in the page's source mirror: "
                    "a quote that is only a locator is swapped with its loc when the loc is the source's words, and any other "
                    "missing quote refuses the page naming the claim."
                ),
            ),
            (
                self.check_note,
                "check_note",
                (
                    "Run write_note's checks on a page payload without writing. Fix the findings, then write. "
                    "Citation rules are write_note's: [[its-file-stem|its.full.id]] for a page, [[its-file-stem#^cN|^cN]] for a claim. "
                    "The quote check runs here too."
                ),
            ),
            (self.read_notes, "read_notes", "Read one file's text together with its open owner note threads."),
            (self.resolve_note, "resolve_note", "Mark one owner note thread on a file resolved, optionally replying first."),
            (
                self.list_pages,
                "list_pages",
                (
                    "List every page of one page_type in a knowledge base by id, at one status when asked. "
                    "With lacking_link, only the pages that have no outgoing link of that relation."
                ),
            ),
            (
                self.read_page,
                "read_page",
                "Read one indexed page by its id: its path and a ready write_note payload (frontmatter, sections, claims) of its file.",
            ),
            (self.request_source, "request_source", "Queue a URL for a knowledge base's next sync."),
            (self.list_undigested, "list_undigested", "List source mirrors that have no digest yet. Read one with read_source."),
            (self.read_source, "read_source", "Read one source mirror whole, as its chunks in order, each with the loc a quote cites."),
            (
                self.trace_source,
                "trace_source",
                (
                    "Where one source's content lives in this base: its digest pages, caches and decision pages, plus "
                    "every page linking into them, start here before removing a source, hand its expected_paths to "
                    "remove_source_artifacts."
                ),
            ),
            (
                self.remove_source_artifacts,
                "remove_source_artifacts",
                (
                    "Delete everything that depends only on one source (mirror, cache, digest pages, decision pages) and "
                    "block the source from re-ingestion, all-or-nothing, refuses when the base drifted from the "
                    "trace_source plan you pass in."
                ),
            ),
            (self.list_recent_digests, "list_recent_digests", "List the digest pages indexed within the last N hours, newest first."),
            (
                self.compounding_work,
                "compounding_work",
                (
                    "The pending inputs for one named derivation in a knowledge base's compounding policy, with the page_types and the status to write. "
                    "Pages rows carry each page's source, file and validation findings, and page_ids reads exactly those pages."
                ),
            ),
            (self.digest_skeleton, "digest_skeleton", "A ready write_note payload for one source's digest: identity filled, blanks to fill."),
            (self.source_queue_policy, "source_queue_policy", "A knowledge base's ingestion policy: its hard rules, its guidance, and its purpose."),
            (self.write_policy, "write_policy", "Replace a knowledge base's ingestion policy on the owner's word, rules merged over the current ones."),
            (
                self.source_queue_rows,
                "source_queue_rows",
                "A knowledge base's source queue: counts per status, the undigested-mirror backlog, then the rows newest first.",
            ),
            (self.source_queue_decide, "source_queue_decide", "Fetch or drop one source queue row on the owner's word."),
            (
                self.decide_page,
                "decide_page",
                (
                    "Move one page to one of its page_type's declared statuses when the owner decided it, with an optional reason and "
                    "the link fields an outcome asks for. An empty status keeps the page's current status, so a step whose instructions "
                    "ask for it can write only link fields and a reason. With add true, the ids join what each named field holds "
                    "instead of replacing it."
                ),
            ),
            (
                self.resolve_entities,
                "resolve_entities",
                (
                    "Resolve the names a page mentions to one entity page id each, creating a page for each name that matches none. "
                    "A name matches a page's title or alias once case, punctuation and a leading 'the' are set aside, and a matched "
                    "page takes the name's new aliases. Pass every name in one call, each with its entity_type, aliases and a "
                    'one-sentence summary. The answer is {"entities": [{"name", "id", "created"}]} in input order.'
                ),
            ),
        )

    def _knowledge_base(self, name: str) -> AuthoringKnowledgeBase:
        found = self._knowledge_bases.get(name)
        if found is None:
            known = ", ".join(sorted(self._knowledge_bases))
            msg = f"no knowledge base named {name!r}. The ones you can write into: {known}"
            raise ModelRetry(msg)
        return found
