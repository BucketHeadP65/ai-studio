"""A frozen, key-to-strategy registry for closed-set dispatch.

When code dispatches over a closed set of strategies (a database dialect, a
provider backend, a named model), the registry wraps an immutable mapping from
key to strategy and dispatches through `select`, so a new strategy is a
one-entry change at the composition root rather than a new branch at every call
site.

The wrapper type earns its keep at the dependency-injection boundary: a
container that resolves dependencies by type has no nominal key for a bare
`Mapping`, whereas a `Registry` subclass (one thin named subclass per registry)
gives it one. Subclass `Registry` with the concrete key and value types to make
each registry a distinct injectable type:

```python
class SearchDialects(Registry[DbDialect, SearchDialect]): ...
```
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class Registry[K, V]:
    """Immutable registry of strategies keyed by `K`, dispatched through `select`."""

    by_key: Mapping[K, V]

    def select(self, key: K) -> V:
        """Return the strategy registered for `key`.

        Args:
            key: The registry key to dispatch on.

        Returns:
            The strategy registered for `key`.

        Raises:
            KeyError: If no strategy is registered for `key`.
        """
        return self.by_key[key]
