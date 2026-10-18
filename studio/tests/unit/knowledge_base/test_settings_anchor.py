"""The knowledge base config anchor, pinned against the parent count that computes it.

`_CONFIG_DIR` walks a fixed number of parents up from this module's own file, so moving the
module one directory deeper or shallower silently retargets it — at a path that still exists on
somebody's disk, which is why the failure is a wrong config rather than a crash. Asserting the
shape (`.../engram/config`) rather than an absolute path keeps the check true on any checkout.
"""

from __future__ import annotations

from studio.knowledge_base.settings import _APPLICATION_SETTINGS_JSON, _CONFIG_DIR


def test_the_config_anchor_lands_on_the_engram_checkouts_config_directory() -> None:
    """The anchor resolves to a real `config/` inside a checkout named `engram`."""
    assert _CONFIG_DIR.is_dir()
    assert _CONFIG_DIR.name == "config"
    assert _CONFIG_DIR.parent.name == "engram"


def test_the_settings_file_is_named_inside_that_directory() -> None:
    """The one JSON the app reads sits in the anchored directory, whether or not it exists yet."""
    assert _APPLICATION_SETTINGS_JSON.parent == _CONFIG_DIR
    assert _APPLICATION_SETTINGS_JSON.name == "application_settings.json"
