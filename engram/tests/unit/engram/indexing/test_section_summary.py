r"""Unit tests for `src/engram/indexing/domain/section_summary.py`.

Pins each branch of the indexed `kb.sections.summary` derivation: a body under the inline
gate returns verbatim; a long prose body returns its leading N sentences; a long body that opens
with a list returns its leading N items flattened to text; a body that is table/code only (no
prose, no list) returns empty; a heading with no body before its first subheading returns its
child headings, comma-joined. Pure functions, no IO.
"""

from __future__ import annotations

from typing import Literal

from engram.indexing.domain.section_summary import derive_section_summary
from engram.kb.contracts import Section, SectionSummarySpec

_DEFAULT_SPEC = SectionSummarySpec()

# Pushes a body past the default 1000-char inline gate with a trailing fenced code block, which
# `derive_section_summary` never scans for sentences/list items — real padding a test can ignore.
_PAD = "\n\n```\n" + ("padding " * 200) + "\n```\n"


def _section(
    *, heading: str = "H", path: str = "h", body: str = "", level: Literal[2, 3] = 2, canon: str | None = None, ord_: int = 0
) -> Section:
    return Section(heading=heading, level=level, path=path, canon=canon, body=body, ord=ord_)


class TestVerbatimUnderGate:
    def test_short_body_is_returned_verbatim(self) -> None:
        section = _section(body="A short section.")
        assert derive_section_summary(section, (section,), _DEFAULT_SPEC) == "A short section."

    def test_gate_boundary_changes_behavior(self) -> None:
        body = "One. Two. Three."
        section = _section(body=body)
        under_gate = SectionSummarySpec(inline_gate_chars=1000)
        over_gate = SectionSummarySpec(inline_gate_chars=5, leading_sentences=1)
        assert derive_section_summary(section, (section,), under_gate) == body
        assert derive_section_summary(section, (section,), over_gate) == "One."


class TestLeadingProseSentences:
    def test_takes_the_leading_n_sentences(self) -> None:
        body = (
            "Fast retrieval is the headline result. It holds even under concurrent load. "
            "Costs stay flat past ten thousand documents. A fourth sentence trails off here."
        ) + _PAD
        section = _section(body=body)
        summary = derive_section_summary(section, (section,), SectionSummarySpec(leading_sentences=2))
        assert summary == "Fast retrieval is the headline result. It holds even under concurrent load."

    def test_fewer_sentences_than_requested_returns_every_available_sentence(self) -> None:
        body = "Only one real sentence here. And a second one to be sure." + _PAD
        section = _section(body=body)
        summary = derive_section_summary(section, (section,), SectionSummarySpec(leading_sentences=5))
        assert summary == "Only one real sentence here. And a second one to be sure."

    def test_abbreviations_do_not_cause_a_false_split(self) -> None:
        body = "See e.g. the appendix for details." + _PAD
        section = _section(body=body)
        summary = derive_section_summary(section, (section,), SectionSummarySpec(leading_sentences=1))
        assert summary == "See e.g. the appendix for details."


class TestLeadingProseStopsAtStructuralBoundary:
    def test_prose_before_a_list_does_not_pull_in_list_text(self) -> None:
        body = "Intro sentence one. Intro sentence two. Intro sentence three.\n\n- Item A.\n- Item B.\n" + _PAD
        section = _section(body=body)
        summary = derive_section_summary(section, (section,), SectionSummarySpec(leading_sentences=5))
        assert summary == "Intro sentence one. Intro sentence two. Intro sentence three."


class TestLeadingListItems:
    def test_takes_the_leading_n_items_flattened(self) -> None:
        body = "- Fast retrieval.\n- Cheap to operate.\n- Scales to millions.\n" + _PAD
        section = _section(body=body)
        summary = derive_section_summary(section, (section,), SectionSummarySpec(leading_sentences=2))
        assert summary == "Fast retrieval.; Cheap to operate."

    def test_ordered_lists_are_recognized_too(self) -> None:
        body = "1. First step.\n2. Second step.\n3. Third step.\n" + _PAD
        section = _section(body=body)
        summary = derive_section_summary(section, (section,), SectionSummarySpec(leading_sentences=1))
        assert summary == "First step."


class TestTableOrCodeOnly:
    def test_a_fenced_code_block_yields_an_empty_summary(self) -> None:
        body = "```python\n" + ("x = 1\n" * 200) + "```\n"
        section = _section(body=body)
        assert derive_section_summary(section, (section,), _DEFAULT_SPEC) == ""

    def test_a_table_yields_an_empty_summary(self) -> None:
        body = "| a | b |\n| - | - |\n" + ("| 1 | 2 |\n" * 150)
        section = _section(body=body)
        assert derive_section_summary(section, (section,), _DEFAULT_SPEC) == ""


class TestHeadingWithNoBodyBeforeItsFirstSubheading:
    def test_summary_is_the_child_headings_comma_joined(self) -> None:
        parent = _section(heading="Approach", path="approach", body="")
        child_a = _section(heading="Graph Indexing", path="approach.graph_indexing", level=3, body="Some text.")
        child_b = _section(heading="Retrieval", path="approach.retrieval", level=3, body="Some other text.")
        summary = derive_section_summary(parent, (parent, child_a, child_b), _DEFAULT_SPEC)
        assert summary == "Graph Indexing, Retrieval"

    def test_a_truly_empty_leaf_section_is_an_empty_summary(self) -> None:
        section = _section(body="")
        assert derive_section_summary(section, (section,), _DEFAULT_SPEC) == ""
