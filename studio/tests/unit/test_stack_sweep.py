"""The stack's restart sweep still finds the worker child `serve-http` spawns.

`scripts/stack/stack.sh` finds the studio worker by its command line. When the module the worker runs from moves,
the sweep stops matching it, a restart leaves the old worker running, and it claims jobs with old code.
"""

import inspect
import re
import sys

from pathlib import Path

from studio.knowledge_base import cli

_STACK_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "stack" / "stack.sh"

_STUDIO_PATTERN = re.compile(r'studio-api\) dir="[^"]*" pattern="(?P<pattern>[^"]*)"')


def _studio_sweep_pattern() -> re.Pattern[str]:
    match = _STUDIO_PATTERN.search(_STACK_SCRIPT.read_text(encoding="utf-8"))
    assert match is not None, f"{_STACK_SCRIPT} no longer declares a studio-api pattern"
    return re.compile(match.group("pattern"))


def test_the_worker_child_runs_the_cli_module_by_its_real_name() -> None:
    source = inspect.getsource(cli._worker_child)

    assert f'"-m", "{cli.__name__}", "worker"' in source


def test_the_restart_sweep_matches_the_worker_child_command_line() -> None:
    command_line = f"{sys.executable} -m {cli.__name__} worker"

    assert _studio_sweep_pattern().search(command_line), f"stack.sh's studio-api pattern misses {command_line!r}"


def test_the_restart_sweep_matches_serve_http() -> None:
    assert _studio_sweep_pattern().search(f"{sys.executable} /checkout/studio/.venv/bin/studio serve-http")
