"""`render_post`: one X post as the markdown its mirror carries."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from engram_connectors.x.domain import render_post


@pytest.mark.parametrize("handle", ["omarsar0", "@omarsar0"])
def test_the_heading_and_channel_carry_the_handle_once(handle: str) -> None:
    rendered = render_post(
        url="https://x.com/omarsar0/status/1", handle=handle, date=datetime(2026, 10, 6, tzinfo=UTC), text="A post.", links=[]
    )

    assert rendered.markdown.startswith("# @omarsar0, 2026-10-06T00:00:00+00:00\n")
    assert rendered.channel == "omarsar0"
