r"""Unit specs for the watch-list slice's wireup registration (`studio.knowledge_base.wiring.watch_list`).

`build_scanners` and `watched_scanners` are pinned directly (no database, no network): the
knowledge base's own watch list says which scanner names to resolve, the built-in `xml` reader mounts
without a registry lookup, a registered name mounts through the registry's injectable seam, and a
name nothing answers to is left out for the scan itself to report.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from engram.config import SourceResources
from engram.sources.contracts import SourceType, WatchListFile, known_scanners
from engram.sources.infrastructure.xml_scanner import XML_SCANNER, XmlScanner
from engram.testing.fakes.connector import FakeConnector
from engram.testing.fakes.watch_list import FakeScanner

from studio.knowledge_base.settings import KnowledgeBaseSettings
from studio.knowledge_base.wiring.sources import ConnectorMap
from studio.knowledge_base.wiring.watch_list import build_scanners, watch_list_handler_provider, watched_scanners

if TYPE_CHECKING:
    from pathlib import Path

    from engram.sources.contracts import Scanner

_PODCAST = "podcast"


class _PodcastScanner:
    """A registered scanner stand-in: `build` hands back the scanner fake, so no network is reachable."""

    @classmethod
    def build(cls, resources: SourceResources) -> Scanner:
        del resources
        return FakeScanner()


class _RefusingScanner:
    """A registered scanner needing its own settings block: on a knowledge base declaring none, it refuses.

    The condition every set of resources here actually meets, since `_resources` declares no settings at all.
    """

    @classmethod
    def build(cls, resources: SourceResources) -> Scanner:
        if _PODCAST not in resources.settings:
            msg = "the podcast scanner requires a podcast settings block, none was given"
            raise ValueError(msg)
        return FakeScanner()


def _resources(knowledge_base_root: Path) -> SourceResources:
    for required in ("Units", "_meta"):
        (knowledge_base_root / required).mkdir(parents=True, exist_ok=True)
    return SourceResources(knowledge_base=KnowledgeBaseSettings(path=knowledge_base_root))


def test_the_built_in_xml_reader_mounts_without_a_registry_lookup(tmp_path: Path) -> None:
    scanners = build_scanners(_resources(tmp_path), [XML_SCANNER], source={})

    assert isinstance(scanners[XML_SCANNER], XmlScanner)


def test_a_registered_scanner_mounts_under_the_name_the_knowledge_base_watches(tmp_path: Path) -> None:
    scanners = build_scanners(_resources(tmp_path), [_PODCAST], source={_PODCAST: _PodcastScanner})

    assert isinstance(scanners[_PODCAST], FakeScanner)


def test_a_kind_nothing_registers_is_left_out_of_the_table(tmp_path: Path) -> None:
    scanners = build_scanners(_resources(tmp_path), [XML_SCANNER, "vimeo"], source={_PODCAST: _PodcastScanner})

    assert set(scanners) == {XML_SCANNER}


def test_a_scanner_that_refuses_to_build_is_left_out_of_the_table(tmp_path: Path) -> None:
    """The knowledge base declares no settings, so a scanner needing its own block refuses."""
    resources = _resources(tmp_path)
    assert resources.settings == {}

    scanners = build_scanners(resources, [_PODCAST], source={_PODCAST: _RefusingScanner})

    assert scanners == {}


def test_the_same_scanner_mounts_once_the_resource_it_needs_is_configured(tmp_path: Path) -> None:
    resources = replace(_resources(tmp_path), settings={_PODCAST: {"api_key": "podcast-secret"}})

    scanners = build_scanners(resources, [_PODCAST], source={_PODCAST: _RefusingScanner})

    assert isinstance(scanners[_PODCAST], FakeScanner)


def test_one_name_watched_twice_is_built_once(tmp_path: Path) -> None:
    scanners = build_scanners(_resources(tmp_path), [XML_SCANNER, XML_SCANNER], source={})

    assert list(scanners) == [XML_SCANNER]


def test_the_watched_kinds_are_the_ones_the_knowledge_base_feeds_file_carries(tmp_path: Path) -> None:
    watch_list = WatchListFile(tmp_path / "watch-list.yaml")
    watch_list.add(XML_SCANNER, "https://blog.example.org/feed.xml")
    watch_list.add(_PODCAST, "someone")

    assert watched_scanners(watch_list) == (XML_SCANNER, _PODCAST)


def test_an_unreadable_feeds_file_falls_back_to_every_known_scanner(tmp_path: Path) -> None:
    path = tmp_path / "watch-list.yaml"
    path.write_text("kind: [unclosed\n", encoding="utf-8")

    assert watched_scanners(WatchListFile(path)) == known_scanners()


def test_the_source_queue_gives_a_url_two_connectors_claim_to_the_first_one_in_the_map(tmp_path: Path) -> None:
    zotero = FakeConnector(name="zotero", claims_result=SourceType.PAPER, key_for_result="zotero-key")
    intake = FakeConnector(name="intake", claims_result=SourceType.ARTICLE, key_for_result="intake-key")
    connectors = ConnectorMap({"zotero": zotero, "intake": intake})

    handler = watch_list_handler_provider(_resources(tmp_path), connectors)
    entry = handler._queue.add("https://example.org/claimed-by-both")

    assert (entry.source_type, entry.key) == (SourceType.PAPER, "zotero-key")
