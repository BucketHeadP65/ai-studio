"""`harness` declares `engram-connectors` so its URL classification is real, not a fallback.

`harness.adapters.knowledge_base` wires `claimants_for` into every knowledge base's source queue; with no
connector package installed, a pasted URL would classify only as the generic article fallback.
"""

from __future__ import annotations

from engram.sources.contracts import SourceType, claimants_for

WATCH_URL = "https://www.youtube.com/watch?v=abc123XYZ_-"


def test_claimants_for_youtube_keys_a_watch_url_as_yt_prefixed() -> None:
    claimants = claimants_for(("youtube",))

    assert len(claimants) == 1
    assert claimants[0].claims(WATCH_URL) is SourceType.VIDEO
    assert claimants[0].key_for(WATCH_URL) == "yt-abc123XYZ_-"
