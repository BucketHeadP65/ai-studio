"""The tool names the follow-me slice mounts or gates, and the names it must never mount."""

from __future__ import annotations

VERBS: tuple[str, ...] = ("navigate", "point", "prefill", "set_tab")
"""The client-executed verbs: the browser performs them and reports each outcome on the next request."""

CONSOLE_ACTS: tuple[str, ...] = (
    "set_model",
    "select_profile",
    "toggle_plugin",
    "run_control",
    "decide_gate",
    "schedule_workflow",
    "update_schedule",
    "delete_schedule",
    "run_schedule_now",
    "archive_workflow",
    "restore_workflow",
    "redescribe_workflow",
)
"""Server tools that act on the owner console.

In a master session each waits for the person's approval before it runs. The three named in
`SCHEDULE_AUTHORING_ACTS` wait in every session. The four schedule tools are the workflow process's own,
gated here and not declared here."""

SCHEDULE_AUTHORING_ACTS: tuple[str, ...] = ("schedule_workflow", "update_schedule", "delete_schedule")
"""The three acts that create, change or delete a schedule. Each waits for the person's approval in every session.

`run_schedule_now` is not one of them. It fires a schedule the person already approved."""

TRUST_ACTS: tuple[str, ...] = ("activate", "promote", "demote", "approve", "import_path", "save_workflow")
"""The owner's own gestures: the pending-to-active gate, capability promotion, demotion, approval, and both savers.

No name here reaches the model while the follow-me plugin is mounted. The first four and `import_path` are owner
commands with no tool anywhere. `save_workflow` is a real tool, and the follow-me plugin filters it off the surface."""
