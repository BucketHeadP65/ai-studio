"""Rewrite this deployment's session state into the Sessions words, and give each run its own log.

`migrate` rewrites the session log and each project's own, the projects file, the plugins file, the activation file, the
custom root, the profile documents and the selected profile.
The run lifecycle lines move to each run's own log under `.state/runs/<run_id>/events.jsonl`, and only the launch stays
in the session log.
A launch line with no run id gets one minted as `<stamp>-log<seq>` and stamped onto it.
Run it with `uv run python -m harness.migrations.rewrite_session_words`, or let `harness serve` run it at boot.
A second run finds nothing and says so.
"""

from __future__ import annotations

import json
import os

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from core.ai.workflows.contracts import (
    GateDecided,
    RecordedRunEvent,
    RunFinished,
    RunLaunched,
    RunPaused,
    RunResumed,
    RunStopped,
    encode_run_event,
)

from harness.migrations.report import MigrationReport
from harness.settings import CHECKOUT, HarnessSettings, StateSettings, aifactory_state, custom_root

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from core.ai.workflows.contracts import RunEvent

MOVED_KINDS = frozenset({"run_paused", "run_resumed", "run_stopped", "run_finished", "gate_decided"})
"""The `kind` values of the run lifecycle lines that leave the session log for the run's own log."""

DROPPED_KINDS = frozenset({"session_switched"})
"""The `kind` values of lines that record a screen act rather than a fact about the conversation. They are dropped."""

ACTIVATION_KINDS = {
    "capability_activation": "capability_activated",
    "leaf_activation": "step_activated",
    "agent_activation": "agent_activated",
    "profile_activation": "profile_activated",
    "capability_promotion": "capability_promoted",
}
"""Each activation record's old `kind` value, and the past-tense event it is now."""

PROJECTION_RENAMES: tuple[str, ...] = (
    """
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'turns' AND column_name = 'door') THEN
            ALTER TABLE turns RENAME COLUMN door TO entry;
        END IF;
    END $$
    """,
    """
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'flags' AND column_name = 'journal_seq') THEN
            ALTER TABLE flags RENAME COLUMN journal_seq TO log_seq;
        END IF;
    END $$
    """,
)
"""The two projected columns that carried an old word, each rename guarded so a re-run is a no-op."""


@dataclass
class Report(MigrationReport):
    """What one run of the migration did, in the order it did it."""

    logs: list[str] = field(default_factory=list)
    """One line per session log rewritten, with its line counts in and out."""
    before: dict[str, int] = field(default_factory=dict)
    """How many lines each `kind` value held across the session logs before the rewrite."""
    after: dict[str, int] = field(default_factory=dict)
    """How many lines each `kind` value holds across the session logs after the rewrite."""
    moved: dict[str, int] = field(default_factory=dict)
    """How many lines moved into each run's own log, by run id."""
    dropped: int = 0
    """How many lines were dropped as screen acts."""
    activations: int = 0
    """How many activation records were rewritten."""
    custom_items: int = 0
    """How many custom items the moved custom root holds."""
    documents: int = 0
    """How many profile documents were rewritten, the selected profile included."""

    @property
    def touched(self) -> bool:
        """Whether anything at all was rewritten."""
        return bool(self.files or self.logs or self.moved or self.dropped or self.activations or self.documents)

    def _counts(self) -> list[str]:
        """The line counts per `kind` value of the session log, before the rewrite and after it."""
        out: list[str] = []
        if self.before:
            out.append("lines per kind before:")
            out.extend(f"  {kind}: {count}" for kind, count in sorted(self.before.items()))
        if self.after:
            out.append("lines per kind after:")
            out.extend(f"  {kind}: {count}" for kind, count in sorted(self.after.items()))
        return out

    def lines(self) -> list[str]:
        """One line per thing rewritten, or the single line that says there was nothing to do.

        Returns:
            The lines to print, in order.
        """
        if not self.touched:
            return ["nothing to migrate: every file and every line already reads the new words"]
        out: list[str] = []
        if self.files:
            out.extend(["renamed:", *(f"  {name}" for name in self.files)])
        out.extend(f"rewrote {log}" for log in self.logs)
        out.extend(self._counts())
        if self.moved:
            out.append(f"moved {sum(self.moved.values())} run line(s) into {len(self.moved)} run log(s):")
            out.extend(f"  {run_id}: {count}" for run_id, count in sorted(self.moved.items()))
        if self.dropped:
            out.append(f"dropped {self.dropped} line(s) recording a screen act rather than a fact")
        if self.activations:
            out.append(f"rewrote {self.activations} activation record(s) into the past-tense event names")
        if self.custom_items:
            out.append(f"moved {self.custom_items} custom item(s) into the custom root")
        if self.documents:
            out.append(f"rewrote the plugin list of {self.documents} profile document(s)")
        if self.refused:
            out.extend(["left alone, unreadable:", *(f"  {why}" for why in self.refused)])
        return out


def _counted(payloads: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """How many lines each `kind` value holds."""
    counts: dict[str, int] = {}
    for payload in payloads:
        kind = str(payload.get("kind", "unknown"))
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _read_lines(path: Path, *, report: Report) -> list[dict[str, Any]]:
    """Every readable JSON line of `path`, in order. An unreadable one is reported and skipped."""
    payloads: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            report.refused.append(f"{path.name}:{number}: not JSON")
            continue
        if not isinstance(payload, dict):
            report.refused.append(f"{path.name}:{number}: not an object")
            continue
        payloads.append(payload)
    return payloads


def minted_run_id(payload: Mapping[str, Any]) -> str:
    """A run id for a launch line that carries none, derived from the line itself.

    It is deterministic, so a second run over the same line files under the same run. It is shaped like a live run id,
    so a listing sorts it chronologically.

    Args:
        payload: The launch line.

    Returns:
        `<stamp>-log<seq>`, with a zero stamp when the line's time does not parse.
    """
    seq = payload.get("seq")
    try:
        stamp = datetime.fromisoformat(str(payload.get("at", ""))).strftime("%Y%m%dT%H%M%S")
    except ValueError:
        stamp = "00000000T000000"
    return f"{stamp}-log{seq}"


def run_event_of(payload: Mapping[str, Any], *, run_id: str, workflow_id: str) -> RunEvent | None:  # noqa: PLR0911 (one return per run event type)
    """The run event one old session-log line becomes, or `None` for a line that is not a run event.

    Args:
        payload: The session-log line.
        run_id: The run the event belongs to.
        workflow_id: The workflow that run runs.

    Returns:
        The run event, or `None` when the line's `kind` names no run event.
    """
    match str(payload.get("kind", "")):
        case "run_launched":
            return RunLaunched(run_id=run_id, workflow_id=workflow_id)
        case "run_paused":
            return RunPaused(run_id=run_id, workflow_id=workflow_id)
        case "run_resumed":
            return RunResumed(run_id=run_id, workflow_id=workflow_id)
        case "run_stopped":
            return RunStopped(run_id=run_id, workflow_id=workflow_id)
        case "run_finished":
            return RunFinished(run_id=run_id, workflow_id=workflow_id, status=str(payload.get("status", "")))
        case "gate_decided":
            by = payload.get("by")
            return GateDecided(
                run_id=run_id,
                workflow_id=workflow_id,
                gate_id=str(payload.get("gate_id", "")),
                approved=bool(payload.get("approved")),
                by="orchestrator" if by == "orchestrator" else "human",
            )
        case _:
            return None


@dataclass
class _OpenRun:
    """The run the walk is currently inside: what to file the next lifecycle line under."""

    run_id: str
    workflow_id: str
    events: list[tuple[str, RunEvent]] = field(default_factory=list)
    """`(at, event)` pairs in log order. Each takes its seq when the run's own log is written."""
    launched: bool = False
    """Whether a launch line opened this run, `False` for a log that starts mid-run."""


def _write_run_log(state: StateSettings, run: _OpenRun, *, report: Report) -> None:
    """Write one run's own log under `state`'s home, unless a file is already there. Never appends to an existing one."""
    moved = len(run.events) - (1 if run.launched else 0)
    if moved <= 0:  # a launch with nothing after it is already whole in the session log
        return
    path = state.run_events(run.run_id)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    written = [
        encode_run_event(RecordedRunEvent(seq=seq, at=datetime.fromisoformat(at), event=event))
        for seq, (at, event) in enumerate(run.events, start=1)
    ]
    path.write_text("\n".join(written) + "\n", encoding="utf-8")
    report.moved[run.run_id] = moved


def renamed_line(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """One line with its renamed fields, or `None` when its `kind` carries no old word.

    A `turn_started` line's `door` becomes `entry`, and a `door_set` line becomes a `plugin_set` naming a `plugin`.

    Args:
        payload: The session-log line. It is never changed.

    Returns:
        A renamed copy of the line, or `None` when it needs no rename.
    """
    kind = str(payload.get("kind", ""))
    if kind == "turn_started" and "door" in payload:
        entry = dict(payload)
        entry["entry"] = entry.pop("door")
        return entry
    if kind == "door_set":
        flipped = dict(payload)
        flipped["kind"] = "plugin_set"
        flipped["plugin"] = flipped.pop("door")
        return flipped
    return None


def _opened(payload: Mapping[str, Any], previous: _OpenRun | None, *, state: StateSettings, report: Report) -> _OpenRun:
    """The run a launch line opens, closing whichever run the walk was still inside."""
    if previous is not None:
        _write_run_log(state, previous, report=report)
    run_id = minted_run_id(payload)
    workflow_id = str(payload.get("workflow_id", ""))
    run = _OpenRun(run_id=run_id, workflow_id=workflow_id)
    launched = run_event_of(payload, run_id=run_id, workflow_id=workflow_id)
    if launched is not None:
        run.events.append((str(payload.get("at", "")), launched))
        run.launched = True
    return run


def _credited(payload: Mapping[str, Any], open_run: _OpenRun | None, *, state: StateSettings, report: Report) -> _OpenRun | None:
    """`payload` filed on the open run or on a run of its own, and the run still open afterwards, or `None` once it ended."""
    run = open_run if open_run is not None else _OpenRun(run_id=minted_run_id(payload), workflow_id=str(payload.get("workflow_id", "")))
    workflow_id = run.workflow_id or str(payload.get("workflow_id", ""))
    event = run_event_of(payload, run_id=run.run_id, workflow_id=workflow_id)
    if event is None:  # pragma: no cover (every moved line has a run event)
        report.refused.append(f"seq {payload.get('seq')}: {payload.get('kind')} is not a run event")
        return run
    run.events.append((str(payload.get("at", "")), event))
    if str(payload.get("kind", "")) in {"run_stopped", "run_finished"}:
        _write_run_log(state, run, report=report)
        return None
    return run


def rewritten_session_log(payloads: Sequence[Mapping[str, Any]], *, state: StateSettings, report: Report) -> list[dict[str, Any]]:
    """The session log with the new words, the run lifecycle filed under its own runs, and screen acts dropped.

    A launch opens a run and takes the minted id. Every lifecycle line after it goes to that run's own log and is left out
    of the result, and a stop or finish line closes the run.

    Args:
        payloads: The session log's lines, in order.
        state: The state settings whose home holds each run's own log.
        report: The report this run adds to.

    Returns:
        The lines the session log keeps, in order.
    """
    kept: list[dict[str, Any]] = []
    open_run: _OpenRun | None = None
    for payload in payloads:
        kind = str(payload.get("kind", ""))
        if kind in DROPPED_KINDS:
            report.dropped += 1
            continue
        renamed = renamed_line(payload)
        if renamed is not None:
            kept.append(renamed)
            continue
        if kind == "run_launched":
            open_run = _opened(payload, open_run, state=state, report=report)
            kept.append({**payload, "run_id": open_run.run_id})
            continue
        if kind in MOVED_KINDS:
            open_run = _credited(payload, open_run, state=state, report=report)
            continue
        kept.append(dict(payload))
    if open_run is not None:
        _write_run_log(state, open_run, report=report)
    return kept


def migrate_session_log(old: Path, new: Path, *, state: StateSettings, report: Report) -> None:
    """Rewrite `old` into `new`, counting the lines of each `kind` value before and after.

    A no-op when `old` is missing or `new` already exists. `old` is deleted once `new` is written.

    Args:
        old: The session log under its old name.
        new: Where the rewritten session log goes.
        state: The state settings whose home holds each run's own log.
        report: The report this run adds to.
    """
    if not old.exists() or new.exists():
        return
    payloads = _read_lines(old, report=report)
    for kind, count in _counted(payloads).items():
        report.before[kind] = report.before.get(kind, 0) + count
    kept = rewritten_session_log(payloads, state=state, report=report)
    for kind, count in _counted(kept).items():
        report.after[kind] = report.after.get(kind, 0) + count
    new.parent.mkdir(parents=True, exist_ok=True)
    new.write_text("".join(json.dumps(payload) + "\n" for payload in kept), encoding="utf-8")
    old.unlink()
    report.logs.append(f"{old} -> {new.name}, {len(payloads)} line(s) in, {len(kept)} out")


def project_state_homes(projects_log: Path) -> list[Path]:
    """Every registered project's own state home, folded from the projects file's whole history.

    It reads the projects file, never the registry.

    Args:
        projects_log: The projects file.

    Returns:
        The state home of each project still registered, or an empty list when the file is missing.
    """
    if not projects_log.exists():
        return []
    live: dict[str, str] = {}
    for line in projects_log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        project_id = str(payload.get("project_id", ""))
        if payload.get("kind") == "project_registered":
            live[project_id] = str(payload.get("root_path", ""))
        elif payload.get("kind") == "project_unregistered":
            live.pop(project_id, None)
    return [aifactory_state(Path(root)) for root in live.values() if root]


def migrate_state_files(state: StateSettings, *, report: Report) -> None:
    """Rename the plugins file and the projects file under `state`'s home, contents untouched.

    Args:
        state: The state settings whose home holds the files.
        report: The report this run adds to.
    """
    home = state.home
    for old_name, new_name in (("disabled-doors.json", "disabled-plugins.json"), ("projects-journal.jsonl", "projects-log.jsonl")):
        old, new = home / old_name, home / new_name
        if old.exists() and not new.exists():
            old.rename(new)
            report.files.append(f"{old_name} -> {new_name}")


def migrate_activations(state: StateSettings, *, report: Report) -> None:
    """Rewrite the activation file's `kind` values into the past-tense event names, under its new name.

    Args:
        state: The state settings whose home holds the activation file.
        report: The report this run adds to.
    """
    home = state.home
    old, new = home / "capability-activations.jsonl", home / "activations.jsonl"
    if not old.exists() or new.exists():
        return
    out: list[str] = []
    for payload in _read_lines(old, report=report):
        record = dict(payload)
        kind = str(record.get("kind", ""))
        if kind in ACTIVATION_KINDS:
            record["kind"] = ACTIVATION_KINDS[kind]
        if "door" in record:
            record["via"] = record.pop("door")
        out.append(json.dumps(record))
        report.activations += 1
    new.write_text("".join(line + "\n" for line in out), encoding="utf-8")
    old.unlink()
    report.files.append(f"{old.name} -> {new.name}")


ITEM_SUFFIXES = frozenset({".py", ".yaml", ".yml"})
"""What a custom item's own file is. A manifest and the promoted list are not items."""


def custom_items_under(root: Path) -> int:
    """How many custom items `root` holds, counted across every type's own directory.

    Args:
        root: The custom root.

    Returns:
        The number of item files one folder below `root`.
    """
    return sum(1 for path in root.glob("*/*") if path.is_file() and path.suffix in ITEM_SUFFIXES)


def migrate_custom_root(old: Path, new: Path, *, report: Report) -> None:
    """Move the custom-item root to its new name, `leaves/` renamed `steps/` inside it.

    The move is a no-op when there is nothing at `old`, when the two are the same folder, or when `new` is already there,
    so a second run finds nothing to do. The `leaves` key of `steps/manifest.json` is checked every run regardless.

    Args:
        old: The custom root under its old name.
        new: The custom root the settings name.
        report: The report this run adds to.
    """
    if not (old == new or not old.is_dir() or new.exists()):
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)
        leaves, steps = new / "leaves", new / "steps"
        if leaves.is_dir() and not steps.exists():
            leaves.rename(steps)
        report.custom_items = custom_items_under(new)
        report.files.append(f"{old} -> {new}, {report.custom_items} custom item(s)")
    migrate_step_manifest_key(new / "steps" / "manifest.json", report=report)


def migrate_step_manifest_key(manifest: Path, *, report: Report) -> None:
    """Rewrite a live `steps/manifest.json`'s top-level `leaves` key to `steps`, contents else unchanged.

    A no-op when the file does not exist, is not valid JSON, or already reads `steps`.

    Args:
        manifest: The step manifest file.
        report: The report this run adds to.
    """
    if not manifest.exists():
        return
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except ValueError:
        report.refused.append(f"{manifest}: not JSON")
        return
    if not isinstance(payload, dict) or "leaves" not in payload:
        return
    record: dict[str, Any] = payload
    record["steps"] = record.pop("leaves")
    manifest.write_text(json.dumps(record, indent=2), encoding="utf-8")
    report.files.append(f"{manifest}: leaves -> steps")


def _renamed_plugin_key(text: str) -> str | None:
    """`text` with its top-level `doors:` key spelled `plugins:`, or `None` when it has none. Everything else comes back byte for byte."""
    lines = text.splitlines(keepends=True)
    rewritten = [line.replace("doors:", "plugins:", 1) if line.startswith("doors:") else line for line in lines]
    return "".join(rewritten) if rewritten != lines else None


def migrate_profile_documents(profiles: Path, *, report: Report) -> None:
    """Rewrite the `doors:` key of every profile document under `profiles` as `plugins:`.

    Args:
        profiles: The folder of profile documents.
        report: The report this run adds to.
    """
    if not profiles.is_dir():
        return
    for path in sorted(profiles.glob("*.y*ml")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            report.refused.append(f"{path.name}: unreadable")
            continue
        rewritten = _renamed_plugin_key(text)
        if rewritten is None:
            continue
        path.write_text(rewritten, encoding="utf-8")
        report.documents += 1


def migrate_selected_profile(path: Path, *, report: Report) -> None:
    """Rewrite the selected profile's snapshot so its `doors` key reads `plugins`.

    Args:
        path: The selected profile's snapshot file.
        report: The report this run adds to.
    """
    if not path.exists():
        return
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report.refused.append(f"{path.name}: not JSON")
        return
    if not isinstance(stored, dict) or "doors" not in stored:
        return
    path.write_text(json.dumps({("plugins" if key == "doors" else key): value for key, value in stored.items()}), encoding="utf-8")
    report.documents += 1


class _ProjectionPool(Protocol):
    """The one call the projection rename needs from the sessions database pool."""

    async def execute(self, query: str, /) -> str:
        """Run one statement and answer with its status."""
        ...


async def migrate_projection(pool: _ProjectionPool) -> None:
    """Rename the two projected columns that carried an old word. A no-op on an already-current database.

    Args:
        pool: The sessions database pool.
    """
    for statement in PROJECTION_RENAMES:
        await pool.execute(statement)


def migrate(state: StateSettings, custom: Path) -> Report:
    """Run every part of the migration against the state under `state`'s home and the custom root at `custom`.

    Args:
        state: The state settings whose home holds the session state.
        custom: The custom root the settings name.

    Returns:
        What the run did.
    """
    report = Report()
    migrate_state_files(state, report=report)
    migrate_activations(state, report=report)
    named = os.environ.get("APP_AUTHORED_ROOT")
    migrate_custom_root(Path(named) if named else CHECKOUT / "authored", custom, report=report)
    migrate_profile_documents(custom / "profiles", report=report)
    migrate_selected_profile(state.selected_profile, report=report)
    migrate_session_log(state.home / "session-journal.jsonl", state.session_log, state=state, report=report)
    for home in project_state_homes(state.home / "projects-log.jsonl"):
        migrate_session_log(home / "session-journal.jsonl", home / "session-log.jsonl", state=state, report=report)
    return report


def main() -> None:
    """Run the migration and print what it did."""
    settings = HarnessSettings()
    report = migrate(settings.state, custom_root(settings.self_authoring))
    for line in report.lines():
        print(line)  # noqa: T201 (the command's whole output is this listing)


if __name__ == "__main__":
    main()
