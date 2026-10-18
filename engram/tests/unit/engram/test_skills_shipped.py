"""The knowledge base skills engram ships as package data, under `engram.skills.contracts.SKILLS`.

No `pydantic_ai_harness` import here: a host loads these through its own skills capability, this
tier only proves the files are where that capability would look.
"""

from __future__ import annotations

import hashlib
import json
import re

from engram.kb.contracts import SourcesSpec
from engram.skills.contracts import SKILLS
from engram.skills.engram_init_schema import render_knowledge_base_json_schema

_SHIPPED_SKILLS = ("documenting", "agent-threads", "searching-the-knowledge-base", "engram-init", "designing-note-writing")

_NAME_LINE = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)

_SOURCE_HOST = re.compile(r"zotero|youtube|arxiv|ar5iv", re.IGNORECASE)
"""Host names a shipped skill must never teach: a connector owns its own host, a template's schema names it."""


def _frontmatter_name(skill_md: str) -> str | None:
    """The `name` field from a `SKILL.md`'s opening frontmatter block, or `None` when absent.

    Read by line rather than as full YAML: a description field can carry a bare colon that a
    strict YAML parser refuses, and the `name` line itself is never one of those.
    """
    _, _, rest = skill_md.partition("---\n")
    block, _, _ = rest.partition("\n---")
    match = _NAME_LINE.search(block)
    return match.group(1) if match else None


def test_every_shipped_skill_directory_exists() -> None:
    for name in _SHIPPED_SKILLS:
        assert (SKILLS / name).is_dir(), f"missing skill directory {name!r} under {SKILLS}"


def test_every_shipped_skill_names_itself_in_frontmatter() -> None:
    for name in _SHIPPED_SKILLS:
        skill_md = SKILLS / name / "SKILL.md"
        assert skill_md.is_file(), f"missing SKILL.md for {name!r}"
        assert _frontmatter_name(skill_md.read_text(encoding="utf-8")) == name


def test_no_shipped_skill_teaches_the_research_knowledge_base_as_the_default() -> None:
    """A dropped view, a facet slot or a skill only one person's knowledge base has never reaches every reader."""
    offenders: list[str] = []
    for skill in _SHIPPED_SKILLS:
        for path in sorted((SKILLS / skill).rglob("*.md")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if any(word in line for word in ("api.pages", "slot=", "ingesting-papers")):
                    offenders.append(f"{path.relative_to(SKILLS)}:{number}: {line.strip()}")
    assert not offenders, "shipped skill text teaches the research knowledge base:\n" + "\n".join(offenders)


def test_the_research_example_is_not_shipped_the_template_is_the_reference() -> None:
    assert not (SKILLS / "engram-init" / "examples" / "research.json").exists()


def test_the_shipped_json_schema_is_what_the_generator_renders_today() -> None:
    """The `engram-init` skill tells an agent to validate its draft against this file before writing it.

    A `KnowledgeBaseSchema` change that is not followed by a regenerate leaves the skill teaching a shape the
    loader no longer accepts (or still teaching one it dropped), which no other test would catch.
    Regenerate with `uv run python -m engram.skills.engram_init_schema`.
    """
    shipped = SKILLS / "engram-init" / "knowledge-base-schema.schema.json"

    assert shipped.read_text(encoding="utf-8") == render_knowledge_base_json_schema()


def test_the_shipped_json_schema_describes_the_sources_block_the_spec_declares() -> None:
    """The sources block is what a knowledge base declares to read outside sources, so its keys must be the real ones."""
    schema = json.loads((SKILLS / "engram-init" / "knowledge-base-schema.schema.json").read_text(encoding="utf-8"))

    assert set(schema["$defs"]["SourcesSpec"]["properties"]) == set(SourcesSpec.model_fields)


def test_no_shipped_skill_text_names_a_source_host() -> None:
    """A connector's own name belongs to the connectors package and to a template's schema, never to engram's own text.

    The examples are excluded on purpose: each is one named knowledge base's own worked spec, where naming
    the connectors that knowledge base uses is the point.
    """
    offenders: list[str] = []
    for skill in _SHIPPED_SKILLS:
        for path in sorted((SKILLS / skill).rglob("*.md")):
            if "examples" in path.parts:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if _SOURCE_HOST.search(line):
                    offenders.append(f"{path.relative_to(SKILLS)}:{number}: {line.strip()}")
    assert not offenders, "shipped skill text names a source host:\n" + "\n".join(offenders)


def test_documenting_carries_the_note_grammar_as_a_supporting_file() -> None:
    grammar = SKILLS / "documenting" / "grammar.md"
    assert grammar.is_file()

    skill_text = (SKILLS / "documenting" / "SKILL.md").read_text(encoding="utf-8")

    assert "grammar.md" in skill_text


_AGENT_THREADS_RELEASE = "2.1.0"
"""The agent-threads release (https://github.com/BucketHeadP65/agent-threads) this copy of its skill is taken from."""

_AGENT_THREADS_DIGESTS = {
    "SKILL.md": "780c9d1e1963dc8adda9d5a6a99ec7cf3ede507f652acf610ab4e0dbce0176d9",
    "scripts/notes.py": "58c602db6262d92bd230a0d545d0dcd07c7445efbb9fbc9a37df3621af35f998",
}


def test_the_agent_threads_skill_is_the_pinned_release_copy() -> None:
    """The skill is owned upstream: update it by copying a new release's `skills/agent-threads/` and these digests."""
    for relative, digest in _AGENT_THREADS_DIGESTS.items():
        actual = hashlib.sha256((SKILLS / "agent-threads" / relative).read_bytes()).hexdigest()
        assert actual == digest, f"{relative} differs from agent-threads {_AGENT_THREADS_RELEASE}"
