"""What the watch-list slice registers: the scan handler over the watch list, the scanners its entries name, and the source queue."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from core.observability import get_logger
from engram.config import SourceResources
from engram.sources.contracts import (
    ConnectorNotInstalledError,
    ScanWatchListHandler,
    SourceQueue,
    WatchListFile,
    known_scanners,
    scanner_class,
)
from engram.sources.infrastructure.xml_scanner import XML_SCANNER, XmlScanner
from pydantic import ValidationError
from wireup import injectable

from studio.knowledge_base.source_queue import SOURCE_QUEUE_PATH
from studio.knowledge_base.watch_list import WATCH_LIST_PATH
from studio.knowledge_base.wiring.sources import ConnectorMap

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from engram.sources.contracts import Scanner, ScannerSource

_logger = get_logger(__name__)


def watched_scanners(watch_list: WatchListFile) -> tuple[str, ...]:
    """Return the scanner names the knowledge base's watch list carries, or every known name when the file cannot be read.

    An unreadable watch list is logged and never refuses the container. The scan itself reports it, and every scanner is
    already there once the file is fixed.

    Args:
        watch_list: The knowledge base's watch list.

    Returns:
        The scanner names to build.
    """
    try:
        return watch_list.scanners()
    except (OSError, yaml.YAMLError, ValidationError) as error:
        _logger.warning("the watch list could not be read, mounting every known scanner: {reason}", reason=str(error))
        return known_scanners()


def build_scanners(resources: SourceResources, names: Iterable[str], *, source: ScannerSource | None = None) -> Mapping[str, Scanner]:
    """Build one scanner per name the knowledge base watches: the built-in `xml` reader, and every other name through the registry.

    A name no installed package registers, or whose scanner refuses to build, is left out with the reason logged. That entry
    fails at scan time and never breaks the container or refuses a sync.

    Args:
        resources: The resources every scanner builds from.
        names: The scanner names to build. A repeated name is built once.
        source: Replaces the installed scanner entry points when given. `None` reads the installed ones.

    Returns:
        The built scanners, keyed by name.
    """
    scanners: dict[str, Scanner] = {}
    for name in dict.fromkeys(names):
        if name == XML_SCANNER:
            scanners[name] = XmlScanner.build(resources)
            continue
        try:
            scanners[name] = scanner_class(name, source=source).build(resources)
        except (ConnectorNotInstalledError, ValueError) as error:
            _logger.warning("the {scanner!r} scanner is not available: {reason}", scanner=name, reason=str(error))
    return scanners


@injectable
def watch_list_handler_provider(resources: SourceResources, connectors: ConnectorMap) -> ScanWatchListHandler:
    """Provide the watch-list scan handler over the knowledge base's watch list, the scanners its entries name, and its source queue.

    Every scanner builds from the same resources a connector builds from, so a scanner that needs a resource nobody configured
    refuses by name instead of building against an invented one.

    Args:
        resources: The resources every scanner builds from.
        connectors: The declared connectors in declared order, which the source queue classifies each new URL with.

    Returns:
        The watch-list scan handler.
    """
    root = resources.knowledge_base.path
    watch_list = WatchListFile(root / WATCH_LIST_PATH)
    queue = SourceQueue(root / SOURCE_QUEUE_PATH, connectors=tuple(connectors.by_key.values()))
    return ScanWatchListHandler(watch_list, build_scanners(resources, watched_scanners(watch_list)), queue)


REGISTRATIONS: list[object] = [watch_list_handler_provider]
"""Everything this slice puts in the container, each under the type its consumers declare."""
