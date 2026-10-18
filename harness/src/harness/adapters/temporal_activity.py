"""What a step can learn about the Temporal run executing it, when one is."""

from __future__ import annotations

from temporalio import activity


def activity_run() -> tuple[str, str] | None:
    """The run id and workflow id of the Temporal activity running this code, when a workflow started one.

    The run id names the execution the activity belongs to. A run's spend log opens under its first execution's
    id, so the two agree only while a run has exactly one execution. No workflow may continue as new
    or start with a retry policy, and `durable_run_id` on the launch side must change together with
    this function.

    Returns:
        `(workflow_run_id, workflow_id)`, or `None` outside an activity and inside one no workflow started.
    """
    if not activity.in_activity():
        return None
    info = activity.info()
    workflow_id = info.workflow_id
    if workflow_id is None:
        return None
    return (info.workflow_run_id or workflow_id, workflow_id)
