"""The xAI connection the X scanner and the source queue post resolver both use.

Importing it requires the `pydantic-ai-slim[xai]` install.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from pydantic import BaseModel
from pydantic_ai.models.xai import XaiModel
from pydantic_ai.providers.xai import XaiProvider

if TYPE_CHECKING:
    from engram.config import SourceResources

SETTINGS_KEY: Final = "xai"
"""The `SourceResources.settings` block this connection is read from."""


class XaiResource(BaseModel):
    """An xAI API connection: the API key and the model to run.

    An empty `api_key` means no connection is configured.
    """

    api_key: str = ""
    model: str = "grok-4-fast-non-reasoning"


def xai_resource(resources: SourceResources) -> XaiResource | None:
    """Read the xAI connection from the `xai` settings block.

    Args:
        resources: The resources whose `settings` may carry an `xai` block.

    Returns:
        The connection, or `None` when there is no `xai` block or it carries no `api_key`.

    Raises:
        ValidationError: The `xai` block is present but does not validate.
    """
    block = resources.settings.get(SETTINGS_KEY)
    if block is None:
        return None
    resource = XaiResource.model_validate(block)
    return resource if resource.api_key else None


def build_xai_model(resource: XaiResource) -> XaiModel:
    """Build the xAI model client for a connection.

    Args:
        resource: The connection whose model and API key the client uses.

    Returns:
        The model client, authenticated with the connection's API key.
    """
    return XaiModel(resource.model, provider=XaiProvider(api_key=resource.api_key))
