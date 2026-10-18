"""Light packaging smoke checks: the PEP-561 typing marker ships with the distribution.

File-level assertions that need no network, so the unit suite stays offline. A real wheel
build plus inspection of its contents is a pre-publish step, not something the unit tier
depends on.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def test_py_typed_marker_is_present() -> None:
    # PEP 561: consumers only see core's inline types when this marker ships inside the package.
    assert (_ROOT / "src" / "core" / "py.typed").is_file()
