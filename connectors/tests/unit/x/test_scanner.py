from __future__ import annotations

import re

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from engram.config import KnowledgeBaseSettings, SourceResources
from engram.sources.contracts import ScanError, SourceType, WatchEntry, claimants_for, url_key
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models.test import TestModel

from engram_connectors.x.contracts import XScanner
from engram_connectors.x.domain import XPost, XScan
from engram_connectors.x.infrastructure.scanner import x_scan_agent
from engram_connectors.xai import XaiResource

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from engram.sources.contracts import Connector, SourceItem, UrlClaimant

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
    """A scriptable `agent_factory`: records every call, replays a scripted `XScan` through `TestModel`."""

    scan: XScan
    seen: list[tuple[list[str], datetime | None]] = field(default_factory=list)

    def __call__(self, handles: Sequence[str], since: datetime | None) -> Agent[None, XScan]:
        """Record the call and hand back a `TestModel` agent that answers with the scripted scan."""
        self.seen.append((list(handles), since))
        return Agent(TestModel(custom_output_args=self.scan.model_dump(mode="json")), output_type=XScan)


def _scanner(factory: AgentFactory, *, cache_dir: Path, max_posts: int = 50) -> XScanner:
    """A scanner over `factory`, keying posts through this suite's own claimant resolution."""
    return XScanner(factory, cache_dir=cache_dir, connectors=(_FAKE_CONNECTOR,), resolve_claimants=_declared_claimants, max_posts=max_posts)


def _resources(root: Path, *, connectors: tuple[str, ...] = (_FAKE_CONNECTOR,), key: str = "xai-secret") -> SourceResources:
    """Resources for a knowledge base root that carries no `knowledge-base-schema.json` at all, so a build reading one could not get past it."""
    for required in ("Units", "_meta"):
        (root / required).mkdir(parents=True, exist_ok=True)
    return SourceResources(knowledge_base=KnowledgeBaseSettings(path=root), connectors=connectors, settings={"xai": {"api_key": key}} if key else {})


async def test_a_post_becomes_an_entry_a_cache_file_and_one_entry_per_link(tmp_path: Path) -> None:
    since = _NOW - timedelta(days=2)
    factory = _AgentFactory(XScan(posts=[POST]))
    scanner = _scanner(factory, cache_dir=tmp_path / "cache")

    entries = await scanner.scan(WatchEntry(scanner="x", ref="someone"), since=since)

    assert [e.url for e in entries] == ["https://x.com/someone/status/1234567890", "https://arxiv.org/abs/2401.01234"]
    assert entries[0].published == POST.date
    assert entries[0].title.startswith("New paper on planning agents")
    body = (tmp_path / "cache" / "x-1234567890.md").read_text(encoding="utf-8")
    assert "@someone" in body
    assert "task list survives compaction" in body
    meta = (tmp_path / "cache" / "x-1234567890.meta.json").read_text(encoding="utf-8")
    assert '"converter":"x-post"' in meta.replace(" ", "")
    assert factory.seen == [(["someone"], since)]


async def test_a_post_with_no_links_yields_exactly_one_entry(tmp_path: Path) -> None:
    post = POST.model_copy(update={"links": []})
    scanner = _scanner(_AgentFactory(XScan(posts=[post])), cache_dir=tmp_path)

    entries = await scanner.scan(WatchEntry(scanner="x", ref="someone"), since=None)

    assert len(entries) == 1
    assert entries[0].url == post.url


async def test_the_post_cap_holds(tmp_path: Path) -> None:
    posts = [POST.model_copy(update={"url": f"https://x.com/someone/status/{n}"}) for n in range(5)]
    scanner = _scanner(_AgentFactory(XScan(posts=posts)), cache_dir=tmp_path, max_posts=2)
    entries = await scanner.scan(WatchEntry(scanner="x", ref="someone"), since=None)
    assert len([e for e in entries if "/status/" in e.url]) == 2


async def test_the_cap_keeps_the_newest_posts_when_the_model_returns_them_out_of_order(tmp_path: Path) -> None:
    dated = [
        POST.model_copy(update={"url": f"https://x.com/someone/status/{n}", "date": datetime(2026, 8, 20 + n, 12, 0, tzinfo=UTC)})
        for n in range(5)
    ]
    shuffled = [dated[2], dated[0], dated[4], dated[1], dated[3]]
    scanner = _scanner(_AgentFactory(XScan(posts=shuffled)), cache_dir=tmp_path, max_posts=2)

    entries = await scanner.scan(WatchEntry(scanner="x", ref="someone"), since=None)

    survivors = [e.url for e in entries if "/status/" in e.url]
    assert survivors == [dated[4].url, dated[3].url]


async def test_a_post_older_than_the_stamp_is_dropped_and_an_undated_one_stays(tmp_path: Path) -> None:
    """The X scanner ends up where the built-in XML scanner does: nothing older than the feed's own stamp."""
    older = POST.model_copy(update={"url": "https://x.com/someone/status/1", "date": _NOW - timedelta(days=3), "links": []})
    newer = POST.model_copy(update={"url": "https://x.com/someone/status/2", "date": _NOW - timedelta(hours=1), "links": []})
    undated = POST.model_copy(update={"url": "https://x.com/someone/status/3", "date": None, "links": []})
    scanner = _scanner(_AgentFactory(XScan(posts=[older, newer, undated])), cache_dir=tmp_path)

    entries = await scanner.scan(WatchEntry(scanner="x", ref="someone"), since=_NOW - timedelta(days=1))

    assert [e.url for e in entries] == [newer.url, undated.url]
    assert not (tmp_path / "x-1.md").exists(), "a dropped post is not rendered into the cache either"


async def test_a_post_the_model_dated_without_an_offset_is_read_as_utc(tmp_path: Path) -> None:
    naive = POST.model_copy(update={"url": "https://x.com/someone/status/4", "date": datetime(2026, 8, 25, 8, 0), "links": []})  # noqa: DTZ001 (a model's answer carrying no offset is the case under test)
    scanner = _scanner(_AgentFactory(XScan(posts=[naive])), cache_dir=tmp_path)

    entries = await scanner.scan(WatchEntry(scanner="x", ref="someone"), since=_NOW - timedelta(days=1))

    assert [e.url for e in entries] == [naive.url]
    assert entries[0].published == datetime(2026, 8, 25, 8, 0, tzinfo=UTC), "read as UTC, so it compares against the stamp"


async def test_a_factory_failure_becomes_a_feed_scan_error(tmp_path: Path) -> None:
    def factory(handles: Sequence[str], since: datetime | None) -> Agent[None, XScan]:
        msg = "no xai api key configured"
        raise RuntimeError(msg)

    scanner = _scanner(factory, cache_dir=tmp_path)

    with pytest.raises(ScanError, match="no xai api key configured"):
        await scanner.scan(WatchEntry(scanner="x", ref="someone"), since=None)


def test_the_scan_agent_answers_as_native_structured_output() -> None:
    agent = x_scan_agent(XaiResource(api_key="test"), handles=["someone"], since=None)

    assert isinstance(agent.output_type, NativeOutput)


def test_building_without_the_xai_settings_block_refuses_naming_it(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="the x scanner requires an xai settings block carrying an api_key"):
        XScanner.build(_resources(tmp_path, key=""))


async def test_a_built_scanner_renders_a_post_under_the_corpus_intake_cache(tmp_path: Path) -> None:
    """The knowledge base root carries no spec file, so a build that read one could not get this far."""
    built = XScanner.build(
        _resources(tmp_path), agent_factory_for=lambda xai: _AgentFactory(XScan(posts=[POST])), resolve_claimants=_declared_claimants
    )

    entries = await built.scan(WatchEntry(scanner="x", ref="someone"), since=None)

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
