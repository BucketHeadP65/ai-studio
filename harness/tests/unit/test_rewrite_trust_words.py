"""The wave 4 rewrite turns every evidence record of the old approval kind into the new one, once."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

from harness.migrations.contracts import migrate_trust_words
from harness.settings import StateSettings

if TYPE_CHECKING:
    from pathlib import Path

_OLD = {"kind": "blessing", "workflow_id": "wf.x", "spec_hash": "h1", "workflow_version": 2, "by": "owner", "at": "2026-09-30T10:00:00Z"}
_RUN = {"kind": "run", "workflow_id": "wf.x", "spec_hash": "h1"}


def _state(tmp_path: Path, lines: list[str]) -> StateSettings:
    state = StateSettings(home=tmp_path)
    state.evidence_log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return state


class TestRewriteTrustWords:
    async def test_an_old_record_becomes_an_approval_with_every_other_key_unchanged(self, tmp_path: Path):
        state = _state(tmp_path, [json.dumps(_OLD)])

        report = await migrate_trust_words(state)

        [line] = state.evidence_log.read_text(encoding="utf-8").splitlines()
        assert json.loads(line) == {**_OLD, "kind": "approval"}
        assert report.touched

    async def test_a_rewritten_record_is_written_compact_like_every_other_evidence_line(self, tmp_path: Path):
        state = _state(tmp_path, [json.dumps({**_OLD, "by": "Ἀλέξης"})])

        await migrate_trust_words(state)

        [line] = state.evidence_log.read_text(encoding="utf-8").splitlines()
        assert '"kind":"approval"' in line
        assert '"by":"Ἀλέξης"' in line

    async def test_a_second_run_rewrites_nothing(self, tmp_path: Path):
        state = _state(tmp_path, [json.dumps(_OLD)])
        await migrate_trust_words(state)
        before = state.evidence_log.read_text(encoding="utf-8")

        report = await migrate_trust_words(state)

        assert state.evidence_log.read_text(encoding="utf-8") == before
        assert not report.touched

    async def test_other_kinds_and_lines_that_are_not_json_come_through_unchanged(self, tmp_path: Path):
        state = _state(tmp_path, [json.dumps(_RUN), "not json", json.dumps(_OLD)])

        await migrate_trust_words(state)

        lines = state.evidence_log.read_text(encoding="utf-8").splitlines()
        assert lines[0] == json.dumps(_RUN)
        assert lines[1] == "not json"
        assert json.loads(lines[2])["kind"] == "approval"

    async def test_a_missing_evidence_file_is_nothing_to_do(self, tmp_path: Path):
        report = await migrate_trust_words(StateSettings(home=tmp_path))

        assert not report.touched
