"""No module-level state path survives: every reader takes the home it writes under."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from core.ai.workflows.contracts import StepRegistry

from harness import session as session_state
from harness import settings as settings_module
from harness.features.running.infrastructure.run_log import RunLog
from harness.session import NoSessionLog, Runtime
from harness.settings import CHECKOUT
from tests.support.runtime import EmptyLibrary, a_runtime
from tests.support.state import a_throwaway_state

if TYPE_CHECKING:
    from core.ai.workflows.contracts import Workflow


class TestNothingReadsAStatePathOffTheModule:
    def test_the_module_holds_no_state_constant(self) -> None:
        assert not hasattr(settings_module, "STATE")

    def test_the_module_builds_no_step_store(self) -> None:
        assert not hasattr(settings_module, "step_store")

    def test_the_module_holds_none_of_the_ten_path_functions(self) -> None:
        gone = (
            "session_log_path",
            "run_log_path",
            "run_events_path",
            "draft_path",
            "evidence_log_path",
            "activation_log_path",
            "external_agents_path",
            "forgotten_marker_path",
            "archive_root",
            "run_state_dir",
        )

        assert [name for name in gone if hasattr(settings_module, name)] == []


class TestASessionWritesUnderNobodysCheckout:
    def test_a_session_cannot_be_built_without_being_told_where_its_files_go(self) -> None:
        """`Runtime.state` has no default, so a test cannot forget to say where a session writes.

        A session built over whatever home the process happened to resolve is how a run's files
        land in this checkout while a test runs.
        """
        with pytest.raises(TypeError, match="state"):
            Runtime(registry=StepRegistry(), library=EmptyLibrary(), agent=cast("Any", None), session_log=NoSessionLog())  # pyright: ignore[reportCallIssue]

    def test_a_session_cannot_be_built_without_being_told_where_its_events_go(self) -> None:
        """`Runtime.session_log` has no default either, so no session narrates into a log nobody named.

        A session holding a log that discards every event records its start nowhere, so the listing
        it should have appeared in comes back empty and says nothing about why.
        """
        with pytest.raises(TypeError, match="session_log"):
            Runtime(registry=StepRegistry(), library=EmptyLibrary(), agent=cast("Any", None), state=a_throwaway_state())  # pyright: ignore[reportCallIssue]

    def test_launching_a_run_in_a_test_built_session_leaves_the_checkout_alone(self, quarterly_report: Workflow) -> None:
        """The falsifiable one: a session `a_runtime()` built writes under its own throwaway home."""
        session = a_runtime()

        waiting = session_state.run_log_path(session)
        RunLog.begin(path=waiting, run_id="a-run", workflow_id="wf.somewhere", spec=quarterly_report, params={"quarter": "Q2"}).started("fetch_sales")

        assert waiting.exists(), "the run log was written somewhere"
        assert CHECKOUT / ".state" not in waiting.parents, f"{waiting} is inside this checkout's own state home"
        assert not str(waiting).startswith(str(CHECKOUT)), f"{waiting} is inside this checkout"
