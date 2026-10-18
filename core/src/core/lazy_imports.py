"""The `__getattr__` and `__dir__` of a module whose public names are imported on first use."""

from __future__ import annotations

import sys

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping


def lazy_module_attributes(
    module_name: str, lazy_imports: Mapping[str, str], exported: Iterable[str]
) -> tuple[Callable[[str], object], Callable[[], list[str]]]:
    """Build the module-level `__getattr__` and `__dir__` of the module `module_name`.

    Assign the pair to `__getattr__, __dir__` in that module. A name in `lazy_imports` is imported from the module it maps to on
    first access, then stored on the module so the next access skips the hook. The getter returns `object`. Each lazy
    name's own type comes from the module's type-checking imports.

    Args:
        module_name: The module's `__name__`.
        lazy_imports: Each lazy name mapped to the dotted name of the module that defines it.
        exported: The module's public names, which `dir()` lists beside what the module already holds.

    Returns:
        The attribute hook and the directory hook.
    """

    def get_attribute(name: str) -> object:
        source = lazy_imports.get(name)
        if source is None:
            msg = f"module {module_name!r} has no attribute {name!r}"
            raise AttributeError(msg)
        value = getattr(import_module(source), name)
        setattr(sys.modules[module_name], name, value)
        return value

    def list_names() -> list[str]:
        return sorted(set(vars(sys.modules[module_name])) | set(exported))

    return get_attribute, list_names
