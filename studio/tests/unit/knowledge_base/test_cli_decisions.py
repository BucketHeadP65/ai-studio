from pathlib import Path

from engram.testing.specs import research_spec
from typer.testing import CliRunner

from studio.knowledge_base.cli import app

FLAG = """---
id: flag.try-planning-reminders
type: flag
title: Try planning reminders
summary: A live task list re-injected every turn.
status: pending
flag_kind: idea
about: ["[[talk.agents-that-plan]]"]
---

## Why

Because compaction drops the plan.

## Proposal

Mount the planning capability.

## RunEvidence

talk.agents-that-plan#findings
"""

MALFORMED = "# Not a note\n\nThis file has no frontmatter fence at all.\n"


def _knowledge_base(tmp_path: Path) -> Path:
    folder = tmp_path / "Wiki" / "Flags"
    folder.mkdir(parents=True)
    (folder / "flag.try-planning-reminders.md").write_text(FLAG, encoding="utf-8")
    (tmp_path / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
    return tmp_path


def test_decisions_list_count_and_decide(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    runner = CliRunner()

    listed = runner.invoke(app, ["decisions", "--knowledge-base", str(knowledge_base)])
    assert listed.exit_code == 0, listed.output
    assert "flag.try-planning-reminders" in listed.output

    count = runner.invoke(app, ["decisions", "--count", "--knowledge-base", str(knowledge_base)])
    assert count.exit_code == 0, count.output
    assert count.stdout.strip() == "1"

    accepted = runner.invoke(app, ["decide", "flag.try-planning-reminders", "--status", "accepted", "--knowledge-base", str(knowledge_base)])
    assert accepted.exit_code == 0, accepted.output
    assert "flag.try-planning-reminders  [flag]" in accepted.output

    count_after = runner.invoke(app, ["decisions", "--count", "--knowledge-base", str(knowledge_base)])
    assert count_after.exit_code == 0, count_after.output
    assert count_after.stdout.strip() == "0"

    dismissed = runner.invoke(app, ["decide", "flag.try-planning-reminders", "--status", "dismissed", "--knowledge-base", str(knowledge_base)])
    assert dismissed.exit_code == 0, dismissed.output
    assert "flag.try-planning-reminders  [flag]" in dismissed.output


def test_decide_writes_a_link_field(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "decide",
            "flag.try-planning-reminders",
            "--status",
            "completed",
            "--link",
            "resolved_by=q.planning",
            "--reason",
            "landed in the sprint",
            "--knowledge-base",
            str(knowledge_base),
        ],
    )

    assert result.exit_code == 0, result.output
    text = (knowledge_base / "Wiki" / "Flags" / "flag.try-planning-reminders.md").read_text(encoding="utf-8")
    assert "status: completed" in text
    assert 'resolved_by: ["[[q.planning]]"]' in text
    assert "decision_reason: landed in the sprint" in text


def test_a_malformed_link_option_is_refused(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    result = CliRunner().invoke(
        app, ["decide", "flag.try-planning-reminders", "--status", "accepted", "--link", "not-a-pair", "--knowledge-base", str(knowledge_base)]
    )
    assert result.exit_code != 0


def test_an_unknown_page_is_refused_by_id(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    result = CliRunner().invoke(app, ["decide", "flag.missing", "--status", "dismissed", "--knowledge-base", str(knowledge_base)])
    assert result.exit_code == 1
    assert "flag.missing" in result.output


def test_an_invalid_status_is_refused(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    result = CliRunner().invoke(
        app, ["decide", "flag.try-planning-reminders", "--status", "not-a-status", "--knowledge-base", str(knowledge_base)]
    )
    assert result.exit_code == 1
    assert "not-a-status" in result.output


def test_an_empty_status_is_refused_and_leaves_the_page_as_it_was(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    page = knowledge_base / "Wiki" / "Flags" / "flag.try-planning-reminders.md"
    before = page.read_text(encoding="utf-8")

    result = CliRunner().invoke(app, ["decide", "flag.try-planning-reminders", "--status", "", "--knowledge-base", str(knowledge_base)])

    assert result.exit_code == 1
    assert "--status" in result.output
    assert page.read_text(encoding="utf-8") == before


def test_decisions_on_a_knowledge_base_with_no_decisions_folder_exits_zero(tmp_path: Path) -> None:
    (tmp_path / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
    result = CliRunner().invoke(app, ["decisions", "--knowledge-base", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "No decisions." in result.output


def test_decisions_skips_a_malformed_file_and_warns_on_stderr(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    (knowledge_base / "Wiki" / "Flags" / "flag.broken.md").write_text(MALFORMED, encoding="utf-8")

    result = CliRunner().invoke(app, ["decisions", "--knowledge-base", str(knowledge_base)])

    assert result.exit_code == 0, result.output
    assert "flag.try-planning-reminders" in result.output
    assert "flag.broken.md" in result.stderr


def test_decide_on_a_malformed_page_id_fails_cleanly(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    (knowledge_base / "Wiki" / "Flags" / "flag.broken.md").write_text(MALFORMED, encoding="utf-8")

    result = CliRunner().invoke(app, ["decide", "flag.broken", "--status", "accepted", "--knowledge-base", str(knowledge_base)])

    assert result.exit_code == 1
    assert "flag.broken" in result.output
    assert "Traceback" not in result.output
