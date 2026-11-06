"""A library holding three maintenance flags: a diverged copy and a repository change open, and a diverged copy a person resolved."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.ai.workflows.contracts import LibraryCollection, MaintenanceFlag

from tests.support.fakes.library_collection import FakeCollection

T0 = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)

DIFF = """```diff
--- st.finish.v2
+++ wf.digest.v3
@@ -1,3 +1,3 @@
 name: finish
-instructions: Say what changed since the last digest.
+instructions: Say what changed since the last digest, in three lines.
 version: 2
```"""
"""The unified diff a diverged copy's evidence carries, fenced."""

DIVERGED_ID = "mf.diverged.digest.finish"
DIVERGED = MaintenanceFlag(
    kind="diverged_copy",
    step="finish",
    step_id="st.finish.v2",
    step_hash="sha256:aaaa",
    workflow="wf.digest.v3",
    title="Diverged copy of finish in digest",
    summary="Decide whether the copy of finish in wf.digest.v3 stays as it is, becomes a new version of finish, or goes back to a saved version.",
    evidence=f"wf.digest.v3 holds a copy of the reusable step finish that matches no saved version. It diverged from st.finish.v2.\n\n{DIFF}",
    raised_by="library",
    raised_at=T0 + timedelta(hours=2),
)
"""An open diverged copy, the newest flag."""

CHANGE_ID = "mf.change.summarise"
CHANGE = MaintenanceFlag(
    kind="repository_change",
    step="summarise",
    step_id="st.summarise.v4",
    step_hash="sha256:bbbb",
    title="Repository change for summarise",
    summary="Decide whether summarise gains a length parameter its consumers set.",
    evidence="Two consumers adopted st.summarise.v4 and one kept v3 for its shorter output.",
    proposed_change="```markdown\nname: summarise\nversion: 5\n```",
    raised_by="agent:step-maintenance",
    raised_at=T0 + timedelta(hours=1),
)
"""An open repository change about a step, so it names no workflow."""

SETTLED_ID = "mf.diverged.report.finish"
SETTLED = MaintenanceFlag(
    kind="diverged_copy",
    step="finish",
    step_id="st.finish.v2",
    step_hash="sha256:aaaa",
    workflow="wf.report.v2",
    title="Diverged copy of finish in report",
    summary="Decide whether the copy of finish in wf.report.v2 stays as it is, becomes a new version of finish, or goes back to a saved version.",
    evidence="wf.report.v2 holds a copy of the reusable step finish that matches no saved version. It diverged from st.finish.v2.",
    raised_by="library",
    raised_at=T0,
)
"""A diverged copy the owner resolved, the oldest flag."""

SETTLED_REASON = "The report keeps its own wording on purpose."


async def a_flag_library() -> tuple[FakeCollection, LibraryCollection]:
    """The three flags written through the library, the oldest resolved by `owner`, over one collection a test can read."""
    store = FakeCollection(prune=True)
    library = LibraryCollection(store, store.ingest, collection=store.collection)
    for flag in (DIVERGED, CHANGE, SETTLED):
        await library.put_flag(flag)
    await library.resolve_flag(SETTLED_ID, by="owner", reason=SETTLED_REASON)
    return store, library
