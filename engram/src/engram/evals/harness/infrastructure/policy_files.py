"""Write a tuned retrieval policy into the working-tree files a live retrieval run reads.

It writes `policy.py`, the retrieval skill's markdown and the knowledge base's retrieval policy file.
It never runs git, so the person running it reviews the diff and commits.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from engram.evals.harness.domain.delivery import replace_managed_section, rewrite_default_instructions
from engram.kb.contracts import apply_retrieval_policy

if TYPE_CHECKING:
    from engram.evals.tuning.contracts import RetrievalPolicy


class ApplyReport(BaseModel):
    """The three working-tree files `apply_policy` wrote, with a note that it ran no git.

    Attributes:
        instructions_target: The `policy.py` module whose `DEFAULT_INSTRUCTIONS` was rewritten.
        skill_target: The skill markdown whose managed instructions block was replaced.
        active_policy_target: The knowledge base's retrieval policy YAML that live retrieval reads.
        note: A short note for the person running it that only the working tree was written.
    """

    model_config = ConfigDict(frozen=True)

    instructions_target: Path
    skill_target: Path
    active_policy_target: Path
    note: str = "wrote the working tree only. Review the diff and commit (apply-policy never runs git)"


def apply_policy(policy: RetrievalPolicy, *, policy_module_path: Path, skill_md_path: Path, active_policy_path: Path) -> ApplyReport:
    """Write `policy` into its three files in the working tree. It never runs git.

    It rewrites `DEFAULT_INSTRUCTIONS` in `policy_module_path`, replaces the managed instructions block
    in `skill_md_path`, and saves the whole policy as the knowledge base's retrieval policy file.
    Both rewrites are computed before any file is written, so a refusal leaves all three files as they were.
    Applying the same policy twice writes the same bytes.

    Args:
        policy: The winning tuned policy to deliver.
        policy_module_path: The `policy.py` source whose `DEFAULT_INSTRUCTIONS` is rewritten in place.
        skill_md_path: The skill markdown whose managed section is replaced with the instructions.
        active_policy_path: The knowledge base's retrieval policy YAML, under its `_meta` folder.

    Returns:
        An `ApplyReport` naming the three written targets.

    Raises:
        ValueError: When `policy_module_path` has no `DEFAULT_INSTRUCTIONS` assignment or `skill_md_path` has no managed block.
    """
    module_text = rewrite_default_instructions(policy_module_path.read_text(encoding="utf-8"), policy.instructions)
    skill_text = replace_managed_section(skill_md_path.read_text(encoding="utf-8"), policy.instructions)

    policy_module_path.write_text(module_text, encoding="utf-8")
    skill_md_path.write_text(skill_text, encoding="utf-8")

    # The knowledge base writes its own retrieval policy file and keeps the outgoing one in its history.
    # The file sits under the knowledge base's `_meta` folder, so the knowledge base root is its grandparent.
    apply_retrieval_policy(policy, knowledge_base_path=active_policy_path.parent.parent)

    return ApplyReport(instructions_target=policy_module_path, skill_target=skill_md_path, active_policy_target=active_policy_path)
