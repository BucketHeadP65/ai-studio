"""In-memory transcript fetcher for connector and sync tests.

It serves the `FetchedVideo` seeded for a video id, or raises the error seeded for that id.
You seed it through the constructor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engram_connectors.youtube.contracts import FetchedVideo


@dataclass
class FakeTranscriptFetcher:
    """Transcript fetcher that records every fetch and answers from seeded videos and errors."""

    videos: dict[str, FetchedVideo] = field(default_factory=dict)
    """The video returned for each video id."""
    errors: dict[str, Exception] = field(default_factory=dict)
    """The error raised for each video id. It is checked before `videos`."""
    fetched: list[str] = field(default_factory=list)
    """Every video id asked for, in order."""

    async def fetch(self, video_id: str) -> FetchedVideo:
        """Record the video id, then answer from the seeded errors and videos.

        Args:
            video_id: The YouTube video id to fetch.

        Returns:
            The video seeded for the id.

        Raises:
            Exception: The error seeded in `errors` for the id.
            KeyError: When neither `errors` nor `videos` holds the id.
        """
        self.fetched.append(video_id)
        if video_id in self.errors:
            raise self.errors[video_id]
        return self.videos[video_id]
