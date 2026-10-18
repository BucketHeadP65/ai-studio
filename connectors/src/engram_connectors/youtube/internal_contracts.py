"""The port the YouTube connector depends on."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from engram_connectors.youtube.domain.transcript import FetchedVideo


class TranscriptFetcher(Protocol):
    """The part of the YouTube transcript fetcher the connector uses."""

    async def fetch(self, video_id: str) -> FetchedVideo:
        """Fetch one video's metadata and transcript.

        Args:
            video_id: The 11-character YouTube video id.

        Returns:
            The video's transcript and the watch URL it was fetched from.

        Raises:
            TranscriptUnavailableError: The video has no caption track, or its track has no usable text.
            VideoFetchFailedError: A request failed, answered a non-OK status, or returned a body that fails validation.
        """
        ...
