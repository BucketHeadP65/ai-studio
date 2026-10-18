"""The dashboard's watcher: why it logs transitions instead of only reporting state.

`LiveProgress` is a `RunObserver`, so the indexer tells it exactly when each node starts and
finishes. The browser, however, *samples* -- and a sampler that only ever asks "what is running
now" cannot see a step whose entire life fell between two samples. Its box goes straight from
idle to done and a person watching learns nothing about it.

That is not fixable by polling faster. Whatever the interval, there is a step quick enough to
hide inside it; shortening it only makes the gap rarer and the requests more numerous. So these
tests pin the property that actually closes it: **a reader with a cursor never misses a
transition, whenever it happens to look.**
"""

from __future__ import annotations

from harness.features.running.domain.progress import LiveProgress, StepEvent


def _nodes(events: list[StepEvent]) -> list[tuple[str, bool]]:
    return [(event["node"], event["finished"]) for event in events]


def test_a_step_too_fast_to_be_sampled_is_still_reported() -> None:
    """The whole point. Both transitions land between two polls, and both survive to the reader.

    The snapshot fields agree with each other and are simply insufficient on their own: by the
    time anyone looks, `running` is empty and `done` says it finished -- true, and silent about
    the fact that it ever ran.
    """
    progress = LiveProgress()
    first = progress.since(0)  # the watcher's first poll: nothing has happened yet
    assert first == []

    progress.started("report")  # ... a step lives and dies entirely inside the gap ...
    progress.finished("report")

    second = progress.since(len(first))
    assert _nodes(second) == [("report", False), ("report", True)]
    assert progress.running == set()  # which is exactly why the snapshot alone could not say so
    assert progress.done == ["report"]


def test_a_cursor_never_replays_and_never_skips() -> None:
    """Sequential reads partition the history: every transition returned exactly once."""
    progress = LiveProgress()
    progress.started("investigate")
    seen = len(progress.since(0))
    progress.started("orient")
    progress.finished("investigate")
    batch = progress.since(seen)

    assert _nodes(batch) == [("orient", False), ("investigate", True)]
    assert progress.since(seen + len(batch)) == []  # caught up: nothing repeated
    assert [event["seq"] for event in progress.events] == [1, 2, 3]


def test_a_cursor_out_of_range_is_not_answered_with_the_wrong_events() -> None:
    """`events[-1:]` is a legal slice, which is exactly what makes a negative cursor dangerous.

    Found by reading a diagnostic loop of my own that lost its cursor and started sending
    nonsense: unguarded, `since(-1)` returns the newest transition as though it were new, so a
    watcher would repaint a finished step on every poll and never stop.
    """
    progress = LiveProgress()
    progress.started("investigate")
    progress.finished("investigate")

    assert _nodes(progress.since(-1)) == [("investigate", False), ("investigate", True)]  # treated as 0
    assert progress.since(99) == []  # past the end is genuinely nothing new, not an error


def test_asking_from_zero_replays_the_whole_run() -> None:
    """What a page reloaded mid-run needs: the history, not just the moment it arrived."""
    progress = LiveProgress()
    for node in ("investigate", "orient"):
        progress.started(node)
        progress.finished(node)

    assert len(progress.since(0)) == 4
    assert _nodes(progress.since(0))[0] == ("investigate", False)


def test_every_event_is_timed_from_the_runs_own_start() -> None:
    """Durations come from the machinery's clock, so the screen cannot inflate a fast step."""
    progress = LiveProgress()
    progress.started("compose")
    progress.finished("compose")

    at = [event["at"] for event in progress.events]
    assert at[0] == 0.0  # the first transition defines the baseline
    assert at[1] >= at[0]  # monotonic, so a duration is never negative


def test_a_rerun_after_a_rejection_is_visible_in_the_log_but_not_duplicated_in_done() -> None:
    """A rejected gate reruns its upstream, and the two views disagree on purpose.

    `done` is a set of conclusions -- the step ended, once, in the state it ended in. The log is
    history, and the second run of a step is a real event: it is how a watcher shows the rewind
    happening rather than the graph appearing to stall.
    """
    progress = LiveProgress()
    progress.started("investigate")
    progress.finished("investigate")
    progress.started("approve")
    progress.finished("approve")
    progress.started("investigate")  # the rejection sent us back
    progress.finished("investigate")

    assert progress.done == ["approve", "investigate"]  # last say wins, no duplicate
    assert _nodes(progress.since(0)).count(("investigate", False)) == 2  # both attempts recorded


def test_a_failure_is_recorded_as_a_failure_not_merely_as_finished() -> None:
    progress = LiveProgress()
    progress.started("publish")
    progress.finished("publish", failed=True)

    assert progress.failed == {"publish"}
    assert progress.events[-1]["failed"] is True


def test_reset_clears_the_history_with_everything_else() -> None:
    """A second run must not inherit the first one's transitions, or the graph replays a ghost."""
    progress = LiveProgress()
    progress.started("investigate")
    progress.finished("investigate")
    progress.reset()

    assert progress.since(0) == []
    assert progress.started_at is None
    assert progress.done == []
