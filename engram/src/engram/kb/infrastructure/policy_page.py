"""The ingestion policy page: one readable `_meta/ingestion-policy.md` per knowledge base, hard rules in its frontmatter, guidance in its body."""

from __future__ import annotations

import re

from typing import TYPE_CHECKING

import yaml

from core.files import atomic_write
from core.markdown import render_frontmatter, split_frontmatter
from pydantic import BaseModel, ConfigDict, ValidationError

from engram.kb.domain.schema import SourceQueuePolicy

if TYPE_CHECKING:
    from pathlib import Path

    from engram.kb.domain.schema import KnowledgeBaseSchema

POLICY_PATH = "_meta/ingestion-policy.md"
"""Where a knowledge base keeps its ingestion policy, relative to the knowledge base root."""

POLICY_ID = "meta.ingestion-policy"
POLICY_TITLE = "Ingestion policy"

DEFAULT_GUIDANCE = (
    "Take what advances this base's purpose. A reference counts when the digest that named it builds on it, "
    "compares against it, or evaluates on it. A link that merely appears in passing does not count."
)
"""The judgment guidance a new knowledge base starts with, until its owner rewrites it."""

_LEADING_HEADING = re.compile(r"\A\s*#[^\n]*\n+")

_NOT_A_RULE_MAPPING = "the page's frontmatter is empty, missing its fence, or not a mapping of rule names to values"

_PAGE_META_FIELDS = frozenset({"id", "type", "title"})
"""The meta-note frontmatter keys `render_policy` always writes, never a rule, never reported as one."""


class SourceQueuePolicyPage(BaseModel):
    """A knowledge base's ingestion policy as the judge reads it: the rules, the guidance, whether the page is on disk, and any read failure.

    `parse_error` is set, and `rules`/`guidance` fall back to the spec's own defaults, when the page exists but
    could not be trusted: invalid YAML, or frontmatter that is not a mapping of rule names to values. `unknown_rules`
    names frontmatter keys the page carried that are not one of `SourceQueuePolicy`'s fields, they took no effect.
    """

    model_config = ConfigDict(frozen=True)

    rules: SourceQueuePolicy
    guidance: str
    present: bool
    parse_error: str | None = None
    unknown_rules: tuple[str, ...] = ()


def read_policy(knowledge_base_path: Path, spec: KnowledgeBaseSchema) -> SourceQueuePolicyPage:
    """Read the knowledge base's policy: the page's rules over the schema's defaults field by field, or the defaults alone.

    Never raises. A page that cannot be read as rules (invalid YAML, or frontmatter that is not a mapping of rule
    names to values, or a rule value outside its closed set) comes back with `present=True`, the schema's own
    defaults, and `parse_error` naming why.

    Args:
        knowledge_base_path: The knowledge base root.
        spec: The knowledge base's schema, whose `sources.source_queue` supplies the defaults.

    Returns:
        The policy as read, with whether the page exists and any read failure.
    """
    path = knowledge_base_path / POLICY_PATH
    if not path.is_file():
        return SourceQueuePolicyPage(rules=spec.sources.source_queue, guidance="", present=False)
    try:
        front, body = split_frontmatter(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        return _unparseable(spec, str(error))
    if not front:
        return _unparseable(spec, _NOT_A_RULE_MAPPING)
    unknown = tuple(key for key in front if key not in SourceQueuePolicy.model_fields and key not in _PAGE_META_FIELDS)
    declared = {key: value for key, value in front.items() if key in SourceQueuePolicy.model_fields}
    try:
        rules = SourceQueuePolicy.model_validate({**spec.sources.source_queue.model_dump(), **declared})
    except ValidationError as error:
        return _unparseable(spec, str(error))
    guidance = _LEADING_HEADING.sub("", body, count=1).strip()
    return SourceQueuePolicyPage(rules=rules, guidance=guidance, present=True, unknown_rules=unknown)


def _unparseable(spec: KnowledgeBaseSchema, error: str) -> SourceQueuePolicyPage:
    """The page as `read_policy` answers it when it exists but cannot be trusted: the spec's own defaults, and why."""
    return SourceQueuePolicyPage(rules=spec.sources.source_queue, guidance="", present=True, parse_error=error)


def render_policy(rules: SourceQueuePolicy, guidance: str) -> str:
    """Render the page text for `rules` and `guidance`: a meta note's frontmatter carrying the rules, an H1, then the guidance.

    Args:
        rules: The hard rules to write into the frontmatter.
        guidance: The judgment guidance to write as the body.

    Returns:
        The page's markdown.
    """
    front = {"id": POLICY_ID, "type": "meta", "title": POLICY_TITLE, **rules.model_dump(mode="json")}
    return render_frontmatter(front, f"# {POLICY_TITLE}\n\n{guidance.strip()}\n")


def write_policy(knowledge_base_path: Path, rules: SourceQueuePolicy, guidance: str) -> Path:
    """Write the policy page whole and return its path. An existing page is replaced.

    Args:
        knowledge_base_path: The knowledge base root.
        rules: The hard rules to write.
        guidance: The judgment guidance to write.

    Returns:
        The path of the written page.
    """
    path = knowledge_base_path / POLICY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, render_policy(rules, guidance))
    return path
