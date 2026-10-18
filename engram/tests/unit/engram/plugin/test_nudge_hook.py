"""The SessionStart nudge. The init prompt, the identity lines, and the pages-awaiting-decision count."""

import os
import subprocess

from pathlib import Path

HOOK = Path(__file__).resolve().parents[4] / "plugin" / "hooks" / "nudge.sh"


def _studio_stub(
    tmp_path: Path,
    *,
    status_lines: tuple[str, ...] = (),
    status_exit: int = 0,
    decisions_count: str | None = "0",
    decisions_exit: int = 0,
    stderr_lines: tuple[str, ...] = (),
) -> Path:
    """A `STUDIO_BIN` stand-in dispatching on its first argument (`status` or `decisions`).

    `status` echoes `status_lines` (stdout) and `stderr_lines` (stderr), then exits `status_exit`.
    `decisions` (always called `--count`) echoes `decisions_count` and exits `decisions_exit`;
    `decisions_count=None` echoes nothing at all. Written beside `tmp_path` rather than inside it,
    so it is never mistaken for a knowledge base file.
    """
    stub = tmp_path.parent / f"{tmp_path.name}-studio-stub.sh"
    decisions_echo = "" if decisions_count is None else f"    echo {decisions_count!r}\n"
    body = (
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  status)\n"
        + "".join(f"    echo {line!r} >&2\n" for line in stderr_lines)
        + "".join(f"    echo {line!r}\n" for line in status_lines)
        + f"    exit {status_exit}\n"
        "    ;;\n"
        "  decisions)\n"
        f"{decisions_echo}"
        f"    exit {decisions_exit}\n"
        "    ;;\n"
        "esac\n"
    )
    stub.write_text(body, encoding="utf-8")
    stub.chmod(0o755)
    return stub


def _run_process(cwd: Path, *, studio_bin: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(HOOK.parents[1]), "STUDIO_BIN": str(studio_bin)}
    # bash and the hook path are fixed local values, not untrusted input.
    return subprocess.run(["bash", str(HOOK)], cwd=cwd, env=env, capture_output=True, text=True, check=False)  # noqa: S603, S607


def _run(cwd: Path, *, studio_bin: Path | None = None) -> str:
    return _run_process(cwd, studio_bin=studio_bin or _studio_stub(cwd)).stdout


def test_an_uninitialized_knowledge_base_still_gets_the_init_nudge(tmp_path: Path) -> None:
    assert "/knowledge-base:init" in _run(tmp_path)


def test_pending_decisions_are_announced(tmp_path: Path) -> None:
    (tmp_path / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    stub = _studio_stub(tmp_path, decisions_count="2")

    out = _run(tmp_path, studio_bin=stub)

    assert "2 pages await a decision" in out


def test_a_knowledge_base_kept_in_its_own_folder_is_found(tmp_path: Path) -> None:
    (tmp_path / "knowledge-base").mkdir()
    (tmp_path / "knowledge-base" / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    stub = _studio_stub(tmp_path, decisions_count="2")

    out = _run(tmp_path, studio_bin=stub)

    assert "2 pages await a decision" in out


def test_a_single_pending_decision_uses_the_singular(tmp_path: Path) -> None:
    (tmp_path / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    stub = _studio_stub(tmp_path, decisions_count="1")

    out = _run(tmp_path, studio_bin=stub)

    assert "1 page awaits a decision" in out
    assert "pages await" not in out


def test_a_knowledge_base_with_no_pending_decision_says_nothing(tmp_path: Path) -> None:
    (tmp_path / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    stub = _studio_stub(tmp_path, decisions_count="0")
    assert _run(tmp_path, studio_bin=stub).strip() == ""


def test_a_failing_decisions_command_says_nothing(tmp_path: Path) -> None:
    """`studio decisions --count` failing (an unreachable spec, a missing binary) never surfaces a bad message."""
    (tmp_path / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    stub = _studio_stub(tmp_path, decisions_count=None, decisions_exit=1)
    assert _run(tmp_path, studio_bin=stub).strip() == ""


def test_a_non_numeric_decisions_output_says_nothing(tmp_path: Path) -> None:
    (tmp_path / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    stub = _studio_stub(tmp_path, decisions_count="not-a-number")
    assert _run(tmp_path, studio_bin=stub).strip() == ""


def test_the_identity_lines_print_before_the_pending_decisions_line(tmp_path: Path) -> None:
    (tmp_path / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    stub = _studio_stub(
        tmp_path,
        status_lines=(
            "knowledge base alpha at /x",
            "db localhost:5434/knowledge bases at 0034 (code expects 0034), last indexed never, never, 1 file awaiting index",
        ),
        decisions_count="1",
    )

    lines = _run(tmp_path, studio_bin=stub).splitlines()

    assert lines[0] == "knowledge base alpha at /x"
    assert lines[1].startswith("db ")
    assert lines[2] == "knowledge-base: 1 page awaits a decision in this knowledge base, run 'studio decisions' to read it."


def test_an_uninitialized_knowledge_base_never_runs_studio_status(tmp_path: Path) -> None:
    """No `knowledge-base-schema.json` means no identity read at all, only the init nudge."""
    stub = _studio_stub(tmp_path, status_lines=("should never print",), decisions_count="5")

    out = _run(tmp_path, studio_bin=stub)

    assert "should never print" not in out
    assert "5" not in out


def test_a_failing_studio_status_never_fails_the_session(tmp_path: Path) -> None:
    """A `studio status` that exits non-zero (an unreachable database) does not trip the hook's `set -e`."""
    (tmp_path / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    stub = _studio_stub(tmp_path, status_lines=("db localhost:5434/knowledge bases unreachable, connection refused.",), status_exit=1)

    result = _run_process(tmp_path, studio_bin=stub)

    assert result.returncode == 0
    assert "unreachable, connection refused." in result.stdout


def test_a_warning_on_stderr_and_a_stray_stdout_line_never_displace_the_identity_lines(tmp_path: Path) -> None:
    """Only lines shaped like the two identity lines reach the session, whatever else `studio status` prints first."""
    (tmp_path / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    stub = _studio_stub(
        tmp_path,
        status_lines=(
            "warning: --knowledge-base overrides APP_KNOWLEDGE_BASE__PATH",
            "knowledge base alpha at /x",
            "db localhost:5434/knowledge bases at 0034 (code expects 0034), last indexed never, never, 1 file awaiting index",
            "Row counts:",
        ),
        stderr_lines=("IncompleteFieldDefinitionWarning: Field 'lifespan' has an incomplete definition",),
        decisions_count="0",
    )

    lines = _run(tmp_path, studio_bin=stub).splitlines()

    assert lines == [
        "knowledge base alpha at /x",
        "db localhost:5434/knowledge bases at 0034 (code expects 0034), last indexed never, never, 1 file awaiting index",
    ]
