"""A standalone Studio bundle works without Claude's environment or the source checkout."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[4] / "plugin"
PACKAGER = PLUGIN.parent / "scripts" / "package_plugin.py"


def _environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith(("CLAUDE_", "PLUGIN_", "STUDIO_"))}


def _run(*args: str | Path, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [str(arg) for arg in args], cwd=cwd, env=env or _environment(), capture_output=True, text=True, timeout=15, check=False
    )


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    destination = tmp_path / "installed plugins" / "knowledge-base"
    result = _run(sys.executable, PACKAGER, destination, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    return destination


def test_packaged_skills_are_real_files_and_the_notes_helper_runs(bundle: Path, tmp_path: Path) -> None:
    assert not any(path.is_symlink() for path in bundle.rglob("*"))
    for skill in (PLUGIN / "skills").iterdir():
        assert (bundle / "skills" / skill.name / "SKILL.md").read_bytes() == (skill / "SKILL.md").read_bytes()
    assert (bundle / "skills/engram-init/knowledge-base-schema.schema.json").is_file()
    assert (bundle / "skills/designing-note-writing/note-writing-skill-template.md").is_file()
    result = _run(sys.executable, bundle / "skills/agent-threads/scripts/notes.py", "list", "--root", tmp_path, cwd=tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("schema_dir", [".", "docs", "knowledge-base"])
def test_codex_mcp_starts_from_session_cwd(bundle: Path, tmp_path: Path, schema_dir: str) -> None:
    project = tmp_path / "project with spaces"
    knowledge_base = project / schema_dir
    knowledge_base.mkdir(parents=True)
    (knowledge_base / "knowledge-base-schema.json").write_text("{}")
    manifest = json.loads((bundle / ".codex-plugin/plugin.json").read_text())
    config = manifest["mcpServers"]["knowledge-base"]
    command = config["command"]
    result = _run(command, *config.get("args", []), cwd=project, env={**_environment(), "STUDIO_BIN": "/bin/echo"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"mcp --knowledge-base {knowledge_base.resolve()}"
    assert json.loads((bundle / ".claude-plugin/plugin.json").read_text())["name"] == "knowledge-base"


def test_nudge_resolves_its_binary_without_plugin_environment(tmp_path: Path) -> None:
    (tmp_path / "knowledge-base-schema.json").write_text("{}")
    stub = tmp_path / "studio-stub"
    stub.write_text('#!/bin/sh\ncase "$1" in\nstatus) echo "knowledge base example" ;;\ndecisions) echo 2 ;;\nesac\n')
    stub.chmod(0o755)
    result = _run("bash", PLUGIN / "hooks/nudge.sh", cwd=tmp_path, env={**_environment(), "STUDIO_BIN": str(stub)})
    assert result.returncode == 0, result.stderr
    assert "knowledge base example" in result.stdout
    assert "2 pages await a decision" in result.stdout


def test_index_resolves_its_binary_without_plugin_environment(tmp_path: Path) -> None:
    (tmp_path / "knowledge-base-schema.json").write_text("{}")
    stub = tmp_path / "studio-stub"
    stub.write_text('#!/bin/sh\necho "Indexed in 0.1s"\n')
    stub.chmod(0o755)
    result = _run("bash", PLUGIN / "hooks/index.sh", cwd=tmp_path, env={**_environment(), "STUDIO_BIN": str(stub)})
    assert result.returncode == 0, result.stderr
    assert "retrieval is current" in result.stdout
    assert "Indexed in 0.1s" in (tmp_path / "_meta/ingest-auto.log").read_text()


def test_index_calls_retrieval_current_and_names_the_errors_standing_on_unchanged_pages(tmp_path: Path) -> None:
    (tmp_path / "knowledge-base-schema.json").write_text("{}")
    stub = tmp_path / "studio-stub"
    stub.write_text(
        "#!/bin/sh\n"
        "echo 'Indexed in 0.1s: 3 pages, 9 sections, 0 claims, 2 edges'\n"
        "echo 'Pruned: 0 | Embedded: 1, reused: 0'\n"
        "echo 'Standing in the knowledge base: 1 error(s), 0 warning(s)'\n"
        "echo '  ERROR note.old: broken-link: [[missing]] resolves to nothing'\n"
        "echo 'Validation: 0 error(s), 0 warning(s)'\n"
        "echo '1 feedback entry filed since the last index, read it with studio feedback'\n"
        "exit 1\n"
    )
    stub.chmod(0o755)
    result = _run("bash", PLUGIN / "hooks/index.sh", cwd=tmp_path, env={**_environment(), "STUDIO_BIN": str(stub)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "knowledge-base: knowledge base reindexed, retrieval is current. Indexed in 0.1s: 3 pages, 9 sections, 0 claims, 2 edges. "
        "Standing in the knowledge base: 1 error(s), 0 warning(s). Full report in _meta/ingest-auto.log."
    )


def test_packaging_refuses_to_overwrite_an_existing_directory(bundle: Path, tmp_path: Path) -> None:
    marker = bundle / "keep.txt"
    marker.write_text("keep")
    result = _run(sys.executable, PACKAGER, bundle, cwd=tmp_path)
    assert result.returncode != 0
    assert marker.read_text() == "keep"


def test_explicit_repack_removes_stale_generated_files(bundle: Path, tmp_path: Path) -> None:
    stale = bundle / "skills/retired/SKILL.md"
    stale.parent.mkdir()
    stale.write_text("retired")
    result = _run(sys.executable, PACKAGER, bundle, "--force", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert not stale.exists()
    assert (bundle / ".codex-plugin/plugin.json").is_file()


def test_force_never_writes_inside_the_source_checkout(tmp_path: Path) -> None:
    inside = PLUGIN.parent.parent / "knowledge-base"
    result = _run(sys.executable, PACKAGER, inside, "--force", cwd=tmp_path)
    assert result.returncode != 0
    assert "outside the source checkout" in result.stderr
    assert not inside.exists()
