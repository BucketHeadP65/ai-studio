"""The knowledge base's retrieval-policy file: the one machine-writable apply path.

A knowledge base owns `_meta/active-policy.yaml`, the policy its read tools serve with, and the
append-only `_meta/policy-history/active-policy.v{N}.yaml` chain beside it. `ApplyRetrievalPolicy`
is the only way in: every apply first snapshots the outgoing live file into the next free history
version, then writes the new one. Sending the command with no policy restores the latest snapshot as
a fresh apply (itself snapshotted), so a rollback is just the next apply and no snapshot is ever
rewritten or deleted. The live filename never changes, so every reader keeps working across applies.

Nothing here touches shipped code or git: the shipped-code channels stay behind the person-run
`evals apply-policy`. Whether a policy has earned an apply is the caller's judgment, Evals runs its
guardrails before it sends the command.

Pure path arithmetic plus three small file operations: version numbers are parsed from the directory
listing, the next free number is max plus one, and a restored policy is re-validated at the boundary
through `load_policy` so a tampered snapshot fails cleanly rather than leaking downstream.
"""

from __future__ import annotations

import re

from typing import TYPE_CHECKING

from core.ai.retrieval.contracts import load_policy, save_policy

from engram.kb.domain.retrieval_policy import RetrievalPolicyReport

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.retrieval.contracts import RetrievalPolicy

ACTIVE_POLICY_PATH = ("_meta", "active-policy.yaml")
"""The folder and file name of a knowledge base's live retrieval policy, under its root."""

# The append-only snapshot chain beside the live file.
_HISTORY_DIRNAME = "policy-history"
_HISTORY_PATTERN = re.compile(r"^active-policy\.v(\d+)\.yaml$")


def _active_policy_path(knowledge_base_path: Path) -> Path:
    """The knowledge base's live active-policy file (`<knowledge_base>/_meta/active-policy.yaml`)."""
    return knowledge_base_path.joinpath(*ACTIVE_POLICY_PATH)


def _history_dir(knowledge_base_path: Path) -> Path:
    """The knowledge base's append-only snapshot directory (`<knowledge_base>/_meta/policy-history/`)."""
    return knowledge_base_path / ACTIVE_POLICY_PATH[0] / _HISTORY_DIRNAME


def _history_versions(history_dir: Path) -> list[tuple[int, Path]]:
    """The `(version, path)` pairs of every snapshot in `history_dir`, unsorted, empty when absent."""
    if not history_dir.is_dir():
        return []
    return [(int(match.group(1)), path) for path in history_dir.iterdir() if (match := _HISTORY_PATTERN.match(path.name))]


def snapshot_history(knowledge_base_path: Path) -> Path | None:
    """Snapshot the live active-policy file into the next free `policy-history` version.

    Copies `_meta/active-policy.yaml` byte for byte to `_meta/policy-history/active-policy.v{N}.yaml`
    (N is max existing plus one, 1 on a clean directory). Append-only, no existing snapshot is ever
    rewritten. A knowledge base with no live file (nothing applied yet) has nothing to snapshot and returns
    `None`, the very-first-apply case.

    Args:
        knowledge_base_path: The knowledge base root the live file and the history chain live under.

    Returns:
        The written snapshot path, or `None` when no live file exists.
    """
    live = _active_policy_path(knowledge_base_path)
    if not live.exists():
        return None
    history = _history_dir(knowledge_base_path)
    history.mkdir(parents=True, exist_ok=True)
    version = max((version for version, _ in _history_versions(history)), default=0) + 1
    target = history / f"active-policy.v{version}.yaml"
    target.write_text(live.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def apply_retrieval_policy(policy: RetrievalPolicy, *, knowledge_base_path: Path) -> RetrievalPolicyReport:
    """Make `policy` live: snapshot the outgoing file, then write the new one.

    Writes `_meta/active-policy.yaml` and nothing else. The live filename never changes, so every
    reader keeps working across applies.

    Args:
        policy: The policy to make live.
        knowledge_base_path: The knowledge base root whose retrieval policy is written.

    Returns:
        The report naming the live target and the outgoing snapshot (`None` first time).
    """
    snapshot = snapshot_history(knowledge_base_path)
    live = _active_policy_path(knowledge_base_path)
    live.parent.mkdir(parents=True, exist_ok=True)
    save_policy(policy, live)
    return RetrievalPolicyReport(active_policy_target=live, history_snapshot=snapshot)


def restore_previous_policy(knowledge_base_path: Path) -> RetrievalPolicyReport:
    """Restore the latest history snapshot's content as a NEW apply (itself snapshotted).

    Loads the highest-numbered `policy-history` snapshot (re-validated at the boundary through
    `load_policy`) and applies it through `apply_retrieval_policy`, so the restored policy is written
    to the unchanged live filename and the outgoing one is itself snapshotted, the history stays a
    complete append-only record. No snapshot is ever deleted, restoring twice is just the next apply.

    Args:
        knowledge_base_path: The knowledge base root whose retrieval policy is restored.

    Returns:
        The report with `rolled_back_from` naming the restored snapshot.

    Raises:
        ValueError: When the knowledge base has no policy history to restore from.
        pydantic.ValidationError: When the latest snapshot is not a valid `RetrievalPolicy`.
    """
    versions = _history_versions(_history_dir(knowledge_base_path))
    if not versions:
        msg = f"no policy history to roll back to under {_history_dir(knowledge_base_path)}"
        raise ValueError(msg)
    _, latest = max(versions, key=lambda pair: pair[0])
    restored = load_policy(latest)
    report = apply_retrieval_policy(restored, knowledge_base_path=knowledge_base_path)
    return report.model_copy(update={"rolled_back_from": latest})
