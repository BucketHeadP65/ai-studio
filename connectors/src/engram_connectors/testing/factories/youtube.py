"""Polyfactory factory for the YouTube transcript model.

The factory builds a deterministic, unchaptered `VideoTranscript`.
A test overrides any field per call.
"""

from __future__ import annotations

from datetime import date

from polyfactory.factories.pydantic_factory import ModelFactory

from engram_connectors.youtube.contracts import TranscriptSegment, VideoTranscript

_SEGMENTS = (
    TranscriptSegment(start_seconds=0.0, text="Hello world."),
    TranscriptSegment(start_seconds=5.0, text="Second line of the talk."),
)


class VideoTranscriptFactory(ModelFactory[VideoTranscript]):
    """Builds a deterministic, unchaptered `VideoTranscript`.

    A test overrides `video_id`, `title`, `segments`, `chapters` or any other field per call.
    """

    __use_defaults__ = True
    video_id = "abc123XYZ_-"
    title = "Sample Talk"
    channel = "Sample Channel"
    published = date(2024, 3, 5)
    segments = _SEGMENTS
