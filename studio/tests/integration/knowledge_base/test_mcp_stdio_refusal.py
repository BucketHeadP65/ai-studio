"""`studio mcp` run as its own process, refusing before it serves, keeps its stdout empty for the MCP client.

An MCP client reads the server's stdout as JSON-RPC, so a refusal written there reaches the client as a broken line
and a closed connection. Run from a folder that is no knowledge base, with none named, the process must exit 1 with
the refusal on stderr and nothing at all on stdout.
"""

from __future__ import annotations

import subprocess
import sys

from pathlib import Path

_STUDIO = Path(sys.executable).parent / "studio"


def test_a_refusal_before_serving_goes_to_stderr_and_stdout_stays_empty(tmp_path: Path) -> None:
    finished = subprocess.run([str(_STUDIO), "mcp"], cwd=tmp_path, capture_output=True, text=True, timeout=120, check=False)  # noqa: S603 (the studio script of this environment, run with fixed arguments)

    assert finished.returncode == 1
    assert finished.stdout == ""
    assert "No knowledge base named" in finished.stderr
