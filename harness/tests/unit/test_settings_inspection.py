"""The inspection section: the watches, the tool facts and the cadences this deployment ships with."""

import pytest

from core.ai.inspection.rules import RULES
from pydantic import ValidationError

from harness.settings import HarnessSettings, InspectionSettings


def test_the_committed_defaults_watch_every_run_with_every_rule_and_the_inspector_where_a_rule_led() -> None:
    section = HarnessSettings().inspection

    (watch,) = section.watches
    assert watch.name == "every-run"
    assert watch.when == "run_end"
    assert watch.inspector == "when_a_rule_led"
    assert set(watch.rules) == set(RULES.by_key)
    assert len(RULES.by_key) == 7
    assert watch.clean_run_sample_rate == 0.1
    assert watch.reconcile_hours == 24
    assert section.inspector_tiers == ("helper", "worker")
    assert (section.dispatch_seconds, section.sweep_seconds, section.batch) == (15, 3600, 200)


def test_the_facts_are_what_the_rules_take() -> None:
    section = InspectionSettings(world_changing=("write_note",), result_cap_chars=20_000)

    facts = section.facts()

    assert facts.world_changing == frozenset({"write_note"})
    assert facts.result_cap_chars == 20_000


def test_an_environment_variable_overrides_one_leaf(monkeypatch) -> None:
    monkeypatch.setenv("APP_INSPECTION__DISPATCH_SECONDS", "5")

    assert HarnessSettings().inspection.dispatch_seconds == 5


def test_an_empty_inspector_tiers_is_refused() -> None:
    with pytest.raises(ValidationError):
        InspectionSettings(inspector_tiers=())


def test_lane_polling_can_be_configured_and_cannot_busy_spin(monkeypatch) -> None:
    monkeypatch.setenv("APP_INSPECTION__JOB_POLL_SECONDS", "0.25")

    assert HarnessSettings().inspection.job_poll_seconds == 0.25
    with pytest.raises(ValidationError):
        InspectionSettings(job_poll_seconds=0)
