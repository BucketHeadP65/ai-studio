"""`studio feedback` lists what agents filed through submit_feedback, newest first, and never loses a line in silence."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

from typer.testing import CliRunner

from studio.knowledge_base.cli import app
from studio.knowledge_base.cli_support import read_feedback

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def _entry(created_at: str, category: str, message: str, tool: str | None = None) -> str:
    return json.dumps({"created_at": created_at, "category": category, "message": message, "tool": tool})


def _knowledge_base(root: Path, lines: list[str]) -> Path:
    (root / "_meta").mkdir(parents=True)
    (root / "knowledge-base-schema.json").write_text(json.dumps({"slug": "alpha", "page_types": [{"page_type": "note"}]}), encoding="utf-8")
    (root / "_meta" / "mcp-feedback.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


class TestReadFeedback:
    def test_entries_come_back_newest_first(self, tmp_path: Path):
        root = _knowledge_base(tmp_path, [_entry("2026-07-16T10:00:00+00:00", "bug", "old"), _entry("2026-08-20T10:00:00+00:00", "usage", "new")])

        entries, unreadable = read_feedback(root / "_meta" / "mcp-feedback.jsonl")

        assert [entry.message for entry in entries] == ["new", "old"]
        assert unreadable == ()

    def test_a_line_that_will_not_parse_is_reported_by_number_not_dropped(self, tmp_path: Path):
        root = _knowledge_base(
            tmp_path,
            [_entry("2026-07-16T10:00:00+00:00", "bug", "old"), "{broken", _entry("2026-08-20T10:00:00+00:00", "usage", "new")],
        )

        entries, unreadable = read_feedback(root / "_meta" / "mcp-feedback.jsonl")

        assert len(entries) == 2
        assert unreadable == (2,)

    def test_a_missing_file_is_no_feedback(self, tmp_path: Path):
        assert read_feedback(tmp_path / "absent.jsonl") == ((), ())


class TestFeedbackCommand:
    def test_lists_category_tool_and_age_newest_first(self, tmp_path: Path):
        root = _knowledge_base(
            tmp_path,
            [
                _entry("2026-07-16T10:00:00+00:00", "bug", "priority facet reports zero pages", tool="run_sql"),
                _entry("2026-08-20T10:00:00+00:00", "usage", "fetch levels unclear", tool="fetch"),
            ],
        )

        result = runner.invoke(app, ["feedback", "--knowledge-base", str(root)])

        assert result.exit_code == 0
        lines = result.stdout.splitlines()
        assert lines[0] == f"knowledge base alpha at {root}"
        assert "usage" in lines[1]
        assert "fetch" in lines[1]
        assert "fetch levels unclear" in lines[1]
        assert "bug" in lines[2]
        assert "run_sql" in lines[2]
        assert "ago" in lines[1]

    def test_since_keeps_only_entries_after_the_date(self, tmp_path: Path):
        root = _knowledge_base(tmp_path, [_entry("2026-07-16T10:00:00+00:00", "bug", "old"), _entry("2026-08-20T10:00:00+00:00", "usage", "new")])

        result = runner.invoke(app, ["feedback", "--knowledge-base", str(root), "--since", "2026-08-01"])

        listing = "\n".join(result.stdout.splitlines()[1:])  # the knowledge base line carries the tmp path, excluded here
        assert "new" in listing
        assert "old" not in listing

    def test_a_malformed_since_exits_2_and_names_the_expected_format(self, tmp_path: Path):
        root = _knowledge_base(tmp_path, [_entry("2026-07-16T10:00:00+00:00", "bug", "old")])

        result = runner.invoke(app, ["feedback", "--knowledge-base", str(root), "--since", "not-a-date"])

        assert result.exit_code == 2
        assert "YYYY-MM-DD" in result.stderr

    def test_no_file_says_so_and_exits_zero(self, tmp_path: Path):
        root = tmp_path / "quiet"
        root.mkdir()
        (root / "knowledge-base-schema.json").write_text(json.dumps({"slug": "quiet", "page_types": [{"page_type": "note"}]}), encoding="utf-8")

        result = runner.invoke(app, ["feedback", "--knowledge-base", str(root)])

        assert result.exit_code == 0
        assert "no feedback filed" in result.stdout

    def test_an_unreadable_line_is_named(self, tmp_path: Path):
        root = _knowledge_base(tmp_path, [_entry("2026-07-16T10:00:00+00:00", "bug", "old"), "{broken"])

        result = runner.invoke(app, ["feedback", "--knowledge-base", str(root)])

        assert "line 2 could not be read" in result.stdout
