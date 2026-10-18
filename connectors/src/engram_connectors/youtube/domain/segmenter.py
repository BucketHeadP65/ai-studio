"""The markdown a transcript renders to, one numbered section per chapter or time window.

Every heading opens with a plain number, `## <n> [<mm:ss>] <title>`, so the indexer gives each section a section path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from engram_connectors.youtube.domain.transcript import TranscriptSegment, VideoTranscript

_WINDOW_SECONDS = 180.0
_TITLE_WORD_COUNT = 6
_SECONDS_PER_MINUTE = 60


class _Section(NamedTuple):
    """One heading-plus-prose block, before it is rendered to markdown."""

    start_seconds: float
    title: str
    body: str


def _format_timestamp(seconds: float) -> str:
    """`mm:ss`, zero-padded, or `h:mm:ss` once the total reaches an hour."""
    total = round(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, remaining_seconds = divmod(remainder, _SECONDS_PER_MINUTE)
    if hours:
        return f"{hours}:{minutes:02d}:{remaining_seconds:02d}"
    return f"{minutes:02d}:{remaining_seconds:02d}"


def _chapter_sections(doc: VideoTranscript) -> list[_Section]:
    """One section per chapter, its body the text of every segment inside the chapter's span."""
    chapters = doc.chapters
    sections: list[_Section] = []
    for index, chapter in enumerate(chapters):
        end = chapters[index + 1].start_seconds if index + 1 < len(chapters) else float("inf")
        body = " ".join(segment.text for segment in doc.segments if chapter.start_seconds <= segment.start_seconds < end)
        sections.append(_Section(start_seconds=chapter.start_seconds, title=chapter.title, body=body))
    return sections


def _window_title(body: str) -> str:
    """The window's heading text: its first few words of prose."""
    return " ".join(body.split()[:_TITLE_WORD_COUNT])


def _windowed_sections(doc: VideoTranscript) -> list[_Section]:
    """Sections over fixed ~180-second windows, each window's heading drawn from its own prose."""
    sections: list[_Section] = []
    window: list[TranscriptSegment] = []
    window_start: float | None = None

    for segment in doc.segments:
        if window_start is not None and segment.start_seconds - window_start >= _WINDOW_SECONDS:
            body = " ".join(item.text for item in window)
            sections.append(_Section(start_seconds=window_start, title=_window_title(body), body=body))
            window = []
        if not window:
            window_start = segment.start_seconds
        window.append(segment)

    if window and window_start is not None:
        body = " ".join(item.text for item in window)
        sections.append(_Section(start_seconds=window_start, title=_window_title(body), body=body))

    return sections


def transcript_markdown(doc: VideoTranscript) -> str:
    """The mirror body for `doc`: one `## <n> [<mm:ss>] <title>` section per chapter or window, prose beneath.

    A transcript with chapters gets one section per chapter. One without gets 180-second windows, each titled with
    its first six words. The text ends with a newline.

    Args:
        doc: The transcript to render.

    Returns:
        The segmented transcript markdown.
    """
    sections = _chapter_sections(doc) if doc.chapters else _windowed_sections(doc)
    blocks = [
        f"## {index} [{_format_timestamp(section.start_seconds)}] {section.title}\n\n{section.body}"
        for index, section in enumerate(sections, start=1)
    ]
    return "\n\n".join(blocks) + "\n"
