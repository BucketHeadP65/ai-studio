"""No module keeps this process's collaborators: every one is taken, never fetched."""

from __future__ import annotations

import pytest

from harness import session as session_module
from harness.adapters import session_log as session_log_module
from harness.adapters import topics as topics_module

GONE = [
    (session_module, "SESSION"),
    (session_module, "POOL"),
    (session_module, "REFRESH"),
    (session_module, "DECIDE_GATE"),
    (session_module, "install"),
    (session_module, "install_pool"),
    (session_module, "install_decide_gate"),
    (session_module, "install_refresh"),
    (session_module, "current"),
    (session_module, "current_pool"),
    (topics_module, "install"),
    (topics_module, "current"),
    (topics_module, "publish"),
    (topics_module, "bound"),
    (topics_module, "_BROADCASTER"),
    (session_log_module, "record"),
    (session_log_module, "events"),
    (session_log_module, "boot_sweep"),
    (session_log_module, "newest_run_id"),
]


@pytest.mark.parametrize(("module", "name"), GONE, ids=lambda value: value if isinstance(value, str) else value.__name__)
def test_the_global_is_gone(module: object, name: str) -> None:
    assert not hasattr(module, name)
