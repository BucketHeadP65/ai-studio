"""The public surface of the sources slice."""

from engram.sources.domain.conversion import ConversionResult
from engram.sources.domain.mirror import MirrorFile, MirrorFrontmatter, MirrorStatus
from engram.sources.domain.source_item import Creator, ItemPreview, SourceItem, UnresolvedItem
from engram.sources.domain.source_queue import (
    NOT_SERVED,
    Resolved,
    SourceQueueDecision,
    SourceQueueEntry,
    SourceQueueStatus,
    SourceType,
    UrlClaimant,
    arxiv_id_of,
    classify_url,
    doi_of,
    url_key,
)
from engram.sources.domain.sync import SyncError, SyncMirrors, SyncReport
from engram.sources.domain.watch_list import FoundSource, ScanReport, ScanWatchList, WatchEntry
from engram.sources.exceptions import ConnectorNotInstalledError, ConversionError, ScanError, SourceQueueUnreadableError
from engram.sources.infrastructure.source_queue import SourceQueue
from engram.sources.infrastructure.watch_list import WatchListFile
from engram.sources.infrastructure.xml_scanner import known_scanners
from engram.sources.internal_contracts import Connector, DocumentConverter, MirrorStore, Scanner
from engram.sources.registry import (
    ConnectorSource,
    ScannerSource,
    available_connectors,
    available_converters,
    claimants_for,
    connector_class,
    converter_factory,
    scanner_class,
)
from engram.sources.service.sync import MultiConnectorSync, SyncMirrorsHandler
from engram.sources.service.watch_list import ScanWatchListHandler

__all__ = [
    "NOT_SERVED",
    "Connector",
    "ConnectorNotInstalledError",
    "ConnectorSource",
    "ConversionError",
    "ConversionResult",
    "Creator",
    "DocumentConverter",
    "FoundSource",
    "ItemPreview",
    "MirrorFile",
    "MirrorFrontmatter",
    "MirrorStatus",
    "MirrorStore",
    "MultiConnectorSync",
    "Resolved",
    "ScanError",
    "ScanReport",
    "ScanWatchList",
    "ScanWatchListHandler",
    "Scanner",
    "ScannerSource",
    "SourceItem",
    "SourceQueue",
    "SourceQueueDecision",
    "SourceQueueEntry",
    "SourceQueueStatus",
    "SourceQueueUnreadableError",
    "SourceType",
    "SyncError",
    "SyncMirrors",
    "SyncMirrorsHandler",
    "SyncReport",
    "UnresolvedItem",
    "UrlClaimant",
    "WatchEntry",
    "WatchListFile",
    "arxiv_id_of",
    "available_connectors",
    "available_converters",
    "claimants_for",
    "classify_url",
    "connector_class",
    "converter_factory",
    "doi_of",
    "known_scanners",
    "scanner_class",
    "url_key",
]
