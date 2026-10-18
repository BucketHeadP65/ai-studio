from __future__ import annotations

from importlib.metadata import EntryPoint
from typing import TYPE_CHECKING

import pytest

from engram.sources.contracts import (
    ConnectorNotInstalledError,
    available_connectors,
    available_converters,
    connector_class,
    converter_factory,
    scanner_class,
)
from engram.sources.registry import _CONNECTORS, _CONVERTERS, _SCANNERS, _Group, _installed, _load, available_scanners

if TYPE_CHECKING:
    from pathlib import Path

    from engram.config import SourceResources
    from engram.sources.contracts import ConversionResult, Scanner, SourceItem, SourceType


class _Zotero:
    """A stand-in `ConnectorClass`, distinct by identity so a resolved class can be asserted on."""

    name = "zotero"

    @staticmethod
    def claims(url: str) -> SourceType | None:
        del url
        return None

    @staticmethod
    def key_for(url: str) -> str:
        return url

    @staticmethod
    def source_type_of(item_type: str) -> SourceType:
        del item_type
        raise NotImplementedError

    @classmethod
    def build(cls, resources: SourceResources) -> _Zotero:
        del resources
        return cls()

    async def list_items_with_pdf(self) -> list[SourceItem]:
        raise NotImplementedError

    async def get(self, provider_key: str) -> SourceItem | None:
        del provider_key
        raise NotImplementedError


class _FakeConverter:
    """A stand-in `DocumentConverter`, never actually called."""

    async def to_markdown(self, pdf_path: Path, *, source_url: str | None = None) -> ConversionResult:
        del pdf_path, source_url
        raise NotImplementedError


class _PodcastScanner:
    """A stand-in `ScannerClass`, distinct by identity so a resolved class can be asserted on."""

    @classmethod
    def build(cls, resources: SourceResources) -> Scanner:
        del resources
        raise NotImplementedError


def _factory(*, document_intelligence: object = None) -> _FakeConverter:
    del document_intelligence
    return _FakeConverter()


def test_connector_class_resolves_the_registered_class_from_a_plain_mapping() -> None:
    assert connector_class("zotero", source={"zotero": _Zotero}) is _Zotero


def test_connector_class_resolves_from_a_loader_callable_too() -> None:
    assert connector_class("zotero", source=lambda: {"zotero": _Zotero}) is _Zotero


def test_connector_class_raises_naming_an_uninstalled_connector() -> None:
    with pytest.raises(ConnectorNotInstalledError, match="no connector named 'vimeo' is installed"):
        connector_class("vimeo", source={})


def test_converter_factory_resolves_the_registered_function() -> None:
    assert converter_factory("zotero", source={"zotero": _factory}) is _factory


def test_converter_factory_raises_naming_an_uninstalled_converter() -> None:
    with pytest.raises(ConnectorNotInstalledError, match="no converter named 'vimeo' is installed"):
        converter_factory("vimeo", source={})


def test_available_connectors_lists_every_registered_name_sorted() -> None:
    assert available_connectors(source={"youtube": _Zotero, "intake": _Zotero}) == ("intake", "youtube")


def test_available_converters_lists_every_registered_name_sorted() -> None:
    assert available_converters(source={"youtube": _factory, "intake": _factory}) == ("intake", "youtube")


def test_available_connectors_is_empty_when_nothing_is_registered() -> None:
    assert available_connectors(source={}) == ()


def test_connector_class_at_its_default_raises_naming_a_name_with_none_installed() -> None:
    """No test group here registers a real entry point, so the default (real) lookup finds nothing."""
    with pytest.raises(ConnectorNotInstalledError, match="no connector named 'vimeo' is installed"):
        connector_class("vimeo")


def test_converter_factory_at_its_default_raises_naming_a_name_with_none_installed() -> None:
    with pytest.raises(ConnectorNotInstalledError, match="no converter named 'vimeo' is installed"):
        converter_factory("vimeo")




def test_feed_scanner_class_resolves_the_registered_class_from_a_plain_mapping() -> None:
    assert scanner_class("podcast", source={"podcast": _PodcastScanner}) is _PodcastScanner


def test_feed_scanner_class_resolves_from_a_loader_callable_too() -> None:
    assert scanner_class("podcast", source=lambda: {"podcast": _PodcastScanner}) is _PodcastScanner


def test_feed_scanner_class_raises_naming_an_uninstalled_scanner() -> None:
    with pytest.raises(ConnectorNotInstalledError, match="no scanner named 'podcast' is installed"):
        scanner_class("podcast", source={})


def test_available_feed_scanners_lists_every_registered_name_sorted() -> None:
    assert available_scanners(source={"podcast": _PodcastScanner, "x": _PodcastScanner}) == ("podcast", "x")


def test_feed_scanner_class_at_its_default_raises_naming_a_name_with_none_installed() -> None:
    with pytest.raises(ConnectorNotInstalledError, match="no scanner named 'podcast' is installed"):
        scanner_class("podcast")


@pytest.mark.parametrize(("group", "noun"), [(_CONNECTORS, "connector"), (_CONVERTERS, "converter"), (_SCANNERS, "scanner")])
def test_the_shared_load_path_raises_naming_the_group_noun_for_a_name_its_finder_finds_nothing_for(group: _Group[object], noun: str) -> None:
    """One loading path serves the three groups, so the not-found sentence is pinned once, per group's own noun."""
    with pytest.raises(ConnectorNotInstalledError, match=f"no {noun} named 'vimeo' is installed"):
        _load(group, "vimeo", finder=lambda group, name: ())


def test_the_shared_load_path_raises_chaining_the_underlying_error_when_the_found_entry_point_fails_to_load() -> None:
    broken = EntryPoint(name="vimeo", value="engram_test_registry_no_such_module:Thing", group=_CONNECTORS.group_name)

    with pytest.raises(ConnectorNotInstalledError, match=r"the connector named 'vimeo' could not be loaded \(") as excinfo:
        _load(_CONNECTORS, "vimeo", finder=lambda group, name: (broken,))

    assert isinstance(excinfo.value.__cause__, ImportError)


def test_each_group_carries_its_own_entry_point_name_and_message_noun() -> None:
    assert [(group.group_name, group.noun) for group in (_CONNECTORS, _CONVERTERS, _SCANNERS)] == [
        ("engram.sources.connectors", "connector"),
        ("engram.sources.converters", "converter"),
        ("engram.sources.scanners", "scanner"),
    ]


def test_a_listing_skips_an_entry_point_that_fails_to_load_and_keeps_the_rest() -> None:
    """A broken install must not fail a host's listing: a POST validating a name against it answers, rather than 500ing."""
    good = EntryPoint(name="xml", value="engram.sources.infrastructure.xml_scanner:XmlScanner", group=_SCANNERS.group_name)
    broken = EntryPoint(name="vimeo", value="engram_test_registry_no_such_module:Thing", group=_SCANNERS.group_name)

    installed = _installed(_SCANNERS, lister=lambda group: (good, broken))

    assert set(installed) == {"xml"}
