"""An in-memory `SourceLinkReader` stand-in for unit tests, with no knowledge base folder on disk.

:class:`FakeSourceLinks` answers a scripted map from source key to deep link instead of scanning a
knowledge base's `Sources/` folder. `links` is the scripted map and `reads` counts every read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


class FakeSourceLinks:
    """Answers a scripted source-link map, counting the reads (a :class:`SourceLinkReader`)."""

    def __init__(self, links: Mapping[str, str] | None = None) -> None:
        """Script the map to answer.

        Args:
            links: The map from source key to deep link. A key absent from it stands for a
                mirror carrying no deep link. Empty by default.
        """
        self.links = dict(links or {})
        self.reads = 0

    async def load_source_links(self) -> dict[str, str]:
        """Answer the scripted map, counting the read.

        Returns:
            A copy of `links`.
        """
        self.reads += 1
        return dict(self.links)
