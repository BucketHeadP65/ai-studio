"""Ergonomics demo: the repo-docs knowledge_base_schema served through `ResultDisclosure`, live.

Run from `studio/` (uses the same `application_settings.json` Azure connection as everything else;
pass a deployment name to override the default chat model):

    uv run python examples/disclosure_ergonomics.py [deployment]

The agent holds two plain tools — `knowledge_base_spec()` returning the full `KnowledgeBaseSchema` and
`page_types()` returning `list[PageTypeSpec]` — with `ResultDisclosure` installed. The transcript
printed at the end shows exactly what the model saw at each step: the collapsed ref+summary
envelopes, any `load_result` calls it chose to make, and the raw-vs-collapsed size of every
disclosed result.
"""

from __future__ import annotations

import sys

from pathlib import Path

from core.ai import create_model
from core.ai.disclosure.contracts import ResultDisclosure
from engram.evals.harness.contracts import configure_logfire
from engram.kb.contracts import KnowledgeBaseSchema, PageTypeSpec
from pydantic_ai import Agent
from pydantic_ai.capabilities import Instrumentation
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.toolsets import FunctionToolset

from studio.knowledge_base.settings import fetch_application_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC = KnowledgeBaseSchema.from_path(REPO_ROOT / "docs" / "knowledge-base-schema.json")

QUESTION = (
    "Which canonical sections must a memory-unit page have, and which relations exist in this knowledge base? "
    "Use the tools; load full records only if the summaries are not enough."
)


def build_agent(deployment: str, endpoint: str) -> Agent[None, str]:
    """The demo agent: two plain spec tools, `ResultDisclosure`, nothing else."""
    toolset: FunctionToolset[None] = FunctionToolset()

    async def knowledge_base_spec() -> KnowledgeBaseSchema:
        return SPEC

    async def page_types() -> list[PageTypeSpec]:
        return list(SPEC.page_types)

    toolset.add_function(knowledge_base_spec, takes_ctx=False, name="knowledge_base_spec", description="The full knowledge base ontology.")
    toolset.add_function(page_types, takes_ctx=False, name="page_types", description="The knowledge base's page-type declarations.")
    return Agent(
        create_model(deployment, endpoint=endpoint),
        deps_type=type(None),
        toolsets=[toolset],
        capabilities=[Instrumentation(), ResultDisclosure[None]()],
        instructions="Answer from the knowledge base tools.",
    )


def print_transcript(messages: list[ModelRequest | ModelResponse]) -> None:
    """Every part of every message, sized, in order — what the model saw and what it did."""
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                print(f"  -> tool call   {part.tool_name}({part.args})")
            elif isinstance(part, ToolReturnPart):
                rendered = part.model_response_str()
                print(f"  <- tool return {part.tool_name}: {len(rendered)} chars")
                print(f"       {rendered[:400]}{'…' if len(rendered) > 400 else ''}")
            elif isinstance(part, TextPart):
                print(f"  == answer ==\n{part.content}\n")


def main() -> None:
    """Run the demo and print raw-vs-disclosed sizes plus the full transcript."""
    settings = fetch_application_settings()
    shipped = configure_logfire(settings.infrastructure.logfire)
    print(f"logfire    : {'configured — spans ship to your project' if shipped else 'NOT configured (no write token in application_settings.json)'}")
    deployment = sys.argv[1] if len(sys.argv) > 1 else settings.ingestion.summary_model
    endpoint = settings.infrastructure.azure_openai.endpoint
    if not endpoint:
        print("No Azure endpoint configured (infrastructure.azure_openai.endpoint) — fill application_settings.json first.")
        raise SystemExit(1)

    raw_spec = len(SPEC.model_dump_json())
    raw_types = sum(len(pt.model_dump_json()) for pt in SPEC.page_types)
    print(f"raw sizes  : knowledge_base_spec {raw_spec} chars; page_types {raw_types} chars")
    print(f"disclosed  : knowledge_base_spec -> 1 stub ({len(SPEC.disclosure_summary)} chars); page_types -> ", end="")
    print(f"{len(SPEC.page_types)} stubs ({sum(len(pt.disclosure_summary) for pt in SPEC.page_types)} chars)")
    print(f"model      : {deployment} @ {endpoint}\n")
    print(f"question   : {QUESTION}\n")

    agent = build_agent(deployment, endpoint)
    result = agent.run_sync(QUESTION)

    print_transcript(list(result.all_messages()))
    usage = result.usage
    print(f"\nusage: {usage.requests} requests, {usage.input_tokens} in / {usage.output_tokens} out")


if __name__ == "__main__":
    main()
