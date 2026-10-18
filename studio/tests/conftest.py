"""Root fixtures for the knowledge base app's suite.

`tests.support.knowledge_base_db` is loaded as a plugin here rather than imported per test: the knowledge base
tiers' `pg` / `migrated` / `reader_url` fixtures live in a support module, and only a plugin
declaration at the root conftest makes them visible to every tier that asks for them.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

import studio.knowledge_base.settings

from tests.support.http_app import SETTINGS_JSON

if TYPE_CHECKING:
    from pathlib import Path

pytest_plugins = ["engram.testing.infrastructure.knowledge_base_db"]


@pytest.fixture(autouse=True)
def _settings_json_off_to_one_side(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reads the developer's real `application_settings.json`. Autouse, because forgetting it is silent.

    `ApplicationSettings` falls back to `engram/config/application_settings.json` whenever a
    construction omits `json_file`, so a test that builds settings from init kwargs alone still
    merges whatever the developer's live config holds. A populated `knowledge_bases` registry there
    makes such a construction refuse outright, and the machine's own database URL silently becomes
    an invisible test input. The redirect points the fallback at a neutral file carrying every
    declared section over a database nothing in a unit test connects to. A test that WANTS a JSON
    source passes its own `json_file` explicitly, which bypasses this constant.
    """
    fallback = tmp_path / "application_settings.fallback.json"
    fallback.write_text(json.dumps(SETTINGS_JSON), encoding="utf-8")
    monkeypatch.setattr(studio.knowledge_base.settings, "_APPLICATION_SETTINGS_JSON", fallback)
