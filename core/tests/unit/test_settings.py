"""The shared infrastructure section and the reader that takes it out of a settings file."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from core.settings import GatewaySettings, InfrastructureSettings, read_infrastructure

if TYPE_CHECKING:
    from pathlib import Path

DATABASE = {"url": "postgresql+asyncpg://owner:pw@localhost:5434/kb", "reader_url": "postgresql+asyncpg://reader:pw@localhost:5434/kb"}


def test_only_the_database_is_required() -> None:
    settings = InfrastructureSettings.model_validate({"database": DATABASE})

    assert settings.azure_openai.endpoint == ""
    assert settings.openrouter.api_key == ""
    assert settings.harness.base_url == "http://127.0.0.1:8770"


def test_a_missing_database_is_refused() -> None:
    with pytest.raises(ValueError, match="database"):
        InfrastructureSettings.model_validate({})


def test_the_reader_takes_the_infrastructure_section_and_ignores_the_rest(tmp_path: Path) -> None:
    path = tmp_path / "application_settings.json"
    path.write_text(json.dumps({"retrieval": {"dialect": "pg_textsearch"}, "infrastructure": {"database": DATABASE, "gateway": {"api_key": "k"}}}))

    settings = read_infrastructure(path)

    assert settings.database.reader_url == DATABASE["reader_url"]
    assert settings.gateway.api_key == "k"


def test_a_file_without_the_section_is_refused_naming_it(tmp_path: Path) -> None:
    path = tmp_path / "application_settings.json"
    path.write_text(json.dumps({"resources": {"database": DATABASE}}))

    with pytest.raises(ValueError, match="infrastructure"):
        read_infrastructure(path)


def test_a_missing_file_raises_os_error(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="No such file"):
        read_infrastructure(tmp_path / "absent.json")


def test_the_gateway_reaches_typesafe_on_its_type_safe_route_by_default() -> None:
    assert GatewaySettings().typesafe_route == "type-safe"
