"""A YouTube video's identity, parsed from any of the site's URL forms, and a channel's feed URL."""

from __future__ import annotations

import re

from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict

from engram_connectors.youtube.exceptions import VideoUrlUnrecognizedError

VIDEO_KEY_PREFIX = "yt-"
"""What a video row's key starts with. The rest of the key is the video id."""

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

_WATCH_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}


def channel_atom_url(channel_id: str) -> str:
    """The Atom feed YouTube publishes for a channel, which needs no key.

    Args:
        channel_id: The YouTube channel id.

    Returns:
        The feed URL a knowledge base puts in its watch-list file to watch that channel.
    """
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def _video_id_from_watch_host(path: str, query: str) -> str | None:
    """The `v` query value from a `/watch` path, or the id from a `/shorts/`, `/embed/` or `/live/` path."""
    if path == "/watch":
        values = parse_qs(query).get("v")
        return values[0] if values else None
    for prefix in ("/shorts/", "/embed/", "/live/"):
        if path.startswith(prefix):
            return path.removeprefix(prefix).split("/")[0] or None
    return None


class VideoRef(BaseModel):
    """A YouTube video identity, resolved from any of the site's URL forms."""

    model_config = ConfigDict(frozen=True)

    video_id: str

    @property
    def key(self) -> str:
        """The key the video's row and mirror share: `yt-` followed by the video id."""
        return f"{VIDEO_KEY_PREFIX}{self.video_id}"

    @classmethod
    def parse(cls, url: str) -> VideoRef:
        """Extract the 11-character video id from a watch, short, shorts, embed or live URL.

        Args:
            url: The URL to parse.

        Returns:
            The video identity the URL names.

        Raises:
            VideoUrlUnrecognizedError: The host is not YouTube's, or the id is missing or fails the 11-character
                `[A-Za-z0-9_-]` shape.
        """
        parsed = urlsplit(url)
        host = parsed.hostname or ""  # `.hostname` (unlike `.netloc`) is lowercased and strips an explicit port

        video_id: str | None = None
        if host in _SHORT_HOSTS:
            video_id = parsed.path.lstrip("/").split("/")[0] or None
        elif host in _WATCH_HOSTS:
            video_id = _video_id_from_watch_host(parsed.path, parsed.query)

        if video_id is None or _VIDEO_ID_RE.match(video_id) is None:
            raise VideoUrlUnrecognizedError(url)
        return cls(video_id=video_id)
