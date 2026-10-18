"""`open_workspace`: the state home a project gets, and the session log it hands back."""

from __future__ import annotations

from typing import TYPE_CHECKING

from harness.domain.events import Booted
from harness.workspace import open_workspace

if TYPE_CHECKING:
    from pathlib import Path


class TestOpenWorkspace:
    def test_creates_the_state_home_and_the_gitignore(self, tmp_path: Path) -> None:
        workspace = open_workspace(tmp_path)

        assert workspace.root == tmp_path
        assert workspace.state.home == tmp_path / ".aifactory" / "state"
        assert workspace.state.home.is_dir()
        gitignore = tmp_path / ".aifactory" / ".gitignore"
        assert gitignore.read_text(encoding="utf-8").splitlines() == ["state/"]

    def test_is_idempotent(self, tmp_path: Path) -> None:
        first = open_workspace(tmp_path)
        first.session_log.record(Booted(resumed_run_id="run-1"))

        second = open_workspace(tmp_path)

        assert second.state.home == first.state.home
        assert [recorded.event for recorded in second.session_log.events()] == [Booted(resumed_run_id="run-1")]
        gitignore = tmp_path / ".aifactory" / ".gitignore"
        assert gitignore.read_text(encoding="utf-8").splitlines() == ["state/"]

    def test_never_clobbers_an_existing_gitignore(self, tmp_path: Path) -> None:
        aifactory_dir = tmp_path / ".aifactory"
        aifactory_dir.mkdir(parents=True)
        (aifactory_dir / ".gitignore").write_text("other-thing/\n", encoding="utf-8")

        open_workspace(tmp_path)

        lines = (aifactory_dir / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert lines == ["other-thing/", "state/"]


class TestWorkspaceJournalIsolation:
    def test_two_workspaces_keep_separate_journals(self, tmp_path: Path) -> None:
        first = open_workspace(tmp_path / "project-a")
        second = open_workspace(tmp_path / "project-b")

        first.session_log.record(Booted(resumed_run_id="run-a"))
        second.session_log.record(Booted(resumed_run_id="run-b"))

        assert [recorded.event for recorded in first.session_log.events()] == [Booted(resumed_run_id="run-a")]
        assert [recorded.event for recorded in second.session_log.events()] == [Booted(resumed_run_id="run-b")]
