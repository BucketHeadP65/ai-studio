"""Discovers `Connector`, `DocumentConverter` and `Scanner` implementations a knowledge base declares by name.

Three entry-point groups each resolve a name a knowledge base declares: `engram.sources.connectors`
(a `ConnectorClass`), `engram.sources.converters` (a plain `DocumentConverter` factory function)
and `engram.sources.scanners` (a `ScannerClass`, the name a watch entry's `scanner` carries). A
lookup loads only the one entry point named, never the whole group, so a knowledge base never
imports the dependencies of an implementation it does not declare. The `available_*` listings
load every entry point in their group. An optional `source` (a plain `Mapping`, or a zero-arg
callable that builds one) overrides the installed-package lookup on every function here, so a test
drives it with a dict of fakes. A name no installed package registers, or whose entry point fails
to load, raises one sentence naming it and chaining the underlying error, never an import crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import TYPE_CHECKING

from core.observability import get_logger

from engram.sources.exceptions import ConnectorNotInstalledError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from importlib.metadata import EntryPoint

    from engram.sources.domain.source_queue import UrlClaimant
    from engram.sources.internal_contracts import ConnectorClass, DocumentConverter, ScannerClass

type Source[T] = Mapping[str, T] | Callable[[], Mapping[str, T]]
type ConnectorSource = Source[ConnectorClass]
type ConverterSource = Source[Callable[..., DocumentConverter]]
type ScannerSource = Source[ScannerClass]
type Finder = Callable[[str, str], Iterable[EntryPoint]]
type Lister = Callable[[str], Iterable[EntryPoint]]


_logger = get_logger(__name__)


@dataclass(frozen=True)
class _Group[T]:
    """One entry-point group: where a name resolves, and the noun a refusal calls what it did not find."""

    group_name: str
    noun: str


_CONNECTORS: _Group[ConnectorClass] = _Group("engram.sources.connectors", "connector")
_CONVERTERS: _Group[Callable[..., DocumentConverter]] = _Group("engram.sources.converters", "converter")
_SCANNERS: _Group[ScannerClass] = _Group("engram.sources.scanners", "scanner")


def _find_entry_points(group: str, name: str) -> Iterable[EntryPoint]:
    return entry_points(group=group, name=name)


def _all_entry_points(group: str) -> Iterable[EntryPoint]:
    return entry_points(group=group)


def _not_installed[T](group: _Group[T], name: str) -> ConnectorNotInstalledError:
    """The one refusal sentence every lookup in `group` raises for a name it cannot find."""
    return ConnectorNotInstalledError(f"no {group.noun} named {name!r} is installed")


def _installed[T](group: _Group[T], *, lister: Lister = _all_entry_points) -> Mapping[str, T]:
    """Every entry point in `group` that loads, by name, leaving out with a warning each one that fails to load."""
    found: dict[str, T] = {}
    for entry_point in lister(group.group_name):
        try:
            found[entry_point.name] = entry_point.load()
        except (ImportError, AttributeError) as error:
            _logger.warning(
                "skipping the {kind} named {entry_name!r}, its entry point {entry_point!r} could not be loaded: {reason}",
                kind=group.noun,
                entry_name=entry_point.name,
                entry_point=entry_point.value,
                reason=str(error),
            )
    return found


def _load[T](group: _Group[T], name: str, *, finder: Finder = _find_entry_points) -> T:
    """The one entry point named `name` in `group`, loading only that one, or `ConnectorNotInstalledError`."""
    found = tuple(finder(group.group_name, name))
    if not found:
        raise _not_installed(group, name)
    try:
        return found[0].load()
    except (ImportError, AttributeError) as error:
        msg = f"the {group.noun} named {name!r} could not be loaded ({error})"
        raise ConnectorNotInstalledError(msg) from error


def _mapping[T](source: Source[T]) -> Mapping[str, T]:
    """`source` itself when it is already a mapping, else the mapping calling it produces."""
    return source() if callable(source) else source


def _resolve[T](group: _Group[T], name: str, source: Source[T] | None) -> T:
    """What `name` names in `group`: the one installed entry point, or `source`'s own entry for it."""
    if source is None:
        return _load(group, name)
    found = _mapping(source).get(name)
    if found is None:
        raise _not_installed(group, name)
    return found


def installed_connectors() -> Mapping[str, ConnectorClass]:
    """Every `ConnectorClass` an installed package registers, by name. Loads every one, for listing only.

    An entry point that fails to load is left out with a warning, never raised, so a host
    validating a name against the listing refuses that name. Asking for the broken name itself
    still raises.

    Returns:
        The classes by name.
    """
    return _installed(_CONNECTORS)


def installed_converters() -> Mapping[str, Callable[..., DocumentConverter]]:
    """Every converter factory an installed package registers, by name. Loads every one, for listing only.

    An entry point that fails to load is left out with a warning, never raised.

    Returns:
        The factories by name.
    """
    return _installed(_CONVERTERS)


def installed_scanners() -> Mapping[str, ScannerClass]:
    """Every `ScannerClass` an installed package registers, by name. Loads every one, for listing only.

    An entry point that fails to load is left out with a warning, never raised.

    Returns:
        The classes by name.
    """
    return _installed(_SCANNERS)


def connector_class(name: str, *, source: ConnectorSource | None = None) -> ConnectorClass:
    """The `ConnectorClass` registered under `name` (default: the installed `engram.sources.connectors` entry point).

    Args:
        name: The connector name a knowledge base declares.
        source: Replaces the installed entry points, read whole. `None` loads only the entry point named.

    Returns:
        The connector's class.

    Raises:
        ConnectorNotInstalledError: `name` is not registered (or not in `source`), or its entry
            point failed to load.
    """
    return _resolve(_CONNECTORS, name, source)


def converter_factory(name: str, *, source: ConverterSource | None = None) -> Callable[..., DocumentConverter]:
    """The converter factory registered under `name` (default: the installed `engram.sources.converters` entry point).

    Args:
        name: The connector name a knowledge base declares.
        source: Replaces the installed entry points, read whole. `None` loads only the entry point named.

    Returns:
        The converter factory.

    Raises:
        ConnectorNotInstalledError: `name` is not registered (or not in `source`), or its entry
            point failed to load.
    """
    return _resolve(_CONVERTERS, name, source)


def scanner_class(name: str, *, source: ScannerSource | None = None) -> ScannerClass:
    """The `ScannerClass` registered under `name` (default: the installed `engram.sources.scanners` entry point).

    A knowledge base whose watch list only names the built-in reader never imports a registered
    scanner's own dependencies.

    Args:
        name: What a watch entry carries as its `scanner`.
        source: Replaces the installed entry points, read whole. `None` loads only the entry point named.

    Returns:
        The scanner's class.

    Raises:
        ConnectorNotInstalledError: `name` is not registered (or not in `source`), or its entry
            point failed to load.
    """
    return _resolve(_SCANNERS, name, source)


def available_connectors(*, source: ConnectorSource = installed_connectors) -> tuple[str, ...]:
    """Every name `source` registers, sorted.

    Args:
        source: The connectors to list, every installed one by default.

    Returns:
        The names.
    """
    return tuple(sorted(_mapping(source)))


def available_converters(*, source: ConverterSource = installed_converters) -> tuple[str, ...]:
    """Every name `source` registers, sorted.

    Args:
        source: The converter factories to list, every installed one by default.

    Returns:
        The names.
    """
    return tuple(sorted(_mapping(source)))


def available_scanners(*, source: ScannerSource = installed_scanners) -> tuple[str, ...]:
    """Every name `source` registers, sorted.

    Args:
        source: The scanners to list, every installed one by default.

    Returns:
        The names.
    """
    return tuple(sorted(_mapping(source)))


def claimants_for(names: Sequence[str], *, source: ConnectorSource | None = None) -> tuple[UrlClaimant, ...]:
    """The knowledge base's own declared connectors, in order, as `UrlClaimant`s.

    Each name resolves through `connector_class`, so engram itself names no connector. Every
    registered class's `claims` and `key_for` is a stateless static method, so the class itself,
    never instantiated, is the claimant: a caller with no live connectors in hand (a
    filesystem-only CLI or HTTP path) still gets the real recognition every declared, installed
    connector owns. A name with no installed package behind it is skipped, like an unrecognized
    one. Validating names against what is installed is the host's job.

    Args:
        names: The connector names the knowledge base declares, in order.
        source: The same seam `connector_class` takes. `None` looks up each installed entry point.

    Returns:
        The claimants, in declared order.
    """
    claimants: list[UrlClaimant] = []
    for name in names:
        try:
            claimants.append(connector_class(name, source=source))
        except ConnectorNotInstalledError:
            continue
    return tuple(claimants)
