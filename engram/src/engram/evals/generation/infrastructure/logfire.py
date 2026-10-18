"""The Logfire adapter that publishes an evaluation dataset to the hosted datasets API."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.settings import LogfireSettings
    from logfire.experimental.api_client import LogfireAPIClient

    from engram.evals.generation.domain.datasets import EvaluationDataset
    from engram.evals.generation.internal_contracts import LogfireDatasetClient


def push_dataset_to_logfire(dataset: EvaluationDataset, *, client: LogfireDatasetClient, name: str | None = None) -> None:
    """Publish a dataset to the hosted Logfire datasets API.

    The local YAML file stays the source of truth. A repeated push updates the cases by name.
    The hosted metadata the client returns is dropped, so call the client directly when you need it.

    Args:
        dataset: The dataset to publish.
        client: The Logfire datasets client (the real `LogfireAPIClient`, or a test fake).
        name: Optional hosted-dataset name override (defaults to `dataset.name`).
    """
    client.push_dataset(dataset, name=name)


def build_logfire_dataset_client(settings: LogfireSettings) -> LogfireAPIClient:
    """Construct a `LogfireAPIClient` from the configured Logfire settings.

    It uses the write token, or the read token when no write token is set, and the configured base URL.
    `logfire` is imported only when this is called.

    Args:
        settings: The `infrastructure.logfire` settings (tokens + base URL).

    Returns:
        A configured `LogfireAPIClient`, a context manager you must close after use.
    """
    from logfire.experimental.api_client import LogfireAPIClient  # noqa: PLC0415 (lazy: logfire loads only when publishing)

    return LogfireAPIClient(api_key=settings.write_token or settings.read_token, base_url=settings.base_url or None)
