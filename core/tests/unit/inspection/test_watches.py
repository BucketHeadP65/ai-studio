"""A watch: which runs, which rules, whether the inspector, when."""

import pytest

from pydantic import ValidationError

from core.ai.inspection.domain.watches import Watch


def test_a_watch_on_an_agent_selects_that_agents_runs_in_any_session() -> None:
    watch = Watch(name="author", agent="harness-author", rules=("duplicate_call",))

    assert watch.selects(agent_name="harness-author", conversation_id="s1")
    assert not watch.selects(agent_name="other", conversation_id="s1")


def test_a_watch_on_a_session_selects_that_sessions_runs_whatever_the_agent() -> None:
    watch = Watch(name="live", session="s1", rules=("duplicate_call",))

    assert watch.selects(agent_name="anyone", conversation_id="s1")
    assert not watch.selects(agent_name="anyone", conversation_id="s2")


def test_a_watch_naming_nothing_selects_everything() -> None:
    assert Watch(name="all", rules=("duplicate_call",)).selects(agent_name=None, conversation_id=None)


def test_a_sweep_watch_needs_its_window_and_an_event_watch_refuses_one() -> None:
    with pytest.raises(ValidationError, match="sweep_hours"):
        Watch(name="nightly", rules=("duplicate_call",), when="sweep")
    with pytest.raises(ValidationError, match="sweep_hours"):
        Watch(name="live", rules=("duplicate_call",), when="run_end", sweep_hours=24)


def test_a_watch_with_no_rule_is_refused_unless_the_inspector_reads_every_run() -> None:
    assert Watch(name="scan-only", inspector="every_run").rules == ()
    with pytest.raises(ValidationError, match="only inspector: every_run"):
        Watch(name="empty", inspector="when_a_rule_led")
    with pytest.raises(ValidationError, match="only inspector: every_run"):
        Watch(name="empty", inspector="never")


def test_the_inspector_reads_where_a_rule_led_unless_the_watch_says_otherwise() -> None:
    assert Watch(name="author", agent="harness-author", rules=("duplicate_call",)).inspector == "when_a_rule_led"


def test_clean_run_sampling_is_stable_across_watch_reload_and_has_both_outcomes() -> None:
    watch = Watch(name="author", rules=("duplicate_call",))
    sources = [f"record:run-{index}" for index in range(100)]

    first = [source for source in sources if watch.samples(source)]
    reloaded = Watch.model_validate_json(watch.model_dump_json())

    assert 0 < len(first) < len(sources)
    assert [source for source in reversed(sources) if reloaded.samples(source)] == list(reversed(first))


@pytest.mark.parametrize("rate", [-0.1, 1.1])
def test_clean_run_sampling_rejects_rates_outside_a_fraction(rate: float) -> None:
    with pytest.raises(ValidationError):
        Watch(name="author", rules=("duplicate_call",), clean_run_sample_rate=rate)
