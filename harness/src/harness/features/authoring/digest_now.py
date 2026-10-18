"""`digest_now`: land a source, sync and index a knowledge base, then start its digest workflow.

One call queues a URL when given, syncs and indexes the knowledge base over the studio API, then
starts the newest live version of the workflow the knowledge base's schema names under
`sources.digest_workflow`. It answers as soon as the run starts and never waits for the workflow to
finish. Every failure comes back as a refusal in the tool's answer, never as an exception: the studio
unreachable, the sync failing, a schema that names no digest workflow, no workflow of that name
registered, or a workflow that no longer validates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

from core.ai.workflows.contracts import LibraryError, unsatisfied_parameters, validate
from pydantic import ValidationError

# Runtime imports, not type-only: pydantic-ai evaluates the tool function's annotations when it
# builds the tool schema, so every name in `digest_now`'s signature must resolve at import time.
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry

from harness.features.authoring.exceptions import DigestWorkflowUnreadableError
from harness.session import Runtime

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from core.ai.workflows.contracts import WorkflowArguments, WorkflowLibrary

    from harness.features.authoring.internal_contracts import KnowledgeBaseRefresher, LaunchWorkflow

async def _latest_workflow_id(library: WorkflowLibrary, name: str) -> str | None:
    """The newest live version's page id for `name` by the library's own `current`, or `None` when it holds no version."""
    first = await library.locate(name, 1)
    if first is None:
        return None
    return await library.current(first)


async def _resolve_and_launch(session: Runtime, knowledge_base: str, workflow_name: str, launch: LaunchWorkflow) -> str:
    """Resolve the current version of `workflow_name`, validate it and launch it, answering in words either way."""
    workflow_id = await _latest_workflow_id(session.library, workflow_name)
    if workflow_id is None:
        return f"{knowledge_base!r} was synced and indexed, but no {workflow_name!r} workflow is registered here."
    try:
        workflow = await session.library.fetch_workflow(workflow_id)
    except (LibraryError, ValidationError) as exc:
        return f"could not load {workflow_id!r}: {exc}"
    report = validate(workflow, session.registry)
    if not report.ok:
        detail = ". ".join(f"{issue.where}: {issue.detail}" for issue in report.issues)
        return f"{workflow_id} does not check out here: {detail}"
    params: WorkflowArguments = {"collection": knowledge_base}
    unsatisfied = unsatisfied_parameters(workflow, params)
    if unsatisfied:
        return f"{workflow_id} needs values for: {', '.join(unsatisfied)}"
    await launch(session, spec=workflow, workflow_id=workflow_id, params=params, runner="app:chat:digest-now")
    # The `/workflows/[id]` page is keyed by the workflow id, and a session id there answers 404.
    return f"Started {workflow_id} over {knowledge_base!r}. Watch it at /workflows/{quote(workflow_id, safe='')}."


def digest_now_tool(
    refresher: KnowledgeBaseRefresher, launch: LaunchWorkflow
) -> Callable[[RunContext[Runtime], str, str | None], Awaitable[str]]:
    """Build the `digest_now` tool.

    Args:
        refresher: The studio API that queues a source and syncs and indexes a knowledge base.
        launch: What starts the digest run, `running.start` as the composition root hands it in.

    Returns:
        The tool function, ready to mount.
    """

    async def digest_now(ctx: RunContext[Runtime], collection: str, url: str | None = None) -> str:
        """Land a source, sync and index a knowledge base, then start its digest workflow.

        Waits for the sync-and-index job to land, then answers as soon as the digest run
        starts, and it never waits for the workflow to finish. Open the returned path, or call
        check_runs, to see how it went. It is refused while this session holds a paused run.

        Args:
            ctx: The run context. The session it carries owns the run this starts.
            collection: The knowledge base's registry name.
            url: A source to queue on the knowledge base's source queue first. Omitted, this only syncs,
                indexes, and digests whatever is already undigested.
        """
        session = ctx.deps
        if (paused := session.active.paused_run_refusal()) is not None:
            raise ModelRetry(paused)
        if session.active.task is not None and not session.active.task.done():
            return (
                f"{session.active.workflow_id} is still running in this session. Wait for it, or tell "
                "the person, before starting another digest."
            )
        if url is not None and (refusal := await refresher.queue_source(collection, url)) is not None:
            return refusal
        if (refusal := await refresher.sync_and_index(collection)) is not None:
            return refusal
        try:
            workflow_name = await refresher.digest_workflow(collection)
        except DigestWorkflowUnreadableError as exc:
            return str(exc)
        if not workflow_name:
            return (
                f"{collection!r} was synced and indexed, but its schema names no digest workflow. Set sources.digest_workflow "
                "in its knowledge-base-schema.json to the workflow name that digests its new sources."
            )
        return await _resolve_and_launch(session, collection, workflow_name, launch)

    return digest_now
