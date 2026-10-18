"""The studio embeds a query the way the knowledge base was embedded, or it does not embed at all.

A vector only ranks against vectors from the same deployment at the same width. The contract
therefore is not "some embedder": it is the indexing deployment and width, read from the shared
settings file, so the two cannot drift apart without somebody editing that file.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from harness.adapters.embedding import embedding_contract

if TYPE_CHECKING:
    from pathlib import Path


_DATABASE = {"url": "postgresql+asyncpg://owner:pw@localhost:5434/kb", "reader_url": "postgresql+asyncpg://reader:pw@localhost:5434/kb"}


def _settings(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "application_settings.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_the_contract_is_read_from_the_shared_settings(tmp_path: Path) -> None:
    """Deployment, connection and width come from the file that governs the knowledge base, never from here."""
    path = _settings(
        tmp_path,
        {
            "infrastructure": {"database": _DATABASE, "azure_openai": {"endpoint": "https://example.openai.azure.com"}},
            "ingestion": {"embedding_deployment": "text-embedding-3-large", "embedding_dimensions": 1024},
        },
    )

    contract = embedding_contract(path)

    assert contract.deployment == "text-embedding-3-large"
    assert contract.infrastructure.azure_openai.endpoint == "https://example.openai.azure.com"
    assert contract.dimensions == 1024


def test_the_gateway_connection_needs_the_gateway_key_and_no_azure_endpoint(tmp_path: Path) -> None:
    """Embeddings served by the gateway are reached with its key, so an Azure endpoint is not required."""
    path = _settings(
        tmp_path,
        {
            "infrastructure": {"database": _DATABASE, "embeddings": "gateway", "gateway": {"api_key": "pylf_test"}},
            "ingestion": {"embedding_deployment": "text-embedding-3-large", "embedding_dimensions": 1024},
        },
    )

    contract = embedding_contract(path)

    assert contract.infrastructure.embeddings == "gateway"
    assert contract.infrastructure.gateway.api_key == "pylf_test"
    assert contract.infrastructure.azure_openai.endpoint == ""


def test_the_gateway_connection_without_a_key_refuses_naming_the_key(tmp_path: Path) -> None:
    """A deployment that has an Azure endpoint but sends embeddings to the gateway still needs the gateway key."""
    path = _settings(
        tmp_path,
        {
            "infrastructure": {"database": _DATABASE, "embeddings": "gateway", "azure_openai": {"endpoint": "https://e"}},
            "ingestion": {"embedding_deployment": "d", "embedding_dimensions": 1024},
        },
    )

    with pytest.raises(ValueError, match=r"infrastructure\.gateway\.api_key"):
        embedding_contract(path)


def test_the_azure_connection_without_an_endpoint_refuses_naming_the_endpoint(tmp_path: Path) -> None:
    """The default connection is Azure OpenAI, which cannot be reached without its endpoint."""
    path = _settings(tmp_path, {"infrastructure": {"database": _DATABASE}, "ingestion": {"embedding_deployment": "d", "embedding_dimensions": 1024}})

    with pytest.raises(ValueError, match=r"infrastructure\.azure_openai\.endpoint"):
        embedding_contract(path)


def test_a_settings_file_missing_the_width_refuses_rather_than_guessing(tmp_path: Path) -> None:
    """A guessed width is a different vector space, and a different space fails silently.

    Defaulting the dimensions would produce vectors of a plausible shape that rank against
    nothing, so the absence has to stop the composition instead of being filled in.
    """
    path = _settings(tmp_path, {"infrastructure": {"database": _DATABASE, "azure_openai": {"endpoint": "https://e"}}, "ingestion": {"embedding_deployment": "d"}})

    with pytest.raises(ValueError, match="embedding_dimensions"):
        embedding_contract(path)


def test_a_settings_file_missing_the_deployment_refuses(tmp_path: Path) -> None:
    """Same ruling for the model: the knowledge base was embedded by a named deployment or by nothing."""
    path = _settings(tmp_path, {"infrastructure": {"database": _DATABASE, "azure_openai": {"endpoint": "https://e"}}, "ingestion": {"embedding_dimensions": 1024}})

    with pytest.raises(ValueError, match="deployment"):
        embedding_contract(path)
