from pathlib import Path

from engram.sources.contracts import WatchListFile
from engram.sources.infrastructure.xml_scanner import XML_SCANNER
from typer.testing import CliRunner

from studio.knowledge_base.cli import app


def test_watch_list_add_list_remove(tmp_path: Path) -> None:
    runner = CliRunner()
    added = runner.invoke(app, ["watch-list", "add", "xml", "https://blog.example.org/feed.xml", "--knowledge-base", str(tmp_path)])
    assert added.exit_code == 0, added.output
    assert "xml  https://blog.example.org/feed.xml" in added.output
    entries = WatchListFile(tmp_path / "_meta" / "watch-list.yaml").entries()
    assert entries[0].scanner == XML_SCANNER
    listed = runner.invoke(app, ["watch-list", "list", "--knowledge-base", str(tmp_path)])
    assert "never scanned" in listed.output
    removed = runner.invoke(app, ["watch-list", "remove", "xml", "https://blog.example.org/feed.xml", "--knowledge-base", str(tmp_path)])
    assert removed.exit_code == 0
    assert WatchListFile(tmp_path / "_meta" / "watch-list.yaml").entries() == []


def test_an_unknown_scanner_is_refused_in_words(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["watch-list", "add", "podcast", "someone", "--knowledge-base", str(tmp_path)])
    assert result.exit_code == 1
    assert "unknown scanner 'podcast'" in result.output
    assert XML_SCANNER in result.output, "the refusal names the scanners this machine has"
    assert not (tmp_path / "_meta" / "watch-list.yaml").exists(), "a refused scanner never reaches the file"


def test_an_entry_whose_scanner_is_gone_can_still_be_removed(tmp_path: Path) -> None:
    WatchListFile(tmp_path / "_meta" / "watch-list.yaml").add("podcast", "someone")

    result = CliRunner().invoke(app, ["watch-list", "remove", "podcast", "someone", "--knowledge-base", str(tmp_path)])

    assert result.exit_code == 0
    assert WatchListFile(tmp_path / "_meta" / "watch-list.yaml").entries() == []


def test_removing_an_absent_entry_fails_cleanly(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["watch-list", "remove", "xml", "https://nope.example.org/feed.xml", "--knowledge-base", str(tmp_path)])
    assert result.exit_code == 1
    assert "no such watch-list entry" in result.output
