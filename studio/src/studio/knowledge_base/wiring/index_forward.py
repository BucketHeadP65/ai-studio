"""What the indexing area registers for the outbox: the subscriber that tells the harness about each index's changed pages."""

from __future__ import annotations

from functools import partial

from wireup import injectable

from studio.knowledge_base.indexing.infrastructure.harness_forward import HarnessIndexForward, registered_knowledge_base_named
from studio.knowledge_base.settings import HarnessServiceSettings, SettingsFile


@injectable
def harness_index_forward_provider(harness: HarnessServiceSettings, settings_file: SettingsFile) -> HarnessIndexForward:
    """Provide the subscriber that posts each index's changed pages to the harness, at the one base URL this app was configured with.

    It names each knowledge base from the registry of knowledge bases in the settings file, read again for every event.

    Args:
        harness: The harness's base URL and the timeout of each request to it.
        settings_file: The settings file the registry of knowledge bases is read from.

    Returns:
        The harness index forward.
    """
    return HarnessIndexForward(
        harness.base_url,
        knowledge_base_named=partial(registered_knowledge_base_named, settings_file.path),
        timeout_seconds=harness.timeout_seconds,
    )


REGISTRATIONS: list[object] = [harness_index_forward_provider]
"""Everything this module puts in the container."""
