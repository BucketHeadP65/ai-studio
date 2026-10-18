"""The `notify_owner` step: says how many pages await review, and posts a notification when any do.

`RecordingRunner` stands in for the real `osascript` process, so the step's own decisions --
when to notify, what to say when the channel fails or does not exist -- are testable with no
process ever spawned. `RaisingRunner` and `RaisingNotifier` stand in for the process or the
channel breaking outright, rather than merely declining to deliver.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai.models.test import TestModel

from harness.adapters.notify import MacNotifier, NoopNotifier
from harness.adapters.steps import notify_owner_step
from harness.composition import build_environment
from harness.session import NoSessionLog
from harness.settings import WebSearchSettings
from tests.support.environment import fake_engram
from tests.support.state import records_under
from tests.support.tiers import tiers_under

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class RecordingRunner:
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.argv: list[Sequence[str]] = []

    async def __call__(self, argv: Sequence[str]) -> int:
        self.argv.append(argv)
        return self.exit_code


class RaisingRunner:
    """A `CommandRunner` that cannot start the process at all, not just exit non-zero."""

    async def __call__(self, argv: Sequence[str]) -> int:
        msg = "osascript: no such file or directory"
        raise OSError(msg)


class RaisingNotifier:
    """A `Notifier` whose channel breaks outright, not just declines to deliver."""

    async def notify(self, title: str, message: str) -> bool:
        msg = "notification center unavailable"
        raise RuntimeError(msg)


async def test_the_step_posts_one_notification_naming_the_base_and_the_count() -> None:
    runner = RecordingRunner()
    step = notify_owner_step(MacNotifier(run=runner))

    said = await step("harness-research", 3)

    assert said == "3 pages awaiting review in harness-research, notified"
    assert len(runner.argv) == 1
    script = runner.argv[0][-1]
    assert "3 pages awaiting review" in script
    assert "harness-research" in script


async def test_a_quote_and_a_backslash_in_the_title_and_message_are_escaped() -> None:
    """Neither half of the constructed script can break out of its AppleScript string literal."""
    runner = RecordingRunner()
    await MacNotifier(run=runner).notify('say "hi" \\ there', 'flag "count" \\ here')
    script = runner.argv[0][-1]
    assert 'say \\"hi\\" \\\\ there' in script
    assert 'flag \\"count\\" \\\\ here' in script


async def test_zero_pages_posts_nothing() -> None:
    runner = RecordingRunner()
    said = await notify_owner_step(MacNotifier(run=runner))("harness-research", 0)
    assert said == "0 pages awaiting review in harness-research"
    assert runner.argv == []


async def test_a_repair_that_needs_the_owner_notifies_with_no_pages_written() -> None:
    runner = RecordingRunner()
    said = await notify_owner_step(MacNotifier(run=runner))("research", 0, 1)
    assert said == "0 pages awaiting review in research, 1 repair needs you, notified"
    assert len(runner.argv) == 1
    assert "1 repair needs you" in runner.argv[0][-1]


async def test_several_repairs_read_in_the_plural() -> None:
    said = await notify_owner_step(NoopNotifier())("research", 2, 3)
    assert said == "2 pages awaiting review in research, 3 repairs need you, no notifier on this host"


async def test_no_page_and_no_repair_posts_nothing() -> None:
    runner = RecordingRunner()
    said = await notify_owner_step(MacNotifier(run=runner))("research", 0, 0)
    assert said == "0 pages awaiting review in research"
    assert runner.argv == []


async def test_a_failed_osascript_is_said_not_raised() -> None:
    said = await notify_owner_step(MacNotifier(run=RecordingRunner(exit_code=1)))("research", 2)
    assert said == "2 pages awaiting review in research, notification failed"


async def test_a_runner_that_cannot_start_yields_false_not_an_exception() -> None:
    """`osascript` missing, or any other `OSError` starting it, is a declined delivery, not a crash."""
    assert await MacNotifier(run=RaisingRunner()).notify("title", "message") is False


async def test_a_notifier_that_raises_is_said_not_raised_by_the_step() -> None:
    """The step's own guard: whatever a `Notifier` does, the step that called it never sees an exception."""
    said = await notify_owner_step(RaisingNotifier())("research", 2)
    assert said == "2 pages awaiting review in research, notification failed: notification center unavailable"


async def test_the_noop_notifier_says_it_did_not_notify() -> None:
    assert await notify_owner_step(NoopNotifier())("research", 2) == "2 pages awaiting review in research, no notifier on this host"


async def test_build_world_registers_the_step_with_its_real_contract(tmp_path: Path) -> None:
    registry = await build_environment(TestModel(), engram=fake_engram(), notifier=NoopNotifier(), external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))
    step = registry.get("notify_owner")
    assert step is not None
    assert step.input_types == {"collection": str, "written": int, "needs_you": int}
    assert step.required == frozenset({"collection", "written"})
    assert step.output_type is str


async def test_build_world_with_no_notifier_falls_back_to_the_no_channel_reply(tmp_path: Path) -> None:
    """`notifier=None` (every host that never passes one) is a `NoopNotifier`, not a refusal to register."""
    registry = await build_environment(TestModel(), engram=fake_engram(), external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))
    step = registry.get("notify_owner")
    assert step is not None
    said = await step.function(collection="harness-research", written=2)
    assert said == "2 pages awaiting review in harness-research, no notifier on this host"
