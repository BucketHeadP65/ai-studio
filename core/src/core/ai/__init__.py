"""The shared, app-agnostic AI building blocks: model and embedding factories, the registry, capabilities and retrieval.

This module never imports `core.ai.workflows`. A consumer reaches the workflow engine by explicit submodule import.
This module also runs inside the Temporal workflow sandbox at worker startup, so a new import here is lazy.
"""

from __future__ import annotations

from core.ai.models import create_azure_model, create_azure_openai_client, create_embedding_model, create_gateway_model, create_model

__all__ = ["create_azure_model", "create_azure_openai_client", "create_embedding_model", "create_gateway_model", "create_model"]
