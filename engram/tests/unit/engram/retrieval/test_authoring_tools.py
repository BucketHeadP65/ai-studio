"""`AuthoringTools`: `write_note`, `request_source`, `list_undigested`, `read_source`, `knowledge_base_profile`, `recall_beliefs`, and `remember_belief`."""

from __future__ import annotations

import re

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

import pytest

from core.ai.retrieval.contracts import SqlResult
from pydantic import ValidationError
from pydantic_ai import ModelRetry
from pydantic_ai_harness.memory import InMemoryStore

from engram.agent_memory.contracts import EngramMemoryStore
from engram.agent_memory.domain import path_of
from engram.authoring.contracts import AuthoringKnowledgeBase, AuthoringTools, ReindexFinding, collection_of
from engram.authoring.tools import source_type_of
from engram.kb.contracts import ClaimGrammarSpec, FrontmatterFieldSpec, KnowledgeBaseSchema, SourceQueuePolicy, read_policy, write_policy
from engram.sources.contracts import SourceQueue, SourceQueueStatus, SourceType, claimants_for, url_key
from engram.testing.fakes.agent_memory import FakeCards, FakeIngest
from engram.testing.fakes.authoring import FakeAdminGateway, FakeReindex
from engram.testing.fakes.connector import FakeConnector
from engram.testing.fakes.source_queue import FakeQueueLock
from engram.testing.specs import research_spec
from tests.unit.engram.retrieval.test_note_writer import TALK

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from engram.kb.contracts import PageTypeSpec


def _fake_connector_claims(url: str) -> SourceType | None:
    """A made-up URL grammar this file's fakes claim, deliberately unlike any real provider's: a path segment names the page_type.

    `/paper/<id>` is a paper, `/video/<id>` is a video, anything else is unclaimed (falls through
    to the generic article/hash default). Proves `claimants_for`/`classify_url` dispatch to a
    provider's own `claims`, never a real provider's URL scheme, which stays proven only in
    `connectors/tests`.
    """
    segments = urlsplit(url).path.strip("/").split("/")
    if len(segments) >= 2 and segments[0] == "paper":
        return SourceType.PAPER
    if len(segments) >= 2 and segments[0] == "video":
        return SourceType.VIDEO
    return None


def _fake_connector_key_for(url: str) -> str:
    """The made-up grammar's key: `paper-<id>` / `video-<id>` for a URL `_fake_connector_claims` claimed, else the generic hash key."""
    segments = urlsplit(url).path.strip("/").split("/")
    if len(segments) >= 2 and segments[0] in {"paper", "video"}:
        return f"{segments[0]}-{segments[1]}"
    return url_key(url)


def _fake_zotero_like_medium_of(item_type: str) -> SourceType:
    """A made-up two-bucket fold, deliberately unlike the real Zotero adapter's own type set: one marker reads as an article, the rest as a paper."""
    return SourceType.ARTICLE if item_type.strip().lower() == "page_type-article" else SourceType.PAPER


def _fake_catch_all_source_type(item_type: str) -> SourceType:
    """Engram's own four page_type values fold to themselves, `SourceType`'s closed vocabulary, not a provider's."""
    return SourceType(item_type)


_CONNECTORS = {
    "intake": FakeConnector(
        name="intake", claims_from=_fake_connector_claims, key_for_from=_fake_connector_key_for, source_type_from=_fake_catch_all_source_type
    ),
    "zotero": FakeConnector(name="zotero", source_type_from=_fake_zotero_like_medium_of),
    "youtube": FakeConnector(name="youtube", source_type_result=SourceType.VIDEO),
}
"""Three fake providers under the names the bundled research knowledge_base_schema and this file's own fixture
rows declare (`sources.connectors`, `sources_raw.provider`), each scripted with a vocabulary and URL
grammar invented for this file, never a copy of a real provider's own rules: the
real URL recognition and vocabulary folds are proven only in `connectors/tests/unit/{source_queue,youtube,zotero}`
(`TestClaims`, `TestKeyFor`, `TestMediumOf`). Keeps this test group free of a dependency on the
`providers` package, and free of a second copy of logic that could silently drift from the real
adapters it stands in for."""

_QUEUE_CONNECTORS = claimants_for(("intake",), source=_CONNECTORS)
"""The general catch-all connector: every page type these tests need to classify, through the made-up grammar above."""


def _doors(
    tmp_path: Path,
    admin: FakeAdminGateway | None = None,
    queue: SourceQueue | None = None,
    spec: KnowledgeBaseSchema | None = None,
    memory_for: Callable[[AuthoringKnowledgeBase], EngramMemoryStore] | None = None,
    current_run: Callable[[], str | None] | None = None,
) -> tuple[AuthoringTools, FakeReindex, SourceQueue]:
    (tmp_path / "_meta" / "vocabulary").mkdir(parents=True)
    knowledge_base = AuthoringKnowledgeBase(name="research", collection="research", path=tmp_path, schema=spec or research_spec())
    reindex = FakeReindex()
    queue = queue if queue is not None else SourceQueue(tmp_path / "_meta" / "source-queue.yaml", connectors=_QUEUE_CONNECTORS)
    doors = AuthoringTools(
        knowledge_bases={"research": knowledge_base},
        reindex=reindex,
        admin=admin or FakeAdminGateway(),
        queue_for=lambda _t: queue,
        memory_for=memory_for,
        connectors=_CONNECTORS,
        current_run=current_run,
    )
    return doors, reindex, queue


def _talk_mirror() -> FakeAdminGateway:
    """A database that holds the source mirror `TALK` digests."""
    return FakeAdminGateway(rows=[{"source_id": TALK["frontmatter"]["digest_of"], "provider": "intake", "title": "Agents that plan"}])


def _memory_store(rows: list[tuple[str, str, str]] | None = None) -> EngramMemoryStore:
    return EngramMemoryStore(InMemoryStore(), FakeIngest(), FakeCards(rows or []))


async def test_write_note_writes_then_reindexs_the_target_once(tmp_path: Path) -> None:
    doors, reindex, _ = _doors(tmp_path, _talk_mirror())
    said = await doors.write_note("research", TALK)
    assert said == "talk.agents-that-plan written to Wiki/Talks/agents-that-plan.md, indexed clean"
    assert [knowledge_base.name for knowledge_base in reindex.knowledge_bases] == ["research"]


async def test_write_note_reports_the_landed_write_separately_from_validation_errors_elsewhere(tmp_path: Path) -> None:
    """A page that landed, on a knowledge base whose OTHER dirty pages fail validation, is never reported as a failed write."""
    doors, reindex, _ = _doors(tmp_path, _talk_mirror())
    reindex.outcome = "validation_errors"
    reindex.findings = (
        ReindexFinding(page_id="paper.one", rule="missing-quote", message="claim quote absent from its mirror"),
        ReindexFinding(page_id="paper.two", rule="missing-quote", message="claim quote absent from its mirror"),
    )
    reindex.findings_total = 3

    said = await doors.write_note("research", TALK)

    assert said == (
        "talk.agents-that-plan written to Wiki/Talks/agents-that-plan.md, indexed with 3 validation errors on other pages, "
        "paper.one (missing-quote), paper.two (missing-quote) and 1 more"
    )


async def test_write_note_reports_a_failed_compile_as_its_own_outcome(tmp_path: Path) -> None:
    doors, reindex, _ = _doors(tmp_path, _talk_mirror())
    reindex.outcome = "not_indexed"
    reindex.detail = "studio index for research failed with exit 2"

    said = await doors.write_note("research", TALK)

    assert said == (
        "talk.agents-that-plan written to Wiki/Talks/agents-that-plan.md, "
        "the knowledge base did not index (studio index for research failed with exit 2)"
    )


async def test_write_note_says_a_deferred_index_waits_for_the_run_to_end(tmp_path: Path) -> None:
    doors, reindex, _ = _doors(tmp_path, _talk_mirror())
    reindex.outcome = "deferred"

    said = await doors.write_note("research", TALK)

    assert said == "talk.agents-that-plan written to Wiki/Talks/agents-that-plan.md, indexed once this run ends, so search does not find it yet"


async def test_write_note_stamps_created_when_the_author_leaves_it_out(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path, _talk_mirror())

    await doors.write_note("research", TALK)

    written = (tmp_path / "Wiki" / "Talks" / "agents-that-plan.md").read_text(encoding="utf-8")
    assert re.search(rf"^created: '?{datetime.now(tz=UTC).date().isoformat()}'?$", written, flags=re.MULTILINE)


async def test_write_note_surfaces_lint_warnings_in_its_reply(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path, _talk_mirror())
    dangling_link = {**TALK, "frontmatter": {**TALK["frontmatter"], "mentions": ["[[ent.something]]"]}}
    said = await doors.write_note("research", dangling_link)
    assert "warnings:" in said
    assert "cross-note link" in said


async def test_a_refused_note_is_a_model_retry_with_the_findings(tmp_path: Path) -> None:
    doors, reindex, _ = _doors(tmp_path, _talk_mirror())
    with pytest.raises(ModelRetry, match=r"talk\."):
        await doors.write_note("research", {**TALK, "frontmatter": {**TALK["frontmatter"], "id": "nope"}})
    assert reindex.knowledge_bases == []


async def test_write_note_refuses_a_digest_whose_source_link_names_no_mirror(tmp_path: Path) -> None:
    doors, reindex, _ = _doors(tmp_path, FakeAdminGateway(rows=[]))
    by_url = {**TALK, "frontmatter": {**TALK["frontmatter"], "digest_of": "https://youtu.be/abc123XYZ_-"}}

    with pytest.raises(ModelRetry, match=r"digest_of 'https://youtu.be/abc123XYZ_-' names no source mirror in 'research'"):
        await doors.write_note("research", by_url)

    assert not (tmp_path / "Wiki" / "Talks" / "agents-that-plan.md").exists()
    assert reindex.knowledge_bases == []


async def test_check_note_answers_a_source_link_that_names_no_mirror(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path, FakeAdminGateway(rows=[]))
    by_url = {**TALK, "frontmatter": {**TALK["frontmatter"], "digest_of": "https://youtu.be/abc123XYZ_-"}}

    said = await doors.check_note("research", by_url)

    assert said.startswith("the page would be refused: digest_of 'https://youtu.be/abc123XYZ_-' names no source mirror")


async def test_check_note_says_the_page_passes_and_where_it_would_land(tmp_path: Path) -> None:
    doors, reindex, _ = _doors(tmp_path, _talk_mirror())
    said = await doors.check_note("research", TALK)
    assert said == "talk.agents-that-plan passes research's grammar, would land at Wiki/Talks/agents-that-plan.md"
    assert not (tmp_path / "Wiki" / "Talks" / "agents-that-plan.md").exists()
    assert reindex.knowledge_bases == []


async def test_check_note_answers_the_findings_instead_of_raising(tmp_path: Path) -> None:
    doors, reindex, _ = _doors(tmp_path, _talk_mirror())
    said = await doors.check_note("research", {**TALK, "frontmatter": {**TALK["frontmatter"], "id": "nope"}})
    assert "would be refused" in said
    assert "talk." in said
    assert reindex.knowledge_bases == []


async def test_check_note_surfaces_lint_warnings_on_a_passing_page(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path, _talk_mirror())
    dangling_link = {**TALK, "frontmatter": {**TALK["frontmatter"], "mentions": ["[[ent.something]]"]}}
    said = await doors.check_note("research", dangling_link)
    assert "passes" in said
    assert "warnings:" in said
    assert "cross-note link" in said


async def test_check_note_of_an_unknown_knowledge_base_names_the_known_ones(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path)
    with pytest.raises(ModelRetry, match="research"):
        await doors.check_note("elsewhere", TALK)


async def test_check_note_rides_in_the_tools_bundle(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path)
    names = [name for _fn, name, _description in doors.tools()]
    assert "check_note" in names
    assert names.index("check_note") == names.index("write_note") + 1


async def test_an_unknown_knowledge_base_names_the_known_ones(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path)
    with pytest.raises(ModelRetry, match="research"):
        await doors.write_note("elsewhere", TALK)


async def test_request_source_lands_a_row_with_its_reason_and_says_so(tmp_path: Path) -> None:
    doors, _, queue = _doors(tmp_path)
    url = "https://source.test/paper/2401.01234"

    said = await doors.request_source("research", url, found_via="talk.agents-that-plan", reason="it extends the method")

    assert said == f"requested {url}, it extends the method"
    row = queue.get("paper-2401.01234")
    assert row is not None
    assert row.found_via == "talk.agents-that-plan"
    assert row.reason == "it extends the method"
    assert (
        await doors.request_source("research", url, found_via="talk.agents-that-plan", reason="again") == f"already requested {url}"
    )


async def test_request_source_of_a_removed_url_answers_with_its_reason(tmp_path: Path) -> None:
    doors, _, queue = _doors(tmp_path)
    queue.remove("https://arxiv.org/abs/2401.01234", reason="removed 2026-08-31: off-domain", found_at=datetime.now(tz=UTC))

    said = await doors.request_source("research", "https://arxiv.org/abs/2401.01234", found_via="talk.agents-that-plan", reason="again")

    assert said == "removed https://arxiv.org/abs/2401.01234: removed 2026-08-31: off-domain"


async def test_request_source_answers_with_the_mirror_that_already_holds_the_paper(tmp_path: Path) -> None:
    """A paper Zotero already mirrors is not fetched a second time under an arXiv id: the door names the mirror instead."""
    admin = FakeAdminGateway(rows=[{"source_id": "MJ389JS8", "provider": "zotero"}])
    doors, _, queue = _doors(tmp_path, admin)

    said = await doors.request_source("research", "https://arxiv.org/abs/2505.10978", found_via="post.verl-agent")

    assert said == "no_url already on file as MJ389JS8 (zotero) in research, read_source it"
    assert queue.entries() == []
    (sql, params) = admin.calls[0]
    assert "meta->>'arxiv_id'" in sql
    assert params == {"collection": "research", "url": "https://arxiv.org/abs/2505.10978", "arxiv_id": "2505.10978", "doi": ""}


async def test_request_source_stops_a_source_at_the_policy_cap(tmp_path: Path) -> None:
    doors, _, queue = _doors(tmp_path)
    cap = SourceQueuePolicy().per_source_cap
    for n in range(cap):
        await doors.request_source("research", f"https://arxiv.org/abs/2401.0000{n}", found_via="paper.graph-cost", reason="cited")

    said = await doors.request_source("research", "https://arxiv.org/abs/2401.00099", found_via="paper.graph-cost", reason="cited")

    assert said == f"seen https://arxiv.org/abs/2401.00099, paper.graph-cost already has {cap} references on research's queue, past the cap of {cap}"
    assert len(queue.entries()) == cap
    unattributed = await doors.request_source("research", "https://arxiv.org/abs/2401.00099")
    assert unattributed.startswith("requested "), "a request with no found_via is the owner's and is not bounded"


async def test_a_reference_needs_a_reason(tmp_path: Path) -> None:
    doors, _, queue = _doors(tmp_path)

    with pytest.raises(ModelRetry, match="needs a reason"):
        await doors.request_source("research", "https://arxiv.org/abs/2401.01234", found_via="paper.x")

    assert queue.entries() == []


async def test_a_reference_of_a_reference_is_seen_not_fetched(tmp_path: Path) -> None:
    """A hop is between two queue rows: `first` (found via a root row) is one hop, so `second` (found via `first`) is two."""
    doors, _, queue = _doors(tmp_path)
    root = await doors.request_source("research", "https://source.test/paper/2401.00000")
    assert root == "requested https://source.test/paper/2401.00000, queued for research's next sync"
    first = await doors.request_source("research", "https://source.test/paper/2401.00001", found_via="paper-2401.00000", reason="builds on it")
    assert first == "requested https://source.test/paper/2401.00001, builds on it"

    said = await doors.request_source("research", "https://source.test/paper/2401.00002", found_via="paper-2401.00001", reason="compares against it")

    assert said == (
        "seen https://source.test/paper/2401.00002, 2 hops from a source the owner added and research's policy fetches 1, "
        "ask for it with source_queue_decide"
    )
    row = queue.get("paper-2401.00002")
    assert row is not None
    assert row.status is SourceQueueStatus.SEEN
    assert row.reason == "beyond hop depth 1 (found via paper-2401.00001 via paper-2401.00000)"


async def test_the_policy_page_sets_the_cap_and_the_depth(tmp_path: Path) -> None:
    write_policy(tmp_path, SourceQueuePolicy(hop_depth=2, per_source_cap=1), "")
    doors, _, queue = _doors(tmp_path)
    await doors.request_source("research", "https://arxiv.org/abs/2401.00001", found_via="MJ389JS8", reason="builds on it")

    capped = await doors.request_source("research", "https://arxiv.org/abs/2401.00003", found_via="MJ389JS8", reason="also")
    deeper = await doors.request_source("research", "https://arxiv.org/abs/2401.00002", found_via="arxiv-2401.00001", reason="compares against it")

    assert capped == "seen https://arxiv.org/abs/2401.00003, MJ389JS8 already has 1 reference on research's queue, past the cap of 1"
    assert deeper == "requested https://arxiv.org/abs/2401.00002, compares against it"
    assert len(queue.entries()) == 2


async def test_per_source_cap_counts_seen_rows_too_and_stops_growth(tmp_path: Path) -> None:
    """The cap is a total across every status: a source beyond hop depth cannot grow the queue past it either."""
    write_policy(tmp_path, SourceQueuePolicy(hop_depth=1, per_source_cap=5), "")
    doors, _, queue = _doors(tmp_path)
    await doors.request_source("research", "https://source.test/paper/2401.00000")
    await doors.request_source("research", "https://source.test/paper/2401.00001", found_via="paper-2401.00000", reason="builds on it")

    for n in range(9):
        await doors.request_source("research", f"https://source.test/paper/2401.0100{n}", found_via="paper-2401.00001", reason="cited")

    rows = [row for row in queue.entries() if row.found_via == "paper-2401.00001"]
    assert len(rows) == 5, "the cap holds even though every one of these references is beyond hop depth"
    assert all(row.status is SourceQueueStatus.SEEN for row in rows)


class _Runs:
    """Which run the calling step works for, as a host answers it, switched by the test between calls."""

    def __init__(self, run: str | None) -> None:
        self.run = run

    def __call__(self) -> str | None:
        return self.run


async def test_request_source_stops_a_pass_at_its_per_pass_cap(tmp_path: Path) -> None:
    write_policy(tmp_path, SourceQueuePolicy(per_pass_cap=2), "")
    doors, _, queue = _doors(tmp_path, current_run=_Runs("run-1"))
    await doors.request_source("research", "https://source.test/paper/2401.00010", found_via="paper.a", reason="cited")
    await doors.request_source("research", "https://source.test/paper/2401.00011", found_via="paper.b", reason="cited")

    said = await doors.request_source("research", "https://source.test/paper/2401.00012", found_via="paper.c", reason="cited")

    assert said == (
        "seen https://source.test/paper/2401.00012, run run-1 already queued 2 references on research's queue, "
        "past the ceiling of 2, ask for it with source_queue_decide"
    )
    row = queue.get("paper-2401.00012")
    assert row is not None
    assert row.status is SourceQueueStatus.SEEN
    assert row.reason == "run run-1 already queued 2 references on research's queue, past the ceiling of 2"


async def test_request_source_under_the_pass_cap_is_unaffected(tmp_path: Path) -> None:
    write_policy(tmp_path, SourceQueuePolicy(per_pass_cap=5), "")
    doors, _, queue = _doors(tmp_path, current_run=_Runs("run-1"))

    said = await doors.request_source("research", "https://arxiv.org/abs/2401.00020", found_via="paper.a", reason="cited")

    assert said == "requested https://arxiv.org/abs/2401.00020, cited"
    assert len(queue.entries()) == 1


async def test_pass_cap_counts_only_what_this_pass_added(tmp_path: Path) -> None:
    """Rows an earlier pass added, however many, never count against the current pass's own ceiling."""
    write_policy(tmp_path, SourceQueuePolicy(per_pass_cap=1), "")
    doors, _, queue = _doors(tmp_path, current_run=_Runs("run-1"))
    stale = datetime(2000, 1, 1, tzinfo=UTC)
    for n in range(3):
        queue.add_bounded(
            f"https://arxiv.org/abs/2401.0090{n}", found_via=f"paper.old{n}", cap=999, reason="an earlier pass", found_at=stale
        )

    said = await doors.request_source("research", "https://arxiv.org/abs/2401.00030", found_via="paper.new", reason="cited")

    assert said == "requested https://arxiv.org/abs/2401.00030, cited"


async def test_references_queued_a_minute_ago_by_another_pass_do_not_count(tmp_path: Path) -> None:
    """The ceiling counts the run's own additions, never whatever else reached the queue in the same hour."""
    write_policy(tmp_path, SourceQueuePolicy(per_pass_cap=1), "")
    doors, _, queue = _doors(tmp_path, current_run=_Runs("run-2"))
    moments_ago = datetime.now(tz=UTC)
    for n in range(3):
        queue.add_bounded(
            f"https://arxiv.org/abs/2401.0080{n}", found_via=f"paper.other{n}", cap=999, reason="another pass", found_at=moments_ago
        )

    said = await doors.request_source("research", "https://arxiv.org/abs/2401.00031", found_via="paper.new", reason="cited")

    assert said == "requested https://arxiv.org/abs/2401.00031, cited"


async def test_two_runs_back_to_back_each_get_their_own_ceiling(tmp_path: Path) -> None:
    """A run that starts minutes after another may queue its full ceiling, and the first run stays held at its own."""
    write_policy(tmp_path, SourceQueuePolicy(per_pass_cap=1), "")
    runs = _Runs("run-a")
    doors, _, _ = _doors(tmp_path, current_run=runs)
    first = await doors.request_source("research", "https://arxiv.org/abs/2401.00040", found_via="paper.a", reason="cited")

    runs.run = "run-b"
    second = await doors.request_source("research", "https://arxiv.org/abs/2401.00041", found_via="paper.b", reason="cited")
    runs.run = "run-a"
    held = await doors.request_source("research", "https://arxiv.org/abs/2401.00042", found_via="paper.c", reason="cited")

    assert first == "requested https://arxiv.org/abs/2401.00040, cited"
    assert second == "requested https://arxiv.org/abs/2401.00041, cited"
    assert held.startswith("seen https://arxiv.org/abs/2401.00042, run run-a already queued 1 reference on research's queue"), held


async def test_a_reference_requested_outside_any_run_meets_no_pass_ceiling(tmp_path: Path) -> None:
    """A pass is one run of a workflow, so a call no run is making has no pass to count against."""
    write_policy(tmp_path, SourceQueuePolicy(per_pass_cap=1), "")
    doors, _, queue = _doors(tmp_path, current_run=_Runs(None))

    for n in range(3):
        said = await doors.request_source("research", f"https://arxiv.org/abs/2401.0005{n}", found_via=f"paper.x{n}", reason="cited")
        assert said.startswith("requested "), said

    assert len(queue.entries()) == 3


async def test_with_no_per_pass_cap_a_pass_is_unbounded(tmp_path: Path) -> None:
    assert SourceQueuePolicy().per_pass_cap is None
    doors, _, queue = _doors(tmp_path, current_run=_Runs("run-1"))

    for n in range(20):
        said = await doors.request_source(
            "research", f"https://arxiv.org/abs/2401.030{n:02d}", found_via=f"paper.many{n}", reason="cited"
        )
        assert said.startswith("requested "), said

    assert len(queue.entries()) == 20


async def test_request_source_drops_a_reference_whose_class_is_on_the_never_list(tmp_path: Path) -> None:
    doors, _, queue = _doors(tmp_path)

    said = await doors.request_source(
        "research", "https://github.com/Aider-AI/polyglot-benchmark", found_via="paper.x", reason="the benchmark it evaluates on"
    )

    assert said == "dropped https://github.com/Aider-AI/polyglot-benchmark, class repository is on the never list"
    assert queue.entries() == []


async def test_request_source_drops_a_reference_whose_source_type_is_not_declared(tmp_path: Path) -> None:
    write_policy(tmp_path, SourceQueuePolicy(source_types=("paper",)), "")
    # A page_type-based refusal needs a URL-classifying provider declared; the bundled research spec's
    # own `("zotero",)` never claims a URL, so this knowledge base also declares "intake" for the check.
    base = research_spec()
    spec = base.model_copy(update={"sources": base.sources.model_copy(update={"connectors": ("zotero", "intake")})})
    doors, _, queue = _doors(tmp_path, spec=spec)

    said = await doors.request_source("research", "https://source.test/video/abc123", found_via="paper.x", reason="a talk it compares against")

    assert said == "dropped https://source.test/video/abc123, source type video is not in source_types"
    assert queue.entries() == []


async def test_write_policy_repairs_an_unparseable_page(tmp_path: Path) -> None:
    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "ingestion-policy.md").write_text(
        "---\nid: meta.ingestion-policy\ntype: meta\ntitle: Ingestion policy\nnever: [profile\n---\n\n# Ingestion policy\n\nx\n",
        encoding="utf-8",
    )
    doors, _, _ = _doors(tmp_path)

    said = await doors.write_policy("research", {"hop_depth": 2}, "Take papers only.")

    assert said == "research's ingestion policy page could not be read, replaced wholesale: hop_depth now 2 (was 1), guidance replaced"
    page = read_policy(tmp_path, research_spec())
    assert page.parse_error is None
    assert page.rules.hop_depth == 2
    assert page.guidance == "Take papers only."


async def test_request_source_refuses_a_reference_on_an_unparseable_page(tmp_path: Path) -> None:
    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "ingestion-policy.md").write_text(
        "---\nid: meta.ingestion-policy\ntype: meta\ntitle: Ingestion policy\nnever: [profile\n---\n\n# Ingestion policy\n\nx\n",
        encoding="utf-8",
    )
    doors, _, queue = _doors(tmp_path)

    said = await doors.request_source("research", "https://arxiv.org/abs/2401.01234", found_via="paper.x", reason="builds on it")

    assert said == "dropped https://arxiv.org/abs/2401.01234, research's ingestion policy page could not be read, fix it with write_policy"
    assert queue.entries() == []


async def test_request_source_decides_new_or_known_inside_the_one_hold_it_takes(tmp_path: Path) -> None:
    """A URL another process landed first is reported as known, because the read is inside the write's hold."""
    path = tmp_path / "_meta" / "source-queue.yaml"
    other = SourceQueue(path, connectors=_QUEUE_CONNECTORS)

    def land_it_first() -> None:
        other.add("https://arxiv.org/abs/2401.01234")

    lock = FakeQueueLock(before_first=land_it_first)
    doors, _, queue = _doors(tmp_path, queue=SourceQueue(path, connectors=_QUEUE_CONNECTORS, lock=lock))

    said = await doors.request_source("research", "https://arxiv.org/abs/2401.01234")

    assert said == "already requested https://arxiv.org/abs/2401.01234"
    assert lock.holds == [True], "one hold, and no read outside it"
    assert len(queue.entries()) == 1


async def test_list_undigested_reads_the_admin_gateway_scoped_to_the_collection(tmp_path: Path) -> None:
    admin = FakeAdminGateway(rows=[{"source_id": "yt-abc", "provider": "intake", "item_type": "video", "title": "a talk"}])
    doors, _, _ = _doors(tmp_path, admin)
    rows = await doors.list_undigested("research", limit=10)
    assert rows == [{"source_id": "yt-abc", "provider": "intake", "item_type": "video", "title": "a talk"}]
    sql, params = admin.calls[0]
    assert "kb.sources_raw" in sql
    assert "digest_of" in sql
    assert "zotero_key" in sql
    assert params == {"collection": "research", "limit": 10}


async def test_list_undigested_joins_by_the_spec_own_field_names_not_a_literal(tmp_path: Path) -> None:
    """A bundle naming its mirror fields differently changes the join, proving it is spec-driven, not hardcoded."""
    base = research_spec()
    renamed = base.model_copy(
        update={"sources": base.sources.model_copy(update={"key_field": "source_key", "digest_link_field": "source_of"})}
    )
    admin = FakeAdminGateway(rows=[])
    doors, _, _ = _doors(tmp_path, admin, spec=renamed)

    await doors.list_undigested("research")

    sql, _params = admin.calls[0]
    assert "source_key" in sql
    assert "source_of" in sql
    assert "zotero_key" not in sql
    assert "digest_of" not in sql


class _NewestFirstMirrors(FakeAdminGateway):
    """Answers the undigested lookup as the database does: the picked rows when `source_ids` is bound, else the newest `limit`, then at most `cap`."""

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        bound = params or {}
        self.calls.append((sql, bound))
        picked = bound.get("source_ids")
        limit = bound.get("limit")
        if isinstance(picked, list):
            answered = [row for row in self.rows_out if row["source_id"] in picked]
        else:
            assert isinstance(limit, int)
            answered = self.rows_out[:limit]
        return SqlResult(rows=tuple(answered[:cap]))


async def test_list_undigested_with_source_ids_answers_every_picked_mirror_past_the_newest_500_and_ignores_limit(tmp_path: Path) -> None:
    mirrors: list[dict[str, object]] = [
        {"source_id": f"src-{number:03d}", "provider": "intake", "item_type": "paper", "title": f"mirror {number}"} for number in range(501)
    ]
    admin = _NewestFirstMirrors(mirrors)
    doors, _, _ = _doors(tmp_path, admin)
    picked = ["src-500", *(f"src-{number:03d}" for number in range(60))]

    rows = await doors.list_undigested("research", source_ids=picked)

    assert {row["source_id"] for row in rows} == set(picked)
    sql, params = admin.calls[0]
    assert "sr.source_id = ANY(:source_ids)" in sql
    assert "LIMIT" not in sql
    assert params == {"collection": "research", "source_ids": picked}


async def test_list_undigested_with_no_source_ids_answers_nothing_and_reads_nothing(tmp_path: Path) -> None:
    admin = FakeAdminGateway(rows=[{"source_id": "yt-abc", "provider": "intake", "item_type": "video", "title": "a talk"}])
    doors, _, _ = _doors(tmp_path, admin)

    assert await doors.list_undigested("research", source_ids=[]) == []
    assert admin.calls == []


_TRACE_FLAG_PAGE = """---
id: flag.check-source-z1
type: flag
title: Check source z-1
summary: Something worth flagging.
status: pending
flag_kind: idea
about: ["[[paper.a-paper]]"]
---

## Why

Mentions the mirror z-1 explicitly.

## Proposal

Review z-1.

## Evidence

z-1
"""

_TRACE_FLAG_PAGE_COLLISION = """---
id: flag.check-source-z10
type: flag
title: Check source z-10
summary: A different mirror worth a look.
status: pending
flag_kind: idea
about: ["[[paper.another-paper]]"]
---

## Why

Mentions the mirror z-10 explicitly.

## Proposal

Review z-10.

## Evidence

z-10
"""


async def test_trace_source_resolves_by_id_url_or_title_and_reports_the_closure(tmp_path: Path) -> None:
    admin = FakeAdminGateway(
        rows_queue=[
            [{"source_id": "z-1", "provider": "zotero", "provider_key": "ZKEY1", "title": "A paper", "url": "https://example.com/paper"}],
            [
                {"id": "paper.a-paper", "page_type": "paper", "title": "A paper", "path": "Wiki/Papers/a-paper.md"},
                {"id": "paper.a-paper-part2", "page_type": "paper", "title": "A paper (part 2)", "path": "Wiki/Papers/a-paper-part2.md"},
            ],
            [{"page_id": "note.summary", "title": "Summary note", "path": "Wiki/Notes/summary.md", "rel": "mentions"}],
            [{"page_id": "note.summary", "title": "Summary note", "path": "Wiki/Notes/summary.md", "rel": "mentions"}],
            [],
            [{"path": "Wiki/Mirrors/z-1.md"}],
        ]
    )
    queue = SourceQueue(tmp_path / "_meta" / "source-queue.yaml", connectors=_QUEUE_CONNECTORS)
    queue.add("https://example.com/paper")
    doors, _, _ = _doors(tmp_path, admin, queue=queue)
    cache_dir = tmp_path / "_meta" / "source-queue-cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "z-1.md").write_text("cached mirror text\n", encoding="utf-8")
    (cache_dir / "z-1.meta.json").write_text("{}\n", encoding="utf-8")
    flags_folder = tmp_path / "Wiki" / "Flags"
    flags_folder.mkdir(parents=True)
    (flags_folder / "check-source-z1.md").write_text(_TRACE_FLAG_PAGE, encoding="utf-8")
    (flags_folder / "check-source-z10.md").write_text(_TRACE_FLAG_PAGE_COLLISION, encoding="utf-8")

    closure = await doors.trace_source("research", "z-1")

    assert closure == {
        "source": {"source_id": "z-1", "title": "A paper", "provider": "zotero", "url": "https://example.com/paper"},
        "only_x": {
            "digest_pages": [
                {"id": "paper.a-paper", "page_type": "paper", "title": "A paper", "path": "Wiki/Papers/a-paper.md"},
                {"id": "paper.a-paper-part2", "page_type": "paper", "title": "A paper (part 2)", "path": "Wiki/Papers/a-paper-part2.md"},
            ],
            "mirror_path": "Wiki/Mirrors/z-1.md",
            "cache_paths": ["_meta/source-queue-cache/z-1.md", "_meta/source-queue-cache/z-1.meta.json"],
            "decision_pages": ["Wiki/Flags/check-source-z1.md"],
            "source_queue_status": "requested",
        },
        "candidates": [
            {
                "page_id": "note.summary",
                "title": "Summary note",
                "path": "Wiki/Notes/summary.md",
                "via": ["links paper.a-paper", "links paper.a-paper-part2"],
            }
        ],
        "expected_paths": [
            "Wiki/Flags/check-source-z1.md",
            "Wiki/Mirrors/z-1.md",
            "Wiki/Papers/a-paper-part2.md",
            "Wiki/Papers/a-paper.md",
            "_meta/source-queue-cache/z-1.md",
            "_meta/source-queue-cache/z-1.meta.json",
        ],
    }
    resolve_sql, resolve_params = admin.calls[0]
    assert "kb.sources_raw" in resolve_sql
    assert resolve_params == {"collection": "research", "q": "z-1"}
    digests_sql, digests_params = admin.calls[1]
    assert "kb.index_manifest" in digests_sql
    assert digests_params == {"collection": "research", "source_id": "z-1"}
    inbound_sql, inbound_params = admin.calls[2]
    assert "kb.link_refs" in inbound_sql
    assert inbound_params == {"collection": "research", "page_id": "paper.a-paper"}
    mirror_sql, mirror_params = admin.calls[-1]
    assert "kb.index_manifest" in mirror_sql
    assert "source_key" in mirror_sql
    assert mirror_params == {"collection": "research", "provider_key": "ZKEY1"}


async def test_trace_source_unknown_source_is_a_model_retry(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path, FakeAdminGateway(rows=[]))
    with pytest.raises(ModelRetry, match="no source"):
        await doors.trace_source("research", "nope")


_REMOVAL_FLAG_PAGE = """---
id: flag.check-source-src-1
type: flag
title: Check source src-1
summary: Something worth flagging.
status: pending
flag_kind: idea
about: ["[[paper.src-one]]"]
---

## Why

Mentions the mirror src-1 explicitly.

## Proposal

Review src-1.

## Evidence

src-1
"""


def _removal_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    """Real files under `tmp_path` matching one source's removal closure: mirror, digest, two cache files, one flag page."""
    mirror = tmp_path / "Wiki" / "Mirrors" / "src-1.md"
    mirror.parent.mkdir(parents=True)
    mirror.write_text("mirror text\n", encoding="utf-8")
    digest = tmp_path / "Wiki" / "Papers" / "src-one.md"
    digest.parent.mkdir(parents=True)
    digest.write_text("digest text\n", encoding="utf-8")
    cache_dir = tmp_path / "_meta" / "source-queue-cache"
    cache_dir.mkdir(parents=True)
    cache_md = cache_dir / "src-1.md"
    cache_md.write_text("cached\n", encoding="utf-8")
    cache_meta = cache_dir / "src-1.meta.json"
    cache_meta.write_text("{}\n", encoding="utf-8")
    flags_folder = tmp_path / "Wiki" / "Flags"
    flags_folder.mkdir(parents=True)
    flag_page = flags_folder / "check-source-src-1.md"
    flag_page.write_text(_REMOVAL_FLAG_PAGE, encoding="utf-8")
    return mirror, digest, cache_md, cache_meta, flag_page


def _removal_admin() -> FakeAdminGateway:
    """A gateway scripted the way `trace_source` would answer for `src-1`: one digest page, no inbound citers, one mirror path."""
    return FakeAdminGateway(
        rows_queue=[
            [{"source_id": "src-1", "provider": "zotero", "provider_key": "ZKEY-SRC1", "title": "Src One", "url": "https://example.com/src-1"}],
            [{"id": "paper.src-one", "page_type": "paper", "title": "Src One", "path": "Wiki/Papers/src-one.md"}],
            [],
            [{"path": "Wiki/Mirrors/src-1.md"}],
        ]
    )


async def test_remove_source_artifacts_deletes_the_set_blocks_the_queue_and_reindexes(tmp_path: Path) -> None:
    mirror, digest, cache_md, cache_meta, flag_page = _removal_fixture(tmp_path)
    queue = SourceQueue(tmp_path / "_meta" / "source-queue.yaml", connectors=_QUEUE_CONNECTORS)
    queue.add("https://example.com/src-1")
    doors, reindex, _ = _doors(tmp_path, _removal_admin(), queue=queue)
    expected_paths = [
        "Wiki/Flags/check-source-src-1.md",
        "Wiki/Mirrors/src-1.md",
        "Wiki/Papers/src-one.md",
        "_meta/source-queue-cache/src-1.md",
        "_meta/source-queue-cache/src-1.meta.json",
    ]

    said = await doors.remove_source_artifacts("research", "src-1", expected_paths, reason="superseded")

    assert said == "removed src-1 from research: 5 file(s) gone, queue blocked, indexed clean"
    for path in (mirror, digest, cache_md, cache_meta, flag_page):
        assert not path.exists()
    rows = queue.entries()
    assert len(rows) == 1
    assert rows[0].status is SourceQueueStatus.REMOVED
    assert rows[0].reason is not None
    assert "superseded" in rows[0].reason
    assert [knowledge_base.name for knowledge_base in reindex.knowledge_bases] == ["research"]


async def test_remove_source_artifacts_reports_a_final_reindex_failure_after_removing_everything(tmp_path: Path) -> None:
    """The files are gone and the queue is blocked before the reindex ever runs; the ModelRetry must say so."""
    mirror, digest, cache_md, cache_meta, flag_page = _removal_fixture(tmp_path)
    queue = SourceQueue(tmp_path / "_meta" / "source-queue.yaml", connectors=_QUEUE_CONNECTORS)
    queue.add("https://example.com/src-1")
    doors, reindex, _ = _doors(tmp_path, _removal_admin(), queue=queue)
    reindex.outcome = "not_indexed"
    reindex.detail = "schema is behind head"
    expected_paths = [
        "Wiki/Flags/check-source-src-1.md",
        "Wiki/Mirrors/src-1.md",
        "Wiki/Papers/src-one.md",
        "_meta/source-queue-cache/src-1.md",
        "_meta/source-queue-cache/src-1.meta.json",
    ]

    with pytest.raises(ModelRetry) as excinfo:
        await doors.remove_source_artifacts("research", "src-1", expected_paths, reason="superseded")

    message = str(excinfo.value)
    assert "5 file(s) gone" in message
    assert "queue blocked" in message
    assert "schema is behind head" in message
    for path in (mirror, digest, cache_md, cache_meta, flag_page):
        assert not path.exists(), f"{path} was already removed before the reindex failed"
    rows = queue.entries()
    assert len(rows) == 1
    assert rows[0].status is SourceQueueStatus.REMOVED, "the queue block already happened before the reindex failed"


async def test_remove_source_artifacts_answers_a_deferred_index_without_a_retry(tmp_path: Path) -> None:
    _removal_fixture(tmp_path)
    queue = SourceQueue(tmp_path / "_meta" / "source-queue.yaml", connectors=_QUEUE_CONNECTORS)
    queue.add("https://example.com/src-1")
    doors, reindex, _ = _doors(tmp_path, _removal_admin(), queue=queue)
    reindex.outcome = "deferred"
    expected_paths = [
        "Wiki/Flags/check-source-src-1.md",
        "Wiki/Mirrors/src-1.md",
        "Wiki/Papers/src-one.md",
        "_meta/source-queue-cache/src-1.md",
        "_meta/source-queue-cache/src-1.meta.json",
    ]

    said = await doors.remove_source_artifacts("research", "src-1", expected_paths, reason="superseded")

    assert said == "removed src-1 from research: 5 file(s) gone, queue blocked, indexed once this run ends, so search does not find it yet"


async def test_remove_source_artifacts_refuses_on_drift(tmp_path: Path) -> None:
    mirror, digest, cache_md, cache_meta, flag_page = _removal_fixture(tmp_path)
    queue = SourceQueue(tmp_path / "_meta" / "source-queue.yaml", connectors=_QUEUE_CONNECTORS)
    queue.add("https://example.com/src-1")
    doors, reindex, _ = _doors(tmp_path, _removal_admin(), queue=queue)
    stale_paths = [
        "Wiki/Mirrors/src-1.md",
        "Wiki/Papers/src-one.md",
        "_meta/source-queue-cache/src-1.md",
        "_meta/source-queue-cache/src-1.meta.json",
    ]  # missing the flag page a later edit would have added

    with pytest.raises(ModelRetry, match="drifted"):
        await doors.remove_source_artifacts("research", "src-1", stale_paths)

    for path in (mirror, digest, cache_md, cache_meta, flag_page):
        assert path.exists()
    assert [row.status for row in queue.entries()] == [SourceQueueStatus.REQUESTED]
    assert reindex.knowledge_bases == []


async def test_remove_source_artifacts_refuses_a_path_outside_the_vault(tmp_path: Path) -> None:
    admin = FakeAdminGateway(
        rows_queue=[
            [{"source_id": "src-1", "provider": "zotero", "provider_key": "ZKEY-SRC1", "title": "Src One", "url": "https://example.com/src-1"}],
            [],
            [{"path": "../outside.md"}],
        ]
    )
    queue = SourceQueue(tmp_path / "_meta" / "source-queue.yaml", connectors=_QUEUE_CONNECTORS)
    queue.add("https://example.com/src-1")
    doors, reindex, _ = _doors(tmp_path, admin, queue=queue)

    with pytest.raises(ModelRetry):
        await doors.remove_source_artifacts("research", "src-1", ["../outside.md"])

    assert [row.status for row in queue.entries()] == [SourceQueueStatus.REQUESTED]
    assert reindex.knowledge_bases == []


async def test_remove_source_artifacts_survives_a_mid_loop_unlink_failure(tmp_path: Path) -> None:
    mirror, digest, cache_md, cache_meta, flag_page = _removal_fixture(tmp_path)
    queue = SourceQueue(tmp_path / "_meta" / "source-queue.yaml", connectors=_QUEUE_CONNECTORS)
    queue.add("https://example.com/src-1")
    doors, reindex, _ = _doors(tmp_path, _removal_admin(), queue=queue)
    expected_paths = [
        "Wiki/Flags/check-source-src-1.md",
        "Wiki/Mirrors/src-1.md",
        "Wiki/Papers/src-one.md",
        "_meta/source-queue-cache/src-1.md",
        "_meta/source-queue-cache/src-1.meta.json",
    ]
    protected_dir = digest.parent  # Wiki/Papers only holds src-one.md, so this alone stays undeletable
    protected_dir.chmod(0o555)
    try:
        with pytest.raises(ModelRetry) as excinfo:
            await doors.remove_source_artifacts("research", "src-1", expected_paths)
    finally:
        protected_dir.chmod(0o755)

    message = str(excinfo.value)
    assert "Wiki/Papers/src-one.md" in message
    assert "Wiki/Flags/check-source-src-1.md" in message
    assert digest.exists(), "the undeletable file survives"
    for path in (mirror, cache_md, cache_meta, flag_page):
        assert not path.exists(), f"{path} was attempted despite the earlier failure"
    rows = queue.entries()
    assert len(rows) == 1
    assert rows[0].status is SourceQueueStatus.REQUESTED, "a partial removal never touches the queue"
    assert [knowledge_base.name for knowledge_base in reindex.knowledge_bases] == ["research"], "the best-effort heal still runs once"


async def test_remove_source_artifacts_says_a_deferred_heal_leaves_trace_source_stale_until_the_run_ends(tmp_path: Path) -> None:
    _mirror, digest, _cache_md, _cache_meta, _flag_page = _removal_fixture(tmp_path)
    queue = SourceQueue(tmp_path / "_meta" / "source-queue.yaml", connectors=_QUEUE_CONNECTORS)
    queue.add("https://example.com/src-1")
    doors, reindex, _ = _doors(tmp_path, _removal_admin(), queue=queue)
    reindex.outcome = "deferred"
    expected_paths = [
        "Wiki/Flags/check-source-src-1.md",
        "Wiki/Mirrors/src-1.md",
        "Wiki/Papers/src-one.md",
        "_meta/source-queue-cache/src-1.md",
        "_meta/source-queue-cache/src-1.meta.json",
    ]
    protected_dir = digest.parent
    protected_dir.chmod(0o555)
    try:
        with pytest.raises(ModelRetry) as excinfo:
            await doors.remove_source_artifacts("research", "src-1", expected_paths)
    finally:
        protected_dir.chmod(0o755)

    message = str(excinfo.value)
    assert "was reindexed" not in message
    assert "the knowledge base indexes once this run ends, so until then trace_source still lists the removed files" in message
    assert "Fix those files (permissions, locks), then once this run has ended re-run trace_source" in message


async def test_read_source_answers_the_mirror_whole_as_chunks_in_order(tmp_path: Path) -> None:
    admin = FakeAdminGateway(
        rows=[
            {
                "source_id": "z-1", "provider": "zotero", "title": "a paper", "item_type": "page_type-paper",
                "chunk_id": "z-1#0", "section_path": None, "heading": "Abstract", "seq": 0, "body": "We show.", "chunk_count": 2,
                "section_count": 2,
            },
            {
                "source_id": "z-1", "provider": "zotero", "title": "a paper", "item_type": "page_type-paper",
                "chunk_id": "z-1#1", "section_path": "s2.s1", "heading": "2.1 Method", "seq": 1, "body": "We do.", "chunk_count": 2,
                "section_count": 2,
            },
        ]
    )
    doors, _, _ = _doors(tmp_path, admin)
    mirror = await doors.read_source("research", "z-1")
    # A chunk's loc is the `§` locator the indexer maps back onto the mirror; an unnumbered chunk cites its heading.
    assert mirror == {
        "source_id": "z-1",
        "provider": "zotero",
        "title": "a paper",
        "item_type": "paper",
        "chunks": [
            {"loc": "Abstract", "heading": "Abstract", "text": "We show."},
            {"loc": "§2.1", "heading": "2.1 Method", "text": "We do."},
        ],
        "chunk_count": 2,
        "section_count": 2,
    }
    sql, params = admin.calls[0]
    assert "kb.raw_chunks" in sql
    assert "AS section_count" in sql
    assert "ORDER BY rc.seq" in sql
    assert params == {"collection": "research", "source_id": "z-1"}


async def test_list_recent_digests_reads_the_admin_gateway_within_the_window(tmp_path: Path) -> None:
    admin = FakeAdminGateway(rows=[{"digest_id": "paper.react", "page_type": "paper", "title": "ReAct"}])
    doors, _, _ = _doors(tmp_path, admin)

    rows = await doors.list_recent_digests("research", since_hours=26, limit=10)

    assert rows == [{"digest_id": "paper.react", "page_type": "paper", "title": "ReAct"}]
    sql, params = admin.calls[0]
    assert "digest_of" in sql
    assert "p.created >=" in sql
    assert params == {"collection": "research", "since_hours": 26, "limit": 10}


async def test_list_recent_digests_names_a_digest_by_the_spec_own_field_not_a_literal(tmp_path: Path) -> None:
    """A bundle naming its digest-link field differently changes the check, proving it is spec-driven, not hardcoded."""
    base = research_spec()
    renamed = base.model_copy(update={"sources": base.sources.model_copy(update={"digest_link_field": "source_of"})})
    admin = FakeAdminGateway(rows=[])
    doors, _, _ = _doors(tmp_path, admin, spec=renamed)

    await doors.list_recent_digests("research")

    sql, _params = admin.calls[0]
    assert "source_of" in sql
    assert "digest_of" not in sql


async def test_digest_skeleton_prefills_identity_from_the_mirror(tmp_path: Path) -> None:
    admin = FakeAdminGateway(
        rows=[{"source_id": "arxiv-2401.01234", "provider": "intake", "title": "Agents that plan", "source_hash": "h1", "item_type": "paper", "year": "2024"}]
    )
    doors, _, _ = _doors(tmp_path, admin)

    skeleton = await doors.digest_skeleton("research", "arxiv-2401.01234")

    frontmatter = skeleton["frontmatter"]
    assert isinstance(frontmatter, dict)
    assert frontmatter["id"] == "paper.agents-that-plan"
    assert frontmatter["type"] == "paper"
    assert frontmatter["digest_of"] == "arxiv-2401.01234"
    assert frontmatter["zotero_key"] == "arxiv-2401.01234"
    assert frontmatter["source_hash"] == "h1"
    assert frontmatter["year"] == 2024
    assert frontmatter["paper_type"] == "", "required, unknown to the mirror, present and empty"
    sections = skeleton["sections"]
    assert isinstance(sections, list)
    headings = [section["heading"] for section in sections if isinstance(section, dict)]
    assert headings[:2] == ["TL;DR", "Context"], "the paper page_type's own canonical sections, in its order"
    assert "Findings" not in headings, "the renderer fills the claims section from claims"
    fill = skeleton["fill"]
    assert isinstance(fill, dict)
    assert "claim.kind" in fill
    assert any("kind from claim.kind" in rule for rule in fill["rules"]), "the claim key the skeleton's claim carries"
    assert any("verbatim" in rule for rule in fill["rules"])
    assert any(rule.startswith("paper_type is empty") for rule in fill["rules"]), "no vocabulary values here, so the rule says fill it from the source"
    assert any("'[[page.id]]'" in rule and "not a unit link" in rule for rule in fill["rules"]), "the link shape is spelled out, the refusal it prevents named"
    assert any("never the frontmatter dict alone" in rule for rule in fill["rules"]), "the payload shape is spelled out"


async def test_digest_skeleton_fill_carries_the_purpose_page(tmp_path: Path) -> None:
    admin = FakeAdminGateway(rows=[{"source_id": "arxiv-1", "provider": "intake", "title": "T", "source_hash": "h", "item_type": "paper"}])
    doors, _, _ = _doors(tmp_path, admin)
    (tmp_path / "_meta" / "purpose.md").write_text("Retrieval for coding agents.", encoding="utf-8")

    skeleton = await doors.digest_skeleton("research", "arxiv-1")

    fill = skeleton["fill"]
    assert isinstance(fill, dict)
    assert fill["purpose"] == "Retrieval for coding agents."


async def test_digest_skeleton_purpose_keeps_summary_and_scope_sections_of_a_long_page(tmp_path: Path) -> None:
    admin = FakeAdminGateway(rows=[{"source_id": "arxiv-1", "provider": "intake", "title": "T", "source_hash": "h", "item_type": "paper"}])
    doors, _, _ = _doors(tmp_path, admin)
    page = (
        "---\nid: meta.purpose\nsummary: What belongs here.\n---\n\n# Purpose\n\n## Mission\nRetrieval research.\n\n"
        "## In scope\nPapers on retrieval.\n\n## Out of scope\nNews.\n\n## Consumers\n" + "x" * 3000
    )
    (tmp_path / "_meta" / "purpose.md").write_text(page, encoding="utf-8")

    skeleton = await doors.digest_skeleton("research", "arxiv-1")

    fill = skeleton["fill"]
    assert isinstance(fill, dict)
    assert fill["purpose"] == "What belongs here.\n\n## Mission\nRetrieval research.\n\n## In scope\nPapers on retrieval.\n\n## Out of scope\nNews."


async def test_digest_skeleton_purpose_is_empty_when_the_knowledge_base_has_none(tmp_path: Path) -> None:
    admin = FakeAdminGateway(rows=[{"source_id": "arxiv-1", "provider": "intake", "title": "T", "source_hash": "h", "item_type": "paper"}])
    doors, _, _ = _doors(tmp_path, admin)
    purpose_page = tmp_path / "_meta" / "purpose.md"
    if purpose_page.exists():
        purpose_page.unlink()

    skeleton = await doors.digest_skeleton("research", "arxiv-1")

    fill = skeleton["fill"]
    assert isinstance(fill, dict)
    assert fill["purpose"] == ""


async def test_digest_skeleton_numbers_the_id_when_another_sources_digest_holds_it(tmp_path: Path) -> None:
    held = tmp_path / "Wiki" / "Papers" / "agents-that-plan.md"
    held.parent.mkdir(parents=True)
    held.write_text("---\nid: paper.agents-that-plan\ntype: paper\ndigest_of: arxiv-other\n---\n", encoding="utf-8")
    admin = FakeAdminGateway(
        rows=[{"source_id": "arxiv-1", "provider": "intake", "title": "Agents that plan", "source_hash": "h", "item_type": "paper"}]
    )
    doors, _, _ = _doors(tmp_path, admin)

    skeleton = await doors.digest_skeleton("research", "arxiv-1")

    frontmatter = skeleton["frontmatter"]
    assert isinstance(frontmatter, dict)
    assert frontmatter["id"] == "paper.agents-that-plan-2"


async def test_digest_skeleton_uses_the_spec_own_digest_link_field_name(tmp_path: Path) -> None:
    """A bundle naming its digest-link field differently changes the frontmatter key, proving it is spec-driven, not hardcoded."""
    base = research_spec()
    renamed = base.model_copy(update={"sources": base.sources.model_copy(update={"digest_link_field": "source_of"})})
    admin = FakeAdminGateway(
        rows=[{"source_id": "arxiv-1", "provider": "intake", "title": "A paper", "source_hash": "h", "item_type": "paper"}]
    )
    doors, _, _ = _doors(tmp_path, admin, spec=renamed)

    skeleton = await doors.digest_skeleton("research", "arxiv-1")

    frontmatter = skeleton["frontmatter"]
    assert isinstance(frontmatter, dict)
    assert frontmatter["source_of"] == "arxiv-1"
    assert "digest_of" not in frontmatter


async def test_digest_skeleton_refuses_an_item_type_the_spec_maps_to_no_kind(tmp_path: Path) -> None:
    spec = research_spec().model_copy(update={"sources": research_spec().sources.model_copy(update={"digest_types": {"paper": "paper"}})})
    admin = FakeAdminGateway(rows=[{"source_id": "yt-abc", "provider": "intake", "title": "a talk", "source_hash": "h", "item_type": "video"}])
    doors, _, _ = _doors(tmp_path, admin, spec=spec)

    with pytest.raises(ModelRetry, match="digest_types"):
        await doors.digest_skeleton("research", "yt-abc")


async def _digest_skeleton_for(
    spec: KnowledgeBaseSchema, *, page_type: str, tmp_path: Path, year: str = "", meta: dict[str, str | None] | None = None
) -> dict[str, object]:
    item_type = next(item for item, digest_type in spec.sources.digest_types.items() if digest_type == page_type)
    row: dict[str, object] = {"source_id": "src-1", "provider": "intake", "title": "A source", "source_hash": "h", "item_type": item_type, "year": year}
    if meta is not None:
        row["meta"] = meta
    doors, _, _ = _doors(tmp_path, FakeAdminGateway(rows=[row]), spec=spec)
    return await doors.digest_skeleton("research", "src-1")


def _named_link_fields(rules: object, spec: KnowledgeBaseSchema) -> set[str]:
    link_fields = {field.name for page_type in spec.page_types for field in page_type.frontmatter_fields if field.type in {"link", "link-list"}}
    rule = next(rule for rule in cast("list[str]", rules) if "'[[page.id]]'" in rule)
    return {word for word in re.findall(r"[a-z_]+", rule) if word in link_fields}


@pytest.mark.parametrize("page_type", ["talk", "paper"])
async def test_the_skeleton_rules_name_the_digest_type_s_own_link_fields(tmp_path: Path, page_type: str) -> None:
    spec = research_spec()
    skeleton = await _digest_skeleton_for(spec, page_type=page_type, tmp_path=tmp_path)

    fill = cast("dict[str, object]", skeleton["fill"])
    page_type_schema = spec.page_type(page_type)
    assert page_type_schema is not None
    assert _named_link_fields(fill["rules"], spec) == set(page_type_schema.edge_fields)


async def test_the_mirror_year_lands_only_in_a_field_the_digest_type_declares(tmp_path: Path) -> None:
    skeleton = await _digest_skeleton_for(research_spec(), page_type="talk", tmp_path=tmp_path, year="2024")

    assert "year" not in cast("dict[str, object]", skeleton["frontmatter"])


async def test_without_a_claim_grammar_no_section_is_left_for_claims(tmp_path: Path) -> None:
    spec = research_spec().model_copy(update={"claims": None})
    skeleton = await _digest_skeleton_for(spec, page_type="paper", tmp_path=tmp_path)

    headings = [section["heading"] for section in cast("list[dict[str, str]]", skeleton["sections"])]
    assert "Findings" in headings


def _replacing_page_type(spec: KnowledgeBaseSchema, replacement: PageTypeSpec) -> KnowledgeBaseSchema:
    page_types = tuple(replacement if entry.page_type == replacement.page_type else entry for entry in spec.page_types)
    return spec.model_copy(update={"page_types": page_types})


def _research_paper() -> PageTypeSpec:
    paper = research_spec().page_type("paper")
    assert paper is not None
    return paper


async def test_the_skeleton_keeps_the_source_link_a_digest_type_requires(tmp_path: Path) -> None:
    skeleton = await _digest_skeleton_for(research_spec(), page_type="talk", tmp_path=tmp_path)

    assert cast("dict[str, object]", skeleton["frontmatter"])["digest_of"] == "src-1", "talk declares digest_of required"


async def test_the_skeleton_fills_optional_key_and_hash_fields_the_digest_type_declares(tmp_path: Path) -> None:
    paper = _research_paper()
    fields = tuple(
        field.model_copy(update={"required": False}) if field.name in {"zotero_key", "source_hash"} else field
        for field in paper.frontmatter_fields
    )
    spec = _replacing_page_type(research_spec(), paper.model_copy(update={"frontmatter_fields": fields}))

    skeleton = await _digest_skeleton_for(spec, page_type="paper", tmp_path=tmp_path)

    frontmatter = cast("dict[str, object]", skeleton["frontmatter"])
    assert frontmatter["zotero_key"] == "src-1"
    assert frontmatter["source_hash"] == "h"


async def test_the_skeleton_leaves_out_key_and_hash_fields_the_digest_type_does_not_declare(tmp_path: Path) -> None:
    skeleton = await _digest_skeleton_for(research_spec(), page_type="talk", tmp_path=tmp_path)

    frontmatter = cast("dict[str, object]", skeleton["frontmatter"])
    assert "zotero_key" not in frontmatter
    assert "source_hash" not in frontmatter


async def test_the_skeleton_of_a_digest_type_without_claims_has_no_claim_stub_kinds_or_rules(tmp_path: Path) -> None:
    spec = _replacing_page_type(research_spec(), _research_paper().model_copy(update={"claims_enabled": False}))

    skeleton = await _digest_skeleton_for(spec, page_type="paper", tmp_path=tmp_path)

    assert skeleton["claims"] == []
    fill = cast("dict[str, object]", skeleton["fill"])
    assert "claim.kind" not in fill
    assert not [rule for rule in cast("list[str]", fill["rules"]) if "claim" in rule]
    headings = [section["heading"] for section in cast("list[dict[str, str]]", skeleton["sections"])]
    assert len(headings) == len(_research_paper().canonical_sections), "a page type without claims keeps every canonical section"
    assert "Findings" in headings, "nothing fills the claims section from claims, so the model writes it"


async def test_the_skeleton_of_a_digest_type_with_claims_keeps_the_claim_stub_kinds_and_rule(tmp_path: Path) -> None:
    skeleton = await _digest_skeleton_for(research_spec(), page_type="paper", tmp_path=tmp_path)

    assert skeleton["claims"] == [{"text": "", "kind": "", "quote": "", "loc": ""}]
    fill = cast("dict[str, object]", skeleton["fill"])
    assert "claim.kind" in fill
    assert any("kind from claim.kind" in rule for rule in cast("list[str]", fill["rules"]))


def _link_rule_of(skeleton: dict[str, object]) -> str:
    fill = cast("dict[str, object]", skeleton["fill"])
    return next(rule for rule in cast("list[str]", fill["rules"]) if "'[[page.id]]'" in rule)


async def test_the_link_rule_names_a_claim_s_edges_when_the_claim_grammar_declares_edge_relations(tmp_path: Path) -> None:
    skeleton = await _digest_skeleton_for(research_spec(), page_type="paper", tmp_path=tmp_path)

    assert "a claim's edges are [relation, '[[page.id]]'] pairs" in _link_rule_of(skeleton)


async def test_the_link_rule_names_no_claim_edges_when_the_claim_grammar_declares_no_edge_relations(tmp_path: Path) -> None:
    base = research_spec()
    assert base.claims is not None
    spec = base.model_copy(update={"claims": base.claims.model_copy(update={"edge_relations": ()})})

    skeleton = await _digest_skeleton_for(spec, page_type="paper", tmp_path=tmp_path)

    assert "edges" not in _link_rule_of(skeleton)


async def test_digest_skeleton_names_the_part_field_when_the_schema_splits_long_sources(tmp_path: Path) -> None:
    base = research_spec()
    spec = base.model_copy(update={"sources": base.sources.model_copy(update={"digest_sections_over": 40, "digest_part_field": "part_of"})})

    skeleton = await _digest_skeleton_for(spec, page_type="paper", tmp_path=tmp_path)

    assert cast("dict[str, object]", skeleton["fill"])["part_field"] == "part_of"


async def test_digest_skeleton_names_no_part_field_when_the_schema_never_splits(tmp_path: Path) -> None:
    spec = research_spec()
    assert spec.sources.digest_sections_over is None

    skeleton = await _digest_skeleton_for(spec, page_type="paper", tmp_path=tmp_path)

    assert "part_field" not in cast("dict[str, object]", skeleton["fill"])


_REGION = FrontmatterFieldSpec(name="region", type="scalar")
_ISSUED_ON = FrontmatterFieldSpec(name="issued_on", type="scalar")


def _metadata_spec(digest_fields: dict[str, str], *fields: FrontmatterFieldSpec) -> KnowledgeBaseSchema:
    """The research schema whose paper digest also declares `fields` and maps `digest_fields` from the mirror metadata."""
    paper = _research_paper()
    spec = _replacing_page_type(research_spec(), paper.model_copy(update={"frontmatter_fields": (*paper.frontmatter_fields, *fields)}))
    sources = spec.sources.model_copy(update={"digest_types": {"paper": "paper"}, "digest_fields": digest_fields})
    return KnowledgeBaseSchema.model_validate(spec.model_copy(update={"sources": sources}).model_dump(mode="json"))


async def test_digest_skeleton_fills_every_mapped_digest_field_from_the_mirror_metadata(tmp_path: Path) -> None:
    spec = _metadata_spec({"region": "region", "issued_on": "issued_on"}, _REGION, _ISSUED_ON)

    skeleton = await _digest_skeleton_for(spec, page_type="paper", tmp_path=tmp_path, meta={"region": "north", "issued_on": "2024-01-02"})

    frontmatter = cast("dict[str, object]", skeleton["frontmatter"])
    assert frontmatter["region"] == "north"
    assert frontmatter["issued_on"] == "2024-01-02"


async def test_digest_skeleton_fills_the_field_a_metadata_key_maps_to_not_a_field_of_the_key_s_name(tmp_path: Path) -> None:
    spec = _metadata_spec({"region": "area"}, FrontmatterFieldSpec(name="area", type="scalar"))

    skeleton = await _digest_skeleton_for(spec, page_type="paper", tmp_path=tmp_path, meta={"region": "north"})

    frontmatter = cast("dict[str, object]", skeleton["frontmatter"])
    assert frontmatter["area"] == "north"
    assert "region" not in frontmatter


async def test_digest_skeleton_leaves_out_a_mapped_field_whose_key_the_mirror_metadata_lacks(tmp_path: Path) -> None:
    spec = _metadata_spec({"region": "region", "issued_on": "issued_on"}, _REGION, _ISSUED_ON)

    skeleton = await _digest_skeleton_for(spec, page_type="paper", tmp_path=tmp_path, meta={"region": "north"})

    frontmatter = cast("dict[str, object]", skeleton["frontmatter"])
    assert frontmatter["region"] == "north"
    assert "issued_on" not in frontmatter


async def test_digest_skeleton_counts_a_null_metadata_value_as_missing(tmp_path: Path) -> None:
    spec = _metadata_spec({"region": "region", "issued_on": "issued_on"}, _REGION, _ISSUED_ON)

    skeleton = await _digest_skeleton_for(spec, page_type="paper", tmp_path=tmp_path, meta={"region": "north", "issued_on": None})

    assert "issued_on" not in cast("dict[str, object]", skeleton["frontmatter"])


async def test_digest_skeleton_leaves_a_required_mapped_field_present_and_empty_when_the_metadata_lacks_its_key(tmp_path: Path) -> None:
    spec = _metadata_spec({"issued_on": "issued_on"}, FrontmatterFieldSpec(name="issued_on", type="scalar", required=True))

    skeleton = await _digest_skeleton_for(spec, page_type="paper", tmp_path=tmp_path, meta={"region": "north"})

    assert cast("dict[str, object]", skeleton["frontmatter"])["issued_on"] == ""
    fill = cast("dict[str, object]", skeleton["fill"])
    assert any(rule.startswith("issued_on is empty") for rule in cast("list[str]", fill["rules"]))


async def test_digest_skeleton_keeps_an_identity_field_over_a_mapped_field_of_the_same_name(tmp_path: Path) -> None:
    spec = _metadata_spec({"region": "source_hash"})

    skeleton = await _digest_skeleton_for(spec, page_type="paper", tmp_path=tmp_path, meta={"region": "north"})

    assert cast("dict[str, object]", skeleton["frontmatter"])["source_hash"] == "h", "the mirror's own hash, never the metadata value"


def _claim_text_fields_spec(*, switch: bool, text_scalar_fields: tuple[str, ...]) -> KnowledgeBaseSchema:
    """The research schema with `text_scalar_fields` and the switch, validated afresh so no cached grammar field survives."""
    base = research_spec()
    assert base.claims is not None
    claims = base.claims.model_copy(update={"text_scalar_fields": text_scalar_fields})
    sources = base.sources.model_copy(update={"digest_claim_text_fields": switch})
    return KnowledgeBaseSchema.model_validate(base.model_copy(update={"claims": claims, "sources": sources}).model_dump(mode="json"))


def _claim_rule_of(skeleton: dict[str, object]) -> str:
    fill = cast("dict[str, object]", skeleton["fill"])
    return next(rule for rule in cast("list[str]", fill["rules"]) if rule.startswith("One claims entry per finding"))


async def test_with_claim_text_fields_on_the_claim_stub_and_rule_name_every_declared_text_scalar_field(tmp_path: Path) -> None:
    declared = ("condition", "baseline", "magnitude", "eval_setting", "derived")
    spec = _claim_text_fields_spec(switch=True, text_scalar_fields=(*declared, "status"))

    skeleton = await _digest_skeleton_for(spec, page_type="paper", tmp_path=tmp_path)

    assert skeleton["claims"] == [dict.fromkeys(("text", "kind", "quote", "loc", *declared), "")]
    assert list(cast("list[dict[str, str]]", skeleton["claims"])[0])[:4] == ["text", "kind", "quote", "loc"]
    assert "status" not in cast("list[dict[str, str]]", skeleton["claims"])[0], "status is a built-in claim field, never offered as a blank"
    assert "Also fill condition, baseline, magnitude, eval_setting and derived from the source" in _claim_rule_of(skeleton)
    assert "status" not in _claim_rule_of(skeleton)


async def test_with_claim_text_fields_on_the_claim_stub_never_lists_a_link_field_or_an_edge_relation(tmp_path: Path) -> None:
    spec = _claim_text_fields_spec(switch=True, text_scalar_fields=("condition", "on_task", "metric", "supports"))

    skeleton = await _digest_skeleton_for(spec, page_type="paper", tmp_path=tmp_path)

    stub = {"text": "", "kind": "", "quote": "", "loc": "", "condition": ""}
    assert skeleton["claims"] == [stub], "on_task and metric are link fields, supports is an edge relation"


async def test_with_claim_text_fields_off_the_claim_stub_and_rule_are_today_s(tmp_path: Path) -> None:
    spec = _claim_text_fields_spec(switch=False, text_scalar_fields=("condition", "baseline"))

    skeleton = await _digest_skeleton_for(spec, page_type="paper", tmp_path=tmp_path)

    assert skeleton["claims"] == [{"text": "", "kind": "", "quote": "", "loc": ""}]
    assert _claim_rule_of(skeleton) == (
        "One claims entry per finding: text, kind from claim.kind, quote copied verbatim from a chunk, loc copied from that chunk."
    )


def test_a_claims_block_must_name_its_section() -> None:
    with pytest.raises(ValidationError, match="section"):
        ClaimGrammarSpec.model_validate({})


async def test_digest_skeleton_of_an_unknown_mirror_is_a_model_retry(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path, FakeAdminGateway(rows=[]))
    with pytest.raises(ModelRetry, match="list_undigested"):
        await doors.digest_skeleton("research", "nope")


async def test_read_source_of_an_unknown_mirror_is_a_model_retry_pointing_at_list_undigested(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path, FakeAdminGateway(rows=[]))
    with pytest.raises(ModelRetry, match="list_undigested"):
        await doors.read_source("research", "nope")


async def test_read_source_chunk_count_is_honest_even_when_the_returned_chunks_are_capped(tmp_path: Path) -> None:
    """`chunk_count` answers the mirror's true total, not `len(chunks)` — the row a cap trimmed to still carries it."""
    admin = FakeAdminGateway(
        rows=[
            {
                "source_id": "z-1", "provider": "zotero", "title": "a paper", "item_type": "page_type-paper",
                "chunk_id": "z-1#0", "heading": "Abstract", "seq": 0, "body": "We show.", "chunk_count": 5000,
            }
        ]
    )
    doors, _, _ = _doors(tmp_path, admin)
    mirror = await doors.read_source("research", "z-1")
    assert mirror["chunk_count"] == 5000
    chunks = mirror["chunks"]
    assert isinstance(chunks, list)
    assert len(chunks) == 1


async def test_read_source_omits_the_split_fields_when_the_spec_sets_no_threshold(tmp_path: Path) -> None:
    admin = FakeAdminGateway(
        rows=[
            {
                "source_id": "z-1", "provider": "zotero", "title": "a paper", "item_type": "page_type-paper",
                "chunk_id": "z-1#0", "heading": "Abstract", "seq": 0, "body": "We show.", "chunk_count": 1,
            }
        ]
    )
    doors, _, _ = _doors(tmp_path, admin)
    mirror = await doors.read_source("research", "z-1")
    assert "split_recommended" not in mirror
    assert "digest_sections_over" not in mirror


def _threshold_spec(threshold: int) -> KnowledgeBaseSchema:
    base = research_spec()
    return base.model_copy(update={"sources": base.sources.model_copy(update={"digest_sections_over": threshold})})


def _chunk_row(index: int, *, chunk_count: int, section_count: int) -> dict[str, object]:
    return {
        "source_id": "z-1", "provider": "zotero", "title": "a paper", "item_type": "page_type-paper",
        "chunk_id": f"z-1#{index}", "heading": "H", "seq": index, "body": "b", "chunk_count": chunk_count, "section_count": section_count,
    }


async def test_read_source_recommends_a_split_when_its_heading_sections_pass_the_threshold(tmp_path: Path) -> None:
    admin = FakeAdminGateway(rows=[_chunk_row(i, chunk_count=12, section_count=5) for i in range(12)])
    doors, _, _ = _doors(tmp_path, admin, spec=_threshold_spec(3))
    mirror = await doors.read_source("research", "z-1")
    assert mirror["chunk_count"] == 12
    assert mirror["section_count"] == 5
    assert mirror["split_recommended"] is True
    assert mirror["digest_sections_over"] == 3


async def test_read_source_does_not_recommend_a_split_at_or_under_the_threshold(tmp_path: Path) -> None:
    admin = FakeAdminGateway(rows=[_chunk_row(i, chunk_count=12, section_count=5) for i in range(12)])
    doors, _, _ = _doors(tmp_path, admin, spec=_threshold_spec(5))
    mirror = await doors.read_source("research", "z-1")
    assert mirror["split_recommended"] is False


async def test_read_source_does_not_recommend_a_split_when_only_its_chunks_pass_the_threshold(tmp_path: Path) -> None:
    admin = FakeAdminGateway(rows=[_chunk_row(i, chunk_count=12, section_count=3) for i in range(12)])
    doors, _, _ = _doors(tmp_path, admin, spec=_threshold_spec(5))
    mirror = await doors.read_source("research", "z-1")
    assert mirror["chunk_count"] == 12
    assert mirror["split_recommended"] is False


def test_collection_of_is_the_targets_collection_suffixed(tmp_path: Path) -> None:
    knowledge_base = AuthoringKnowledgeBase(name="research", collection="research", path=tmp_path, schema=research_spec())
    assert collection_of(knowledge_base) == "research-agent-memory"


async def test_knowledge_base_profile_purpose_is_empty_when_the_file_is_absent(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path)
    profile = await doors.knowledge_base_profile("research")
    assert profile["purpose"] == ""


async def test_knowledge_base_profile_answers_purpose_vocabulary_and_decisions(tmp_path: Path) -> None:
    admin = FakeAdminGateway(rows=[{"id": "flag.x", "page_type": "flag", "title": "A flag", "status": "pending"}])
    doors, _, _ = _doors(tmp_path, admin)
    (tmp_path / "_meta" / "purpose.md").write_text("An indexed research wiki.\n", encoding="utf-8")

    profile = await doors.knowledge_base_profile("research")

    assert profile["purpose"] == "An indexed research wiki.\n"
    assert profile["decisions"] == [
        {
            "page_type": "flag",
            "open": "pending",
            "outcomes": [
                {"status": "accepted", "link": None},
                {"status": "dismissed", "link": None},
                {"status": "completed", "link": "resolved_by"},
            ],
            "pages": [{"id": "flag.x", "title": "A flag", "status": "pending"}],
        },
        {
            "page_type": "stance",
            "open": "considering",
            "outcomes": [
                {"status": "trying", "link": None},
                {"status": "adopted", "link": None},
                {"status": "rejected", "link": None},
            ],
            "pages": [],
        },
    ]
    vocabulary = profile["vocabulary"]
    assert isinstance(vocabulary, dict)
    facets = vocabulary["facets"]
    assert isinstance(facets, list)
    tasks_facet = next(facet for facet in facets if facet["key"] == "tasks")
    assert "multi-hop-qa" in tasks_facet["seed"]
    relations = vocabulary["relations"]
    assert isinstance(relations, list)
    relation_slugs = {relation["slug"] for relation in relations}
    assert {"builds_on", "part_of", "mentions"} <= relation_slugs
    claim_types = vocabulary["claim_types"]
    assert isinstance(claim_types, list)
    claim_type_values = {entry["value"] for entry in claim_types}
    assert "benchmark-result" in claim_type_values
    sql, params = admin.calls[0]
    assert "kb.pages" in sql
    assert "page_type = ANY(:page_types)" in sql
    assert params == {"collection": "research", "page_types": ["flag", "stance"]}


def _profile_page_type(profile: dict[str, object], page_type: str) -> dict[str, object]:
    page_types = profile["page_types"]
    assert isinstance(page_types, list)
    return next(entry for entry in cast("list[dict[str, object]]", page_types) if entry["page_type"] == page_type)


async def test_knowledge_base_profile_lists_each_page_type_edge_fields_with_their_relation_domain_and_range(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path)

    profile = await doors.knowledge_base_profile("research")

    relations = {relation.slug: relation.description for relation in research_spec().relations}
    assert _profile_page_type(profile, "stance")["edge_fields"] == [
        {"field": "synthesizes", "holds": "list", "domain": "page", "range": "paper", "description": relations["synthesizes"]},
        {"field": "recommends", "holds": "list", "domain": "playbook", "range": "entity", "description": relations["recommends"]},
        {"field": "advises_against", "holds": "list", "domain": "playbook", "range": "entity", "description": relations["advises_against"]},
    ]
    assert _profile_page_type(profile, "question")["edge_fields"] == [
        {"field": "answered_by", "holds": "one", "domain": "question", "range": "entity", "description": relations["answered_by"]},
    ]
    assert _profile_page_type(profile, "moc")["edge_fields"] == []


async def test_knowledge_base_profile_names_the_entity_page_type_the_entity_types_and_the_claim_link_fields(tmp_path: Path) -> None:
    spec = KnowledgeBaseSchema.model_validate({**research_spec().model_dump(), "entities": {"page_type": "entity"}})
    doors, _, _ = _doors(tmp_path, spec=spec)

    profile = await doors.knowledge_base_profile("research")

    assert profile["entity_page_type"] == "entity"
    vocabulary = profile["vocabulary"]
    assert isinstance(vocabulary, dict)
    assert {"value": "technique", "definition": "A method/algorithm/strategy that can be applied, composed, or compared"} in vocabulary["entity_types"]
    assert vocabulary["claim_link_fields"] == [
        {"field": "subject", "holds": "one"},
        {"field": "on_dataset", "holds": "list"},
        {"field": "metric", "holds": "list"},
        {"field": "on_task", "holds": "one"},
    ]


async def test_knowledge_base_profile_of_a_base_with_no_entity_pages_and_no_claims_names_neither(tmp_path: Path) -> None:
    spec = research_spec().model_copy(update={"claims": None})
    doors, _, _ = _doors(tmp_path, spec=spec)

    profile = await doors.knowledge_base_profile("research")

    assert profile["entity_page_type"] == ""
    vocabulary = profile["vocabulary"]
    assert isinstance(vocabulary, dict)
    assert vocabulary["claim_link_fields"] == []


async def test_knowledge_base_profile_answers_an_edge_field_with_no_declared_relation_with_an_empty_domain_and_range(tmp_path: Path) -> None:
    base = research_spec()
    spec = base.model_copy(update={"relations": tuple(relation for relation in base.relations if relation.slug != "answered_by")})
    doors, _, _ = _doors(tmp_path, spec=spec)

    profile = await doors.knowledge_base_profile("research")

    assert _profile_page_type(profile, "question")["edge_fields"] == [{"field": "answered_by", "holds": "one", "domain": "", "range": "", "description": ""}]


async def test_recall_beliefs_answers_the_live_cards_as_id_text_pairs(tmp_path: Path) -> None:
    memory = _memory_store([("mem.research.beliefs.project.belief-1", "Belief title", "The base prefers claim-grounded flags.")])
    doors, _, _ = _doors(tmp_path, memory_for=lambda _t: memory)

    beliefs = await doors.recall_beliefs("research")

    assert beliefs == [{"id": "mem.research.beliefs.project.belief-1", "text": "The base prefers claim-grounded flags."}]


async def test_recall_beliefs_falls_back_to_the_title_when_a_card_has_no_summary(tmp_path: Path) -> None:
    memory = _memory_store([("mem.research.beliefs.project.belief-1", "Belief title", "")])
    doors, _, _ = _doors(tmp_path, memory_for=lambda _t: memory)

    beliefs = await doors.recall_beliefs("research")

    assert beliefs == [{"id": "mem.research.beliefs.project.belief-1", "text": "Belief title"}]


async def test_recall_beliefs_without_memory_wired_says_so(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path)
    assert await doors.recall_beliefs("research") == "agent memory is not mounted for 'research' in this deployment"


async def test_remember_belief_writes_one_fresh_note_under_the_project_category(tmp_path: Path) -> None:
    memory = _memory_store()
    doors, _, _ = _doors(tmp_path, memory_for=lambda _t: memory)

    said = await doors.remember_belief("research", "The base prefers claim-grounded flags.")

    assert said.startswith("remembered for research: mem.research.beliefs.project.belief-")
    page_id = said.removeprefix("remembered for research: ")
    written = await memory.read(path_of(page_id), max_chars=1_000)
    assert written is not None
    assert written.content == "The base prefers claim-grounded flags.\n"


async def test_remember_belief_writes_a_distinct_note_on_every_call(tmp_path: Path) -> None:
    memory = _memory_store()
    doors, _, _ = _doors(tmp_path, memory_for=lambda _t: memory)

    first = await doors.remember_belief("research", "A belief.")
    second = await doors.remember_belief("research", "A belief.")

    assert first != second


async def test_remember_belief_without_memory_wired_says_so_and_writes_nothing(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path)
    assert await doors.remember_belief("research", "anything") == "agent memory is not mounted for 'research' in this deployment"


def test_the_doors_mount_every_authoring_tool_in_order(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path)
    assert [name for _fn, name, _description in doors.tools()] == [
        "write_note",
        "check_note",
        "read_notes",
        "resolve_note",
        "list_pages",
        "read_page",
        "request_source",
        "list_undigested",
        "read_source",
        "trace_source",
        "remove_source_artifacts",
        "list_recent_digests",
        "compounding_work",
        "digest_skeleton",
        "source_queue_policy",
        "write_policy",
        "source_queue_rows",
        "source_queue_decide",
        "decide_page",
        "resolve_entities",
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("page_type-article", SourceType.ARTICLE),
        ("page_type-paper", SourceType.PAPER),
        ("some-other-type", SourceType.PAPER),
        ("", SourceType.PAPER),
    ],
)
def test_intake_kind_of_dispatches_to_the_named_providers_own_medium_of(raw: str, expected: SourceType) -> None:
    """`zotero`'s fake folds this made-up vocabulary two ways, proving dispatch, never the real adapter's own type set (`connectors/tests`)."""
    assert source_type_of("zotero", raw, source=_CONNECTORS) is expected


def test_intake_and_youtube_items_route_through_their_own_provider() -> None:
    assert source_type_of("intake", "video", source=_CONNECTORS) is SourceType.VIDEO
    assert source_type_of("intake", "post", source=_CONNECTORS) is SourceType.POST
    assert source_type_of("intake", "article", source=_CONNECTORS) is SourceType.ARTICLE
    assert source_type_of("intake", "paper", source=_CONNECTORS) is SourceType.PAPER
    assert source_type_of("youtube", "anything", source=_CONNECTORS) is SourceType.VIDEO


def test_intake_kind_of_reports_an_unregistered_provider_rather_than_defaulting_to_paper() -> None:
    with pytest.raises(ValueError, match="no registered connector named 'not-a-connector'"):
        source_type_of("not-a-connector", "page_type-paper", source=_CONNECTORS)


async def test_list_undigested_answers_a_zotero_item_type_as_one_of_the_four_kinds(tmp_path: Path) -> None:
    admin = FakeAdminGateway(
        rows=[
            {"source_id": "z-1", "provider": "zotero", "item_type": "page_type-paper", "title": "a paper"},
            {"source_id": "z-2", "provider": "zotero", "item_type": "page_type-article", "title": "a post on a blog"},
            {"source_id": "yt-1", "provider": "intake", "item_type": "video", "title": "a talk"},
        ]
    )
    doors, _, _ = _doors(tmp_path, admin)
    rows = await doors.list_undigested("research")
    assert [row["item_type"] for row in rows] == ["paper", "article", "video"]


async def test_intake_policy_answers_the_defaults_and_the_purpose_when_no_page_exists(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path)
    (tmp_path / "_meta" / "purpose.md").write_text("Retrieval for coding agents.", encoding="utf-8")

    answer = await doors.source_queue_policy("research")

    assert answer["present"] is False
    assert answer["purpose"] == "Retrieval for coding agents."
    assert answer["guidance"] == ""
    assert answer["rules"] == SourceQueuePolicy().model_dump(mode="json")
    assert answer["unknown_rules"] == []


async def test_intake_policy_names_an_unknown_rule_key(tmp_path: Path) -> None:
    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "ingestion-policy.md").write_text(
        "---\nid: meta.ingestion-policy\ntype: meta\ntitle: Ingestion policy\nhopdepth: 4\n---\n\n# Ingestion policy\n\nx\n",
        encoding="utf-8",
    )
    doors, _, _ = _doors(tmp_path)

    answer = await doors.source_queue_policy("research")

    assert answer["rules"] == SourceQueuePolicy().model_dump(mode="json")
    assert answer["unknown_rules"] == ["hopdepth"]


async def test_write_policy_merges_rules_over_the_current_ones_and_echoes_the_change(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path)

    said = await doors.write_policy("research", {"max_age_years": 6}, "No surveys older than the field's last turn.")
    again = await doors.source_queue_policy("research")

    assert said == "policy written for research: max_age_years now 6 (was none), guidance replaced"
    assert again["present"] is True
    assert isinstance(again["rules"], dict)
    assert again["rules"]["max_age_years"] == 6
    assert again["rules"]["per_source_cap"] == 5
    assert again["guidance"] == "No surveys older than the field's last turn."
    assert (tmp_path / "_meta" / "ingestion-policy.md").is_file()


async def test_write_policy_renders_tuple_values_as_plain_words(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path)

    said = await doors.write_policy("research", {"source_types": ["paper"]}, "")

    assert said == "policy written for research: source_types now paper (was video, paper, post, article), guidance unchanged"


async def test_write_policy_with_empty_guidance_keeps_the_current_guidance(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path)
    await doors.write_policy("research", {}, "Take papers on memory for agents.")

    said = await doors.write_policy("research", {"hop_depth": 2}, "")

    assert said == "policy written for research: hop_depth now 2 (was 1), guidance unchanged"
    again = await doors.source_queue_policy("research")
    assert again["guidance"] == "Take papers on memory for agents."


async def test_write_policy_refuses_a_rule_outside_its_set_naming_the_field(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path)

    with pytest.raises(ModelRetry, match="never"):
        await doors.write_policy("research", {"never": ["forum"]}, "")

    assert not (tmp_path / "_meta" / "ingestion-policy.md").exists()


async def test_write_policy_refuses_an_unknown_rule_and_names_it(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path)

    with pytest.raises(ModelRetry, match="max_age"):
        await doors.write_policy("research", {"max_age": 6}, "")

    page = read_policy(tmp_path, research_spec())
    assert page.rules == SourceQueuePolicy()


async def test_source_queue_rows_lists_the_queue_newest_first_with_reasons_and_filters_by_status(tmp_path: Path) -> None:
    doors, _, queue = _doors(tmp_path)
    queue.add_seen("https://source.test/paper/2401.00002", found_via="paper-2401.00001", reason="beyond hop depth 1", found_at=datetime(2026, 8, 26, 10, tzinfo=UTC))
    queue.add_bounded("https://source.test/paper/2401.00001", found_via="MJ389JS8", cap=5, reason="builds on it", found_at=datetime(2026, 8, 26, 9, tzinfo=UTC))

    ledger_view = await doors.source_queue_rows("research")
    seen_view = await doors.source_queue_rows("research", status="seen")

    rows = cast("list[dict[str, str]]", ledger_view["rows"])
    seen = cast("list[dict[str, str]]", seen_view["rows"])
    assert ledger_view["counts"] == {"seen": 1, "requested": 1}
    assert ledger_view["undigested_mirrors"] == 0
    assert "resolved means fetched and mirrored, not digested" in str(ledger_view["note"])
    assert seen_view["counts"] == {"seen": 1, "requested": 1}, "the counts cover the whole queue whatever the filter"
    assert [row["key"] for row in rows] == ["paper-2401.00002", "paper-2401.00001"]
    assert rows[1] == {
        "key": "paper-2401.00001",
        "url": "https://source.test/paper/2401.00001",
        "source_type": "paper",
        "status": "requested",
        "reason": "builds on it",
        "found_via": "MJ389JS8",
        "found_at": "2026-08-26T09:00:00+00:00",
    }
    assert [row["key"] for row in seen] == ["paper-2401.00002"]
    with pytest.raises(ModelRetry, match="no queue status 'pending'"):
        await doors.source_queue_rows("research", status="pending")


async def test_intake_rows_tolerates_a_naive_found_at(tmp_path: Path) -> None:
    """A hand-edited `_meta/source-queue.yaml` may carry a naive `found_at`; sorting it beside an aware one never raises."""
    doors, _, queue = _doors(tmp_path)
    naive = datetime(2026, 8, 26, 10)  # noqa: DTZ001 (a hand-edited queue row carrying no offset is the case under test)
    queue.add_seen("https://source.test/paper/2401.00002", found_via="paper-2401.00001", reason="beyond hop depth 1", found_at=naive)
    queue.add_bounded("https://source.test/paper/2401.00001", found_via="MJ389JS8", cap=5, reason="builds on it", found_at=datetime(2026, 8, 26, 9, tzinfo=UTC))

    ledger_view = await doors.source_queue_rows("research")

    rows = cast("list[dict[str, str]]", ledger_view["rows"])
    assert [row["key"] for row in rows] == ["paper-2401.00002", "paper-2401.00001"]


async def test_source_queue_decide_moves_a_row_and_names_what_it_cannot_move(tmp_path: Path) -> None:
    doors, _, queue = _doors(tmp_path)
    queue.add_seen("https://source.test/paper/2401.00002", found_via="paper-2401.00001", reason="beyond hop depth 1")

    fetched = await doors.source_queue_decide("research", "paper-2401.00002", "fetch")
    nothing = await doors.source_queue_decide("research", "paper-2401.00002", "fetch")
    dropped = await doors.source_queue_decide("research", "paper-2401.00002", "drop", reason="not our field")

    assert fetched == "paper-2401.00002 is now requested in research"
    assert nothing == "paper-2401.00002 is requested, nothing to fetch"
    assert dropped == "paper-2401.00002 is now skipped in research"
    row = queue.get("paper-2401.00002")
    assert row is not None
    assert row.reason == "not our field"
    with pytest.raises(ModelRetry, match="no queue row 'nope'"):
        await doors.source_queue_decide("research", "nope", "drop")


_FLAG_PAGE = """---
id: flag.try-planning-reminders
type: flag
title: Try planning reminders
summary: A live task list re-injected every turn.
status: pending
flag_kind: idea
about: ["[[talk.agents-that-plan]]"]
---

## Why

Because compaction drops the plan.

## Proposal

Mount the planning capability.

## Evidence

talk.agents-that-plan#findings
"""


async def test_decide_page_moves_the_page_and_reindexs_so_searches_see_it(tmp_path: Path) -> None:
    folder = tmp_path / "Wiki" / "Flags"
    folder.mkdir(parents=True)
    (folder / "try-planning-reminders.md").write_text(_FLAG_PAGE, encoding="utf-8")
    doors, reindex, _ = _doors(tmp_path)

    said = await doors.decide_page("research", "flag.try-planning-reminders", "dismissed")

    assert said == "flag.try-planning-reminders (flag) is now dismissed in research, indexed clean"
    assert "status: dismissed" in (folder / "try-planning-reminders.md").read_text(encoding="utf-8")
    assert [knowledge_base.name for knowledge_base in reindex.knowledge_bases] == ["research"]
    with pytest.raises(ModelRetry, match=re.escape("no page 'flag.nope'")):
        await doors.decide_page("research", "flag.nope", "accepted")


async def test_decide_page_with_no_status_keeps_the_page_status_and_writes_the_links(tmp_path: Path) -> None:
    folder = tmp_path / "Wiki" / "Flags"
    folder.mkdir(parents=True)
    (folder / "try-planning-reminders.md").write_text(_FLAG_PAGE, encoding="utf-8")
    doors, reindex, _ = _doors(tmp_path)

    said = await doors.decide_page("research", "flag.try-planning-reminders", links={"resolved_by": ("q.planning",)})

    assert said == "flag.try-planning-reminders (flag) stays pending in research, indexed clean, resolved_by: q.planning"
    text = (folder / "try-planning-reminders.md").read_text(encoding="utf-8")
    assert "status: pending" in text
    assert 'resolved_by: ["[[q.planning]]"]' in text
    assert [knowledge_base.name for knowledge_base in reindex.knowledge_bases] == ["research"]


async def test_decide_page_with_add_merges_the_links_and_answers_what_the_field_now_holds(tmp_path: Path) -> None:
    folder = tmp_path / "Wiki" / "Flags"
    folder.mkdir(parents=True)
    (folder / "try-planning-reminders.md").write_text(_FLAG_PAGE, encoding="utf-8")
    doors, _, _ = _doors(tmp_path)
    await doors.decide_page("research", "flag.try-planning-reminders", links={"resolved_by": ("q.old",)})

    said = await doors.decide_page("research", "flag.try-planning-reminders", links={"resolved_by": ("q.new", "q.old")}, add=True)

    assert said == "flag.try-planning-reminders (flag) stays pending in research, indexed clean, resolved_by now holds q.old, q.new"
    assert 'resolved_by: ["[[q.old]]", "[[q.new]]"]' in (folder / "try-planning-reminders.md").read_text(encoding="utf-8")


async def test_decide_page_reports_the_landed_decision_separately_from_validation_errors_elsewhere(tmp_path: Path) -> None:
    folder = tmp_path / "Wiki" / "Flags"
    folder.mkdir(parents=True)
    (folder / "try-planning-reminders.md").write_text(_FLAG_PAGE, encoding="utf-8")
    doors, reindex, _ = _doors(tmp_path)
    reindex.outcome = "validation_errors"
    reindex.findings = (ReindexFinding(page_id="paper.one", rule="missing-quote", message="claim quote absent from its mirror"),)
    reindex.findings_total = 1

    said = await doors.decide_page("research", "flag.try-planning-reminders", "dismissed")

    assert said == (
        "flag.try-planning-reminders (flag) is now dismissed in research, "
        "indexed with 1 validation error on other pages, paper.one (missing-quote)"
    )


async def test_decide_page_reports_a_failed_compile_as_its_own_outcome(tmp_path: Path) -> None:
    folder = tmp_path / "Wiki" / "Flags"
    folder.mkdir(parents=True)
    (folder / "try-planning-reminders.md").write_text(_FLAG_PAGE, encoding="utf-8")
    doors, reindex, _ = _doors(tmp_path)
    reindex.outcome = "not_indexed"
    reindex.detail = "studio index for research failed with exit 2"

    said = await doors.decide_page("research", "flag.try-planning-reminders", "dismissed")

    assert said == (
        "flag.try-planning-reminders (flag) is now dismissed in research, "
        "the knowledge base did not index (studio index for research failed with exit 2)"
    )


async def test_decide_page_completes_a_flag(tmp_path: Path) -> None:
    folder = tmp_path / "Wiki" / "Flags"
    folder.mkdir(parents=True)
    (folder / "try-planning-reminders.md").write_text(_FLAG_PAGE, encoding="utf-8")
    doors, _, _ = _doors(tmp_path)

    said = await doors.decide_page("research", "flag.try-planning-reminders", "completed")

    assert said == "flag.try-planning-reminders (flag) is now completed in research, indexed clean"
    assert "status: completed" in (folder / "try-planning-reminders.md").read_text(encoding="utf-8")


async def test_decide_page_refuses_a_status_outside_the_kind_status_set(tmp_path: Path) -> None:
    folder = tmp_path / "Wiki" / "Flags"
    folder.mkdir(parents=True)
    (folder / "try-planning-reminders.md").write_text(_FLAG_PAGE, encoding="utf-8")
    doors, _, _ = _doors(tmp_path)

    with pytest.raises(ModelRetry, match="not-a-status"):
        await doors.decide_page("research", "flag.try-planning-reminders", "not-a-status")


async def test_decide_page_refuses_a_link_field_outside_the_kind_edge_fields(tmp_path: Path) -> None:
    folder = tmp_path / "Wiki" / "Flags"
    folder.mkdir(parents=True)
    (folder / "try-planning-reminders.md").write_text(_FLAG_PAGE, encoding="utf-8")
    doors, _, _ = _doors(tmp_path)

    with pytest.raises(ModelRetry, match="not_a_field"):
        await doors.decide_page("research", "flag.try-planning-reminders", "accepted", links={"not_a_field": ("q.x",)})


async def test_decide_page_with_a_reason_notes_it_in_the_reply_and_the_file(tmp_path: Path) -> None:
    folder = tmp_path / "Wiki" / "Flags"
    folder.mkdir(parents=True)
    (folder / "try-planning-reminders.md").write_text(_FLAG_PAGE, encoding="utf-8")
    doors, _, _ = _doors(tmp_path)

    said = await doors.decide_page("research", "flag.try-planning-reminders", "completed", reason="landed in the sprint")

    assert said == "flag.try-planning-reminders (flag) is now completed in research, indexed clean, reason noted"
    text = (folder / "try-planning-reminders.md").read_text(encoding="utf-8")
    assert "decision_reason: landed in the sprint" in text


async def test_decide_page_with_no_reason_does_not_append_reason_noted(tmp_path: Path) -> None:
    folder = tmp_path / "Wiki" / "Flags"
    folder.mkdir(parents=True)
    (folder / "try-planning-reminders.md").write_text(_FLAG_PAGE, encoding="utf-8")
    doors, _, _ = _doors(tmp_path)

    said = await doors.decide_page("research", "flag.try-planning-reminders", "accepted")

    assert said == "flag.try-planning-reminders (flag) is now accepted in research, indexed clean"
    assert "reason noted" not in said


async def test_decide_page_writes_the_resolving_page_as_a_link(tmp_path: Path) -> None:
    folder = tmp_path / "Wiki" / "Flags"
    folder.mkdir(parents=True)
    (folder / "try-planning-reminders.md").write_text(_FLAG_PAGE, encoding="utf-8")
    doors, _, _ = _doors(tmp_path)

    said = await doors.decide_page(
        "research", "flag.try-planning-reminders", "completed", links={"resolved_by": ("q.planning-reminders",)}
    )

    assert said == (
        "flag.try-planning-reminders (flag) is now completed in research, indexed clean, resolved_by: q.planning-reminders"
    )
    assert 'resolved_by: ["[[q.planning-reminders]]"]' in (folder / "try-planning-reminders.md").read_text(encoding="utf-8")
