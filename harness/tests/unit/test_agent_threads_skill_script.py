"""The agent-threads skill script writes the sidecar the harness reads, byte for byte."""

from __future__ import annotations

import importlib.util
import json

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from engram.kb.contracts import Note, NoteAnchor, NoteSheet, Reply

from harness.features.projects.domain import resolve_anchor
from harness.features.projects.infrastructure.note_sidecars import load_sheet, sidecar_path

if TYPE_CHECKING:
    from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[3] / "engram" / "plugin" / "skills" / "agent-threads" / "scripts" / "notes.py"


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agent_threads_script", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sheet() -> NoteSheet:
    return NoteSheet(
        notes=(
            Note(
                id="n1",
                anchor=NoteAnchor(exact="the sea", prefix="of ", suffix=" at"),
                text="say more",
                created_at=datetime(2026, 8, 28, 10, 0, 0, tzinfo=UTC),
                replies=(Reply(author="owner", text="please", at=datetime(2026, 8, 28, 10, 0, 0, 123456, tzinfo=UTC)),),
            ),
            Note(
                id="n2",
                anchor=NoteAnchor(exact="noon"),
                text="settled",
                created_at=datetime(2026, 8, 28, 11, 0, 0, tzinfo=UTC),
                status="resolved",
            ),
        ),
    )


def _write(root: Path, rel: str, sheet: NoteSheet, body: str = "the colour of the sea at noon") -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    sidecar = sidecar_path(root, target)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(sheet.model_dump_json(), encoding="utf-8")
    return sidecar


class TestFormat:
    def test_dump_matches_model_dump_json_byte_for_byte(self, script: ModuleType) -> None:
        expected = _sheet().model_dump_json()

        assert script.dump(json.loads(expected)) == expected

    def test_a_sheet_missing_replies_and_status_normalizes_to_the_model_defaults(self, script: ModuleType) -> None:
        raw = '{"version":1,"notes":[{"id":"n1","anchor":{"exact":"x","prefix":"","suffix":""},"text":"t","created_at":"2026-08-28T10:00:00Z","archived":false}]}'

        assert script.dump(json.loads(raw)) == NoteSheet.model_validate_json(raw).model_dump_json()

    def test_non_ascii_text_is_written_unescaped_like_the_model(self, script: ModuleType) -> None:
        sheet = NoteSheet(notes=(Note(id="n1", anchor=NoteAnchor(exact="θάλασσα"), text="ναι", created_at=datetime(2026, 8, 28, tzinfo=UTC)),))
        expected = sheet.model_dump_json()

        assert script.dump(json.loads(expected)) == expected


class TestVerbs:
    def test_reply_round_trips_through_the_harness_model(self, script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        sidecar = _write(tmp_path, "docs/a.md", _sheet())

        assert script.main(["reply", "docs/a.md", "n1", "done, see the new section", "--root", str(tmp_path)]) == 0

        sheet = load_sheet(tmp_path, tmp_path / "docs" / "a.md")
        reply = sheet.notes[0].replies[-1]
        assert reply.author == "agent"
        assert reply.text == "done, see the new section"
        assert reply.at.tzinfo is not None
        assert sidecar.read_text(encoding="utf-8") == sheet.model_dump_json()
        assert "n1" in capsys.readouterr().out

    def test_resolve_then_reopen_flip_the_status(self, script: ModuleType, tmp_path: Path) -> None:
        _write(tmp_path, "docs/a.md", _sheet())
        target = tmp_path / "docs" / "a.md"

        assert script.main(["resolve", "docs/a.md", "n1", "--root", str(tmp_path)]) == 0
        assert load_sheet(tmp_path, target).notes[0].status == "resolved"
        assert script.main(["reopen", "docs/a.md", "n1", "--root", str(tmp_path)]) == 0
        assert load_sheet(tmp_path, target).notes[0].status == "open"

    def test_an_unknown_note_id_exits_2_and_names_the_ids_that_exist(self, script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write(tmp_path, "docs/a.md", _sheet())

        assert script.main(["reply", "docs/a.md", "n9", "hello", "--root", str(tmp_path)]) == 2

        err = capsys.readouterr().err
        assert "n9" in err
        assert "n1" in err
        assert "n2" in err

    def test_a_file_with_no_sidecar_refuses_a_reply_and_shows_nothing(self, script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "a.md").write_text("body", encoding="utf-8")

        assert script.main(["reply", "docs/a.md", "n1", "hello", "--root", str(tmp_path)]) == 2
        assert script.main(["show", "docs/a.md", "--root", str(tmp_path)]) == 0
        assert "No threads on docs/a.md" in capsys.readouterr().out

    def test_a_missing_file_exits_2(self, script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert script.main(["show", "docs/missing.md", "--root", str(tmp_path)]) == 2

        err = capsys.readouterr().err
        assert "docs/missing.md" in err

    def test_a_malformed_sidecar_exits_2_and_names_the_path(self, script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        target = tmp_path / "docs" / "a.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("body", encoding="utf-8")
        sidecar = sidecar_path(tmp_path, target)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text('{"version":1,"notes":[', encoding="utf-8")

        assert script.main(["show", "docs/a.md", "--root", str(tmp_path)]) == 2

        err = capsys.readouterr().err
        assert str(sidecar) in err

    def test_list_shows_open_threads_and_all_includes_resolved(self, script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write(tmp_path, "docs/a.md", _sheet())

        assert script.main(["list", "--root", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert "n1" in out
        assert "n2" not in out
        assert "line 1" in out

        assert script.main(["list", "--all", "--root", str(tmp_path)]) == 0
        assert "n2" in capsys.readouterr().out

    def test_show_reports_a_lost_anchor(self, script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write(tmp_path, "docs/a.md", _sheet(), body="nothing here matches")

        assert script.main(["show", "docs/a.md", "--root", str(tmp_path)]) == 0
        assert "anchor lost" in capsys.readouterr().out

    def test_root_before_the_verb_is_accepted(self, script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert script.main(["--root", str(tmp_path), "list"]) == 0
        assert str(tmp_path) in capsys.readouterr().out


class TestHomes:
    def test_the_nearest_agent_threads_ancestor_owns_the_sidecar(self, script: ModuleType, tmp_path: Path) -> None:
        (tmp_path / ".agent-threads").mkdir()
        (tmp_path / "sub" / ".agent-threads").mkdir(parents=True)

        assert script.sidecar_for(tmp_path, Path("sub/x.md")) == tmp_path / "sub" / ".agent-threads" / "x.md.threads.json"
        assert script.sidecar_for(tmp_path, Path("y.md")) == tmp_path / ".agent-threads" / "y.md.threads.json"
        assert script.sidecar_for(tmp_path, Path("other/z.md")) == tmp_path / ".agent-threads" / "other" / "z.md.threads.json"

    def test_a_root_without_agent_threads_is_still_the_home(self, script: ModuleType, tmp_path: Path) -> None:
        assert script.sidecar_for(tmp_path, Path("y.md")) == tmp_path / ".agent-threads" / "y.md.threads.json"

    def test_list_walks_every_home_under_the_root(self, script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write(tmp_path, "docs/a.md", _sheet())
        nested = tmp_path / "sub"
        (nested / ".agent-threads").mkdir(parents=True)
        _write(nested, "b.md", _sheet())

        assert script.main(["list", "--root", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert "docs/a.md" in out
        assert "sub/b.md" in out

    def test_show_and_reply_read_a_sidecar_at_another_home(self, script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        root_sidecar = _write(tmp_path, "sub/x.md", _sheet())
        before = root_sidecar.read_text(encoding="utf-8")
        (tmp_path / "sub" / ".agent-threads").mkdir(parents=True, exist_ok=True)

        assert script.main(["show", "sub/x.md", "--root", str(tmp_path)]) == 0
        assert "n1" in capsys.readouterr().out

        assert script.main(["reply", "sub/x.md", "n1", "answered", "--root", str(tmp_path)]) == 0

        nested_sidecar = tmp_path / "sub" / ".agent-threads" / "x.md.threads.json"
        sheet = load_sheet(tmp_path / "sub", tmp_path / "sub" / "x.md")
        assert sheet.notes[0].replies[-1].author == "agent"
        assert sheet.notes[0].replies[-1].text == "answered"
        assert nested_sidecar.read_text(encoding="utf-8") == sheet.model_dump_json()
        assert root_sidecar.read_text(encoding="utf-8") == before


class TestAnchors:
    @pytest.mark.parametrize(
        ("text", "anchor"),
        [
            ("the colour of the sea at noon", NoteAnchor(exact="the sea")),
            ("the sea, the sea at noon, the sea", NoteAnchor(exact="the sea", prefix="", suffix=" at")),
            ("the sea, the sea at noon, the sea", NoteAnchor(exact="the sea")),
            ("the\n  sea at noon", NoteAnchor(exact="the sea")),
            ("nothing here", NoteAnchor(exact="the sea")),
            ("nothing here", NoteAnchor(exact="")),
            ("line one\nline two\nthe sea", NoteAnchor(exact="the sea")),
            ("alpha repeat beta and gamma repeat delta", NoteAnchor(exact="repeat", prefix="gamma ", suffix=" rewritten")),
            ("alpha repeat beta and gamma repeat delta", NoteAnchor(exact="repeat", prefix="rewritten ", suffix=" delta")),
            (
                "- [[#R3. Certification|R3. Certification]]\n\nNew tail.\n\n## R3. Certification\n\nA rewritten body.",
                NoteAnchor(exact="R3. Certification", prefix="Old tail.\n\n## ", suffix="\n\nSource: the status field."),
            ),
            ("## target\n\nbody target here and body target there", NoteAnchor(exact="target", prefix="gone ", suffix=" gone")),
            ("intro\n\n## Notes\n\nfirst\n\n## Notes\n\nsecond", NoteAnchor(exact="Notes", prefix="gone\n\n## ", suffix="\n\ngone")),
        ],
    )
    def test_resolve_line_agrees_with_the_harness(self, script: ModuleType, text: str, anchor: NoteAnchor) -> None:
        hit = resolve_anchor(text, anchor)
        expected = None if hit is None else hit.line

        assert script.resolve_line(text, anchor.model_dump()) == expected
