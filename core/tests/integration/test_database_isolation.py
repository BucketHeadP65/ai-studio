"""The integration tier runs on a database of its own, never on the stack's."""

from urllib.parse import urlsplit

STABLE_PORTS = frozenset({5433, 5434, 8100, 8770, 3031})
"""The ports the local stack serves on."""


def test_the_tier_never_binds_a_stable_port(database_url: str) -> None:
    assert urlsplit(database_url).port not in STABLE_PORTS
