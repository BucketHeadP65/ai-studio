"""The one-time rewrite of the session state written under the old Sessions words.

Five things have to hold: the file takes its new name with the renamed fields inside it, a screen
act is dropped, the run lifecycle lands in the run's own log with only the launch left on the
conversation's timeline, every kind is accounted for before and after, and a second run finds
nothing to do.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING, Any

import pytest

from harness.domain.events import decode
from harness.migrations import rewrite_session_words
from harness.settings import StateSettings

if TYPE_CHECKING:
    from pathlib import Path

LINES: list[dict[str, Any]] = [
    {"seq": 1, "at": "2026-08-23T13:50:00+00:00", "kind": "session_started", "session_id": "s-1", "session_kind": "chat"},
    {"seq": 2, "at": "2026-08-23T13:50:34+00:00", "kind": "door_set", "door": "sql", "enabled": True},
    {"seq": 3, "at": "2026-08-23T13:51:00+00:00", "kind": "turn_started", "door": "spa", "chars": 12, "session_id": "s-1"},
    {"seq": 4, "at": "2026-08-23T13:51:30+00:00", "kind": "session_switched", "session_id": "s-1"},
    {"seq": 5, "at": "2026-08-23T16:12:11+00:00", "kind": "run_launched", "workflow_id": "wf.gate-check.v1"},
    {"seq": 6, "at": "2026-08-23T16:12:20+00:00", "kind": "run_paused", "workflow_id": "wf.gate-check.v1"},
    {"seq": 7, "at": "2026-08-23T16:12:30+00:00", "kind": "run_resumed", "workflow_id": "wf.gate-check.v1"},
    {"seq": 8, "at": "2026-08-23T16:12:39+00:00", "kind": "gate_decided", "gate_id": "approve", "approved": True},
    {"seq": 9, "at": "2026-08-23T16:12:39+00:00", "kind": "run_finished", "workflow_id": "wf.gate-check.v1", "status": "completed"},
    {"seq": 10, "at": "2026-08-23T16:13:00+00:00", "kind": "turn_settled", "run_id": "harness-author-abc", "started_seq": 3},
]
"""One of every line this migration has an opinion about, in the order a real log holds them."""


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A state home under `tmp_path`, so every path this migration resolves lands there."""
    home = tmp_path / ".state"
    monkeypatch.setattr(rewrite_session_words, "CHECKOUT", tmp_path)
    monkeypatch.delenv("APP_AUTHORED_ROOT", raising=False)
    home.mkdir(parents=True)
    return home


def _migrate(home: Path) -> rewrite_session_words.Report:
    """The migration, over the state home at `home` and a custom root beside it."""
    return rewrite_session_words.migrate(StateSettings(home=home), home.parent / "custom")


def _seed(home: Path, payloads: list[dict[str, Any]]) -> Path:
    path = home / "session-journal.jsonl"
    path.write_text("".join(json.dumps(payload) + "\n" for payload in payloads), encoding="utf-8")
    return path


def _kept(home: Path) -> list[dict[str, Any]]:
    text = (home / "session-log.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_the_file_takes_its_new_name_and_the_old_one_is_gone(state: Path) -> None:
    _seed(state, LINES)

    _migrate(state)

    assert (state / "session-log.jsonl").exists()
    assert not (state / "session-journal.jsonl").exists()


def test_a_plugin_flip_and_a_turns_entry_read_the_new_words(state: Path) -> None:
    _seed(state, LINES)

    _migrate(state)

    kept = _kept(state)
    flipped = next(line for line in kept if line["kind"] == "plugin_set")
    assert flipped["plugin"] == "sql"
    assert "door" not in flipped
    started = next(line for line in kept if line["kind"] == "turn_started")
    assert started["entry"] == "spa"
    assert "door" not in started


def test_every_kept_line_parses_under_todays_grammar(state: Path) -> None:
    _seed(state, LINES)

    _migrate(state)

    for line in (state / "session-log.jsonl").read_text(encoding="utf-8").splitlines():
        assert decode(line) is not None, line


def test_a_session_switch_is_dropped(state: Path) -> None:
    _seed(state, LINES)

    report = _migrate(state)

    assert report.dropped == 1
    assert not any(line["kind"] == "session_switched" for line in _kept(state))


def test_the_run_lifecycle_moves_and_only_the_launch_stays(state: Path) -> None:
    _seed(state, LINES)

    report = _migrate(state)

    kept_kinds = [line["kind"] for line in _kept(state)]
    assert kept_kinds == ["session_started", "plugin_set", "turn_started", "run_launched", "turn_settled"]
    assert list(report.moved.values()) == [4], "the pause, the resume, the decision and the finish"
    run_id = next(iter(report.moved))
    logged = [json.loads(line) for line in (state / "runs" / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [entry["kind"] for entry in logged] == ["run_launched", "run_paused", "run_resumed", "gate_decided", "run_finished"]
    assert [entry["seq"] for entry in logged] == [1, 2, 3, 4, 5]
    assert {entry["run_id"] for entry in logged} == {run_id}


def test_the_launch_that_stays_names_the_run_its_own_log_is_filed_under(state: Path) -> None:
    _seed(state, LINES)

    report = _migrate(state)

    launched = next(line for line in _kept(state) if line["kind"] == "run_launched")
    assert launched["run_id"] == next(iter(report.moved))


def test_the_counts_account_for_every_line_before_and_after(state: Path) -> None:
    _seed(state, LINES)

    report = _migrate(state)

    assert sum(report.before.values()) == len(LINES)
    assert sum(report.after.values()) + sum(report.moved.values()) + report.dropped == len(LINES)


def test_a_lifecycle_line_with_no_launch_before_it_still_lands_somewhere(state: Path) -> None:
    _seed(state, [LINES[8]])

    report = _migrate(state)

    assert list(report.moved.values()) == [1]
    assert _kept(state) == []


def test_the_plugins_file_and_the_projects_file_are_renamed_untouched(state: Path) -> None:
    (state / "disabled-doors.json").write_text('["recall", "sql"]', encoding="utf-8")
    (state / "projects-journal.jsonl").write_text('{"at": "2026-08-23T05:27:57+00:00", "kind": "project_registered"}\n', encoding="utf-8")

    report = _migrate(state)

    assert (state / "disabled-plugins.json").read_text(encoding="utf-8") == '["recall", "sql"]'
    assert (state / "projects-log.jsonl").exists()
    assert not (state / "disabled-doors.json").exists()
    assert report.files == ["disabled-doors.json -> disabled-plugins.json", "projects-journal.jsonl -> projects-log.jsonl"]


def test_the_activation_records_take_the_past_tense_names(state: Path) -> None:
    (state / "capability-activations.jsonl").write_text(
        json.dumps({"kind": "leaf_activation", "name": "double", "action": "activate", "door": "cli", "at": "2026-08-30T10:00:00Z"})
        + "\n"
        + json.dumps({"kind": "capability_promotion", "name": "marker", "action": "activate", "door": "cli", "at": "2026-08-30T10:01:00Z"})
        + "\n",
        encoding="utf-8",
    )

    report = _migrate(state)

    written = [json.loads(line) for line in (state / "activations.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [record["kind"] for record in written] == ["step_activated", "capability_promoted"]
    assert [record["via"] for record in written] == ["cli", "cli"]
    assert report.activations == 2


def test_the_whole_migration_moves_the_custom_root_and_rewrites_its_profiles(state: Path, tmp_path: Path) -> None:
    _seed(state, LINES)
    profiles = tmp_path / "authored" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "researcher.yaml").write_text("doors: [knowledge-base]\n", encoding="utf-8")
    (state / "selected-profile.json").write_text(json.dumps({"name": "researcher", "doors": ["knowledge-base"]}), encoding="utf-8")

    report = _migrate(state)

    assert (tmp_path / "custom" / "profiles" / "researcher.yaml").read_text(encoding="utf-8") == "plugins: [knowledge-base]\n"
    assert json.loads((state / "selected-profile.json").read_text(encoding="utf-8"))["plugins"] == ["knowledge-base"]
    assert report.custom_items == 1
    assert report.documents == 2, "the profile document and the selection snapshot"


def test_a_second_run_finds_nothing_to_do(state: Path) -> None:
    _seed(state, LINES)
    _migrate(state)

    again = _migrate(state)

    assert not again.touched
    assert again.lines() == ["nothing to migrate: every file and every line already reads the new words"]


def test_a_torn_line_is_reported_and_never_raised(state: Path) -> None:
    path = _seed(state, LINES)
    path.write_text(path.read_text(encoding="utf-8") + '{"seq": 11, "at": "2026-08-23', encoding="utf-8")

    report = _migrate(state)

    assert not report.clean
    assert report.refused == ["session-journal.jsonl:11: not JSON"]


def test_a_registered_projects_own_session_log_is_migrated_too(state: Path, tmp_path: Path) -> None:
    project = tmp_path / "client-repo"
    home = project / ".aifactory" / "state"
    home.mkdir(parents=True)
    _seed(home, LINES)
    (state / "projects-log.jsonl").write_text(
        json.dumps({"at": "2026-08-23T05:27:57+00:00", "kind": "project_registered", "project_id": "p-1", "root_path": str(project)}) + "\n",
        encoding="utf-8",
    )

    _migrate(state)

    assert (home / "session-log.jsonl").exists()
    assert not (home / "session-journal.jsonl").exists()


def test_an_unregistered_project_is_left_alone(state: Path, tmp_path: Path) -> None:
    project = tmp_path / "gone-repo"
    home = project / ".aifactory" / "state"
    home.mkdir(parents=True)
    _seed(home, LINES)
    (state / "projects-log.jsonl").write_text(
        json.dumps({"at": "2026-08-23T05:27:57+00:00", "kind": "project_registered", "project_id": "p-1", "root_path": str(project)})
        + "\n"
        + json.dumps({"at": "2026-08-27T19:29:11+00:00", "kind": "project_unregistered", "project_id": "p-1"})
        + "\n",
        encoding="utf-8",
    )

    _migrate(state)

    assert (home / "session-journal.jsonl").exists(), "a root the registry no longer names is nobody's to rewrite"


def test_the_custom_root_is_moved_whole_with_leaves_renamed_steps(tmp_path: Path) -> None:
    old, new = tmp_path / "authored", tmp_path / "custom"
    (old / "capabilities").mkdir(parents=True)
    (old / "leaves").mkdir()
    (old / "agents").mkdir()
    (old / "capabilities" / "marker.py").write_text("# a capability\n", encoding="utf-8")
    (old / "capabilities" / "manifest.json").write_text("{}", encoding="utf-8")
    (old / "leaves" / "double.py").write_text("# a step\n", encoding="utf-8")
    (old / "agents" / "reviewer.yaml").write_text("model: flash\n", encoding="utf-8")
    report = rewrite_session_words.Report()

    rewrite_session_words.migrate_custom_root(old, new, report=report)

    assert not old.exists()
    assert (new / "steps" / "double.py").read_text(encoding="utf-8") == "# a step\n"
    assert (new / "capabilities" / "marker.py").exists()
    assert not (new / "leaves").exists()
    assert report.custom_items == 3, "a manifest is not a custom item"
    assert report.files == [f"{old} -> {new}, 3 custom item(s)"]


def test_moving_the_custom_root_a_second_time_does_nothing(tmp_path: Path) -> None:
    old, new = tmp_path / "authored", tmp_path / "custom"
    (old / "agents").mkdir(parents=True)
    (old / "agents" / "reviewer.yaml").write_text("model: flash\n", encoding="utf-8")
    rewrite_session_words.migrate_custom_root(old, new, report=rewrite_session_words.Report())

    again = rewrite_session_words.Report()
    rewrite_session_words.migrate_custom_root(old, new, report=again)

    assert not again.touched
    assert (new / "agents" / "reviewer.yaml").exists()


def test_a_live_steps_manifest_takes_the_steps_key_and_keeps_its_entries(tmp_path: Path) -> None:
    old, new = tmp_path / "authored", tmp_path / "custom"
    (old / "leaves").mkdir(parents=True)
    (old / "leaves" / "double.py").write_text("# a step\n", encoding="utf-8")
    (old / "leaves" / "manifest.json").write_text(
        json.dumps({"leaves": [{"name": "double", "module_file": "double.py", "function_names": ["double"], "status": "active"}]}),
        encoding="utf-8",
    )
    report = rewrite_session_words.Report()

    rewrite_session_words.migrate_custom_root(old, new, report=report)

    manifest = json.loads((new / "steps" / "manifest.json").read_text(encoding="utf-8"))
    assert "leaves" not in manifest
    assert manifest["steps"][0]["name"] == "double"
    assert f"{new / 'steps' / 'manifest.json'}: leaves -> steps" in report.files


def test_a_steps_manifest_already_moved_still_takes_the_steps_key(tmp_path: Path) -> None:
    steps = tmp_path / "custom" / "steps"
    steps.mkdir(parents=True)
    (steps / "manifest.json").write_text(json.dumps({"leaves": [{"name": "double"}]}), encoding="utf-8")
    report = rewrite_session_words.Report()

    rewrite_session_words.migrate_custom_root(tmp_path / "custom", tmp_path / "custom", report=report)

    manifest = json.loads((steps / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["steps"][0]["name"] == "double"


def test_a_steps_manifest_already_on_the_steps_key_is_left_alone(tmp_path: Path) -> None:
    steps = tmp_path / "custom" / "steps"
    steps.mkdir(parents=True)
    manifest_path = steps / "manifest.json"
    manifest_path.write_text(json.dumps({"steps": [{"name": "double"}]}), encoding="utf-8")
    report = rewrite_session_words.Report()

    rewrite_session_words.migrate_step_manifest_key(manifest_path, report=report)

    assert not report.files
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {"steps": [{"name": "double"}]}


def test_a_profile_document_takes_the_plugins_key_and_nothing_else_moves(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    document = "# the research profile\ndescription: Retrieval work only.\ndoors: [knowledge-base, leveled-reads]\ninstructions: |\n  doors: not a key\n"
    (profiles / "researcher.yaml").write_text(document, encoding="utf-8")
    (profiles / "worker.yaml").write_text("plugins: [knowledge-base]\n", encoding="utf-8")
    report = rewrite_session_words.Report()

    rewrite_session_words.migrate_profile_documents(profiles, report=report)

    assert (profiles / "researcher.yaml").read_text(encoding="utf-8") == document.replace("doors: [knowledge-base", "plugins: [knowledge-base")
    assert "  doors: not a key\n" in (profiles / "researcher.yaml").read_text(encoding="utf-8"), "only a top-level key is a key"
    assert (profiles / "worker.yaml").read_text(encoding="utf-8") == "plugins: [knowledge-base]\n"
    assert report.documents == 1


def test_the_selected_profile_snapshot_takes_the_plugins_key(tmp_path: Path) -> None:
    path = tmp_path / "selected-profile.json"
    path.write_text(json.dumps({"name": "researcher", "doors": ["knowledge-base"], "instructions": "stay put", "skills": []}), encoding="utf-8")
    report = rewrite_session_words.Report()

    rewrite_session_words.migrate_selected_profile(path, report=report)

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "name": "researcher",
        "plugins": ["knowledge-base"],
        "instructions": "stay put",
        "skills": [],
    }
    assert report.documents == 1


def test_rewriting_the_profile_documents_twice_changes_nothing(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "researcher.yaml").write_text("doors: [knowledge-base]\n", encoding="utf-8")
    selection = tmp_path / "selected-profile.json"
    selection.write_text(json.dumps({"name": "researcher", "doors": ["knowledge-base"]}), encoding="utf-8")
    rewrite_session_words.migrate_profile_documents(profiles, report=rewrite_session_words.Report())
    rewrite_session_words.migrate_selected_profile(selection, report=rewrite_session_words.Report())

    again = rewrite_session_words.Report()
    rewrite_session_words.migrate_profile_documents(profiles, report=again)
    rewrite_session_words.migrate_selected_profile(selection, report=again)

    assert again.documents == 0
    assert (profiles / "researcher.yaml").read_text(encoding="utf-8") == "plugins: [knowledge-base]\n"


def test_a_snapshot_that_is_not_json_is_reported_and_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "selected-profile.json"
    path.write_text("{torn", encoding="utf-8")
    report = rewrite_session_words.Report()

    rewrite_session_words.migrate_selected_profile(path, report=report)

    assert report.refused == ["selected-profile.json: not JSON"]
    assert path.read_text(encoding="utf-8") == "{torn"
