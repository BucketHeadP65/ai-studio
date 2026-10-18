"""What the refresh area registers: the subscriber that tells the harness about each ended refresh run."""

from __future__ import annotations

from wireup import injectable

from studio.knowledge_base.refresh.infrastructure.harness_forward import HarnessRefreshForward
from studio.knowledge_base.settings import HarnessServiceSettings


@injectable
def harness_refresh_forward_provider(harness: HarnessServiceSettings) -> HarnessRefreshForward:
    """Provide the subscriber that posts ended refresh runs to the harness, at the one base URL this app was configured with.

    Args:
        harness: The harness's base URL and the timeout of each request to it.

    Returns:
        The harness forward.
    """
    return HarnessRefreshForward(harness.base_url, timeout_seconds=harness.timeout_seconds)


REGISTRATIONS: list[object] = [harness_refresh_forward_provider]
"""Everything this slice puts in the container."""
