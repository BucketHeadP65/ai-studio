"""The YouTube transcript fetcher: watch-page metadata and the caption-track transcript, over httpx.

It makes three requests per video: the public watch page (title, channel, publish date and a caption-track list),
the player API asked as the iOS client (the preferred caption-track list), and the selected track's `json3`
transcript. The watch page's list is used only when the player API lists no track. Every response body is
validated before its fields are read.
"""

from __future__ import annotations

import json

from datetime import date
from typing import TYPE_CHECKING

import httpx

from pydantic import BaseModel, ConfigDict, ValidationError

from engram_connectors.youtube.domain.transcript import FetchedVideo, TranscriptSegment, VideoTranscript
from engram_connectors.youtube.exceptions import TranscriptUnavailableError, VideoFetchFailedError

if TYPE_CHECKING:
    from collections.abc import Sequence

_HTTP_TIMEOUT = httpx.Timeout(30.0)
_WATCH_URL = "https://www.youtube.com/watch"
_PLAYER_API_URL = "https://www.youtube.com/youtubei/v1/player"
_PLAYER_API_CLIENT = {"clientName": "IOS", "clientVersion": "20.10.4"}
_PLAYER_RESPONSE_MARKER = "ytInitialPlayerResponse"


class _VideoDetails(BaseModel):
    """The fields of `ytInitialPlayerResponse.videoDetails` the fetcher reads."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    title: str
    author: str


class _PlayerMicroformatRenderer(BaseModel):
    """The publish date of `microformat.playerMicroformatRenderer`."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    publishDate: str | None = None  # noqa: N815 (upstream field name)


class _Microformat(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    playerMicroformatRenderer: _PlayerMicroformatRenderer | None = None  # noqa: N815 (upstream field name)


class _CaptionTrack(BaseModel):
    """One entry of `captions.playerCaptionsTracklistRenderer.captionTracks`."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    baseUrl: str  # noqa: N815 (upstream field name)
    languageCode: str  # noqa: N815 (upstream field name)
    kind: str | None = None


class _PlayerCaptionsTracklistRenderer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    captionTracks: tuple[_CaptionTrack, ...] = ()  # noqa: N815 (upstream field name)


class _Captions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    playerCaptionsTracklistRenderer: _PlayerCaptionsTracklistRenderer | None = None  # noqa: N815 (upstream field name)


class _PlayerResponse(BaseModel):
    """The fields of `ytInitialPlayerResponse` the fetcher reads."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    videoDetails: _VideoDetails  # noqa: N815 (upstream field name)
    microformat: _Microformat | None = None
    captions: _Captions | None = None


class _CaptionRun(BaseModel):
    """One text run of a `json3` caption event."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    utf8: str


class _CaptionEvent(BaseModel):
    """One `json3` caption event: its start offset plus the text runs it carries."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    tStartMs: int  # noqa: N815 (upstream field name)
    segs: tuple[_CaptionRun, ...] = ()


class _CaptionsPayload(BaseModel):
    """The `json3`-format response body a caption track's `baseUrl` serves."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    events: tuple[_CaptionEvent, ...] = ()


def _extract_player_response(html: str, url: str) -> _PlayerResponse:
    """Parse the first JSON object after the `ytInitialPlayerResponse` marker in a watch page and validate it."""
    marker = html.find(_PLAYER_RESPONSE_MARKER)
    brace = html.find("{", marker) if marker != -1 else -1
    if brace == -1:
        msg = f"{url}: no ytInitialPlayerResponse in watch page"
        raise VideoFetchFailedError(msg)
    try:
        payload, _ = json.JSONDecoder().raw_decode(html, brace)
    except json.JSONDecodeError as error:
        msg = f"{url}: malformed ytInitialPlayerResponse JSON"
        raise VideoFetchFailedError(msg) from error
    try:
        return _PlayerResponse.model_validate(payload)
    except ValidationError as error:
        msg = f"{url}: ytInitialPlayerResponse missing expected fields"
        raise VideoFetchFailedError(msg) from error


def _caption_tracks(player_response: _PlayerResponse) -> tuple[_CaptionTrack, ...]:
    """The caption-track list a player response carries, empty when it carries none."""
    renderer = player_response.captions.playerCaptionsTracklistRenderer if player_response.captions is not None else None
    return renderer.captionTracks if renderer is not None else ()


def _select_caption_track(tracks: Sequence[_CaptionTrack]) -> _CaptionTrack | None:
    """The first manually authored track (`kind` unset), falling back to the first auto-generated one."""
    for track in tracks:
        if track.kind != "asr":
            return track
    return tracks[0] if tracks else None


def _parse_published(microformat: _Microformat | None) -> date | None:
    """`microformat.playerMicroformatRenderer.publishDate` as a `date`, or `None` when absent or unparseable."""
    renderer = microformat.playerMicroformatRenderer if microformat is not None else None
    raw = renderer.publishDate if renderer is not None else None
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _segments_from_payload(payload: _CaptionsPayload) -> tuple[TranscriptSegment, ...]:
    """One `TranscriptSegment` per non-empty caption event, its text the joined run of `segs`."""
    segments: list[TranscriptSegment] = []
    for event in payload.events:
        text = "".join(seg.utf8 for seg in event.segs).strip()
        if not text:
            continue
        segments.append(TranscriptSegment(start_seconds=event.tStartMs / 1000.0, text=text))
    return tuple(segments)


class YouTubeTranscriptFetcher:
    """Fetches one video's metadata and transcript from YouTube's public watch page, player API and caption endpoint."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        """Keep an injected async client, or open and close one on each call.

        Args:
            client: A preconfigured async client. When `None`, each `fetch` opens a redirect-following client and
                closes it before returning.
        """
        self._client = client

    async def fetch(self, video_id: str) -> FetchedVideo:
        """Fetch the video from its watch page, preferring the caption tracks the player API lists.

        Args:
            video_id: The 11-character YouTube video id.

        Returns:
            The video's transcript and the watch URL it was fetched from.

        Raises:
            TranscriptUnavailableError: No caption track is listed, or every event of the selected track strips to no
                text.
            VideoFetchFailedError: A non-OK HTTP status, a transport failure, or a response body that fails validation, on
                the watch-page or the caption-track request.
        """
        if self._client is not None:
            return await self._fetch(self._client, video_id)
        async with httpx.AsyncClient(follow_redirects=True, timeout=_HTTP_TIMEOUT) as client:
            return await self._fetch(client, video_id)

    async def _fetch(self, client: httpx.AsyncClient, video_id: str) -> FetchedVideo:
        """Fetch and validate the watch page, select a caption track, then fetch and parse its transcript."""
        url = f"{_WATCH_URL}?v={video_id}"
        try:
            response = await client.get(url, follow_redirects=True, timeout=_HTTP_TIMEOUT)
        except httpx.HTTPError as error:
            msg = f"{url}: {error}"
            raise VideoFetchFailedError(msg) from error
        if response.status_code != httpx.codes.OK:
            msg = f"{url}: HTTP {response.status_code}"
            raise VideoFetchFailedError(msg)
        player_response = _extract_player_response(response.text, url)

        tracks = await self._fetch_player_tracks(client, video_id) or _caption_tracks(player_response)
        track = _select_caption_track(tracks)
        if track is None:
            raise TranscriptUnavailableError(video_id)

        captions_payload = await self._fetch_captions(client, track)
        segments = _segments_from_payload(captions_payload)
        if not segments:
            raise TranscriptUnavailableError(video_id)

        doc = VideoTranscript(
            video_id=video_id,
            title=player_response.videoDetails.title,
            channel=player_response.videoDetails.author,
            published=_parse_published(player_response.microformat),
            segments=segments,
        )
        return FetchedVideo(doc=doc, url=url)

    @staticmethod
    async def _fetch_player_tracks(client: httpx.AsyncClient, video_id: str) -> tuple[_CaptionTrack, ...]:
        """The caption tracks the player API lists for `video_id` when asked as the iOS client, empty on any failure."""
        body = {"context": {"client": _PLAYER_API_CLIENT}, "videoId": video_id}
        try:
            response = await client.post(_PLAYER_API_URL, json=body, follow_redirects=True, timeout=_HTTP_TIMEOUT)
        except httpx.HTTPError:
            return ()
        if response.status_code != httpx.codes.OK:
            return ()
        try:
            player_response = _PlayerResponse.model_validate(response.json())
        except (json.JSONDecodeError, ValidationError):
            return ()
        return _caption_tracks(player_response)

    @staticmethod
    async def _fetch_captions(client: httpx.AsyncClient, track: _CaptionTrack) -> _CaptionsPayload:
        """GET the selected track's `json3` transcript payload and validate it."""
        caption_url = httpx.URL(track.baseUrl).copy_merge_params({"fmt": "json3"})
        try:
            response = await client.get(caption_url, follow_redirects=True, timeout=_HTTP_TIMEOUT)
        except httpx.HTTPError as error:
            msg = f"{caption_url}: {error}"
            raise VideoFetchFailedError(msg) from error
        if response.status_code != httpx.codes.OK:
            msg = f"{caption_url}: HTTP {response.status_code}"
            raise VideoFetchFailedError(msg)
        if not response.content.strip():
            msg = f"{caption_url}: empty caption body"
            raise VideoFetchFailedError(msg)
        try:
            payload = response.json()
        except json.JSONDecodeError as error:
            msg = f"{caption_url}: malformed caption JSON"
            raise VideoFetchFailedError(msg) from error
        try:
            return _CaptionsPayload.model_validate(payload)
        except ValidationError as error:
            msg = f"{caption_url}: caption payload missing expected fields"
            raise VideoFetchFailedError(msg) from error
