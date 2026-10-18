"""Drift guard for the packages' public doors and lazy imports.

`engram.__getattr__` resolves each advertised name through a private `_LAZY_IMPORTS` table, and `engram.init.contracts.__getattr__`
through a private `_LAZY_IMPORTS` table, so a rename that
touches one side and not the other leaves a door advertised but unreachable — or reachable but unadvertised. These specs pin the two
sides to each other, resolve every advertised name for real, and cover the miss branch and the `dir()` contract.
"""

import pytest

import engram
import engram.init.contracts


def test_every_advertised_door_has_a_table_entry():
    assert set(engram.__all__) == set(engram._LAZY_IMPORTS)


def test_every_advertised_door_resolves():
    # The guard is that `getattr` does not raise; a door whose target legitimately is `None` still resolves.
    for name in engram.__all__:
        getattr(engram, name)


def test_unknown_attribute_is_not_a_door():
    with pytest.raises(AttributeError):
        _ = engram.definitely_not_a_door


def test_dir_lists_every_advertised_door():
    assert set(engram.__all__) <= set(dir(engram))


def test_every_advertised_init_lazy_import_has_a_table_entry():
    assert set(engram.init.contracts.__all__) == set(engram.init.contracts._LAZY_IMPORTS)


def test_every_advertised_init_lazy_import_resolves():
    # The guard is that `getattr` does not raise; a lazy import whose target legitimately is `None` still resolves.
    for name in engram.init.contracts.__all__:
        getattr(engram.init.contracts, name)


def test_unknown_init_attribute_is_not_a_lazy_import():
    with pytest.raises(AttributeError):
        _ = engram.init.contracts.definitely_not_a_lazy_import


def test_dir_lists_every_advertised_init_lazy_import():
    assert set(engram.init.contracts.__all__) <= set(dir(engram.init.contracts))
