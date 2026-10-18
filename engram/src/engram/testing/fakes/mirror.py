"""In-memory `MirrorStore` fake for reconcile tests.

`index` maps each key to its `MirrorFile` and `bodies` maps each key to its Markdown body.
Changes are recorded in `written` and `deleted`. Seed it through the constructor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from engram.sources.contracts import MirrorFile, MirrorStatus

if TYPE_CHECKING:
    from collections.abc import Mapping

    from engram.sources.contracts import MirrorFrontmatter


@dataclass
class FakeMirrorStore:
    """Serves the `MirrorStore` port from in-memory dicts and records writes and deletes."""

    index: dict[str, MirrorFile] = field(default_factory=dict)
    bodies: dict[str, str] = field(default_factory=dict)
    assets: dict[str, dict[str, bytes]] = field(default_factory=dict)
    written: list[MirrorFrontmatter] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    async def load_index(self) -> dict[str, MirrorFile]:
        """Snapshot the index.

        Returns:
            A copy of `index`.
        """
        return dict(self.index)

    async def read_body(self, key: str) -> str | None:
        """Read the body of an active mirror.

        Args:
            key: The mirror's source key.

        Returns:
            The body, or None when the key is absent or removed.
        """
        entry = self.index.get(key)
        if entry is None or entry.status is MirrorStatus.REMOVED:
            return None
        return self.bodies.get(key)

    async def write(self, *, frontmatter: MirrorFrontmatter, body: str, assets: Mapping[str, bytes] | None = None) -> None:
        """Insert or replace the mirror's index entry and body, and record the frontmatter.

        The stored `MirrorFile` keeps the frontmatter's `converter` and `provider`. Assets merge
        per file, so an empty write keeps earlier assets.

        Args:
            frontmatter: The mirror's frontmatter, recorded in `written`.
            body: The Markdown body.
            assets: Asset files by name, merged into the earlier ones.
        """
        self.index[frontmatter.source_key] = MirrorFile(
            source_key=frontmatter.source_key,
            source_version=frontmatter.source_version,
            source_hash=frontmatter.source_hash,
            converter=frontmatter.converter,
            provider=frontmatter.provider,
            status=frontmatter.status,
        )
        self.bodies[frontmatter.source_key] = body
        self.assets.setdefault(frontmatter.source_key, {}).update(assets or {})
        self.written.append(frontmatter)

    async def soft_delete(self, key: str) -> bool:
        """Mark the entry `REMOVED` and record the key.

        Args:
            key: The mirror's source key.

        Returns:
            True when the key was present.
        """
        entry = self.index.get(key)
        if entry is None:
            return False
        self.index[key] = entry.model_copy(update={"status": MirrorStatus.REMOVED})
        self.deleted.append(key)
        return True
