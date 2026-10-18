"""The errors the YouTube connector and its transcript fetcher raise."""


class VideoUrlUnrecognizedError(Exception):
    """A URL does not resolve to a YouTube video id."""

    def __init__(self, url: str) -> None:
        """Name the URL that was refused.

        Args:
            url: The URL that does not resolve to a YouTube video id.
        """
        msg = f"not a YouTube video URL: {url}"
        super().__init__(msg)


class TranscriptUnavailableError(Exception):
    """A video exists but has no caption track with usable text."""

    def __init__(self, video_id: str) -> None:
        """Name the video that has no transcript.

        Args:
            video_id: The YouTube video id.
        """
        msg = f"no caption track available for video {video_id}"
        super().__init__(msg)


class VideoFetchFailedError(Exception):
    """A request failed, answered a non-OK status, or returned a body that fails validation. The message names the URL."""
