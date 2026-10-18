"""Unit tests for the YouTube slice domain — URL parsing and the frozen value objects.

`VideoRef.parse` is the one branchy piece of logic here (a URL-form matrix); the rest is field
storage and frozen-model behaviour that pydantic already guarantees, so only `zotero_link`-style
derived logic and immutability get pinned, matching the sibling `sources/zotero` domain tests.
"""

from __future__ import annotations

from datetime import date

import pydantic
import pytest

from engram_connectors.youtube.contracts import TranscriptSegment, VideoRef, VideoTranscript, VideoUrlUnrecognizedError
from engram_connectors.youtube.domain.transcript import Chapter
from engram_connectors.youtube.domain.video import channel_atom_url

_VIDEO_ID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={_VIDEO_ID}",
        f"https://youtube.com/watch?v={_VIDEO_ID}&list=PLabc123&index=3",
        f"https://youtu.be/{_VIDEO_ID}",
        f"https://youtu.be/{_VIDEO_ID}?si=abc123XYZ",
        f"https://www.youtube.com/shorts/{_VIDEO_ID}",
        f"https://www.youtube.com/embed/{_VIDEO_ID}",
        f"https://m.youtube.com/watch?v={_VIDEO_ID}",
        f"https://music.youtube.com/watch?v={_VIDEO_ID}",
        f"https://www.youtube.com:443/watch?v={_VIDEO_ID}",
        f"https://www.youtube.com/live/{_VIDEO_ID}",
    ],
    ids=[
        "watch",
        "watch-with-extra-query",
        "short",
        "short-with-query",
        "shorts",
        "embed",
        "mobile-host",
        "music-host",
        "explicit-port",
        "live",
    ],
)
def test_parse_accepts_every_youtube_url_form(url: str) -> None:
    assert VideoRef.parse(url).video_id == _VIDEO_ID


@pytest.mark.parametrize(
    "url",
    [
        "https://vimeo.com/123456789",
        "https://www.youtube.com/watch",
        "https://www.youtube.com/watch?v=",
        "https://www.youtube.com/watch?v=tooshort",
        "https://youtu.be/",
        "not a url at all",
    ],
    ids=[
        "non-youtube-host",
        "missing-id",
        "empty-id",
        "wrong-length-id",
        "empty-short-path",
        "malformed",
    ],
)
def test_parse_rejects_non_video_urls(url: str) -> None:
    with pytest.raises(VideoUrlUnrecognizedError) as exc_info:
        VideoRef.parse(url)

    assert url in str(exc_info.value)


def test_video_ref_is_frozen() -> None:
    ref = VideoRef.parse(f"https://youtu.be/{_VIDEO_ID}")

    with pytest.raises(pydantic.ValidationError):
        ref.video_id = "changedXXXX"  # type: ignore[misc]


def test_transcript_doc_holds_segments_and_optional_chapters() -> None:
    doc = VideoTranscript(
        video_id=_VIDEO_ID,
        title="A Talk",
        channel="A Channel",
        published=date(2026, 1, 15),
        segments=(
            TranscriptSegment(start_seconds=0.0, text="Hello."),
            TranscriptSegment(start_seconds=5.0, text="World."),
        ),
        chapters=(Chapter(start_seconds=0.0, title="Intro"),),
    )

    assert doc.video_id == _VIDEO_ID
    assert doc.published == date(2026, 1, 15)
    assert [segment.text for segment in doc.segments] == ["Hello.", "World."]
    assert doc.chapters[0].title == "Intro"


def test_transcript_doc_chapters_default_to_empty() -> None:
    doc = VideoTranscript(
        video_id=_VIDEO_ID,
        title="A Talk",
        channel="A Channel",
        published=None,
        segments=(TranscriptSegment(start_seconds=0.0, text="Hello."),),
    )

    assert doc.chapters == ()


def test_transcript_doc_is_frozen() -> None:
    doc = VideoTranscript(
        video_id=_VIDEO_ID,
        title="A Talk",
        channel="A Channel",
        published=None,
        segments=(TranscriptSegment(start_seconds=0.0, text="Hello."),),
    )

    with pytest.raises(pydantic.ValidationError):
        doc.title = "mutated"  # type: ignore[misc]


def test_a_channel_atom_url_is_the_atom_feed_youtube_publishes_for_that_channel() -> None:
    assert channel_atom_url("UCabc") == "https://www.youtube.com/feeds/videos.xml?channel_id=UCabc"
