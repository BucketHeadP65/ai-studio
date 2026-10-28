"""The memory store the agent's `Memory` capability mounts: an inner store holds the notes, engram indexes them.

The inner store owns the bytes, the compare-and-set versions and the operation receipts. Every successful write
also lands the note in the agent-memory knowledge base as an indexed, embedded page, and a delete marks that page retired.
A scope's `MEMORY.md` is rendered from the live cards and refuses writes. A failure to index after the inner write
raises. Retrying the same operation replays the stored result, and the page is indexed again on the next write.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from pydantic_ai_harness.memory import MemoryFile, MemorySearchMatch, MemorySearchResult, SearchableMemoryStore

from engram.agent_memory.domain import CATEGORIES, derive, is_index, path_of, render_index

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_ai_harness.memory import MemoryMutation, MemoryOperation, MemoryStore

    from engram.agent_memory.domain import NotePieces
    from engram.agent_memory.internal_contracts import CardRows, SearchHits
    from engram.retrieval.contracts import IngestDocument

_INDEX_REFUSAL = (
    "MEMORY.md is indexed from the notes and cannot be written. Write one fact per note under "
    f"<category>/<slug>.md instead (categories: {', '.join(CATEGORIES)})"
)

# Reading a deleted note back needs its whole body. No note this store holds comes near this size.
_TOMBSTONE_READ_CHARS = 1_000_000


class EngramMemoryStore:
    """A `MemoryStore` and `SearchableMemoryStore` that indexes every note into the agent-memory knowledge base."""

    def __init__(
        self,
        inner: MemoryStore,
        ingest: IngestDocument,
        cards: CardRows,
        *,
        collection: str = "agent-memory",
        search_hits: SearchHits | None = None,
    ) -> None:
        """Wrap an inner store.

        Args:
            inner: The store that holds the notes, their versions and the operation receipts.
            ingest: Indexes one page into the agent-memory knowledge base.
            cards: Reads the live cards the index file is rendered from.
            collection: The collection pages are indexed into.
            search_hits: Ranks pages for `search`. `None` falls back to the inner store's own search.
        """
        self._inner = inner
        self._ingest = ingest
        self._cards = cards
        self._collection = collection
        self._search_hits = search_hits

    async def read(self, path: str, *, max_chars: int) -> MemoryFile | None:
        """Read one file. `MEMORY.md` is rendered from the live cards, every other path comes from the inner store.

        Args:
            path: The store path.
            max_chars: The most characters of content to return.

        Returns:
            The file, or `None` when the inner store holds nothing at `path`. The index answers version `"derived"`
            and no operation id.
        """
        if is_index(path):
            content = await self._render_index(path)
            return MemoryFile(content=content[:max_chars], version="derived", operation_id=None, truncated=len(content) > max_chars)
        return await self._inner.read(path, max_chars=max_chars)

    async def get_operation(self, operation: MemoryOperation) -> MemoryMutation | None:
        """Return the stored result of an earlier operation, after the inner store checks its fingerprint.

        Args:
            operation: The operation to look up.

        Returns:
            The earlier result, or `None` when the operation never ran.
        """
        return await self._inner.get_operation(operation)

    async def write(
        self,
        path: str,
        content: str,
        *,
        expected_version: str | None,
        operation: MemoryOperation | None = None,
    ) -> MemoryMutation:
        """Check and store a note, then index it as a live page.

        The note is checked before the inner write. A replayed operation is not indexed again.

        Args:
            path: The store path, `{namespace}/{agent}/{category}/{slug}.md`.
            content: The note's markdown.
            expected_version: The version the caller last read, or `None` for a new note.
            operation: The operation that makes a retry replay the stored result.

        Returns:
            The inner store's result.

        Raises:
            ValueError: `path` names `MEMORY.md`, or the path or content breaks the note rules.
        """
        if is_index(path):
            raise ValueError(_INDEX_REFUSAL)
        pieces = derive(path, content)
        mutation = await self._inner.write(path, content, expected_version=expected_version, operation=operation)
        if not mutation.replayed:
            await self._project(pieces, status="live")
        return mutation

    async def delete(
        self,
        path: str,
        *,
        expected_version: str | None,
        operation: MemoryOperation | None = None,
    ) -> MemoryMutation:
        """Delete a note, then index its page again as retired.

        The note is checked before the inner delete. The page is retired only when the note existed and the
        operation is not a replay.

        Args:
            path: The store path.
            expected_version: The version the caller last read, or `None`.
            operation: The operation that makes a retry replay the stored result.

        Returns:
            The inner store's result.

        Raises:
            ValueError: `path` names `MEMORY.md`, or the stored note breaks the note rules.
        """
        if is_index(path):
            raise ValueError(_INDEX_REFUSAL)
        current = await self._inner.read(path, max_chars=_TOMBSTONE_READ_CHARS)
        # A note that breaks the rules must fail here, before the inner delete removes it.
        pieces = derive(path, current.content) if current is not None else None
        mutation = await self._inner.delete(path, expected_version=expected_version, operation=operation)
        if mutation.existed and not mutation.replayed and pieces is not None:
            await self._project(pieces, status="retired")
        return mutation

    async def list_paths(self, prefix: str = "", *, limit: int) -> list[str]:
        """List the stored paths that start with a prefix.

        Args:
            prefix: The store path prefix.
            limit: The most paths to return.

        Returns:
            The paths, sorted.
        """
        return await self._inner.list_paths(prefix, limit=limit)

    async def cards(self) -> Sequence[tuple[str, str, str]]:
        """Read the live cards, the same read `MEMORY.md` is rendered from.

        Returns:
            `(page_id, title, summary)` for every live page that nothing supersedes.
        """
        return await self._cards()

    async def search(
        self,
        prefix: str,
        query: str,
        *,
        limit: int,
        max_files: int,
        max_chars: int,
        max_file_chars: int,
    ) -> MemorySearchResult:
        """Search the notes under a prefix the application resolved and trusts, in every category folder below it.

        With `search_hits` wired it ranks pages in the agent-memory knowledge base, in one ranked search. Without it
        the inner store's own search answers through `search_categories`. An inner store that cannot search answers
        an empty result. A ranked hit with an empty snippet is returned with its path alone.

        Args:
            prefix: The store path prefix to search under.
            query: The search text.
            limit: The most matches to return.
            max_files: The most hits to ask the ranking for.
            max_chars: The most characters across all paths and snippets.
            max_file_chars: The most characters per file, passed to the inner store's search.

        Returns:
            The matches, how many hits were scanned, and whether anything was cut.
        """
        if self._search_hits is None:
            inner = self._inner
            if isinstance(inner, SearchableMemoryStore):
                return await search_categories(
                    inner, prefix, query, limit=limit, max_files=max_files, max_chars=max_chars, max_file_chars=max_file_chars
                )
            return MemorySearchResult(matches=[], scanned=0, truncated=False)
        hits = await self._search_hits(query, prefix=prefix, limit=max_files)
        ranked = [MemorySearchMatch(path=path_of(page_id), snippet=snippet, score=score) for page_id, snippet, score in hits]
        matches, cut = _bounded(ranked, limit=limit, max_chars=max_chars)
        return MemorySearchResult(matches=matches, scanned=len(hits), truncated=cut or len(hits) > limit)

    async def _project(self, pieces: NotePieces, *, status: str) -> None:
        """Index one note as a page at the given status."""
        frontmatter: dict[str, object] = {
            "id": pieces.page_id,
            "type": "memory",
            "title": pieces.title,
            "summary": pieces.summary,
            "status": status,
            "category": pieces.category,
        }
        if pieces.source is not None:
            frontmatter["source"] = pieces.source
        await self._ingest(
            {"frontmatter": frontmatter, "sections": [{"heading": "Note", "body": pieces.body}]},
            collection=self._collection,
            embed=True,
        )

    async def _render_index(self, index_path: str) -> str:
        """Render the index for the scope `index_path` sits in."""
        scope_prefix = index_path.rsplit("/", 1)[0] + "/"
        rows = await self._cards()
        entries = [(path, title, summary) for page_id, title, summary in rows if (path := path_of(page_id)).startswith(scope_prefix)]
        return render_index(sorted(entries))


async def search_categories(
    store: SearchableMemoryStore,
    prefix: str,
    query: str,
    *,
    limit: int,
    max_files: int,
    max_chars: int,
    max_file_chars: int,
) -> MemorySearchResult:
    """Search the notes of every category folder under a prefix, best score first, within the bounds of one search.

    A store's own search reads only the files directly under the prefix it is given, so each category folder is searched
    on its own, all at once, and the matches are ranked together and cut to `limit` matches and `max_chars` characters.

    Args:
        store: The store to search.
        prefix: The scope's store path prefix, ending in `/`.
        query: The search text.
        limit: The most matches to return.
        max_files: The most files each category's search may scan.
        max_chars: The most characters across all paths and snippets.
        max_file_chars: The most characters read from each file.

    Returns:
        The matches, how many files were scanned in all, and whether anything was cut.
    """
    found = await asyncio.gather(
        *(
            store.search(f"{prefix}{category}/", query, limit=limit, max_files=max_files, max_chars=max_chars, max_file_chars=max_file_chars)
            for category in CATEGORIES
        )
    )
    ranked = sorted((match for result in found for match in result.matches), key=lambda match: match.score, reverse=True)
    matches, cut = _bounded(ranked, limit=limit, max_chars=max_chars)
    truncated = cut or len(ranked) > limit or any(result.truncated for result in found)
    return MemorySearchResult(matches=matches, scanned=sum(result.scanned for result in found), truncated=truncated)


def _bounded(ranked: Sequence[MemorySearchMatch], *, limit: int, max_chars: int) -> tuple[list[MemorySearchMatch], bool]:
    """The best `limit` matches whose paths and snippets fit `max_chars`, and whether one was dropped for room."""
    matches: list[MemorySearchMatch] = []
    remaining = max_chars
    for match in ranked[:limit]:
        available = remaining - len(match.path)
        if available <= 0:
            return matches, True
        # A match with an empty snippet is still a match: it shows its path alone.
        clipped = match.snippet[:available]
        matches.append(MemorySearchMatch(path=match.path, snippet=clipped, score=match.score))
        remaining -= len(match.path) + len(clipped)
    return matches, False
