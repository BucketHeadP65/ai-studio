"""The stand-in environment shared by the surface and loop-guard tests.

A plain `FunctionToolset` wearing `RetrievalSurface`'s two methods rather than a live engram,
so an environment can be built and its grants asserted without an engram subprocess. The seams that
broke are all still real: the filter, the per-step grant, the derived offer, and the cold
refusal. What it does not exercise is the MCP transport, which was never the thing at fault --
that was verified against the running server instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp
from typing import TYPE_CHECKING, Any

from core.ai.workflows.contracts import Briefing, FromNode, FromParameter, ParameterSpec, RunNode, Shape, Workflow
from engram.authoring.contracts import AuthoringKnowledgeBase, AuthoringTools
from engram.sources.contracts import SourceQueue, SourceQueueDecision, claimants_for
from engram.testing.fakes.authoring import FakeAdminGateway, FakeReindex
from engram.testing.specs import research_spec
from mcp import types as mcp_types
from pydantic_ai.toolsets import FunctionToolset

from harness.adapters.model_list import ModelList
from harness.adapters.model_tiers import ModelTiers
from harness.composition import build_environment
from harness.session import NoSessionLog
from harness.settings import WebSearchSettings
from tests.support.state import a_state, records_under
from tests.support.tiers import answering_on

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from core.ai.workflows.contracts import StepRegistry
    from pydantic_ai import RunContext
    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai.models import Model
    from pydantic_ai.tools import ToolDefinition
    from pydantic_ai.toolsets import FilteredToolset
    from pydantic_monty import AsyncMonty

    from harness.adapters.model_tiers import ModelBuilder
    from harness.domain.ports import StepModelWrapper
    from harness.session import ActiveRun

CALLED: list[str] = []
"""Which tools the surface was actually asked to run, in order."""

BOOT: list[str] = []
"""Tools called OUTSIDE any run. Expected to stay EMPTY: a host that reads a server before serving
is doing something a test should have to opt into, and this app no longer does it at all.

Separate from `CALLED` on purpose -- an in-run tool call and a boot read are different events, and
folding them together would make every "what did this step call" assertion carry a boot artifact."""

SEEN: list[str] = []
"""What the MODEL was offered on its first request -- the other half of the question.

Since the steps hold `CodeMode`, the granted tools are Python functions inside one `run_code`
rather than tools the model calls itself, so "what did it hold" and "what did it call" are two
different assertions and both matter."""


@dataclass
class FakeSurface:
    """Engram's MCP server's silhouette -- `list_tools`, `filtered`, `direct_call_tool` -- over plain functions.

    An environment can be built and its grants asserted without an engram subprocess. Structurally
    satisfies `harness.domain.ports.KnowledgeBaseSurface`, the same three-member slice `MCPToolset` does, so no
    cast is needed at the one call site -- a stand-in that renamed or reshaped one of the three
    would fail the same way the real thing would.
    """

    inner: FunctionToolset[object]
    """`object` deps, like the real surface: an engram toolset wants nothing from an agent's deps."""
    names: tuple[str, ...]
    schema: str = "# api.cards (view)\n- id text: Page id.\n- title text: Page title."
    """What `describe_schema` answers here. Short, but real in shape: the capability puts it in the
    instructions, so a test can assert the columns reached the agent."""

    async def list_tools(self) -> list[mcp_types.Tool]:
        return [mcp_types.Tool(name=name, input_schema={}) for name in self.names]

    def filtered(self, filter_func: Callable[[RunContext[Any], ToolDefinition], bool | Awaitable[bool]]) -> FilteredToolset[Any]:
        return self.inner.filtered(filter_func)

    async def direct_call_tool(self, name: str, args: dict[str, Any], *, metadata: dict[str, Any] | None = None, use_task: bool = False) -> Any:
        """What `read_schema` calls at boot. A stand-in answers with a schema a test can assert on."""
        BOOT.append(name)
        return self.schema


def fake_engram() -> FakeSurface:
    """Reads, the noise tool, and the four authoring tools, under engram's real tool names.

    The names matter: the app filters and grants by name, so a stand-in that renamed them would
    pass while the real thing failed.
    """

    def search(query: str, k: int = 5) -> str:
        """Ranked search over the knowledge base."""
        CALLED.append("search")
        return "paper.one :: a hit"

    def fetch(unit_id: str = "") -> str:
        """One knowledge base's contents, opened by id, or listed if none is given."""
        CALLED.append("fetch")
        return "paper.one :: the whole page"

    def run_sql(sql: str) -> dict[str, object]:
        """One read-only statement against the api surface."""
        CALLED.append("run_sql")
        return {"rows": [{"claim_a": "paper.one#c1", "claim_b": "paper.two#c3"}], "note": None}

    def graph_expand(seed_ids: list[str], hops: int = 1) -> str:
        """Typed relations out from some seeds."""
        CALLED.append("graph_expand")
        return "ent.a --requires--> ent.b"

    def describe_schema() -> str:
        """Every view and its columns -- a tool, not an injection."""
        CALLED.append("describe_schema")
        return "# api.cards (view)\n- id text: Page id.\n- title text: Page title."

    def submit_feedback(message: str) -> str:
        """Feedback about the retrieval tools -- real, and irrelevant to a workflow step."""
        CALLED.append("submit_feedback")
        return "thanks"

    functions = [search, fetch, run_sql, graph_expand, describe_schema, submit_feedback, *_authoring_fakes()]
    return FakeSurface(inner=FunctionToolset[object](functions), names=tuple(fn.__name__ for fn in functions))


def _authoring_fakes() -> list[Callable[..., object]]:
    """The authoring plugins under engram's real tool names, each recording its call."""

    def list_undigested(knowledge_base: str, limit: int = 40, source_ids: list[str] | None = None) -> list[dict[str, str]]:
        """List source mirrors that have no digest yet."""
        CALLED.append("list_undigested")
        return [{"source_id": "yt-abc", "provider": "intake", "item_type": "video", "title": "a talk"}]

    def write_note(knowledge_base: str, payload: dict[str, object]) -> str:
        """Write one validated page into a knowledge base and index it."""
        CALLED.append("write_note")
        return "talk.a-talk"

    def request_source(knowledge_base: str, url: str, found_via: str = "") -> str:
        """Queue a URL for a knowledge base's next sync."""
        CALLED.append("request_source")
        return "requested"

    def read_source(knowledge_base: str, source_id: str) -> dict[str, object]:
        """Read one source mirror whole, as its chunks in order."""
        CALLED.append("read_source")
        return {
            "source_id": source_id,
            "provider": "intake",
            "title": "a talk",
            "item_type": "video",
            "chunks": [{"loc": f"{source_id}#0", "heading": "Intro", "text": "Hello."}],
        }

    def list_recent_digests(knowledge_base: str, since_hours: int = 26, limit: int = 40) -> list[dict[str, str]]:
        """List the digest pages indexed within the last N hours, newest first."""
        CALLED.append("list_recent_digests")
        return [{"digest_id": "talk.a-talk", "kind": "talk", "title": "a talk"}]

    def digest_skeleton(knowledge_base: str, source_id: str) -> dict[str, object]:
        """A ready write_note payload for one source's digest: identity filled, blanks to fill."""
        CALLED.append("digest_skeleton")
        return {
            "frontmatter": {"id": "talk.a-talk", "type": "talk", "title": "a talk", "summary": "", "status": "active", "digest_of": source_id},
            "sections": [{"heading": "TL;DR", "body": ""}, {"heading": "Entities", "body": ""}],
            "claims": [{"text": "", "kind": "", "quote": "", "loc": ""}],
            "fill": {"claim.kind": ["observation"], "rules": []},
        }

    def resolve_entities(knowledge_base: str, entities: list[dict[str, object]]) -> str:
        """Resolve the names a page mentions to one entity page id each, creating a page for each name that matches none."""
        CALLED.append("resolve_entities")
        return '{"entities": [{"name": "Kestrel Labs", "id": "ent.kestrel-labs", "created": true}]}'

    def read_page(knowledge_base: str, page_id: str) -> dict[str, object]:
        """Read one indexed page by its id: its path and a ready write_note payload of its file."""
        CALLED.append("read_page")
        frontmatter = {"id": page_id, "type": "talk", "title": "a talk", "status": "active"}
        return {"page_id": page_id, "path": "Wiki/Talks/a-talk.md", "payload": {"frontmatter": frontmatter, "sections": [], "claims": []}}

    return [
        list_undigested,
        write_note,
        request_source,
        read_source,
        list_recent_digests,
        digest_skeleton,
        resolve_entities,
        read_page,
        *_owner_plugin_fakes(),
    ]


def _owner_plugin_fakes() -> list[Callable[..., object]]:
    """The owner's five plugins over the source-queue policy, the source queue and its decisions, each recording its call."""

    def source_queue_policy(knowledge_base: str) -> dict[str, object]:
        """A knowledge base's source-queue policy: its hard rules, its guidance, and its purpose."""
        CALLED.append("source_queue_policy")
        return {
            "rules": {"kinds": ["video", "paper", "post", "article"], "max_age_years": None, "hop_depth": 1, "per_source_cap": 5, "never": ["repository"]},
            "guidance": "Take what advances the base.",
            "purpose": "A test base.",
            "present": True,
        }

    def write_policy(knowledge_base: str, rules: dict[str, object], guidance: str) -> str:
        """Replace a knowledge base's source-queue policy on the owner's word."""
        CALLED.append("write_policy")
        return "policy written"

    def source_queue_rows(knowledge_base: str, status: str = "", limit: int = 40) -> list[dict[str, str]]:
        """A knowledge base's source queue newest first."""
        CALLED.append("source_queue_rows")
        return [{"key": "arxiv-2401.00001", "url": "https://arxiv.org/abs/2401.00001", "kind": "paper", "status": "seen", "reason": "beyond hop depth 1", "found_via": "yt-abc", "found_at": ""}]

    def source_queue_decide(knowledge_base: str, key: str, action: SourceQueueDecision, reason: str = "") -> str:
        """Fetch or drop one source-queue row on the owner's word."""
        CALLED.append("source_queue_decide")
        return f"{key} is now requested"

    def decide_page(
        knowledge_base: str, page_id: str, status: str = "", links: dict[str, list[str]] | None = None, reason: str = "", *, add: bool = False
    ) -> str:
        """Move one page to one of its page type's declared statuses, or write only its link fields."""
        CALLED.append("decide_page")
        return f"{page_id} is now {status}"

    return [source_queue_policy, write_policy, source_queue_rows, source_queue_decide, decide_page]


async def world_over(
    model: Model,
    *,
    plugins: AuthoringTools | None = None,
    spend: AbstractCapability[object] | None = None,
    state: Path | None = None,
    active: ActiveRun | None = None,
    sandbox: AsyncMonty | None = None,
    build: ModelBuilder | None = None,
    models: ModelList | None = None,
    wrap_model: StepModelWrapper | None = None,
) -> StepRegistry:
    """An environment over the stand-in surface. There is no knowledge base DSN any more: nothing here connects.

    `plugins` is `None` by default -- the grounding tools stay off `environment.grants`, exactly as a
    environment with no `AuthoringTools` behaves in production. `fake_plugins()` builds one over engram's
    own fakes for a test that needs `knowledge_base_profile`/`recall_beliefs`/`remember_belief` grantable.
    `spend` is `None` by default -- a step's agent mounts no money brake, exactly as an environment built
    with no priced deployment behaves in production; a test on the brake itself passes one. `state` is the
    home this environment's plugin switches, tier settings and spend rows live under; `None` opens a fresh
    directory of its own, so nothing here reads or writes the real one. `active` is the run state a
    worker step reads and writes, `sandbox` an entered pool for a step that brings a program, and
    `build` how the orchestrator and helper tiers build their model, and `models` the list a stored
    tier setting is checked against -- `None` for either answers both tiers on `model` itself.
    `wrap_model` is what every role's model is put through before a step agent is built over it,
    `None` leaving each role on the model itself, which is what production does.
    """
    home = state if state is not None else Path(mkdtemp(prefix="harness-environment-"))
    tiers = ModelTiers(
        state=a_state(home),
        models=models if models is not None else ModelList(listing=None),
        build=build if build is not None else answering_on(model),
    )
    return await build_environment(
        model,
        engram=fake_engram(),
        plugins=plugins,
        spend=spend,
        active=active,
        sandbox=sandbox,
        web_search=WebSearchSettings(),
        tiers=tiers,
        external_agents=dict,
        session_log=NoSessionLog(),
        custom=home / "custom",
        records=records_under(home),
        wrap_model=wrap_model,
    )


def fake_plugins() -> AuthoringTools:
    """An `AuthoringTools` over engram's own fakes -- no filesystem, no database, one knowledge_base: `research`.

    `memory_for` is left unset, so `recall_beliefs`/`remember_belief` answer that agent memory is
    not mounted rather than raising -- what is under test here is the grant and the mount, not the
    memory store behind it.
    """
    spec = research_spec()
    knowledge_base = AuthoringKnowledgeBase(name="research", collection="research", path=Path(), schema=spec)
    return AuthoringTools(
        knowledge_bases={"research": knowledge_base},
        reindex=FakeReindex(),
        admin=FakeAdminGateway(),
        queue_for=lambda one: SourceQueue(one.path / "_meta" / "source-queue.yaml", connectors=claimants_for(one.schema.sources.connectors)),
    )


def defined_step(*, tools: tuple[str, ...]) -> Briefing:
    return Briefing(
        name="dig",
        instructions="Find what the knowledge base says about the question and report it in a single note.",
        inputs={"question": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}),
        tools=tools,
    )


def spec_for(task: Briefing) -> Workflow:
    return Workflow(
        name="dig-once",
        version=1,
        description="One invented step, so the tools it asked for are the tools under test.",
        parameters={"question": ParameterSpec(description="what to look into", json_schema={"type": "string"})},
        inline=(task,),
        nodes=(RunNode(id="dig", work="dig", inputs={"question": FromParameter(parameter="question")}),),
        output=FromNode(node="dig"),
    )
