from __future__ import annotations

import re

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from engram.config import KnowledgeBaseSettings, SourceResources
from engram.sources.contracts import (
    ScanError,
    ScanReport,
    ScanWatchList,
    ScanWatchListHandler,
    SourceQueue,
    SourceType,
    WatchEntry,
    WatchListFile,
    claimants_for,
    url_key,
)
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.exceptions import ContentFilterError, ModelContextWindowExceededError, ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.native_tools import XSearchTool

from engram_connectors.x.contracts import XScanner
from engram_connectors.x.domain import XPost, XScan
from engram_connectors.x.infrastructure.scanner import x_scan_agent
from engram_connectors.xai import XaiResource

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from engram.sources.contracts import Connector, ItemPreview, SourceItem, UrlClaimant
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo

    from engram_connectors.x.infrastructure.scanner import AgentFactory

_NOW = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
_X_STATUS = re.compile(r"/status/(\w+)")
_FAKE_CONNECTOR = "fake-host"
"""The one provider name this suite's knowledge bases declare, resolved through the registry's own injectable seam."""


class _FakeXProvider:
    """Stands in for a provider that recognizes X post URLs, so this suite needs no provider package installed."""

    @staticmethod
    def claims(url: str) -> SourceType | None:
        return SourceType.POST if _X_STATUS.search(url) else None

    @staticmethod
    def key_for(url: str) -> str:
        match = _X_STATUS.search(url)
        return f"x-{match.group(1)}" if match else url

    @staticmethod
    def source_type_of(item_type: str) -> SourceType:
        del item_type
        raise NotImplementedError

    @classmethod
    def build(cls, resources: SourceResources) -> Connector:
        del resources
        raise NotImplementedError

    async def list_items_with_pdf(self) -> list[SourceItem]:
        raise NotImplementedError

    async def preview_items(self) -> ItemPreview:
        raise NotImplementedError

    async def get(self, provider_key: str) -> SourceItem | None:
        del provider_key
        raise NotImplementedError


def _declared_claimants(names: Sequence[str]) -> Sequence[UrlClaimant]:
    """The declared names resolved against this suite's own registry source, never the installed packages."""
    return claimants_for(names, source={_FAKE_CONNECTOR: _FakeXProvider})


POST = XPost(
    url="https://x.com/someone/status/1234567890",
    handle="someone",
    date=datetime(2026, 8, 24, 18, 0, tzinfo=UTC),
    text="New paper on planning agents: https://arxiv.org/abs/2401.01234 The task list survives compaction.",
    links=["https://arxiv.org/abs/2401.01234"],
)


@dataclass
class _AgentFactory:
    """A scriptable `agent_factory`: records every call, replays a scripted `XScan` through `TestModel` for every slice."""

    scan: XScan
    seen: list[tuple[list[str], datetime, datetime]] = field(default_factory=list)

    def __call__(self, handles: Sequence[str], since: datetime, until: datetime) -> Agent[None, XScan]:
        """Record the call and hand back a `TestModel` agent that answers with the scripted scan."""
        self.seen.append((list(handles), since, until))
        return Agent(TestModel(custom_output_args=self.scan.model_dump(mode="json")), output_type=XScan)


def _scanner(factory: AgentFactory, *, cache_dir: Path) -> XScanner:
    """A scanner over `factory` whose clock reads `_NOW`, keying posts through this suite's own claimant resolution."""
    return XScanner(factory, cache_dir=cache_dir, connectors=(_FAKE_CONNECTOR,), resolve_claimants=_declared_claimants, clock=lambda: _NOW)


def _resources(root: Path, *, connectors: tuple[str, ...] = (_FAKE_CONNECTOR,), key: str = "xai-secret") -> SourceResources:
    """Resources for a knowledge base root whose `knowledge-base-schema.json` no reader could load, so a build reading it could not get past it."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    return SourceResources(knowledge_base=KnowledgeBaseSettings(path=root), connectors=connectors, settings={"xai": {"api_key": key}} if key else {})


async def test_a_post_becomes_an_entry_a_cache_file_and_one_entry_per_link(tmp_path: Path) -> None:
    since = _NOW - timedelta(days=2)
    factory = _AgentFactory(XScan(posts=[POST]))
    scanner = _scanner(factory, cache_dir=tmp_path / "cache")

    entries = (await scanner.scan(WatchEntry(scanner="x", ref="someone"), since=since)).sources

    assert [e.url for e in entries] == ["https://x.com/someone/status/1234567890", "https://arxiv.org/abs/2401.01234"]
    assert entries[0].published == POST.date
    assert entries[0].title.startswith("New paper on planning agents")
    body = (tmp_path / "cache" / "x-1234567890.md").read_text(encoding="utf-8")
    assert "@someone" in body
    assert "task list survives compaction" in body
    meta = (tmp_path / "cache" / "x-1234567890.meta.json").read_text(encoding="utf-8")
    assert '"converter":"x-post"' in meta.replace(" ", "")
    assert factory.seen == [(["someone"], since, _NOW - timedelta(days=1)), (["someone"], _NOW - timedelta(days=1), _NOW)]


async def test_a_post_with_no_links_yields_exactly_one_entry(tmp_path: Path) -> None:
    post = POST.model_copy(update={"links": []})
    scanner = _scanner(_AgentFactory(XScan(posts=[post])), cache_dir=tmp_path)

    entries = (await scanner.scan(WatchEntry(scanner="x", ref="someone"), since=None)).sources

    assert len(entries) == 1
    assert entries[0].url == post.url


async def test_a_slice_that_reaches_the_cap_ends_the_walk_with_every_post_it_answered(tmp_path: Path) -> None:
    posts = [POST.model_copy(update={"url": f"https://x.com/someone/status/{n}"}) for n in range(5)]
    factory = _AgentFactory(XScan(posts=posts))
    scanner = _scanner(factory, cache_dir=tmp_path)

    scan = await scanner.scan(WatchEntry(scanner="x", ref="someone", max_items=2), since=None)

    assert len(factory.seen) == 1, "the first day already holds the cap"
    assert len([e for e in scan.sources if "/status/" in e.url]) == 5, "a post past the cap inside a walked day is kept, not skipped"
    assert scan.covered_until == _NOW - timedelta(days=6)


async def test_the_posts_come_back_newest_first_when_the_model_answers_them_out_of_order(tmp_path: Path) -> None:
    dated = [
        POST.model_copy(update={"url": f"https://x.com/someone/status/{n}", "date": datetime(2026, 8, 20 + n, 12, 0, tzinfo=UTC)})
        for n in range(5)
    ]
    shuffled = [dated[2], dated[0], dated[4], dated[1], dated[3]]
    scanner = _scanner(_AgentFactory(XScan(posts=shuffled)), cache_dir=tmp_path)

    entries = (await scanner.scan(WatchEntry(scanner="x", ref="someone", max_items=2), since=None)).sources

    assert [e.url for e in entries if "/status/" in e.url] == [post.url for post in reversed(dated)]


async def test_a_post_older_than_the_stamp_is_dropped_and_an_undated_one_stays(tmp_path: Path) -> None:
    """The X scanner ends up where the built-in XML scanner does: nothing older than the feed's own stamp."""
    older = POST.model_copy(update={"url": "https://x.com/someone/status/1", "date": _NOW - timedelta(days=3), "links": []})
    newer = POST.model_copy(update={"url": "https://x.com/someone/status/2", "date": _NOW - timedelta(hours=1), "links": []})
    undated = POST.model_copy(update={"url": "https://x.com/someone/status/3", "date": None, "links": []})
    scanner = _scanner(_AgentFactory(XScan(posts=[older, newer, undated])), cache_dir=tmp_path)

    entries = (await scanner.scan(WatchEntry(scanner="x", ref="someone"), since=_NOW - timedelta(days=1))).sources

    assert [e.url for e in entries] == [newer.url, undated.url]
    assert not (tmp_path / "x-1.md").exists(), "a dropped post is not rendered into the cache either"


async def test_a_post_the_model_dated_without_an_offset_is_read_as_utc(tmp_path: Path) -> None:
    naive = POST.model_copy(update={"url": "https://x.com/someone/status/4", "date": datetime(2026, 8, 25, 8, 0), "links": []})  # noqa: DTZ001 (a model's answer carrying no offset is the case under test)
    scanner = _scanner(_AgentFactory(XScan(posts=[naive])), cache_dir=tmp_path)

    entries = (await scanner.scan(WatchEntry(scanner="x", ref="someone"), since=_NOW - timedelta(days=1))).sources

    assert [e.url for e in entries] == [naive.url]
    assert entries[0].published == datetime(2026, 8, 25, 8, 0, tzinfo=UTC), "read as UTC, so it compares against the stamp"


async def test_a_factory_failure_becomes_a_feed_scan_error(tmp_path: Path) -> None:
    def factory(handles: Sequence[str], since: datetime, until: datetime) -> Agent[None, XScan]:
        msg = "no xai api key configured"
        raise RuntimeError(msg)

    scanner = _scanner(factory, cache_dir=tmp_path)

    with pytest.raises(ScanError, match="no xai api key configured"):
        await scanner.scan(WatchEntry(scanner="x", ref="someone"), since=None)


_OUT_OF_CREDITS = (
    "Your team 0c3b7a52-9d1e-4f6a-8b2c-5e4d3f2a1b0c has either used all available credits or reached its monthly spending limit. "
    "To continue making API requests, please purchase more credits or raise your spending limit."
)
"""What xAI answers over gRPC when the account behind the API key has run out of credits."""


def _failing_with(error: Exception) -> AgentFactory:
    """An agent factory whose agent's model call raises `error`, as the xAI model does on an error status."""

    def answer(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise error

    return lambda _handles, _since, _until: Agent(FunctionModel(answer), output_type=XScan)


async def test_an_http_error_from_xai_is_one_line_with_the_first_sentence_and_no_team_id(tmp_path: Path) -> None:
    scanner = _scanner(_failing_with(ModelHTTPError(403, "grok-4-fast-non-reasoning", body=_OUT_OF_CREDITS)), cache_dir=tmp_path)

    with pytest.raises(ScanError) as raised:
        await scanner.scan(WatchEntry(scanner="x", ref="someone"), since=None)

    assert str(raised.value) == (
        "xAI answered HTTP 403 for grok-4-fast-non-reasoning: "
        "Your team has either used all available credits or reached its monthly spending limit."
    )


async def test_an_http_error_whose_body_is_longer_than_a_line_drops_the_body(tmp_path: Path) -> None:
    page = "<html><head><title>Bad gateway</title></head><body>" + "x" * 500 + "</body></html>"
    scanner = _scanner(_failing_with(ModelHTTPError(502, "grok-4-fast-non-reasoning", body=page)), cache_dir=tmp_path)

    with pytest.raises(ScanError) as raised:
        await scanner.scan(WatchEntry(scanner="x", ref="someone"), since=None)

    assert str(raised.value) == "xAI answered HTTP 502 for grok-4-fast-non-reasoning"


_DAY_SKIPPED = "skipped the day from 2026-08-24T09:00:00+00:00: "
"""How a one-day scan from `_NOW` less a day opens the failure line of a day it skipped."""


async def _scan_failure(error: Exception, tmp_path: Path) -> str:
    """The one line a one-day scan answers or raises when the agent's model call raises `error`."""
    scanner = _scanner(_failing_with(error), cache_dir=tmp_path)
    try:
        scan = await scanner.scan(WatchEntry(scanner="x", ref="someone"), since=_NOW - timedelta(days=1))
    except ScanError as raised:
        return str(raised)
    return scan.failure


async def test_an_unexpected_model_behavior_is_its_message_without_the_body(tmp_path: Path) -> None:
    error = UnexpectedModelBehavior("Received empty model response", body='{"choices": [], "id": "resp-1"}')

    assert await _scan_failure(error, tmp_path) == _DAY_SKIPPED + "Received empty model response"


async def test_a_content_filter_is_the_sentence_naming_its_categories_without_the_body(tmp_path: Path) -> None:
    details = {"finish_reason": "content_filter", "content_filter_result": {"violence": {"filtered": True, "severity": "medium"}}}
    response = ModelResponse(parts=[], model_name="grok-4-fast-non-reasoning", finish_reason="content_filter", provider_details=details)
    error = ContentFilterError("Content filter triggered.", body=ModelMessagesTypeAdapter.dump_json([response]).decode())

    assert await _scan_failure(error, tmp_path) == (
        _DAY_SKIPPED + "Content filter triggered. Finish reason: 'content_filter'. Filter results: violence filtered at severity medium."
    )


async def test_a_team_id_is_cut_from_any_failure(tmp_path: Path) -> None:
    error = RuntimeError("Your team 0c3b7a52-9d1e-4f6a-8b2c-5e4d3f2a1b0c is blocked from X search.")

    assert await _scan_failure(error, tmp_path) == "Your team is blocked from X search."


def test_the_scan_agent_answers_as_native_structured_output() -> None:
    agent = x_scan_agent(XaiResource(api_key="test"), handles=["someone"], since=_NOW - timedelta(days=1), until=_NOW)

    assert isinstance(agent.output_type, NativeOutput)


_EMPTY_SCAN = '{"posts": []}'


def _requests_answered_empty(seen: list[ModelRequest], tools: list[XSearchTool] | None = None) -> FunctionModel:
    """A model that records each request it receives and the X search tools it is given, and answers an empty scan."""

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(message for message in messages if isinstance(message, ModelRequest))
        if tools is not None:
            tools.extend(tool for tool in info.model_request_parameters.native_tools if isinstance(tool, XSearchTool))
        return ModelResponse(parts=[TextPart(_EMPTY_SCAN)])

    return FunctionModel(answer)


async def test_the_scan_agent_searches_only_its_slice_of_the_window() -> None:
    agent = x_scan_agent(XaiResource(api_key="test"), handles=["someone"], since=_NOW - timedelta(days=1), until=_NOW)
    tools: list[XSearchTool] = []

    with agent.override(model=_requests_answered_empty([], tools)):
        await agent.run("List every post by @someone.")

    assert [(tool.allowed_x_handles, tool.from_date, tool.to_date) for tool in tools] == [(["someone"], _NOW - timedelta(days=1), _NOW)]


async def test_the_scan_agent_tells_the_model_how_to_search_and_what_to_return() -> None:
    agent = x_scan_agent(XaiResource(api_key="test"), handles=["someone"], since=_NOW - timedelta(days=1), until=_NOW)
    seen: list[ModelRequest] = []

    with agent.override(model=_requests_answered_empty(seen)):
        await agent.run("List every post by @someone.")

    assert seen[0].instructions == (
        "You list what an X account posted. Use X search over the allowed handles. "
        "Search with the query `from:<handle>` alone. Never put a date operator such as `since:` or `until:` in the query, "
        "because the date window you were given already bounds X search. "
        "When a search answers a post from outside the window, return it too. "
        "Search more than once before you answer that there are no posts: search in Latest mode and in Top mode, "
        "and never repeat a search that found nothing. "
        "Return every post with substance in the window (a claim, a link, a thread), up to the number of posts the request asks for. "
        "Keep searching while a search still finds posts you do not hold yet. "
        "Give each post's URL, the handle, the date, the full text, and every link it carries. Newest first. "
        "Do not summarize. Copy the text."
    )


async def _scan_prompts(tmp_path: Path, entry: WatchEntry, *, since: datetime | None) -> list[str]:
    """The user prompt of every search run one scan of `entry` makes, in the order it makes them."""
    seen: list[ModelRequest] = []
    model = _requests_answered_empty(seen)
    scanner = _scanner(lambda _handles, _since, _until: Agent(model, output_type=NativeOutput(XScan)), cache_dir=tmp_path)

    await scanner.scan(entry, since=since)

    prompts = [part.content for request in seen for part in request.parts if isinstance(part, UserPromptPart)]
    assert all(isinstance(prompt, str) for prompt in prompts)
    return [str(prompt) for prompt in prompts]


async def test_a_search_run_asks_for_the_posts_in_its_slice_up_to_the_entrys_cap(tmp_path: Path) -> None:
    entry = WatchEntry(scanner="x", ref="someone", max_items=150)

    prompts = await _scan_prompts(tmp_path, entry, since=_NOW - timedelta(days=1))

    assert prompts == ["List every post by @someone from 2026-08-24T09:00:00+00:00 to 2026-08-25T09:00:00+00:00, up to 150 posts."]


def _post_at(when: datetime, number: int) -> XPost:
    """A post with no links, made at `when`, whose URL carries `number`."""
    return XPost(url=f"https://x.com/someone/status/{number}", handle="someone", date=when, text=f"post {number}")


@dataclass
class _Timeline:
    """A scriptable `agent_factory` over one account's posts, as X search answers them.

    Each run answers the posts dated inside its slice, newest first, at most the number its prompt asks for and at
    most `ceiling`. The run numbered in `failures` raises that error instead, and every run from number `fail_from`
    on raises `failure`. `windows` records each run's slice.
    """

    posts: list[XPost]
    ceiling: int = 20
    fail_from: int | None = None
    failure: Exception = field(default_factory=lambda: _service_fault(503, "Service unavailable."))
    failures: dict[int, Exception] = field(default_factory=dict)
    windows: list[tuple[datetime, datetime]] = field(default_factory=list)

    def __call__(self, handles: Sequence[str], since: datetime, until: datetime) -> Agent[None, XScan]:
        """Record the slice and hand back an agent that answers the posts inside it, or one that raises this run's error."""
        del handles
        self.windows.append((since, until))
        run = len(self.windows)
        error = self.failures.get(run, self.failure if self.fail_from is not None and run >= self.fail_from else None)
        if error is not None:
            return _failing_with(error)([], since, until)
        inside = sorted((post for post in self.posts if post.date is not None and since <= post.date <= until), key=_newest_first)

        def answer(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            prompt = " ".join(
                str(part.content)
                for message in messages
                if isinstance(message, ModelRequest)
                for part in message.parts
                if isinstance(part, UserPromptPart)
            )
            asked = _ASKED.search(prompt)
            wanted = min(int(asked.group(1)) if asked else self.ceiling, self.ceiling)
            return ModelResponse(parts=[TextPart(XScan(posts=inside[:wanted]).model_dump_json())])

        return Agent(FunctionModel(answer), output_type=NativeOutput(XScan))


_ASKED = re.compile(r"up to (\d+) posts")
"""How a search run's prompt names the number of posts it asks for."""


def _service_fault(status: int, sentence: str) -> ModelHTTPError:
    """An error status from xAI, as the model raises it."""
    return ModelHTTPError(status, "grok-4-fast-non-reasoning", body=sentence)


def _newest_first(post: XPost) -> float:
    """Sort key for newest-first order of dated posts."""
    return -post.date.timestamp() if post.date is not None else 0.0


def _days(start: datetime, count: int) -> list[tuple[datetime, datetime]]:
    """The first `count` one-day slices from `start`, oldest first."""
    return [(start + timedelta(days=day), start + timedelta(days=day + 1)) for day in range(count)]


async def test_a_three_day_window_is_searched_one_day_at_a_time_oldest_first(tmp_path: Path) -> None:
    since = _NOW - timedelta(days=3)
    posts = [_post_at(_NOW - timedelta(hours=hours), hours) for hours in (2, 30, 50)]
    timeline = _Timeline(posts)

    scan = await _scanner(timeline, cache_dir=tmp_path).scan(WatchEntry(scanner="x", ref="someone"), since=since)

    assert timeline.windows == _days(since, 3)
    assert [source.url for source in scan.sources] == [post.url for post in posts], "answered newest first"
    assert (scan.covered_until, scan.failure) == (None, "")


async def test_a_day_answering_eighteen_posts_or_more_is_searched_once_and_noted_as_maybe_holding_more(tmp_path: Path) -> None:
    since = _NOW - timedelta(days=1)
    timeline = _Timeline([_post_at(_NOW - timedelta(minutes=10 * (n + 1)), n) for n in range(25)])

    scan = await _scanner(timeline, cache_dir=tmp_path).scan(WatchEntry(scanner="x", ref="someone", max_items=150), since=since)

    assert timeline.windows == _days(since, 1)
    assert len(scan.sources) == 20, "the search's own ceiling"
    assert scan.notes == ["the day from 2026-08-24T09:00:00+00:00 answered 20 posts, X search may have held more"]
    assert scan.failure == ""


async def test_a_day_answering_seventeen_posts_carries_no_note(tmp_path: Path) -> None:
    since = _NOW - timedelta(days=1)
    timeline = _Timeline([_post_at(_NOW - timedelta(minutes=10 * (n + 1)), n) for n in range(17)])

    scan = await _scanner(timeline, cache_dir=tmp_path).scan(WatchEntry(scanner="x", ref="someone", max_items=150), since=since)

    assert scan.notes == []


async def test_the_walk_stops_at_fourteen_days_and_covers_up_to_the_last_one(tmp_path: Path) -> None:
    since = _NOW - timedelta(days=16)
    timeline = _Timeline([_post_at(since + timedelta(days=day, hours=12), day) for day in range(16)])

    scan = await _scanner(timeline, cache_dir=tmp_path).scan(WatchEntry(scanner="x", ref="someone", max_items=150), since=since)

    assert timeline.windows == _days(since, 14)
    assert len(scan.sources) == 14
    assert (scan.covered_until, scan.failure) == (since + timedelta(days=14), "")


async def test_the_walk_stops_at_the_day_that_reaches_the_cap_and_keeps_all_its_posts(tmp_path: Path) -> None:
    since = _NOW - timedelta(days=3)
    posts = [_post_at(since + timedelta(days=day, hours=hour + 1), 100 * day + hour) for day in range(3) for hour in range(10)]
    timeline = _Timeline(posts)

    scan = await _scanner(timeline, cache_dir=tmp_path).scan(WatchEntry(scanner="x", ref="someone", max_items=15), since=since)

    assert timeline.windows == _days(since, 2), "the second day reaches the cap, so the third is never searched"
    assert sorted(source.url for source in scan.sources) == sorted(post.url for post in posts[:20])
    assert scan.covered_until == since + timedelta(days=2)


async def test_a_slice_that_fails_ends_the_scan_covered_up_to_the_slice_before_it(tmp_path: Path) -> None:
    since = _NOW - timedelta(days=3)
    timeline = _Timeline([_post_at(since + timedelta(hours=2), 1), _post_at(since + timedelta(hours=30), 2)], fail_from=3)

    scan = await _scanner(timeline, cache_dir=tmp_path).scan(WatchEntry(scanner="x", ref="someone"), since=since)

    assert timeline.windows == _days(since, 3), "the failing third day ends the walk"
    assert [source.url for source in scan.sources] == ["https://x.com/someone/status/2", "https://x.com/someone/status/1"]
    assert scan.covered_until == since + timedelta(days=2)
    assert scan.failure == "xAI answered HTTP 503 for grok-4-fast-non-reasoning: Service unavailable."


def _watch_list_stamped(tmp_path: Path, when: datetime) -> WatchListFile:
    """A watch list holding the one entry `x:someone`, stamped at `when`."""
    watch_list = WatchListFile(tmp_path / "watch-list.yaml")
    watch_list.add("x", "someone")
    watch_list.stamp("x", "someone", when)
    return watch_list


async def _refresh(tmp_path: Path, watch_list: WatchListFile, timeline: _Timeline) -> tuple[ScanReport, SourceQueue]:
    """One watch-list scan with the X scanner over `timeline`, at `_NOW`, and the source queue it fed."""
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_declared_claimants((_FAKE_CONNECTOR,)))
    handler = ScanWatchListHandler(watch_list, {"x": _scanner(timeline, cache_dir=tmp_path / "cache")}, queue, clock=lambda: _NOW)
    return await handler.handle(ScanWatchList()), queue


async def test_after_a_slice_fails_the_entry_is_stamped_at_the_end_of_the_slice_before_it(tmp_path: Path) -> None:
    since = _NOW - timedelta(days=3)
    watch_list = _watch_list_stamped(tmp_path, since)

    report, queue = await _refresh(tmp_path, watch_list, _Timeline([_post_at(since + timedelta(hours=2), 1)], fail_from=2))

    assert [row.url for row in queue.entries()] == ["https://x.com/someone/status/1"]
    assert report.failed == (("x:someone", "xAI answered HTTP 503 for grok-4-fast-non-reasoning: Service unavailable."),)
    assert watch_list.entries()[0].last_scanned == since + timedelta(days=1)


async def test_a_budget_stopped_scan_stamps_the_end_of_its_last_day_and_the_next_one_continues_there(tmp_path: Path) -> None:
    since = _NOW - timedelta(days=21)
    watch_list = _watch_list_stamped(tmp_path, since)
    timeline = _Timeline([_post_at(since + timedelta(days=day, hours=12), day) for day in range(21)])

    await _refresh(tmp_path, watch_list, timeline)
    first_stamp = watch_list.entries()[0].last_scanned
    await _refresh(tmp_path, watch_list, timeline)

    assert first_stamp == since + timedelta(days=14)
    assert timeline.windows == _days(since, 21), "the second scan walks the last seven days"
    assert watch_list.entries()[0].last_scanned == _NOW


async def test_a_scan_of_the_whole_window_that_finds_nothing_keeps_only_its_last_day_open(tmp_path: Path) -> None:
    since = _NOW - timedelta(days=3)
    watch_list = _watch_list_stamped(tmp_path, since)

    report, _ = await _refresh(tmp_path, watch_list, _Timeline([]))

    assert (report.found, report.failed) == (0, ())
    assert watch_list.entries()[0].last_scanned == _NOW - timedelta(days=1)


async def test_a_window_shorter_than_a_day_is_one_search_and_an_empty_one_moves_no_stamp(tmp_path: Path) -> None:
    since = _NOW - timedelta(hours=6)
    timeline = _Timeline([])

    scan = await _scanner(timeline, cache_dir=tmp_path).scan(WatchEntry(scanner="x", ref="someone"), since=since)

    assert timeline.windows == [(since, _NOW)]
    assert (scan.sources, scan.covered_until) == ([], since), "not later than the stamp, so the stamp stays"


async def test_a_window_shorter_than_a_day_with_a_post_covers_up_to_now(tmp_path: Path) -> None:
    since = _NOW - timedelta(hours=6)
    timeline = _Timeline([_post_at(_NOW - timedelta(hours=1), 1)])

    scan = await _scanner(timeline, cache_dir=tmp_path).scan(WatchEntry(scanner="x", ref="someone"), since=since)

    assert [source.url for source in scan.sources] == ["https://x.com/someone/status/1"]
    assert scan.covered_until is None


async def test_a_content_fault_skips_its_day_reports_it_and_the_walk_goes_on(tmp_path: Path) -> None:
    since = _NOW - timedelta(days=3)
    posts = [_post_at(since + timedelta(hours=2), 1), _post_at(since + timedelta(days=2, hours=2), 3)]
    timeline = _Timeline(posts, failures={2: UnexpectedModelBehavior("Received empty model response")})

    scan = await _scanner(timeline, cache_dir=tmp_path).scan(WatchEntry(scanner="x", ref="someone"), since=since)

    assert timeline.windows == _days(since, 3)
    assert [source.url for source in scan.sources] == ["https://x.com/someone/status/3", "https://x.com/someone/status/1"]
    assert scan.covered_until is None, "the faulty day is covered, so the entry moves on"
    assert scan.failure == "skipped the day from 2026-08-23T09:00:00+00:00: Received empty model response"


async def test_a_context_window_overflow_on_the_first_day_skips_it_instead_of_raising(tmp_path: Path) -> None:
    since = _NOW - timedelta(days=2)
    overflow = ModelHTTPError.for_category(
        ModelContextWindowExceededError, status_code=400, model_name="grok-4-fast-non-reasoning", body="Maximum prompt length exceeded."
    )
    timeline = _Timeline([_post_at(_NOW - timedelta(hours=2), 2)], failures={1: overflow})

    scan = await _scanner(timeline, cache_dir=tmp_path).scan(WatchEntry(scanner="x", ref="someone"), since=since)

    assert [source.url for source in scan.sources] == ["https://x.com/someone/status/2"]
    assert scan.failure.startswith("skipped the day from 2026-08-23T09:00:00+00:00: xAI answered HTTP 400")


async def test_several_skipped_days_are_one_line_naming_the_first(tmp_path: Path) -> None:
    since = _NOW - timedelta(days=3)
    empty = UnexpectedModelBehavior("Received empty model response")
    timeline = _Timeline([_post_at(_NOW - timedelta(hours=2), 3)], failures={1: empty, 2: empty})

    scan = await _scanner(timeline, cache_dir=tmp_path).scan(WatchEntry(scanner="x", ref="someone"), since=since)

    assert scan.failure == "skipped the day from 2026-08-22T09:00:00+00:00 and 1 more: Received empty model response"


async def test_a_service_fault_after_a_day_keeps_its_own_day_open(tmp_path: Path) -> None:
    since = _NOW - timedelta(days=3)
    out_of_credits = _service_fault(429, "Your team has run out of credits.")
    timeline = _Timeline([_post_at(since + timedelta(hours=2), 1), _post_at(_NOW - timedelta(hours=2), 3)], failures={2: out_of_credits})

    scan = await _scanner(timeline, cache_dir=tmp_path).scan(WatchEntry(scanner="x", ref="someone"), since=since)

    assert timeline.windows == _days(since, 2), "a fault of the service ends the walk"
    assert [source.url for source in scan.sources] == ["https://x.com/someone/status/1"]
    assert scan.covered_until == since + timedelta(days=1)
    assert scan.failure == "xAI answered HTTP 429 for grok-4-fast-non-reasoning: Your team has run out of credits."


async def test_a_service_fault_after_empty_days_answers_how_far_the_walk_got_instead_of_raising(tmp_path: Path) -> None:
    since = _NOW - timedelta(days=3)
    timeline = _Timeline([], fail_from=3)

    scan = await _scanner(timeline, cache_dir=tmp_path).scan(WatchEntry(scanner="x", ref="someone"), since=since)

    assert (scan.sources, scan.covered_until) == ([], since + timedelta(days=2))
    assert scan.failure == "xAI answered HTTP 503 for grok-4-fast-non-reasoning: Service unavailable."


async def test_a_service_fault_on_the_first_day_raises(tmp_path: Path) -> None:
    timeline = _Timeline([], failures={1: _service_fault(401, "Incorrect API key provided.")})

    with pytest.raises(ScanError, match="xAI answered HTTP 401"):
        await _scanner(timeline, cache_dir=tmp_path).scan(WatchEntry(scanner="x", ref="someone"), since=_NOW - timedelta(days=2))


async def test_a_day_whose_run_passes_eight_model_responses_is_skipped_as_a_content_fault(tmp_path: Path) -> None:
    responses: list[int] = []

    def answer(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        responses.append(1)
        return ModelResponse(parts=[TextPart("not a scan")])

    def factory(_handles: Sequence[str], _since: datetime, _until: datetime) -> Agent[None, XScan]:
        return Agent(FunctionModel(answer), output_type=NativeOutput(XScan), retries=50)

    scan = await _scanner(factory, cache_dir=tmp_path).scan(WatchEntry(scanner="x", ref="someone"), since=_NOW - timedelta(days=1))

    assert len(responses) == 8
    assert scan.failure.startswith(_DAY_SKIPPED)
    assert "request_limit of 8" in scan.failure


async def test_a_budget_stopped_scan_that_finds_nothing_still_moves_the_stamp(tmp_path: Path) -> None:
    since = _NOW - timedelta(days=20)
    watch_list = _watch_list_stamped(tmp_path, since)

    await _refresh(tmp_path, watch_list, _Timeline([]))

    assert watch_list.entries()[0].last_scanned == since + timedelta(days=14), "a quiet account never re-walks the same fourteen days"


async def test_a_first_scan_searches_the_last_seven_days_one_day_at_a_time(tmp_path: Path) -> None:
    timeline = _Timeline([_post_at(_NOW - timedelta(days=6, hours=12), 1), _post_at(_NOW - timedelta(days=8), 2)])

    scan = await _scanner(timeline, cache_dir=tmp_path).scan(WatchEntry(scanner="x", ref="someone"), since=None)

    assert timeline.windows == _days(_NOW - timedelta(days=7), 7)
    assert [source.url for source in scan.sources] == ["https://x.com/someone/status/1"]
    assert scan.covered_until is None


async def test_a_first_scan_asks_each_day_of_the_last_seven_up_to_the_entrys_cap(tmp_path: Path) -> None:
    prompts = await _scan_prompts(tmp_path, WatchEntry(scanner="x", ref="someone", max_items=12), since=None)

    assert len(prompts) == 7
    assert prompts[0] == "List every post by @someone from 2026-08-18T09:00:00+00:00 to 2026-08-19T09:00:00+00:00, up to 12 posts."
    assert prompts[-1] == "List every post by @someone from 2026-08-24T09:00:00+00:00 to 2026-08-25T09:00:00+00:00, up to 12 posts."


def test_building_without_the_xai_settings_block_refuses_naming_it(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="the x scanner requires an xai settings block carrying an api_key"):
        XScanner.build(_resources(tmp_path, key=""))


async def test_a_built_scanner_renders_a_post_under_the_corpus_intake_cache(tmp_path: Path) -> None:
    """The knowledge base root carries no spec file, so a build that read one could not get this far."""
    built = XScanner.build(
        _resources(tmp_path), agent_factory_for=lambda xai: _AgentFactory(XScan(posts=[POST])), resolve_claimants=_declared_claimants
    )

    entries = (await built.scan(WatchEntry(scanner="x", ref="someone"), since=None)).sources

    assert [e.url for e in entries] == [POST.url, "https://arxiv.org/abs/2401.01234"]
    rendered = [path.name for path in sorted((tmp_path / "_meta" / "source-queue-cache").glob("*.md"))]
    assert rendered == ["x-1234567890.md"], "keyed by the provider the knowledge base declares"


async def test_a_built_scanner_for_a_corpus_declaring_nothing_keys_a_post_generically(tmp_path: Path) -> None:
    built = XScanner.build(
        _resources(tmp_path, connectors=()),
        agent_factory_for=lambda xai: _AgentFactory(XScan(posts=[POST])),
        resolve_claimants=_declared_claimants,
    )

    await built.scan(WatchEntry(scanner="x", ref="someone"), since=None)

    rendered = [path.name for path in sorted((tmp_path / "_meta" / "source-queue-cache").glob("*.md"))]
    assert rendered == [f"{url_key(POST.url)}.md"], "no declared name claims the URL, so it lands on the generic key"


async def test_a_built_scanner_skips_a_declared_name_nothing_registers(tmp_path: Path) -> None:
    built = XScanner.build(
        _resources(tmp_path, connectors=("nowhere",)),
        agent_factory_for=lambda xai: _AgentFactory(XScan(posts=[POST])),
        resolve_claimants=_declared_claimants,
    )

    await built.scan(WatchEntry(scanner="x", ref="someone"), since=None)

    rendered = [path.name for path in sorted((tmp_path / "_meta" / "source-queue-cache").glob("*.md"))]
    assert rendered == [f"{url_key(POST.url)}.md"], "an unresolvable name is skipped, the scan still renders"
