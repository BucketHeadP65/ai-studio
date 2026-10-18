"""Unit tests for `YouTubeTranscriptFetcher`.

No real network happens: HTTP is faked with `httpx.MockTransport`, whose handler dispatches on
the requested URL to return canned watch-page HTML, a canned player-API response, or a canned
`json3` caption payload. A handler that answers the player API with anything but a player
response exercises the fallback onto the watch page's own caption-track list.
"""

from __future__ import annotations

import json

import httpx
import pytest

from engram_connectors.youtube.contracts import TranscriptUnavailableError, VideoFetchFailedError, YouTubeTranscriptFetcher

VIDEO_ID = "abc123XYZ_-"
WATCH_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
PLAYER_API_URL = "https://www.youtube.com/youtubei/v1/player"
PLAYER_API_CAPTION_BASE_URL = f"https://www.youtube.com/api/timedtext?v={VIDEO_ID}&lang=en&via=player-api"
MANUAL_CAPTION_BASE_URL = f"https://www.youtube.com/api/timedtext?v={VIDEO_ID}&lang=en&manual=1"
ASR_CAPTION_BASE_URL = f"https://www.youtube.com/api/timedtext?v={VIDEO_ID}&lang=en"

PLAYER_RESPONSE = {
    "videoDetails": {"videoId": VIDEO_ID, "title": "Sample Talk", "author": "Sample Channel"},
    "microformat": {"playerMicroformatRenderer": {"publishDate": "2024-03-05"}},
    "captions": {
        "playerCaptionsTracklistRenderer": {
            "captionTracks": [
                {"baseUrl": ASR_CAPTION_BASE_URL, "languageCode": "en", "kind": "asr"},
                {"baseUrl": MANUAL_CAPTION_BASE_URL, "languageCode": "en"},
            ]
        }
    },
}

CAPTIONS_PAYLOAD = {
    "events": [
        {"tStartMs": 0, "segs": [{"utf8": "Hello "}, {"utf8": "world"}]},
        {"tStartMs": 2500, "segs": [{"utf8": "\n"}]},  # append-only marker, no real text -> dropped
        {"tStartMs": 3200, "segs": [{"utf8": "Second line."}]},
    ]
}


def _watch_page(payload: dict) -> str:
    """A watch page with `ytInitialPlayerResponse` embedded amid unrelated inline script noise."""
    return (
        "<!DOCTYPE html><html><head>"
        '<script>var ytcfg = {"INNERTUBE_API_KEY":"noise"};</script>'
        "</head><body>"
        f"<script>var ytInitialPlayerResponse = {json.dumps(payload)};var trailing = 'noise';</script>"
        "</body></html>"
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _happy_path_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url == WATCH_URL:
        return httpx.Response(200, text=_watch_page(PLAYER_RESPONSE))
    if url == PLAYER_API_URL:
        return httpx.Response(200, json=PLAYER_RESPONSE)
    if url.startswith(MANUAL_CAPTION_BASE_URL):
        return httpx.Response(200, json=CAPTIONS_PAYLOAD)
    return pytest.fail(f"unexpected request: {url}")


async def test_fetch_returns_metadata_and_transcript() -> None:
    fetcher = YouTubeTranscriptFetcher(client=_client(_happy_path_handler))

    fetched = await fetcher.fetch(VIDEO_ID)

    assert fetched.url == WATCH_URL
    doc = fetched.doc
    assert doc.video_id == VIDEO_ID
    assert doc.title == "Sample Talk"
    assert doc.channel == "Sample Channel"
    assert doc.published is not None
    assert doc.published.isoformat() == "2024-03-05"
    assert [(segment.start_seconds, segment.text) for segment in doc.segments] == [
        (0.0, "Hello world"),
        (3.2, "Second line."),
    ]


async def test_fetch_prefers_the_manual_track_over_the_asr_track() -> None:
    requested_caption_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == WATCH_URL:
            return httpx.Response(200, text=_watch_page(PLAYER_RESPONSE))
        if url == PLAYER_API_URL:
            return httpx.Response(200, json=PLAYER_RESPONSE)
        requested_caption_urls.append(url)
        return httpx.Response(200, json=CAPTIONS_PAYLOAD)

    fetcher = YouTubeTranscriptFetcher(client=_client(handler))
    await fetcher.fetch(VIDEO_ID)

    assert len(requested_caption_urls) == 1
    assert requested_caption_urls[0].startswith(MANUAL_CAPTION_BASE_URL)
    assert "fmt=json3" in requested_caption_urls[0]


async def test_fetch_takes_the_caption_tracks_from_the_player_api_asked_as_the_ios_client() -> None:
    player_api_payload = {
        **PLAYER_RESPONSE,
        "captions": {"playerCaptionsTracklistRenderer": {"captionTracks": [{"baseUrl": PLAYER_API_CAPTION_BASE_URL, "languageCode": "en"}]}},
    }
    player_api_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == WATCH_URL:
            return httpx.Response(200, text=_watch_page(PLAYER_RESPONSE))
        if url == PLAYER_API_URL:
            player_api_bodies.append(json.loads(request.content))
            return httpx.Response(200, json=player_api_payload)
        if url.startswith(PLAYER_API_CAPTION_BASE_URL):
            return httpx.Response(200, json=CAPTIONS_PAYLOAD)
        if url.startswith(MANUAL_CAPTION_BASE_URL):
            return httpx.Response(200, text="")  # the watch page's own URL, empty as YouTube serves it today
        return pytest.fail(f"unexpected request: {url}")

    fetcher = YouTubeTranscriptFetcher(client=_client(handler))
    fetched = await fetcher.fetch(VIDEO_ID)

    assert len(fetched.doc.segments) == 2
    assert fetched.doc.published is not None, "the publish date still comes from the watch page"
    assert player_api_bodies == [{"context": {"client": {"clientName": "IOS", "clientVersion": "20.10.4"}}, "videoId": VIDEO_ID}]


async def test_fetch_falls_back_to_the_watch_page_tracks_when_the_player_api_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == WATCH_URL:
            return httpx.Response(200, text=_watch_page(PLAYER_RESPONSE))
        if url == PLAYER_API_URL:
            return httpx.Response(503)
        if url.startswith(MANUAL_CAPTION_BASE_URL):
            return httpx.Response(200, json=CAPTIONS_PAYLOAD)
        return pytest.fail(f"unexpected request: {url}")

    fetcher = YouTubeTranscriptFetcher(client=_client(handler))
    fetched = await fetcher.fetch(VIDEO_ID)

    assert len(fetched.doc.segments) == 2


async def test_fetch_names_an_empty_caption_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == WATCH_URL:
            return httpx.Response(200, text=_watch_page(PLAYER_RESPONSE))
        if url == PLAYER_API_URL:
            return httpx.Response(200, json=PLAYER_RESPONSE)
        return httpx.Response(200, text="")

    fetcher = YouTubeTranscriptFetcher(client=_client(handler))

    with pytest.raises(VideoFetchFailedError, match="empty caption body"):
        await fetcher.fetch(VIDEO_ID)


async def test_fetch_falls_back_to_the_asr_track_when_no_manual_track_exists() -> None:
    payload = {**PLAYER_RESPONSE, "captions": {"playerCaptionsTracklistRenderer": {"captionTracks": [{"baseUrl": ASR_CAPTION_BASE_URL, "languageCode": "en", "kind": "asr"}]}}}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == WATCH_URL:
            return httpx.Response(200, text=_watch_page(payload))
        if url == PLAYER_API_URL:
            return httpx.Response(200, json=payload)
        if url.startswith(ASR_CAPTION_BASE_URL):
            return httpx.Response(200, json=CAPTIONS_PAYLOAD)
        return pytest.fail(f"unexpected request: {url}")

    fetcher = YouTubeTranscriptFetcher(client=_client(handler))
    fetched = await fetcher.fetch(VIDEO_ID)

    assert len(fetched.doc.segments) == 2


@pytest.mark.parametrize(
    "captions_field",
    [
        {},  # no "captions" key at all
        {"captions": {"playerCaptionsTracklistRenderer": {"captionTracks": []}}},  # present, empty track list
    ],
)
async def test_fetch_raises_transcript_unavailable_when_no_caption_track_exists(captions_field: dict) -> None:
    payload = {k: v for k, v in PLAYER_RESPONSE.items() if k != "captions"} | captions_field

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_watch_page(payload))

    fetcher = YouTubeTranscriptFetcher(client=_client(handler))

    with pytest.raises(TranscriptUnavailableError, match=VIDEO_ID):
        await fetcher.fetch(VIDEO_ID)


async def test_fetch_raises_on_non_ok_watch_page_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    fetcher = YouTubeTranscriptFetcher(client=_client(handler))

    with pytest.raises(VideoFetchFailedError, match=r"HTTP 404"):
        await fetcher.fetch(VIDEO_ID)


async def test_fetch_raises_on_non_ok_caption_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == WATCH_URL:
            return httpx.Response(200, text=_watch_page(PLAYER_RESPONSE))
        return httpx.Response(500)

    fetcher = YouTubeTranscriptFetcher(client=_client(handler))

    with pytest.raises(VideoFetchFailedError, match=r"HTTP 500"):
        await fetcher.fetch(VIDEO_ID)


async def test_fetch_raises_on_malformed_player_response_json() -> None:
    broken_html = (
        "<!DOCTYPE html><html><body>"
        "<script>var ytInitialPlayerResponse = {this is not valid json;</script>"
        "</body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=broken_html)

    fetcher = YouTubeTranscriptFetcher(client=_client(handler))

    with pytest.raises(VideoFetchFailedError, match="malformed"):
        await fetcher.fetch(VIDEO_ID)


async def test_fetch_raises_when_player_response_marker_is_absent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<!DOCTYPE html><html><body>nothing here</body></html>")

    fetcher = YouTubeTranscriptFetcher(client=_client(handler))

    with pytest.raises(VideoFetchFailedError, match="no ytInitialPlayerResponse"):
        await fetcher.fetch(VIDEO_ID)


async def test_fetch_raises_when_player_response_is_missing_required_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_watch_page({"microformat": {}}))

    fetcher = YouTubeTranscriptFetcher(client=_client(handler))

    with pytest.raises(VideoFetchFailedError, match="missing expected fields"):
        await fetcher.fetch(VIDEO_ID)


async def test_fetch_raises_on_malformed_caption_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == WATCH_URL:
            return httpx.Response(200, text=_watch_page(PLAYER_RESPONSE))
        return httpx.Response(200, text="not json")

    fetcher = YouTubeTranscriptFetcher(client=_client(handler))

    with pytest.raises(VideoFetchFailedError, match="malformed caption JSON"):
        await fetcher.fetch(VIDEO_ID)


async def test_fetch_treats_a_missing_publish_date_as_none() -> None:
    payload = {**PLAYER_RESPONSE, "microformat": {"playerMicroformatRenderer": {}}}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == WATCH_URL:
            return httpx.Response(200, text=_watch_page(payload))
        return httpx.Response(200, json=CAPTIONS_PAYLOAD)

    fetcher = YouTubeTranscriptFetcher(client=_client(handler))
    fetched = await fetcher.fetch(VIDEO_ID)

    assert fetched.doc.published is None


async def test_fetch_wraps_a_watch_page_transport_error() -> None:
    """A network-layer failure (no HTTP response at all) must not escape as a raw httpx exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        msg = "connection refused"
        raise httpx.ConnectError(msg, request=request)

    fetcher = YouTubeTranscriptFetcher(client=_client(handler))

    with pytest.raises(VideoFetchFailedError):
        await fetcher.fetch(VIDEO_ID)


async def test_fetch_wraps_a_caption_track_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == WATCH_URL:
            return httpx.Response(200, text=_watch_page(PLAYER_RESPONSE))
        msg = "timed out"
        raise httpx.ReadTimeout(msg, request=request)

    fetcher = YouTubeTranscriptFetcher(client=_client(handler))

    with pytest.raises(VideoFetchFailedError):
        await fetcher.fetch(VIDEO_ID)


async def test_fetch_raises_transcript_unavailable_when_every_caption_event_is_empty() -> None:
    """Every event present but stripped to nothing (e.g. append-only markers) must not yield a silent empty mirror."""
    empty_payload = {"events": [{"tStartMs": 0, "segs": [{"utf8": "\n"}]}, {"tStartMs": 500, "segs": []}]}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == WATCH_URL:
            return httpx.Response(200, text=_watch_page(PLAYER_RESPONSE))
        return httpx.Response(200, json=empty_payload)

    fetcher = YouTubeTranscriptFetcher(client=_client(handler))

    with pytest.raises(TranscriptUnavailableError, match=VIDEO_ID):
        await fetcher.fetch(VIDEO_ID)


async def test_fetch_uses_the_http_timeout_on_every_request() -> None:
    captured_timeouts: list[dict[str, float | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_timeouts.append(request.extensions.get("timeout", {}))
        url = str(request.url)
        if url == WATCH_URL:
            return httpx.Response(200, text=_watch_page(PLAYER_RESPONSE))
        return httpx.Response(200, json=CAPTIONS_PAYLOAD)

    fetcher = YouTubeTranscriptFetcher(client=_client(handler))
    await fetcher.fetch(VIDEO_ID)

    expected = {"connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0}
    assert captured_timeouts == [expected, expected, expected], "the watch page, the player API, and the caption track"
