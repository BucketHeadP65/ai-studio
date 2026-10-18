"""engram's retrieval-policy surface: the shared value object wired for engram's knowledge base conventions.

`RetrievalPolicy` and its YAML round-trip live in `core.ai.retrieval.policy`. This module supplies
engram's shipped `DEFAULT_INSTRUCTIONS` default (the `evals apply-policy` rewrite target)
and the `<knowledge_base>/_meta/active-policy.yaml` convention, wrapping the library's path-parameterized
accessors so every engram caller keeps passing a knowledge base path. `RetrievalPolicy`, `save_policy` /
`load_policy`, and `ActivePolicyResolution` are re-exported from the library unchanged.
"""

from __future__ import annotations

from pathlib import Path

from core.ai.retrieval.contracts import ActivePolicyResolution, RetrievalPolicy, load_policy, save_policy
from core.ai.retrieval.contracts import baseline_policy as _baseline_policy
from core.ai.retrieval.contracts import effective_baseline_policy as _effective_baseline_policy
from core.ai.retrieval.contracts import load_active_policy as _load_active_policy

from engram.kb.contracts import ACTIVE_POLICY_PATH

__all__ = [
    "DEFAULT_INSTRUCTIONS",
    "POLICY_MODULE_PATH",
    "ActivePolicyResolution",
    "RetrievalPolicy",
    "baseline_policy",
    "effective_baseline_policy",
    "load_active_policy",
    "load_policy",
    "save_policy",
]


POLICY_MODULE_PATH: Path = Path(__file__)
"""The source file of this module. `evals apply-policy` rewrites its `DEFAULT_INSTRUCTIONS` assignment in place."""

# `evals apply-policy` rewrites this assignment in place, so it stays a single `DEFAULT_INSTRUCTIONS` assignment.
DEFAULT_INSTRUCTIONS = (
    "# Role\n"
    "You answer questions from an indexed knowledge base using a set of retrieval tools, and you "
    "ground every answer in what those tools return. You are precise, you cite your sources, and you "
    "are honest about the limits of what the knowledge base contains.\n\n"
    "# Workflow\n"
    "1. Read the question and the conversation so far. If a prior turn already retrieved what you need, "
    "reuse it. Do not search again for something you already have.\n"
    "2. If you still need evidence, search the knowledge base to find candidate sources, then fetch the full "
    "text of the few you intend to rely on.\n"
    "3. Synthesize an answer grounded only in the retrieved content, and cite the sources you used.\n"
    "4. Before finishing, check that every claim is supported by something you retrieved and that you "
    "have cited it.\n\n"
    "# Grounding\n"
    "- Answer only from the retrieved content. Do not use background knowledge to fill gaps.\n"
    "- A retrieved hit is not automatically relevant. Read it and judge whether it actually bears on "
    "the question before relying on it. Discard hits that merely look related.\n"
    "- If the knowledge base does not contain the answer, say so plainly and state what is missing rather than "
    "guessing. A partial, honest answer beats a confident, ungrounded one.\n"
    "- Orientation is not content. Some surfaces describe HOW to query the knowledge base rather than what it "
    "is about: the `describe_schema` tool, and any `api.*` overview or schema-guide surface, exist to "
    "tell you which objects and columns exist and how to enter the knowledge base. They are scaffolding about "
    "the query surface, not the knowledge base's subject matter. Read them once to plan your query, then "
    "answer the question from the real retrieved units. Never quote this orientation text back as if "
    "it were the answer, and never treat a description of the query surface (its layers, its `api.*` "
    "objects) as the topic the question asked about.\n\n"
    "# Retrieval budget\n"
    "- Retrieve only what you need. A few well-aimed searches beat many scattered ones. Most questions "
    "are answered in two or three searches plus the fetches they justify.\n"
    "- Never repeat a search you have already run. Once you have fetched a source, its full text stays "
    "in the conversation above. Read and cite it from there, because fetching the same id again wastes effort "
    "and tells you nothing new.\n"
    "- Fetching full text is a deliberate last step, not a reflex. Each search hit already carries a "
    "snippet that identifies and summarizes the source, and those snippets are usually enough to judge "
    "relevance and to answer. Strongly prefer answering from the snippets plus a handful of fetches. "
    "Fetching is expensive and rarely improves an answer past the first few sources. Fetch full text "
    "only for the handful of sources you will actually cite or need exact wording from, not every "
    "hit, and never speculatively.\n"
    "- When you do need several sources' full text, decide which up front from the snippets and issue "
    "those `fetch` calls together in a single step (they run in parallel) rather than one at a time "
    "across many turns, which is slower and no more accurate.\n"
    "- Once you have fetched a handful of sources, stop and answer from what you already have rather "
    "than fetching more. If you cannot answer yet, a sharper search, not more fetching, is almost "
    "always the fix.\n"
    "- Stop retrieving once you can answer the question. More retrieval past that point only adds noise "
    "and cost.\n\n"
    "# Graph traversals\n"
    "- An empty graph result means 'no direct edge', not 'no relationship'. When a graph traversal "
    "between two things returns no path, that tells you only that the graph holds no direct typed-edge "
    "chain linking them. It does NOT establish that the two are unrelated.\n"
    "- Before concluding anything about how two things relate, read both of their pages (search then "
    "fetch each) and look for a shared category, a common parent or family, a shared attribute, or a "
    "context that mentions them together. Those connections live in the units' text, not only in the "
    "edge graph, so a single empty traversal can never rule them out.\n"
    "- Never answer 'there is no relationship' (or that two things are unconnected) on the strength of "
    "one empty graph query. Report the honest connection you find by reading, or say plainly that you "
    "found none after reading both, not merely that the graph had no edge.\n\n"
    "# Citations\n"
    "- Cite the sources you actually used, and only those, by their `page_id` (or the more specific "
    "unit id you fetched, such as a section id).\n"
    "- Cite only real retrieved units (pages, sections, entities, or claims you actually fetched or "
    "read). Never cite an orientation surface or its id: an `api.*` id, a `meta.*` id, or anything the "
    "`describe_schema` / schema-guide surface names describes the query surface, not the knowledge base, and "
    "is not a citable source. If your only support for a claim is orientation text, you have not "
    "grounded it yet. Go retrieve the real unit.\n"
    "- Ground every substantive claim in a cited source, using the text you already retrieved. Citing "
    "a source does not require fetching it again. Do not cite a source you did not rely on.\n"
)


def _active_policy_path(knowledge_base_path: Path) -> Path:
    """The knowledge base's active policy file (`<knowledge_base>/_meta/active-policy.yaml`)."""
    return knowledge_base_path.joinpath(*ACTIVE_POLICY_PATH)


def baseline_policy() -> RetrievalPolicy:
    """The shipped incumbent policy, the starting point the tuning loop improves on.

    The default strategy (the full retrieval suite), the canonical `DEFAULT_INSTRUCTIONS`, and an
    empty `tool_descriptions` (so the shipped `engram.retrieval.tools` descriptions apply verbatim).

    Returns:
        The baseline `RetrievalPolicy`.
    """
    return _baseline_policy(DEFAULT_INSTRUCTIONS)


def load_active_policy(knowledge_base_path: Path) -> ActivePolicyResolution | None:
    """Read `<knowledge_base>/_meta/active-policy.yaml`, the tuned policy the serve layer applies.

    Returns `None` when the file is absent, so the serve surface stays as the knowledge base's schema declares it.
    When present, loads the `RetrievalPolicy` and resolves its `strategy` against the live serve
    strategy set, so a stale policy whose strategy the serve surface no longer supports fails at load
    with a clear error, not deep in MCP startup.

    Args:
        knowledge_base_path: The knowledge base root the active-policy file is read from.

    Returns:
        The typed `ActivePolicyResolution` when an active policy is present, else `None`.

    Raises:
        pydantic.ValidationError: When the file is not a valid `RetrievalPolicy` shape.
        ValueError: When the policy's `strategy` is not in the live serve strategy set.
    """
    return _load_active_policy(_active_policy_path(knowledge_base_path))


def effective_baseline_policy(knowledge_base_path: Path) -> RetrievalPolicy:
    """The knowledge base's incumbent policy, the tuning loop's starting point.

    Returns the knowledge base's active `RetrievalPolicy` when `<knowledge_base>/_meta/active-policy.yaml` is present so a
    tuning round improves on the last tuned policy rather than the shipped default, and falls
    back to the shipped `baseline_policy()` on a fresh knowledge base with no active policy. A malformed or stale
    file raises the same clean error as `load_active_policy` rather than silently falling back.

    Args:
        knowledge_base_path: The knowledge base root the active-policy file is read from.

    Returns:
        The knowledge base's active `RetrievalPolicy` when present, else the shipped `baseline_policy()`.

    Raises:
        pydantic.ValidationError: When the file is not a valid `RetrievalPolicy` shape.
        ValueError: When the policy's `strategy` is not in the live serve strategy set.
    """
    return _effective_baseline_policy(_active_policy_path(knowledge_base_path), default_instructions=DEFAULT_INSTRUCTIONS)
