"""Unit tests for the mirror chunker: whole, headings, the cut plan, then filling, within the chunk sizes."""

from __future__ import annotations

import base64
import hashlib

from itertools import pairwise

import pytest

from pydantic import ValidationError

from engram.indexing.contracts import DEFAULT_CHUNK_SIZES, ChunkSizes, content_hash, token_count
from engram.indexing.domain.mirror_chunks import (
    DEFAULT_PLANNER_CEILING_TOKENS,
    MIRROR_CHUNK_TOKEN_BUDGET,
    PARAGRAPH_NUMBERING_VERSION,
    MirrorChunk,
    accepted_starts,
    chunk_mirror,
    cut_plan_identity,
    heading_sections_for_planner,
    planner_paragraphs,
)

_SIZES = ChunkSizes(target_tokens=400, max_tokens=800)


def _paragraph(index: int, sentences: int = 10) -> str:
    """One paragraph of short sentences, each naming its paragraph and its place, about 120 tokens for ten."""
    return " ".join(f"Reading {index}.{place} came from the north station at dawn." for place in range(sentences))


def _words(text: str) -> list[str]:
    return text.split()


class TestWhole:
    def test_a_short_document_with_no_heading_stays_one_chunk(self) -> None:
        text = "---\nsource_key: K1\n---\nThe ferry left at noon.\n\nIt reached the island by evening.\n"

        chunks = list(chunk_mirror(text, _SIZES))

        assert chunks == [MirrorChunk(ord=0, section_path=None, heading=None, body="The ferry left at noon.\n\nIt reached the island by evening.")]

    def test_a_short_document_under_one_heading_stays_one_chunk_with_its_heading(self) -> None:
        text = "# 2 Field notes\n\nThe ferry left at noon.\n\nIt reached the island by evening.\n"

        chunks = list(chunk_mirror(text, _SIZES))

        assert chunks == [
            MirrorChunk(ord=0, section_path="s2", heading="2 Field notes", body="The ferry left at noon.\n\nIt reached the island by evening.")
        ]

    def test_a_document_of_only_whitespace_has_no_chunk(self) -> None:
        assert list(chunk_mirror("---\nsource_key: K1\n---\n\n  \n", _SIZES)) == []


class TestHeadings:
    def test_a_headed_document_whose_sections_fit_is_cut_at_headings_exactly_as_before(self) -> None:
        sections = [
            ("1 Overview", "s1", "\n\n".join(_paragraph(index) for index in range(3))),
            ("1.1 Method", "s1.s1", "\n\n".join(_paragraph(index) for index in range(3, 6))),
            ("Appendix", None, "\n\n".join(_paragraph(index) for index in range(6, 9))),
            ("2 Empty", "s2", ""),
        ]
        text = "Opening line of the mirror.\n\n" + "".join(f"{'###' if '.' in heading else '##'} {heading}\n\n{body}\n\n" for heading, _, body in sections)

        chunks = list(chunk_mirror(text, _SIZES))

        assert token_count(text) > _SIZES.max_tokens
        assert all(token_count(body) <= _SIZES.max_tokens for _, _, body in sections)
        assert chunks == [
            MirrorChunk(ord=0, section_path=None, heading=None, body="Opening line of the mirror."),
            *(MirrorChunk(ord=place, section_path=path, heading=heading, body=body) for place, (heading, path, body) in enumerate(sections, start=1)),
        ]

    def test_an_oversized_heading_section_keeps_its_heading_and_section_path_on_every_chunk(self) -> None:
        long_body = "\n\n".join(_paragraph(index) for index in range(12))
        text = f"## 4 Experiments\n\n{long_body}\n\n## 5 Results\n\nThe gauge held steady.\n"

        chunks = list(chunk_mirror(text, _SIZES))

        *experiments, results = chunks
        assert len(experiments) > 1
        assert {(chunk.heading, chunk.section_path) for chunk in experiments} == {("4 Experiments", "s4")}
        assert all(token_count(chunk.body) <= _SIZES.max_tokens for chunk in experiments)
        assert _words(" ".join(chunk.body for chunk in experiments)) == _words(long_body)
        assert results == MirrorChunk(ord=len(experiments), section_path="s5", heading="5 Results", body="The gauge held steady.")


class TestCodeFences:
    def test_a_hash_line_inside_a_code_fence_opens_no_heading_section(self) -> None:
        fence = "```bash\n# install the tools\npip install harbour-gauge\n# 3 start the reader\ngauge-read --station north\n```"
        text = f"## 2 Setup\n\nRun these lines.\n\n{fence}\n\nThen check the reading.\n"

        chunks = list(chunk_mirror(text, _SIZES))

        assert chunks == [MirrorChunk(ord=0, section_path="s2", heading="2 Setup", body=f"Run these lines.\n\n{fence}\n\nThen check the reading.")]

    def test_a_document_with_no_heading_whose_fence_holds_hash_lines_stays_one_heading_section(self) -> None:
        fence = "```python\n# read the gauge\nvalue = gauge.read()\n# 1 store it\nstore(value)\n```"
        body = "\n\n".join([*_paragraphs(6), fence, *(_paragraph(index) for index in range(6, 12))])

        chunks = list(chunk_mirror(body, _SIZES))
        sections = heading_sections_for_planner(body, _SIZES, ceiling_tokens=DEFAULT_PLANNER_CEILING_TOKENS)

        assert sections == [body]
        assert {(chunk.heading, chunk.section_path) for chunk in chunks} == {(None, None)}
        assert any(fence in chunk.body for chunk in chunks)
        assert _words(" ".join(chunk.body for chunk in chunks)) == _words(body)

    def test_a_heading_after_a_closed_fence_still_opens_a_heading_section(self) -> None:
        text = "```\n# not a heading\n```\n\n## 5 Results\n\nThe gauge held steady.\n"

        chunks = list(chunk_mirror(text, _SIZES))

        assert chunks == [
            MirrorChunk(ord=0, section_path=None, heading=None, body="```\n# not a heading\n```"),
            MirrorChunk(ord=1, section_path="s5", heading="5 Results", body="The gauge held steady."),
        ]


class TestFilling:
    def test_a_headless_document_over_the_maximum_is_filled_within_the_sizes_keeping_every_word_in_order(self) -> None:
        body = "\n\n".join(_paragraph(index) for index in range(30))

        chunks = list(chunk_mirror(body, _SIZES))

        assert len(chunks) > 1
        assert all(token_count(chunk.body) <= _SIZES.target_tokens for chunk in chunks)
        assert {(chunk.heading, chunk.section_path) for chunk in chunks} == {(None, None)}
        assert all(chunk.body in body for chunk in chunks)
        assert _words(" ".join(chunk.body for chunk in chunks)) == _words(body)
        assert [chunk.ord for chunk in chunks] == list(range(len(chunks)))

    def test_a_chunk_closes_only_when_the_next_paragraph_would_pass_the_target(self) -> None:
        body = "\n\n".join(_paragraph(index, sentences=4 + index % 5) for index in range(40))

        chunks = list(chunk_mirror(body, _SIZES))

        for current, following in pairwise(chunks):
            next_paragraph = following.body.split("\n\n")[0]
            assert token_count(f"{current.body}\n\n{next_paragraph}") > _SIZES.target_tokens

    def test_a_paragraph_over_the_maximum_is_cut_at_sentence_ends(self) -> None:
        paragraph = _paragraph(1, sentences=150)

        chunks = list(chunk_mirror(paragraph, _SIZES))

        assert token_count(paragraph) > _SIZES.max_tokens
        assert len(chunks) > 1
        assert all(token_count(chunk.body) <= _SIZES.max_tokens for chunk in chunks)
        assert all(chunk.body.startswith("Reading") and chunk.body.endswith("dawn.") for chunk in chunks)
        assert _words(" ".join(chunk.body for chunk in chunks)) == _words(paragraph)

    def test_a_paragraph_over_the_maximum_is_cut_at_full_width_sentence_marks(self) -> None:
        paragraph = "".join(f"\u7b2c{index}\u53f7\u89c2\u6d4b\u7ad9\u5728\u9ece\u660e\u65f6\u8bb0\u5f55\u4e86\u8bfb\u6570\u3002" for index in range(200))

        chunks = list(chunk_mirror(paragraph, _SIZES))

        assert token_count(paragraph) > _SIZES.max_tokens
        assert len(chunks) > 1
        assert all(token_count(chunk.body) <= _SIZES.max_tokens and chunk.body.endswith("\u3002") for chunk in chunks)
        assert "".join(chunk.body for chunk in chunks) == paragraph

    def test_a_block_over_the_maximum_with_no_sentence_end_is_cut_at_line_breaks(self) -> None:
        lines = [f"| gauge {index} | north station | reading {index * 7} | steady |" for index in range(150)]
        table = "\n".join(lines)

        chunks = list(chunk_mirror(table, _SIZES))

        assert token_count(table) > _SIZES.max_tokens
        assert len(chunks) > 1
        assert all(token_count(chunk.body) <= _SIZES.max_tokens for chunk in chunks)
        assert [line for chunk in chunks for line in chunk.body.split("\n")] == lines

    def test_a_line_with_no_sentence_end_is_cut_at_the_maximum(self) -> None:
        line = " ".join(f"w{index}" for index in range(3_000))

        chunks = list(chunk_mirror(line, _SIZES))

        assert len(chunks) > 1
        assert all(token_count(chunk.body) <= _SIZES.max_tokens for chunk in chunks)
        assert all(token_count(chunk.body) > _SIZES.target_tokens for chunk in chunks[:-1])
        assert _words(" ".join(chunk.body for chunk in chunks)) == _words(line)

    def test_a_single_word_over_the_maximum_is_cut_within_it(self) -> None:
        word = "".join(f"{index:04x}" for index in range(3_000))

        chunks = list(chunk_mirror(word, _SIZES))

        assert token_count(word) > _SIZES.max_tokens
        assert all(token_count(chunk.body) <= _SIZES.max_tokens for chunk in chunks)
        assert "".join(chunk.body for chunk in chunks) == word

    def test_a_word_of_600000_characters_is_cut_into_chunks_within_the_maximum(self) -> None:
        word = base64.b64encode(b"".join(hashlib.sha256(str(index).encode()).digest() for index in range(14_000))).decode()[:600_000]

        chunks = list(chunk_mirror(word, _SIZES))

        assert "".join(chunk.body for chunk in chunks) == word
        assert all(token_count(chunk.body) <= _SIZES.max_tokens for chunk in chunks)

    def test_a_blank_line_inside_a_code_fence_never_cuts_a_chunk(self) -> None:
        fence = f"```\n{_paragraph(1, sentences=22)}\n\n{_paragraph(2, sentences=22)}\n```"
        body = "\n\n".join([_paragraph(0), fence, *(_paragraph(index) for index in range(3, 12))])

        chunks = list(chunk_mirror(body, _SIZES))

        assert token_count(fence) <= _SIZES.max_tokens
        assert any(fence in chunk.body for chunk in chunks)


class TestChunkSizes:
    def test_the_default_sizes_are_a_400_token_target_and_an_800_token_maximum(self) -> None:
        assert (DEFAULT_CHUNK_SIZES.target_tokens, DEFAULT_CHUNK_SIZES.max_tokens) == (400, 800)

    def test_a_target_over_the_maximum_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="target_tokens"):
            ChunkSizes(target_tokens=900, max_tokens=800)

    def test_a_maximum_over_the_embedding_budget_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="max_tokens"):
            ChunkSizes(target_tokens=400, max_tokens=MIRROR_CHUNK_TOKEN_BUDGET + 1)


def _paragraphs(count: int) -> list[str]:
    return [_paragraph(index) for index in range(count)]


def _plan_for(section: str, starts: tuple[int, ...]) -> dict[str, tuple[int, ...]]:
    return {content_hash(section): starts}


class TestCutPlans:
    def test_a_cut_plan_cuts_text_with_no_heading_at_its_paragraphs_as_exact_source_text(self) -> None:
        paragraphs = _paragraphs(12)
        text = "---\nsource_key: K1\n---\n" + "\n\n".join(paragraphs) + "\n"
        (section,) = heading_sections_for_planner(text, _SIZES, ceiling_tokens=DEFAULT_PLANNER_CEILING_TOKENS)

        chunks = list(chunk_mirror(text, _SIZES, _plan_for(section, (4, 9))))

        assert [chunk.body for chunk in chunks] == ["\n\n".join(paragraphs[0:3]), "\n\n".join(paragraphs[3:8]), "\n\n".join(paragraphs[8:12])]
        assert [chunk.ord for chunk in chunks] == [0, 1, 2]
        assert {(chunk.heading, chunk.section_path) for chunk in chunks} == {(None, None)}

    @pytest.mark.parametrize("starts", [(9, 4), (4, 4), (0, 4), (4, 13), (-1,)])
    def test_a_cut_plan_that_is_not_strictly_increasing_paragraph_numbers_falls_back_to_filling(self, starts: tuple[int, ...]) -> None:
        text = "\n\n".join(_paragraphs(12))
        (section,) = heading_sections_for_planner(text, _SIZES, ceiling_tokens=DEFAULT_PLANNER_CEILING_TOKENS)

        chunks = list(chunk_mirror(text, _SIZES, _plan_for(section, starts)))

        assert chunks == list(chunk_mirror(text, _SIZES))

    def test_a_chunk_of_a_cut_plan_over_the_maximum_is_filled(self) -> None:
        paragraphs = _paragraphs(12)
        text = "\n\n".join(paragraphs)
        (section,) = heading_sections_for_planner(text, _SIZES, ceiling_tokens=DEFAULT_PLANNER_CEILING_TOKENS)

        first, *rest = chunk_mirror(text, _SIZES, _plan_for(section, (2,)))

        oversized = "\n\n".join(paragraphs[1:])
        assert token_count(oversized) > _SIZES.max_tokens
        assert first.body == paragraphs[0]
        assert [chunk.body for chunk in rest] == [chunk.body for chunk in chunk_mirror(oversized, _SIZES)]
        assert all(token_count(chunk.body) <= _SIZES.max_tokens for chunk in rest)

    def test_a_cut_plan_cuts_an_oversized_heading_section_and_each_chunk_keeps_its_heading(self) -> None:
        paragraphs = _paragraphs(12)
        long_body = "\n\n".join(paragraphs)
        text = f"## 4 Experiments\n\n{long_body}\n\n## 5 Results\n\nThe gauge held steady.\n"
        (section,) = heading_sections_for_planner(text, _SIZES, ceiling_tokens=DEFAULT_PLANNER_CEILING_TOKENS)

        chunks = list(chunk_mirror(text, _SIZES, _plan_for(section, (7,))))

        assert section == long_body
        assert chunks == [
            MirrorChunk(ord=0, section_path="s4", heading="4 Experiments", body="\n\n".join(paragraphs[0:6])),
            MirrorChunk(ord=1, section_path="s4", heading="4 Experiments", body="\n\n".join(paragraphs[6:12])),
            MirrorChunk(ord=2, section_path="s5", heading="5 Results", body="The gauge held steady."),
        ]

    def test_a_cut_plan_for_other_text_changes_nothing(self) -> None:
        text = "\n\n".join(_paragraphs(12))

        chunks = list(chunk_mirror(text, _SIZES, _plan_for("Some other text.", (4, 9))))

        assert chunks == list(chunk_mirror(text, _SIZES))


class TestCutPlanIdentity:
    def test_a_cut_plan_identity_names_the_chunk_planner_and_the_paragraph_numbering_version(self) -> None:
        assert cut_plan_identity("example-model/prompt-1") == f"example-model/prompt-1/paragraphs-{PARAGRAPH_NUMBERING_VERSION}"


class TestAcceptedStarts:
    def test_paragraph_one_is_dropped_since_it_always_starts_the_first_chunk(self) -> None:
        assert accepted_starts([1, 4, 9], 12) == (4, 9)

    def test_an_empty_answer_is_accepted_as_one_chunk(self) -> None:
        assert accepted_starts([], 12) == ()

    def test_the_last_paragraph_may_start_a_chunk(self) -> None:
        assert accepted_starts([12], 12) == (12,)

    @pytest.mark.parametrize("answer", [[9, 4], [4, 4], [0], [13], [-2, 3]])
    def test_an_answer_that_is_not_strictly_increasing_paragraph_numbers_is_refused(self, answer: list[int]) -> None:
        assert accepted_starts(answer, 12) is None


class TestHeadingSectionsForPlanner:
    def test_text_with_no_heading_over_the_maximum_is_one_heading_section_numbered_by_its_paragraphs(self) -> None:
        paragraphs = _paragraphs(12)
        text = "---\nsource_key: K1\n---\n" + "\n\n".join(paragraphs) + "\n"

        sections = heading_sections_for_planner(text, _SIZES, ceiling_tokens=DEFAULT_PLANNER_CEILING_TOKENS)

        assert sections == ["\n\n".join(paragraphs)]
        assert planner_paragraphs(sections[0]) == paragraphs

    def test_text_that_fits_one_chunk_is_not_asked(self) -> None:
        assert heading_sections_for_planner("\n\n".join(_paragraphs(3)), _SIZES, ceiling_tokens=DEFAULT_PLANNER_CEILING_TOKENS) == []

    def test_text_over_the_ceiling_is_not_asked(self) -> None:
        text = "\n\n".join(_paragraphs(12))

        assert token_count(text) > 1_000
        assert heading_sections_for_planner(text, _SIZES, ceiling_tokens=1_000) == []

    def test_one_paragraph_over_the_maximum_is_not_asked_since_it_has_no_paragraph_to_start_a_chunk_at(self) -> None:
        assert heading_sections_for_planner(_paragraph(1, sentences=150), _SIZES, ceiling_tokens=DEFAULT_PLANNER_CEILING_TOKENS) == []

    def test_only_the_heading_sections_over_the_maximum_are_asked(self) -> None:
        long_body = "\n\n".join(_paragraphs(12))
        text = f"Opening line.\n\n## 1 Short\n\nThe gauge held steady.\n\n## 2 Long\n\n{long_body}\n"

        assert heading_sections_for_planner(text, _SIZES, ceiling_tokens=DEFAULT_PLANNER_CEILING_TOKENS) == [long_body]

    def test_a_blank_line_inside_a_code_fence_does_not_number_a_new_paragraph(self) -> None:
        fence = "```\nfirst line\n\nsecond line\n```"

        assert planner_paragraphs(f"Before.\n\n{fence}\n\nAfter.") == ["Before.", fence, "After."]

    def test_the_default_ceiling_is_12000_tokens(self) -> None:
        assert DEFAULT_PLANNER_CEILING_TOKENS == 12_000
