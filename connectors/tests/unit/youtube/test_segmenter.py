"""Unit tests for the transcript segmenter — the markdown the chunker consumes.

Goldens pin the exact heading/body shape for a chaptered and an unchaptered transcript; the
regex test is a drift guard against `chunk_mirror`'s real numbered-heading pattern, imported
rather than copied so the two can never silently diverge.
"""

from __future__ import annotations

import pytest

from engram.indexing.domain.mirror_chunks import _NUMBER_PREFIX_RE

from engram_connectors.youtube.contracts import TranscriptSegment, VideoTranscript
from engram_connectors.youtube.domain.segmenter import _format_timestamp, _windowed_sections, transcript_markdown
from engram_connectors.youtube.domain.transcript import Chapter

_VIDEO_ID = "dQw4w9WgXcQ"


def _chaptered_doc() -> VideoTranscript:
    return VideoTranscript(
        video_id=_VIDEO_ID,
        title="A Talk",
        channel="A Channel",
        published=None,
        segments=(
            TranscriptSegment(start_seconds=0.0, text="Welcome to the show."),
            TranscriptSegment(start_seconds=5.0, text="Today we talk about testing."),
            TranscriptSegment(start_seconds=200.0, text="Now let's discuss deployment."),
            TranscriptSegment(start_seconds=250.0, text="Deployment is tricky."),
        ),
        chapters=(
            Chapter(start_seconds=0.0, title="Introduction"),
            Chapter(start_seconds=200.0, title="Deployment"),
        ),
    )


def _unchaptered_doc() -> VideoTranscript:
    return VideoTranscript(
        video_id=_VIDEO_ID,
        title="A Talk",
        channel="A Channel",
        published=None,
        segments=(
            TranscriptSegment(start_seconds=0.0, text="This video explains the new intake pipeline for engram."),
            TranscriptSegment(start_seconds=30.0, text="We start by parsing the URL."),
            TranscriptSegment(start_seconds=185.0, text="Next we fetch the transcript from YouTube."),
            TranscriptSegment(start_seconds=210.0, text="Finally we segment it into markdown."),
        ),
    )


def test_chaptered_doc_gets_one_heading_per_chapter() -> None:
    expected = (
        "## 1 [00:00] Introduction\n"
        "\n"
        "Welcome to the show. Today we talk about testing.\n"
        "\n"
        "## 2 [03:20] Deployment\n"
        "\n"
        "Now let's discuss deployment. Deployment is tricky.\n"
    )

    assert transcript_markdown(_chaptered_doc()) == expected


def test_unchaptered_doc_gets_windowed_headings() -> None:
    expected = (
        "## 1 [00:00] This video explains the new intake\n"
        "\n"
        "This video explains the new intake pipeline for engram. We start by parsing the URL.\n"
        "\n"
        "## 2 [03:05] Next we fetch the transcript from\n"
        "\n"
        "Next we fetch the transcript from YouTube. Finally we segment it into markdown.\n"
    )

    assert transcript_markdown(_unchaptered_doc()) == expected


def test_windowed_sections_boundary_segment_opens_a_new_window() -> None:
    """A segment landing exactly at `window_start + 180.0` closes the first window and opens the second.

    `180.0 - 0.0 >= 180.0` is true, so the boundary segment must appear once, in the second
    window, never duplicated into the first and never dropped between the two.
    """
    doc = VideoTranscript(
        video_id=_VIDEO_ID,
        title="A Talk",
        channel="A Channel",
        published=None,
        segments=(
            TranscriptSegment(start_seconds=0.0, text="First window segment."),
            TranscriptSegment(start_seconds=180.0, text="Exactly on the boundary."),
        ),
    )

    sections = _windowed_sections(doc)

    assert len(sections) == 2
    assert sections[0].start_seconds == 0.0
    assert sections[0].body == "First window segment."
    assert sections[1].start_seconds == 180.0
    assert sections[1].body == "Exactly on the boundary."


@pytest.mark.parametrize("doc", [_chaptered_doc(), _unchaptered_doc()], ids=["chaptered", "unchaptered"])
def test_every_emitted_heading_matches_the_real_chunker_regex(doc: VideoTranscript) -> None:
    heading_lines = [line.removeprefix("## ") for line in transcript_markdown(doc).splitlines() if line.startswith("## ")]

    assert heading_lines
    for heading_text in heading_lines:
        assert _NUMBER_PREFIX_RE.match(heading_text) is not None


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "00:00"),
        (5.0, "00:05"),
        (65.0, "01:05"),
        (200.0, "03:20"),
        (3599.0, "59:59"),
        (3600.0, "1:00:00"),
        (3665.0, "1:01:05"),
    ],
)
def test_format_timestamp_pads_and_promotes_to_hours_past_an_hour(seconds: float, expected: str) -> None:
    assert _format_timestamp(seconds) == expected
