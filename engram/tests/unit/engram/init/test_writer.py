"""Unit tests for `engram/init/infrastructure/writer.py` — idempotent filesystem writer.

Tests:
  1. Writer idempotency: 2nd write skips prose/templates (skipped), does not clobber.
  2. Force mode: force=True overwrites non-vocabulary files (forced).
  3. Merge preserves human edits: hand-edited entity-type def survives; human-added row survives;
     spec rows not in existing are still present after merge.
  4. Merge is idempotent on vocabulary: writing the same scaffold twice keeps vocabulary stable.
  5. InitReport.summary() returns a non-empty string with the right counts.
  6. knowledge-base-schema.json is treated as prose (skip on 2nd write, force-overwrite only with force=True).
  7. Path-traversal guard: a relpath resolving outside knowledge_base_root (or an absolute one) refuses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _do_write(tmp_path: Path, *, force: bool = False) -> object:
    """Helper: scaffold research spec and write once into tmp_path."""
    from engram.init.contracts import scaffold, write_scaffold

    spec = research_spec()
    files = scaffold(spec)
    return write_scaffold(tmp_path, files, force=force)


# ---------------------------------------------------------------------------
# 1. Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_first_write_creates_all_files(self, tmp_path: Path) -> None:
        from engram.init.contracts import InitReport

        report = _do_write(tmp_path)
        assert isinstance(report, InitReport)
        assert len(report.written) > 0
        assert len(report.skipped) == 0
        assert len(report.merged) == 0
        assert len(report.forced) == 0

    def test_second_write_skips_prose_files(self, tmp_path: Path) -> None:
        _do_write(tmp_path)
        from engram.init.contracts import scaffold, write_scaffold

        spec = research_spec()
        files = scaffold(spec)
        report2 = write_scaffold(tmp_path, files)

        # prose/templates/knowledge_base_schema should be skipped
        assert "_meta/purpose.md" in report2.skipped
        assert "_meta/schema.md" in report2.skipped
        assert "knowledge-base-schema.json" in report2.skipped
        assert "AGENTS.md" in report2.skipped
        assert "CLAUDE.md" in report2.skipped

    def test_second_write_merges_registry_files(self, tmp_path: Path) -> None:
        _do_write(tmp_path)
        from engram.init.contracts import scaffold, write_scaffold

        spec = research_spec()
        files = scaffold(spec)
        report2 = write_scaffold(tmp_path, files)

        # Vocabulary files should be merged, not skipped or forced
        assert "_meta/vocabulary/entity-types.md" in report2.merged
        assert "_meta/vocabulary/relation-types.md" in report2.merged
        assert "_meta/vocabulary/facets.md" in report2.merged

    def test_second_write_nothing_written(self, tmp_path: Path) -> None:
        _do_write(tmp_path)
        from engram.init.contracts import scaffold, write_scaffold

        spec = research_spec()
        files = scaffold(spec)
        report2 = write_scaffold(tmp_path, files)
        assert len(report2.written) == 0

    def test_prose_file_not_clobbered(self, tmp_path: Path) -> None:
        """Human edits to purpose.md survive a second run."""
        _do_write(tmp_path)
        purpose_file = tmp_path / "_meta" / "purpose.md"
        original = purpose_file.read_text(encoding="utf-8")
        human_edit = original + "\n## My custom section\n\nHuman content here.\n"
        purpose_file.write_text(human_edit, encoding="utf-8")

        from engram.init.contracts import scaffold, write_scaffold

        spec = research_spec()
        files = scaffold(spec)
        write_scaffold(tmp_path, files)

        assert "Human content here." in purpose_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 2. Force mode
# ---------------------------------------------------------------------------


class TestForceMode:
    def test_force_overwrites_prose(self, tmp_path: Path) -> None:
        _do_write(tmp_path)
        from engram.init.contracts import scaffold, write_scaffold

        spec = research_spec()
        files = scaffold(spec)
        report2 = write_scaffold(tmp_path, files, force=True)

        assert "_meta/purpose.md" in report2.forced
        assert "AGENTS.md" in report2.forced
        assert "CLAUDE.md" in report2.forced

    def test_force_does_not_affect_skipped_list(self, tmp_path: Path) -> None:
        _do_write(tmp_path)
        from engram.init.contracts import scaffold, write_scaffold

        spec = research_spec()
        files = scaffold(spec)
        report2 = write_scaffold(tmp_path, files, force=True)

        assert len(report2.skipped) == 0

    def test_registry_still_merged_with_force(self, tmp_path: Path) -> None:
        """Vocabulary files always use union-merge, even when force=True."""
        _do_write(tmp_path)
        from engram.init.contracts import scaffold, write_scaffold

        spec = research_spec()
        files = scaffold(spec)
        report2 = write_scaffold(tmp_path, files, force=True)

        assert "_meta/vocabulary/entity-types.md" in report2.merged


# ---------------------------------------------------------------------------
# 3. Merge preserves human edits
# ---------------------------------------------------------------------------


class TestMergePreservesHumanEdits:
    def test_human_edited_definition_preserved(self, tmp_path: Path) -> None:
        """An existing entity-type definition edited by a human must not be overwritten."""
        _do_write(tmp_path)
        entity_file = tmp_path / "_meta" / "vocabulary" / "entity-types.md"
        content = entity_file.read_text(encoding="utf-8")

        # Write a row with our human-curated definition directly
        human_def = "MY CURATED DEFINITION"
        lines = content.splitlines()
        new_lines = []
        for line in lines:
            if line.startswith("| `technique` |"):
                new_lines.append(f"| `technique` | {human_def} | |")
            else:
                new_lines.append(line)
        entity_file.write_text("\n".join(new_lines), encoding="utf-8")

        # Re-run scaffold + write
        from engram.init.contracts import scaffold, write_scaffold

        spec = research_spec()
        files = scaffold(spec)
        write_scaffold(tmp_path, files)

        final = entity_file.read_text(encoding="utf-8")
        assert human_def in final, "Human-edited definition was overwritten!"

    def test_human_added_row_survives(self, tmp_path: Path) -> None:
        """A brand-new entity type added manually by a human survives a re-scaffold."""
        _do_write(tmp_path)
        entity_file = tmp_path / "_meta" / "vocabulary" / "entity-types.md"
        content = entity_file.read_text(encoding="utf-8")

        # Add a brand-new row for a type not in the spec
        new_row = "| `human-entity` | Added by a human curator | |"
        # Append after the last table row
        content_with_human_row = content.rstrip() + "\n" + new_row + "\n"
        entity_file.write_text(content_with_human_row, encoding="utf-8")

        from engram.init.contracts import scaffold, write_scaffold

        spec = research_spec()
        files = scaffold(spec)
        write_scaffold(tmp_path, files)

        final = entity_file.read_text(encoding="utf-8")
        assert "human-entity" in final, "Human-added row did not survive re-scaffold!"
        assert "Added by a human curator" in final, "Human-added definition lost!"

    def test_curated_paragraph_under_a_facet_heading_survives(self, tmp_path: Path) -> None:
        """A paragraph a curator wrote under a facet heading survives a re-scaffold."""
        _do_write(tmp_path)
        facets_file = tmp_path / "_meta" / "vocabulary" / "facets.md"
        lines = facets_file.read_text(encoding="utf-8").splitlines()
        tasks_idx = next(idx for idx, line in enumerate(lines) if line.startswith("## `tasks`"))
        lines[tasks_idx + 1 : tasks_idx + 1] = ["", "Curated note on what a task means here."]
        curated = "\n".join(lines) + "\n"
        facets_file.write_text(curated, encoding="utf-8")

        from engram.init.contracts import scaffold, write_scaffold

        write_scaffold(tmp_path, scaffold(research_spec()))

        assert facets_file.read_text(encoding="utf-8") == curated

    def test_spec_rows_still_present_after_merge(self, tmp_path: Path) -> None:
        """All spec-declared entity types survive a merge that had human edits."""
        _do_write(tmp_path)
        entity_file = tmp_path / "_meta" / "vocabulary" / "entity-types.md"
        content = entity_file.read_text(encoding="utf-8")

        # Add a human row
        content_with_human = content.rstrip() + "\n| `my-custom-type` | Custom | |\n"
        entity_file.write_text(content_with_human, encoding="utf-8")

        from engram.init.contracts import scaffold, write_scaffold
        from engram.kb.contracts import parse_vocabulary

        spec = research_spec()
        files = scaffold(spec)
        write_scaffold(tmp_path, files)

        # All spec entity types must still be present
        final = entity_file.read_text(encoding="utf-8")
        result = parse_vocabulary([("entity-types.md", final)])
        for entry in spec.entity_types:
            assert entry.value in result.index.entity_types, f"Spec entry {entry.value!r} missing after merge"


# ---------------------------------------------------------------------------
# 4. Vocabulary merge idempotency
# ---------------------------------------------------------------------------


class TestMergeIdempotency:
    def test_registry_idempotent_after_two_writes(self, tmp_path: Path) -> None:
        """Two consecutive writes of the same spec produce stable vocabulary files."""
        from engram.init.contracts import scaffold, write_scaffold
        from engram.kb.contracts import parse_vocabulary

        spec = research_spec()
        files = scaffold(spec)
        write_scaffold(tmp_path, files)
        write_scaffold(tmp_path, files)

        entity_file = tmp_path / "_meta" / "vocabulary" / "entity-types.md"
        final = entity_file.read_text(encoding="utf-8")
        result = parse_vocabulary([("entity-types.md", final)])

        # Should have exactly the spec's entity types (no duplicates)
        assert set(result.index.entity_types.keys()) == {e.value for e in spec.entity_types}


# ---------------------------------------------------------------------------
# 5. InitReport.summary()
# ---------------------------------------------------------------------------


class TestInitReport:
    def test_summary_non_empty(self, tmp_path: Path) -> None:
        from engram.init.contracts import InitReport

        report = InitReport(written=("a.md", "b.md"), skipped=("c.md",))
        summary = report.summary()
        assert len(summary) > 0
        assert "2" in summary  # two written
        assert "1" in summary  # one skipped

    def test_summary_all_up_to_date(self, tmp_path: Path) -> None:
        from engram.init.contracts import InitReport

        report = InitReport()
        assert "nothing to do" in report.summary()

    def test_first_write_summary_mentions_created(self, tmp_path: Path) -> None:
        from engram.init.contracts import InitReport

        report = _do_write(tmp_path)
        assert isinstance(report, InitReport)
        summary = report.summary()
        assert "created" in summary


# ---------------------------------------------------------------------------
# 6. knowledge-base-schema.json skipped / force-overwritten
# ---------------------------------------------------------------------------


class TestKnowledgeBaseSchemaJson:
    def test_knowledge_base_schema_skipped_on_second_write(self, tmp_path: Path) -> None:
        _do_write(tmp_path)
        from engram.init.contracts import scaffold, write_scaffold

        spec = research_spec()
        files = scaffold(spec)
        report2 = write_scaffold(tmp_path, files)

        assert "knowledge-base-schema.json" in report2.skipped

    def test_knowledge_base_schema_forced_with_force(self, tmp_path: Path) -> None:
        _do_write(tmp_path)
        from engram.init.contracts import scaffold, write_scaffold

        spec = research_spec()
        files = scaffold(spec)
        report2 = write_scaffold(tmp_path, files, force=True)

        assert "knowledge-base-schema.json" in report2.forced


# ---------------------------------------------------------------------------
# 7. Path-traversal guard: a relpath must never resolve outside knowledge_base_root
# ---------------------------------------------------------------------------


class TestPathTraversalGuard:
    """`write_scaffold` resolves every relpath through `core.files.guarded_child`.

    The belt beneath `PageTypeSpec.page_type`'s own pattern constraint, for any caller (or future
    field) that reaches a scaffold file path without going through it.
    """

    def test_refuses_a_relpath_that_escapes_the_knowledge_base_root(self, tmp_path: Path) -> None:
        from engram.init.contracts import write_scaffold

        knowledge_base_root = tmp_path / "knowledge_base"
        knowledge_base_root.mkdir()
        outside_target = tmp_path / "OUTSIDE.md"

        with pytest.raises(ValueError, match="escapes"):
            write_scaffold(knowledge_base_root, {"../OUTSIDE.md": "PWNED"})

        assert not outside_target.exists()
        assert list(knowledge_base_root.iterdir()) == []

    def test_refuses_an_absolute_relpath(self, tmp_path: Path) -> None:
        from engram.init.contracts import write_scaffold

        knowledge_base_root = tmp_path / "knowledge_base"
        knowledge_base_root.mkdir()
        absolute_target = tmp_path / "ABSOLUTE.md"

        with pytest.raises(ValueError, match="escapes"):
            write_scaffold(knowledge_base_root, {str(absolute_target): "PWNED"})

        assert not absolute_target.exists()
        assert list(knowledge_base_root.iterdir()) == []


def test_a_seed_value_holding_a_pipe_survives_the_merge_into_an_existing_vocabulary(tmp_path: Path) -> None:
    from engram.init.contracts import scaffold, write_scaffold
    from engram.kb.contracts import FacetSpec, parse_vocabulary, schema_vocabulary_drift

    before = research_spec().model_copy(update={"facets": (FacetSpec(key="venue", seed=("Plain Hall",)),)})
    after = before.model_copy(update={"facets": (FacetSpec(key="venue", seed=("Plain Hall", "Grand Hall | East Wing")),)})
    write_scaffold(tmp_path, scaffold(before))

    report = write_scaffold(tmp_path, scaffold(after))

    assert "_meta/vocabulary/facets.md" in report.merged
    documents = [(path.name, path.read_text(encoding="utf-8")) for path in sorted((tmp_path / "_meta" / "vocabulary").glob("*.md"))]
    index = parse_vocabulary(documents).index
    assert tuple(index.facets["venue"]) == ("Plain Hall", "Grand Hall | East Wing")
    assert schema_vocabulary_drift(index, after) == ()
