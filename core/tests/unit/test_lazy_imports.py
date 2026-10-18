"""`lazy_module_attributes` resolves a lazy name once, refuses an unknown one and lists the lazy names."""

import math
import sys

from collections.abc import Iterator
from types import ModuleType

import pytest

from core.lazy_imports import lazy_module_attributes

PROBE = "probe_lazy_module"


@pytest.fixture
def probe() -> Iterator[ModuleType]:
    module = ModuleType(PROBE)
    get_attribute, list_names = lazy_module_attributes(PROBE, {"sqrt": "math"}, ["sqrt", "exported_only"])
    vars(module)["__getattr__"] = get_attribute
    vars(module)["__dir__"] = list_names
    sys.modules[PROBE] = module
    yield module
    del sys.modules[PROBE]


def test_a_lazy_name_resolves_and_is_kept_on_the_module(probe: ModuleType) -> None:
    name = "sqrt"
    assert name not in vars(probe)
    assert getattr(probe, name) is math.sqrt
    assert vars(probe)[name] is math.sqrt


def test_an_unknown_name_raises_attribute_error_naming_the_module(probe: ModuleType) -> None:
    name = "nothing"
    with pytest.raises(AttributeError, match=PROBE):
        getattr(probe, name)


def test_dir_lists_the_lazy_names_before_they_resolve(probe: ModuleType) -> None:
    assert {"sqrt", "exported_only"} <= set(dir(probe))
