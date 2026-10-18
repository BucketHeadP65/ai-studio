"""The command that applies a retrieval policy to a knowledge base, and the report an apply returns."""

from __future__ import annotations

from pathlib import Path

from core.ai.retrieval.contracts import RetrievalPolicy
from core.cqrs.contracts import Command
from pydantic import BaseModel, ConfigDict


class ApplyRetrievalPolicy(Command):
    """Make a retrieval policy the knowledge base's live one, or restore the previous one.

    Attributes:
        knowledge_base_path: The knowledge base root the live file and the history chain live under.
        policy: The policy to make live. `None` restores the latest history snapshot instead, which
            is how a rollback is asked for.
    """

    knowledge_base_path: Path
    policy: RetrievalPolicy | None = None


class RetrievalPolicyReport(BaseModel):
    """One apply (or rollback), the written live target and its lineage.

    Attributes:
        active_policy_target: The live `_meta/active-policy.yaml` path written.
        history_snapshot: The `policy-history` snapshot of the OUTGOING live file, or `None` on the
            very first apply (there was no live file to snapshot).
        rolled_back_from: The history snapshot whose content this apply restored, `None` on a
            forward apply.
    """

    model_config = ConfigDict(frozen=True)

    active_policy_target: Path
    history_snapshot: Path | None
    rolled_back_from: Path | None = None
