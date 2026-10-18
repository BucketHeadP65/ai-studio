"""The YouTube connector, its transcript fetcher, the transcript values and the errors a caller handles."""

from engram_connectors.youtube.domain.segmenter import transcript_markdown
from engram_connectors.youtube.domain.transcript import FetchedVideo, TranscriptSegment, VideoTranscript
from engram_connectors.youtube.domain.video import VIDEO_KEY_PREFIX, VideoRef
from engram_connectors.youtube.exceptions import TranscriptUnavailableError, VideoFetchFailedError, VideoUrlUnrecognizedError
from engram_connectors.youtube.infrastructure.connector import YouTubeConnector
from engram_connectors.youtube.infrastructure.fetcher import YouTubeTranscriptFetcher
from engram_connectors.youtube.internal_contracts import TranscriptFetcher

__all__ = [
    "VIDEO_KEY_PREFIX",
    "FetchedVideo",
    "TranscriptFetcher",
    "TranscriptSegment",
    "TranscriptUnavailableError",
    "VideoFetchFailedError",
    "VideoRef",
    "VideoTranscript",
    "VideoUrlUnrecognizedError",
    "YouTubeConnector",
    "YouTubeTranscriptFetcher",
    "transcript_markdown",
]
