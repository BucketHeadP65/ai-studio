"""Integration-tier fixtures: tests here drive the real CLI against a per-test bed."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _schema_preflight_stubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub `studio index`'s schema-at-head preflight for the whole tier.

    The preflight reads the app settings' database, never this tier's test bed. The guard's own
    behavior is proven in `tests/unit/knowledge_base/test_cli_index.py`.
    """
    monkeypatch.setattr("studio.knowledge_base.cli.assert_schema_at_head", lambda _db_url: None)
