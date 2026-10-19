"""The errors a failed connection raises, built the way the clients raise them, for the tests that check the service is named."""

from __future__ import annotations

import httpx2

from openai import APIConnectionError
from pydantic_ai.exceptions import ModelAPIError

ENDPOINT_HOST = "example.openai.azure.com"
"""The host of every endpoint these errors name."""

REFUSED_DATABASE = ConnectionRefusedError(61, "Connect call failed ('10.0.0.5', 5434)")
"""What asyncpg raises when the database refuses the connection."""


def endpoint_connection_error(path: str) -> ModelAPIError:
    """What pydantic-ai raises when a connection to the OpenAI endpoint at `path` never opened."""
    error = ModelAPIError("deployment-a", "Connection error.")
    error.__cause__ = APIConnectionError(request=httpx2.Request("POST", f"https://{ENDPOINT_HOST}/openai/v1/{path}"))
    return error
