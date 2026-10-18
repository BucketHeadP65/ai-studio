"""Unit tests that the index cuts text with no heading where the chunk planner says, with stored cut plans and filling as the fallback."""

from __future__ import annotations

from dataclasses import replace

from engram.indexing.contracts import ChunkSizes, MirrorDoc, content_hash
from engram.indexing.service.row_builder import RowBuilder
from engram.kb.contracts import SectionSummarySpec
from engram.testing.specs import research_spec

_SIZES = ChunkSizes(target_tokens=40, max_tokens=80)

_PARAGRAPHS = [f"Paragraph {index} records the tide at the harbour wall at dawn." for index in range(12)]

_BODY = "\n\n".join(_PARAGRAPHS)


def _row_builder() -> RowBuilder:
    spec = research_spec()
    return RowBuilder(
        "research",
        section_summary_spec=SectionSummarySpec(),
        source_key_field=spec.sources.key_field,
        source_hash_field=spec.sources.hash_field,
        date_field="created",
        chunk_sizes=_SIZES,
    )


def test_the_row_builder_cuts_a_mirror_by_its_cut_plans_in_step_with_its_embed_inputs() -> None:
    text = f"---\nsource_key: tide-log\nprovider: example-library\nsource_hash: h-tide\n---\n{_BODY}\n"
    mirror = replace(MirrorDoc.from_text("Sources/tide-log.md", text, "research"), cut_plans={content_hash(_BODY): (5, 9)})
    builder = _row_builder()

    builder.add_source(mirror)

    assert [row["body"] for row in builder.chunks] == ["\n\n".join(_PARAGRAPHS[0:4]), "\n\n".join(_PARAGRAPHS[4:8]), "\n\n".join(_PARAGRAPHS[8:12])]
    assert [row["id"] for row in builder.chunks] == list(builder.embed_inputs)
