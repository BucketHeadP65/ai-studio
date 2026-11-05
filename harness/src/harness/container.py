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
from harness.features.maintenance import wiring as maintenance_wiring
from harness.features.maintenance.contracts import FailedFireNotice, LiveTrialWatch, MaintenanceRunWatch
from harness.features.projects import wiring as projects_wiring
from harness.features.running import wiring as running_wiring
from harness.features.running.contracts import RunEndObserver, RunEndObservers
from harness.features.scheduling import wiring as scheduling_wiring
from harness.features.scheduling.contracts import RunEndArming
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
    maintenance_wiring,
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


@wireup.injectable
def run_end_observer(
    arming: RunEndArming, live_trial: LiveTrialWatch, maintenance_run: MaintenanceRunWatch, failed_fire: FailedFireNotice
) -> RunEndObserver:
    """What hears each workflow run's ending once its evidence is written, each part in turn.

    The arming of after-run schedules comes first, then the live trial watch, the maintenance run watch and the
    failed-fire notice. A part that fails is logged and the next one is still told.

    Args:
        arming: Arms the schedules waiting for the run's workflow.
        live_trial: Reads a run of a version on live trial, and reverts it after a failed or worse run.
        maintenance_run: Raises the flags of a step maintenance run its plan judge rejected to the end.
        failed_fire: Tells the owner about a scheduled run that ended failed.

    Returns:
        The run-end observer.
    """
    return RunEndObservers((arming, live_trial, maintenance_run, failed_fire))


def create_container(settings: HarnessSettings) -> wireup.AsyncContainer:
    """Build the container this application resolves from.

    Every section of `settings` is registered under its own model type, so a consumer asks for the
    section it reads and never the whole settings. Everything else comes from a wiring module: the
    adapters' I/O leaves, and each slice's own list. The run-end observer is composed here, because it
    orders parts that several slices register.

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
            run_end_observer,
        ]
    )
