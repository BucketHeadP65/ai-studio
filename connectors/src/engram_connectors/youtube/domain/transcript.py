"""A fetched video's transcript: its caption lines, its chapter markers and its metadata."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict


class TranscriptSegment(BaseModel):
    """One caption line: the moment it starts and its spoken text."""

    model_config = ConfigDict(frozen=True)

    start_seconds: float
    text: str


class Chapter(BaseModel):
    """One chapter marker of a video."""

    model_config = ConfigDict(frozen=True)

    start_seconds: float
    title: str


class VideoTranscript(BaseModel):
    """A fetched video transcript: metadata, the caption timeline, and optional chapter markers."""

    model_config = ConfigDict(frozen=True)

    video_id: str
    title: str
    channel: str
    published: date | None
    segments: tuple[TranscriptSegment, ...]
    chapters: tuple[Chapter, ...] = ()


class FetchedVideo(BaseModel):
    """One fetched video: its transcript and the watch URL it came from."""

    model_config = ConfigDict(frozen=True)

    doc: VideoTranscript
    url: str
