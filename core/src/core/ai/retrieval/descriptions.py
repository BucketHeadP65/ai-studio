"""The per-tool description overrides a registration path applies over the shipped tool descriptions.

`resolve_description` is the one place a registration path asks for an override. It answers the shipped default when a tool
has none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def resolve_description(name: str, default: str, descriptions: Mapping[str, str] | None) -> str:
    """Return the per-tool description override for `name`, else the shipped `default`.

    An absent mapping or an absent key falls through to `default`, so unknown keys (a tool outside
    the selected subset) are silently ignored.

    Args:
        name: The canonical tool name to resolve.
        default: The shipped description to use when there is no override for `name`.
        descriptions: The per-tool override map, or `None` to keep every shipped default.

    Returns:
        The override for `name` when `descriptions` carries one, else `default`.
    """
    return descriptions[name] if descriptions is not None and name in descriptions else default
