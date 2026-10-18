"""Unit tests for the knowledge base's retrieval-policy write path (`kb/infrastructure/retrieval_policy_files.py`).

Pure file-chain arithmetic over `tmp_path`: the live `_meta/active-policy.yaml` filename never
changes, every apply snapshots the outgoing live file into the append-only
`_meta/policy-history/active-policy.v{N}.yaml` chain, and a restore replays the latest snapshot as a
NEW apply (itself snapshotted), raising cleanly when no history exists. `ApplyRetrievalPolicy` is
the command every caller outside the knowledge base reaches that chain through: carrying a policy it
applies, carrying none it restores.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.ai.retrieval.policy import RetrievalPolicy, load_policy

from engram.kb.contracts import ApplyRetrievalPolicy, ApplyRetrievalPolicyHandler, apply_retrieval_policy
from engram.kb.infrastructure.retrieval_policy_files import restore_previous_policy, snapshot_history

if TYPE_CHECKING:
    from pathlib import Path


def _policy(instructions: str) -> RetrievalPolicy:
    """A minimal policy whose `instructions` double as its identity in assertions."""
    return RetrievalPolicy(instructions=instructions, strategy="full", tool_descriptions={})


def _live(knowledge_base: Path) -> Path:
    return knowledge_base / "_meta" / "active-policy.yaml"


def _history(knowledge_base: Path) -> Path:
    return knowledge_base / "_meta" / "policy-history"


class TestSnapshotHistory:
    def test_no_live_file_snapshots_nothing(self, tmp_path: Path):
        assert snapshot_history(tmp_path) is None
        assert not _history(tmp_path).exists()

    def test_snapshots_number_up_from_v1(self, tmp_path: Path):
        apply_retrieval_policy(_policy("A"), knowledge_base_path=tmp_path)

        first = snapshot_history(tmp_path)
        second = snapshot_history(tmp_path)

        assert first is not None
        assert second is not None
        assert first.name == "active-policy.v1.yaml"
        assert second.name == "active-policy.v2.yaml"
        assert load_policy(first).instructions == "A"


class TestApplyRetrievalPolicy:
    def test_first_apply_writes_live_with_no_snapshot(self, tmp_path: Path):
        report = apply_retrieval_policy(_policy("A"), knowledge_base_path=tmp_path)

        assert report.history_snapshot is None  # nothing to snapshot on the very first apply
        assert report.rolled_back_from is None
        assert report.active_policy_target == _live(tmp_path)
        assert load_policy(_live(tmp_path)).instructions == "A"

    def test_each_apply_snapshots_the_outgoing_policy(self, tmp_path: Path):
        apply_retrieval_policy(_policy("A"), knowledge_base_path=tmp_path)
        report_b = apply_retrieval_policy(_policy("B"), knowledge_base_path=tmp_path)
        report_c = apply_retrieval_policy(_policy("C"), knowledge_base_path=tmp_path)

        assert report_b.history_snapshot is not None
        assert report_b.history_snapshot.name == "active-policy.v1.yaml"
        assert load_policy(report_b.history_snapshot).instructions == "A"  # v1 preserves the outgoing A
        assert report_c.history_snapshot is not None
        assert report_c.history_snapshot.name == "active-policy.v2.yaml"
        assert load_policy(report_c.history_snapshot).instructions == "B"
        assert load_policy(_live(tmp_path)).instructions == "C"

    def test_the_live_filename_never_changes(self, tmp_path: Path):
        apply_retrieval_policy(_policy("A"), knowledge_base_path=tmp_path)
        apply_retrieval_policy(_policy("B"), knowledge_base_path=tmp_path)
        restore_previous_policy(tmp_path)

        meta_files = sorted(path.name for path in (tmp_path / "_meta").iterdir())
        assert meta_files == ["active-policy.yaml", "policy-history"]


class TestRestorePreviousPolicy:
    def test_restores_the_latest_snapshot_and_snapshots_the_outgoing(self, tmp_path: Path):
        apply_retrieval_policy(_policy("A"), knowledge_base_path=tmp_path)
        apply_retrieval_policy(_policy("B"), knowledge_base_path=tmp_path)  # history v1 = A, live = B

        report = restore_previous_policy(tmp_path)

        assert load_policy(_live(tmp_path)).instructions == "A"  # the previous content is live again
        assert report.rolled_back_from is not None
        assert report.rolled_back_from.name == "active-policy.v1.yaml"
        assert report.history_snapshot is not None
        assert report.history_snapshot.name == "active-policy.v2.yaml"  # the outgoing B is preserved
        assert load_policy(report.history_snapshot).instructions == "B"

    def test_rollback_of_a_rollback_is_just_the_next_apply(self, tmp_path: Path):
        apply_retrieval_policy(_policy("A"), knowledge_base_path=tmp_path)
        apply_retrieval_policy(_policy("B"), knowledge_base_path=tmp_path)
        restore_previous_policy(tmp_path)  # live A, history: v1=A, v2=B

        report = restore_previous_policy(tmp_path)  # restores v2 (B), snapshots the outgoing A as v3

        assert load_policy(_live(tmp_path)).instructions == "B"
        assert report.rolled_back_from is not None
        assert report.rolled_back_from.name == "active-policy.v2.yaml"

    def test_no_history_raises_cleanly(self, tmp_path: Path):
        with pytest.raises(ValueError, match="no policy history"):
            restore_previous_policy(tmp_path)

    def test_no_history_raises_even_when_a_live_file_exists(self, tmp_path: Path):
        apply_retrieval_policy(_policy("A"), knowledge_base_path=tmp_path)  # first apply: live exists, history empty

        with pytest.raises(ValueError, match="no policy history"):
            restore_previous_policy(tmp_path)


class TestApplyRetrievalPolicyHandler:
    async def test_a_command_with_a_policy_makes_it_live(self, tmp_path: Path):
        report = await ApplyRetrievalPolicyHandler()(ApplyRetrievalPolicy(knowledge_base_path=tmp_path, policy=_policy("A")))

        assert load_policy(_live(tmp_path)).instructions == "A"
        assert report.active_policy_target == _live(tmp_path)
        assert report.history_snapshot is None  # nothing was live to snapshot

    async def test_a_command_with_no_policy_restores_the_previous_one(self, tmp_path: Path):
        handler = ApplyRetrievalPolicyHandler()
        await handler(ApplyRetrievalPolicy(knowledge_base_path=tmp_path, policy=_policy("A")))
        await handler(ApplyRetrievalPolicy(knowledge_base_path=tmp_path, policy=_policy("B")))

        report = await handler(ApplyRetrievalPolicy(knowledge_base_path=tmp_path))

        assert load_policy(_live(tmp_path)).instructions == "A"
        assert report.rolled_back_from is not None
        assert report.rolled_back_from.name == "active-policy.v1.yaml"

    async def test_a_restore_with_no_history_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="no policy history"):
            await ApplyRetrievalPolicyHandler()(ApplyRetrievalPolicy(knowledge_base_path=tmp_path))
