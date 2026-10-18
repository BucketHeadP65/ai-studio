"""The composition map: every slice's registrations, the adapters' leaves, and this deployment's sections."""

from __future__ import annotations

from typing import TYPE_CHECKING

import wireup

from core.resolver import Resolver
from core.wiring.contracts import ContainerResolver, sections_of

from harness.adapters import wiring as adapters_wiring
from harness.features.authoring import wiring as authoring_wiring
from harness.features.browsing import wiring as browsing_wiring
from harness.features.inspection import wiring as inspection_wiring
from harness.features.projects import wiring as projects_wiring
from harness.features.running import wiring as running_wiring
from harness.features.scheduling import wiring as scheduling_wiring
from harness.features.selfauthoring import wiring as selfauthoring_wiring
from harness.features.sessions import wiring as sessions_wiring
from harness.features.terminal import wiring as terminal_wiring

if TYPE_CHECKING:
    from types import ModuleType

    from harness.settings import HarnessSettings

WIRING_MODULES: tuple[ModuleType, ...] = (
    adapters_wiring,
    authoring_wiring,
    browsing_wiring,
    inspection_wiring,
    projects_wiring,
    running_wiring,
    scheduling_wiring,
    selfauthoring_wiring,
    sessions_wiring,
    terminal_wiring,
)
"""Every module that says what goes in the container. A slice that registers nothing is not in here."""


@wireup.injectable
def resolver_provider(container: wireup.AsyncContainer) -> Resolver:
    """A `Resolver` over the container this application owns, for code that resolves a type late.

    Args:
        container: The container this application resolves from.

    Returns:
        A resolver over `container`.
    """
    return ContainerResolver(container)


def create_container(settings: HarnessSettings) -> wireup.AsyncContainer:
    """Build the container this application resolves from.

    Every section of `settings` is registered under its own model type, so a consumer asks for the
    section it reads and never the whole settings. Everything else comes from a wiring module: the
    adapters' I/O leaves, and each slice's own list.

    Args:
        settings: What this deployment was built with.

    Returns:
        A container serving every registration, each a singleton.
    """
    return wireup.create_async_container(
        injectables=[
            *sections_of(settings),
            *(registration for module in WIRING_MODULES for registration in module.REGISTRATIONS),
            resolver_provider,
        ]
    )
