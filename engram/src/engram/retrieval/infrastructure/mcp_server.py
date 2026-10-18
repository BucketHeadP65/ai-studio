r"""The FastMCP stdio server over the retrieval read side.

`create_mcp_server` builds the server from injected dependencies and reads no container and no
environment. `describe_schema` and `run_sql` are always registered. `run_sql` caps rows at
`min(row_limit, 50)` and answers a read-only violation with a message rather than a crash. The read
tools, `run_code` and the opt-in authoring, tuning and recommendation tools register only when their
dependencies are wired. Importing this module imports `engram.evals`.
"""

from __future__ import annotations

import json
import re

from typing import TYPE_CHECKING

# `GraphPathsResult` and `GraphResult` must be imported at runtime: FastMCP evaluates tool annotations to publish `outputSchema`.
from core.ai.retrieval.contracts import RUN_CODE, TOOL_NAMES, GraphPathsResult
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ToolReturn
from pydantic_ai.models import Model
from pydantic_ai.tool_manager import ToolManager
from pydantic_ai.usage import RunUsage

from engram.authoring.contracts import EntityName  # noqa: TC001 (FastMCP evaluates tool annotations at registration)
from engram.evals.generation.contracts import (
    AuthoredCase,
    SeedCaseDraft,
    accept,
    build_dataset,
    load_dataset,
    resolve_archetype,
    save_dataset,
)
from engram.evals.recommendation.contracts import register_recommendation_tools
from engram.evals.shared.contracts import DEFAULT_DIFFICULTY_THRESHOLDS, ExpectedQa, Seed
from engram.evals.tuning.contracts import register_tuning_tools
from engram.retrieval.domain import FeedbackCategory, GraphExpand, GraphPaths, GraphResult
from engram.retrieval.infrastructure.gateway import read_snapshot
from engram.retrieval.queries import expansion_hops, graph_next_steps, paths_next_steps, route_hops, routed_graph_paths, routed_graph_result
from engram.retrieval.sql_tools import (
    DESCRIBE_SCHEMA_DESCRIPTION,
    RUN_SQL_DESCRIPTION,
    SANDBOX_RUN_SQL_DESCRIPTION,
    make_describe_schema,
    make_run_sql,
    make_sandbox_run_sql,
)
from engram.retrieval.tools import (
    FETCH,
    FIND_PATHS,
    GRAPH_EXPAND,
    SEARCH,
    RetrievalHandlers,
    add_routed_read_tools,
    build_retrieval_toolset,
    fetch_description,
    find_paths_description,
    flat_fetch,
    graph_expand_description,
    register_mcp_tools,
    register_routed_mcp_tools,
    reground_phrase,
    resolve_description,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from core.ai.embedding import QueryEmbedder
    from core.ai.retrieval.contracts import StampSource
    from pydantic_ai import FunctionToolset
    from pydantic_ai.messages import ModelMessage, ModelResponse
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.settings import ModelSettings
    from pydantic_ai_harness.code_mode import CodeModeToolset

    from engram.authoring.contracts import AuthoringTools
    from engram.config import RecommendationSettings
    from engram.evals.commands import KnowledgeBaseGatewayFactory, PolicyEvaluatorFactory, SeedRepositoryFactory
    from engram.evals.generation.contracts import EvaluationCase
    from engram.evals.shared.contracts import DifficultyThresholds
    from engram.indexing.contracts import RetrievalSwitches
    from engram.retrieval.capability import ReadSurface
    from engram.retrieval.domain import FeedbackSink, Search, SqlGateway
    from engram.retrieval.envelope import EnvelopeHandler
    from engram.retrieval.queries import DescribeSchemaHandler, FetchHandler, GraphExpandHandler, GraphPathsHandler
    from engram.retrieval.sql_tools import DescribeSchemaTool
    from engram.retrieval.tools import SupportsSearch

    CodeModeToolsetT = CodeModeToolset[None]


# The most seeds one `propose_eval_seeds` call draws, whatever `count` asks for.
_MAX_SEED_COUNT = 50


# The MCP graph tools extend the shared descriptions with their extra arguments. Each anchor sits in a
# sentence the `engram.retrieval.tools` builder never rewrites, so it matches every `can_search` variant.
def _extend_graph_expand_description(base: str) -> str:
    """`base` with the MCP-only `rel_types` / `k` / `include_superseded` arg clauses folded in."""
    return base.replace(
        "(default 2). Each edge",
        "(default 2). `rel_types` restricts the relations followed (omit to traverse every relation). "
        "`k` caps the returned neighbour nodes (default 20). `include_superseded=true` also walks pages a newer one "
        "replaced, which the walk skips by default along with anything reachable only through them. Each edge",
        1,
    )


def _extend_find_paths_description(base: str) -> str:
    """`base` with the MCP-only `max_hops` / `rel_types` / `k` / `include_superseded` arg clauses folded in."""
    anchor = " Use it to explain HOW two items relate"
    extra = (
        " `max_hops` bounds the route length (default 4). `rel_types` restricts the "
        "relations traversed (omit to traverse every relation). `k` caps the number of returned paths "
        "(default 10). `include_superseded=true` also keeps routes through pages a newer one replaced, "
        "which are skipped by default."
    )
    return base.replace(anchor, f"{extra}{anchor}", 1)


# The orienting prose of `run_code`. The code-mode toolset appends the per-function catalogue.
_RUN_CODE_DESCRIPTION = (
    "Run Python in a sandbox to orchestrate retrieval in ONE round-trip instead of many tool calls. "
    "The sandbox (Monty, a Python subset) binds engram's retrieval functions as callables. Every "
    "bound function takes keyword arguments only, exactly like the standalone tools: "
    "`search(query=..., k=12)` and `fetch(unit_id=...)` (no argument opens the knowledge base map) for hybrid "
    "search and a leveled read, `graph_expand(seed_ids=..., hops=2)` and "
    "`find_paths(from_id=..., to_id=...)` (the graph leg), plus `run_sql(sql=..., row_limit=50)` and "
    "`describe_schema()`, the same read-only `api.*` surface the standalone tools expose. Every "
    "function returns STRUCTURED data: `search` a dict with `hits` (a list of hit dicts) and "
    "`next_steps` (what to do next), `fetch` a dict that also carries `next_steps`, `run_sql` a list "
    "of row dicts, etc., so index a result with `row['id']`, not `row.id` (`describe_schema` is the "
    "one exception: it returns M-Schema text). Async functions need `await`. Run independent calls "
    "concurrently, e.g. `import asyncio; hits = await asyncio.gather(search(query='a'), "
    "search(query='b'))`. `run_sql` stays bounded by the read-only `kb_reader` role and the "
    "50-row cap, the same constraints as calling it directly. The last expression's value is returned "
    "(don't `print()` it)."
)

# The `search` signature in `_RUN_CODE_DESCRIPTION`, rewritten without `k` when the host owns the cap.
_RUN_CODE_K_CLAUSE = ("`search(query=..., k=12)`", "`search(query=...)`")

# The graph leg in `_RUN_CODE_DESCRIPTION`, dropped when the sandbox binds no graph tools.
_RUN_CODE_GRAPH_CLAUSE = ("`graph_expand(seed_ids=..., hops=2)` and `find_paths(from_id=..., to_id=...)` (the graph leg), plus ", "plus ")


class _NoModel(Model):
    """A `Model` for the code-mode driver that raises on any request."""

    @property
    def model_name(self) -> str:
        return "engram-no-model"

    @property
    def system(self) -> str:
        return "engram"

    async def request(
        self, messages: list[ModelMessage], model_settings: ModelSettings | None, model_request_parameters: ModelRequestParameters
    ) -> ModelResponse:  # pragma: no cover (the code-mode path is LLM-free, so this is never reached)
        del messages, model_settings, model_request_parameters  # interface args, deliberately unused
        msg = "engram's code-mode sandbox is LLM-free. It never makes a model request."
        raise RuntimeError(msg)


_NO_MODEL = _NoModel()


# The description of `submit_feedback`.
_FEEDBACK_DESCRIPTION = (
    "Send feedback about THESE retrieval tools to the knowledge base's maintainer: anything that would make "
    "retrieval better: a tool whose description was unclear or misleading, a capability you wished "
    "existed, a nice-to-have, a bottleneck or slow path you hit, or a bug. `category` is one of "
    "usage | feature | nice-to-have | bottleneck | bug | other. `tool` optionally names the tool it is "
    "about. `message` is your free-text observation. It is appended to a local file for the operator to "
    "review: it does NOT phone home and does not change retrieval. Use it when you have a concrete, "
    "actionable observation, not for routine acknowledgements."
)


# The two opt-in evaluation authoring tool names. Both register only when `only` names `PROPOSE_SEEDS`
# and the authoring dependencies are wired.
PROPOSE_SEEDS = "propose_eval_seeds"
SUBMIT_CASES = "submit_eval_cases"

# A `submit_eval_cases` dataset name must be one path segment of letters, digits, underscore or hyphen.
_DATASET_NAME = re.compile(r"[A-Za-z0-9_-]+")

# The description of `propose_eval_seeds`, step one of the authoring flow.
_PROPOSE_SEEDS_DESCRIPTION = (
    "Propose structural seeds for authoring golden eval cases (step one of the propose -> draft -> "
    "submit flow). `archetype` is the retrieval shape (pinpoint | survey | trace | synthesis | passage). `count` is how "
    "many seeds to draw. `collection` is the knowledge base partition (default "
    "this server's own). Each returned seed carries `gold_ids` (the correct source ids, KNOWN BY CONSTRUCTION "
    "from the indexed graph: do NOT invent or change them), `source_bodies` (the passages to draft "
    "from), and its structural shape. The reply also carries per-archetype drafting `instructions`. "
    "For each seed write one self-contained question and answer, then set `support_quotes` to SHORT "
    "snippets copied VERBATIM from that seed's `source_bodies`, and submit them with "
    "`submit_eval_cases`. The server re-verifies every quote verbatim against the seed's source "
    "bodies, so a submitted case is quote-checked by construction."
)

# The description of `submit_eval_cases`, step two of the authoring flow.
_SUBMIT_CASES_DESCRIPTION = (
    "Submit drafted golden eval cases for the quote check (step two of propose -> draft -> "
    "submit). `cases_json` is a JSON array of objects, each with the keys `seed` (the seed object "
    "from propose_eval_seeds, unchanged), `question`, `answer`, and `support_quotes` (snippets copied "
    "VERBATIM from that seed's source bodies). `dataset` names the destination dataset (default "
    "authored). The server gates each "
    "case: it keeps only those whose every support quote is VERBATIM in the seed's source bodies "
    "(quote-checked by construction) and takes the gold ids FROM THE SEED, never from the client. Accepted "
    "cases are appended to the dataset YAML. The reply is `{accepted, rejected, dataset}`. A "
    "fabricated quote is dropped (counted in `rejected`), so copy quotes exactly."
)


# The descriptions of the opt-in documentation authoring tools, each naming the `knowledge_base` argument.
_KNOWLEDGE_BASE_CLAUSE = "knowledge_base names which knowledge base. api.about lists the ones reachable here, and an unknown name is answered with the valid ones."

_KNOWLEDGE_BASE_PROFILE_DESCRIPTION = (
    "Read one knowledge base's ground truth before writing into it. Returns its purpose, its facet, "
    "relation, claim and entity vocabulary, every page type with its authoring guidance and its link fields, and its past decisions, "
    f"so word choices and structure match the base's own conventions. {_KNOWLEDGE_BASE_CLAUSE}"
)

_SKELETON_DESCRIPTION = (
    "Get a blank write_note payload for one page type in a knowledge base. The frontmatter and sections "
    "come back already shaped for that page_type, each required field and each section carrying its own "
    f"guidance, so the next call to write_note starts from a correct shape instead of a blank page. {_KNOWLEDGE_BASE_CLAUSE}"
)

_WRITE_NOTE_DESCRIPTION = (
    "Write one validated page into a knowledge base and reindex it. The payload is checked against "
    "the base's own grammar first, and a refusal names every finding at once. Writing an id already on "
    "disk replaces that page. In section prose, cite a page as [[its-file-stem|its.full.id]] and a claim "
    "as [[its-file-stem#^cN|^cN]]. A bare page id or claim anchor that provably resolves is rewritten "
    f"into that link for you, and every rewrite or unresolvable citation is named in the warnings. {_KNOWLEDGE_BASE_CLAUSE} "
    "path is optional, it places the page at that knowledge-base-root-relative file instead of its page_type's own folder."
)

_DECIDE_PAGE_DESCRIPTION = (
    "Move one page to one of its page_type's declared statuses when the owner decided it, set the link fields an outcome asks "
    f"for, then reindex so the change is what searches see. {_KNOWLEDGE_BASE_CLAUSE} page_id is the page's id, status is one of its "
    "page_type's declared statuses, links is an optional mapping of link field name to the page ids it should now hold "
    "(replacing that field's whole entry), and reason is an optional note kept with the decision. An empty status keeps the "
    "page's current status, so a step whose instructions ask for it can write only links and a reason. With add true, the ids "
    "join what each named field already holds, each id once, instead of replacing it, so the call never removes a link."
)

_RESOLVE_ENTITIES_DESCRIPTION = (
    "Resolve the names a page mentions to one entity page id each, creating a page for each name that matches none, and "
    "reindex once when anything was written. A name matches an entity page when it equals the page's title or one of its "
    "aliases once case, punctuation and a leading 'the' are set aside, and a matched page takes the name and its aliases as "
    "new aliases when no page holds them yet. A new name whose page id an existing page already carries joins that page. "
    f"{_KNOWLEDGE_BASE_CLAUSE} entities lists every name to resolve, each with its name, "
    "an entity_type the schema declares, optional aliases and a one-sentence summary a new page carries. Pass every name in "
    'one call. The answer is {"entities": [{"name", "id", "created"}]} in input order.'
)

_READ_PAGE_DESCRIPTION = (
    "Read one indexed page by its id before rewriting it: the answer is its page_id, its path relative to the knowledge "
    "base root and its payload, the file parsed into the frontmatter, sections and claims write_note takes. Change only "
    "what you mean to change on that payload and pass it to write_note with the same path, and every other frontmatter "
    f"value and the body stay as they are. {_KNOWLEDGE_BASE_CLAUSE} An id no indexed page carries is refused, so is a "
    "file the payload cannot carry whole, and a page written since the knowledge base last indexed is not found yet."
)


class _SubmittedCase(BaseModel):
    """One inbound `submit_eval_cases` item, validated at the MCP boundary."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    seed: Seed
    question: str
    answer: str
    support_quotes: list[str]


_SUBMITTED_CASES_ADAPTER = TypeAdapter(list[_SubmittedCase])


def _library_paragraph(collection: str) -> str:
    """The handshake paragraph naming `collection` as the default and pointing at `api.about` for the rest."""
    return (
        f"This database is shared across more than one collection. The default here is '{collection}'. "
        "Run api.about (via run_sql) to see what else is here and what each one covers, and pass "
        "collection to search, or filter on it in run_sql, to reach them."
    )


def create_mcp_server(
    gateway: SqlGateway,
    describe_handler: DescribeSchemaHandler,
    embed_query: QueryEmbedder | None = None,
    *,
    search_handler: SupportsSearch[Search] | None = None,
    fetch_handler: FetchHandler | None = None,
    graph_expand_handler: GraphExpandHandler | None = None,
    find_paths_handler: GraphPathsHandler | None = None,
    envelope_handler: EnvelopeHandler | None = None,
    only: frozenset[str] | None = None,
    search_hybrid: bool | None = None,
    switches: RetrievalSwitches | None = None,
    collection: str,
    feedback_sink: FeedbackSink | None = None,
    seed_repo_factory: SeedRepositoryFactory | None = None,
    datasets_dir: Path | None = None,
    thresholds: DifficultyThresholds | None = None,
    policy_evaluator_factory: PolicyEvaluatorFactory | None = None,
    policies_dir: Path | None = None,
    gateway_factory: KnowledgeBaseGatewayFactory | None = None,
    recommendation_settings: RecommendationSettings | None = None,
    recommendations_dir: Path | None = None,
    knowledge_base_path: Path | None = None,
    descriptions: Mapping[str, str] | None = None,
    instructions: str | None = None,
    k_source: Callable[[], int] | None = None,
    stamp: StampSource | None = None,
    doc_authoring_tools: AuthoringTools | None = None,
    read_surfaces: tuple[ReadSurface, ...] | None = None,
    facet_keys: tuple[str, ...] = (),
) -> FastMCP:
    """Build the FastMCP server with its tools closing over the injected dependencies.

    It reads no container and no environment. `describe_schema` and `run_sql` are always registered.
    Every other tool registers only when its dependencies are wired, as each argument says.

    Args:
        gateway: The read-only SQL gateway `run_sql` reads through.
        describe_handler: Renders the `api` surface for `describe_schema`.
        embed_query: Turns a `vector_query => '<text>'` argument of `run_sql` into a vector. `None`
            degrades it to `query_vec => NULL` with a note.
        search_handler: The `search` handler. Together with `fetch_handler` and both graph handlers it
            registers `search` and `fetch` and makes `run_code` possible.
        fetch_handler: The `fetch` handler. On its own it registers a flat `fetch`.
        graph_expand_handler: When wired, registers `graph_expand`.
        find_paths_handler: When wired, registers `find_paths`.
        envelope_handler: When wired, turns `fetch`, including the one inside `run_code`, into the
            leveled read of a `collection:<name>`, `page:<id>` or `section:<id>` ref. A bad ref fails
            that one call with `ToolError`.
        only: The profile allow-set for `search`, `fetch`, `graph_expand` and `find_paths`. `None`
            admits every tool with a handler. `run_code` and the evaluation authoring tools register
            only when named here.
        search_hybrid: Forces the search leg: `True` hybrid, `False` BM25 only, `None` the agent's choice.
        switches: The stored retrieval switches of an auto-mode knowledge base, read once when the
            server is built. `None` for a fixed-mode one. An off graph switch withholds `graph_expand`
            and `find_paths`, inside `run_code` too, even when `only` names them. Without
            `read_surfaces`, an off embeddings switch makes a `search` whose `search_hybrid` is `None`
            keyword-only.
        collection: The knowledge base's own collection, which `search` scopes to by default. There
            is no default collection.
        feedback_sink: When wired, registers `submit_feedback`, which records through this sink.
        seed_repo_factory: Opens a structural seed repository per `propose_eval_seeds` call. With
            `datasets_dir` and `PROPOSE_SEEDS` in `only`, registers `propose_eval_seeds` and
            `submit_eval_cases`.
        datasets_dir: The directory accepted cases are appended into, one YAML per dataset name. The
            tuning and recommendation tools need it too.
        thresholds: The difficulty band a kept case is stamped with. Defaults to
            `DEFAULT_DIFFICULTY_THRESHOLDS`.
        policy_evaluator_factory: Builds a `PolicyEvaluator` for the model a call chooses. With
            `datasets_dir` and `policies_dir`, registers the tuning tools `diagnose`,
            `evaluate_policy` and `submit_tuning`.
        policies_dir: Where winning policies are written.
        gateway_factory: Opens a knowledge base gateway. With `policy_evaluator_factory`,
            `recommendation_settings`, `datasets_dir`, `recommendations_dir` and `knowledge_base_path`,
            registers the recommendation tools `diagnose_structure`, `what_if_edges` and
            `submit_recommendation`.
        recommendation_settings: The detector and hop settings of the recommendation tools.
        recommendations_dir: Where recommendations are written.
        knowledge_base_path: The knowledge base root every submitted recommendation is validated against.
        descriptions: Description overrides keyed by the four read tool names. `None` keeps the shipped
            descriptions, and keys for other tools are ignored.
        instructions: Guidance the MCP handshake advertises after the server's own paragraph, which
            names `collection` and points at `api.about`. `None` advertises that paragraph alone.
        k_source: Moves the search row cap from the agent to the host. When set, `search`, including
            the one inside `run_code`, takes no `k` and reads the cap on every call.
        stamp: Names the knowledge base that answered and how fresh its index is, on every `search`
            and `fetch` answer as `knowledge_base`, read on every call. `None` leaves it unset.
        doc_authoring_tools: When given, registers `knowledge_base_profile`, `skeleton`, `write_note`,
            `decide_page`, `resolve_entities` and `read_page`, each forwarding to it.
        read_surfaces: When given, `search` and `fetch` route over these knowledge bases, each through
            its own handlers, envelope, stamp and facet keys, inside `run_code` too. One must serve
            `collection`, which an omitted `collection` and an unqualified id read. `search_handler`,
            `fetch_handler` and `stamp` then serve only the tools inside `run_code`.
        facet_keys: The facet keys the knowledge base's schema declares, which the single-base `search`
            takes in `facets`, inside `run_code` too. Any other key fails the call naming them. Empty (the
            default) refuses every key. Unread with `read_surfaces`, whose `search` filters each surface by
            its own `facet_keys`.

    Returns:
        The server. The host runs it with `.run(transport="stdio")`.

    Raises:
        ValueError: When `read_surfaces` is given, none of them serves `collection`, and `search`, `fetch` or
            `run_code` registers.
    """
    library_paragraph = _library_paragraph(collection)
    combined_instructions = f"{library_paragraph}\n\n{instructions}" if instructions else library_paragraph
    server = FastMCP(name="engram", instructions=combined_instructions)

    # The same callables serve the code-mode sandbox below.
    describe_schema = make_describe_schema(describe_handler)
    run_sql = make_run_sql(gateway, embed_query)

    server.add_tool(describe_schema, name="describe_schema", description=DESCRIBE_SCHEMA_DESCRIPTION)
    server.add_tool(run_sql, name="run_sql", description=RUN_SQL_DESCRIPTION)
    if feedback_sink is not None:
        _add_feedback(server, feedback_sink)
    only = _switched_on(only, switches)
    handlers = _full_retrieval_handlers(search_handler, fetch_handler, graph_expand_handler, find_paths_handler)
    available = _available_read_tools(
        handlers=handlers,
        fetch_handler=fetch_handler,
        graph_expand_handler=graph_expand_handler,
        find_paths_handler=find_paths_handler,
        read_surfaces=read_surfaces,
        only=only,
    )
    if graph_expand_handler is not None and _admits(GRAPH_EXPAND, only):
        _add_graph_expand(server, graph_expand_handler, descriptions=descriptions, stamp=stamp, tools=available)
    if find_paths_handler is not None and _admits(FIND_PATHS, only):
        _add_find_paths(server, find_paths_handler, descriptions=descriptions, stamp=stamp, tools=available)
    _register_read_tools(
        server,
        handlers,
        available=available,
        fetch_handler=fetch_handler,
        read_surfaces=read_surfaces,
        only=only,
        search_hybrid=search_hybrid,
        switches=switches,
        descriptions=descriptions,
        collection=collection,
        envelope=envelope_handler,
        k_source=k_source,
        stamp=stamp,
        facet_keys=facet_keys,
    )
    if handlers is not None and only is not None and RUN_CODE in only:
        _add_run_code(
            server,
            handlers,
            gateway=gateway,
            embed_query=embed_query,
            describe_schema=describe_schema,
            search_hybrid=search_hybrid,
            switches=switches,
            collection=collection,
            envelope=envelope_handler,
            k_source=k_source,
            stamp=stamp,
            read_surfaces=read_surfaces,
            facet_keys=facet_keys,
        )
    if seed_repo_factory is not None and datasets_dir is not None and only is not None and PROPOSE_SEEDS in only:
        _add_eval_authoring(server, seed_repo_factory, datasets_dir, thresholds or DEFAULT_DIFFICULTY_THRESHOLDS, collection)
    if policy_evaluator_factory is not None and datasets_dir is not None and policies_dir is not None:
        register_tuning_tools(server, evaluator_factory=policy_evaluator_factory, datasets_dir=datasets_dir, policies_dir=policies_dir)
    if (
        gateway_factory is not None
        and policy_evaluator_factory is not None
        and recommendation_settings is not None
        and datasets_dir is not None
        and recommendations_dir is not None
        and knowledge_base_path is not None
    ):
        register_recommendation_tools(
            server,
            gateway_factory=gateway_factory,
            evaluator_factory=policy_evaluator_factory,
            recommendation_settings=recommendation_settings,
            datasets_dir=datasets_dir,
            recommendations_dir=recommendations_dir,
            knowledge_base_path=knowledge_base_path,
        )
    if doc_authoring_tools is not None:
        _add_doc_authoring(server, doc_authoring_tools)
    return server


def _register_read_tools(
    server: FastMCP,
    handlers: RetrievalHandlers[Search] | None,
    *,
    available: frozenset[str],
    fetch_handler: FetchHandler | None,
    read_surfaces: tuple[ReadSurface, ...] | None,
    only: frozenset[str] | None,
    search_hybrid: bool | None,
    switches: RetrievalSwitches | None,
    descriptions: Mapping[str, str] | None,
    collection: str,
    envelope: EnvelopeHandler | None,
    k_source: Callable[[], int] | None,
    stamp: StampSource | None,
    facet_keys: tuple[str, ...] = (),
) -> None:
    """Add `search` and `fetch`, routed over the registered knowledge bases or scoped to this one, refusals naming `available`."""
    if not (only is None or SEARCH in only or FETCH in only):
        return
    if read_surfaces is not None:
        register_routed_mcp_tools(
            server,
            read_surfaces,
            default_knowledge_base=_served_knowledge_base(read_surfaces, collection),
            only=only,
            descriptions=descriptions,
            k_source=k_source,
        )
        return
    if handlers is None:
        if fetch_handler is not None and _admits(FETCH, only):
            _add_flat_fetch(server, fetch_handler, collection=collection, descriptions=descriptions, stamp=stamp, tools=available)
        return
    _register_single_source_tools(
        server,
        handlers,
        only=only,
        search_hybrid=search_hybrid,
        switches=switches,
        descriptions=descriptions,
        collection=collection,
        envelope=envelope,
        k_source=k_source,
        stamp=stamp,
        facet_keys=facet_keys,
    )


def _served_knowledge_base(surfaces: tuple[ReadSurface, ...], collection: str) -> str:
    """The name of the surface serving this server's own `collection`, raising `ValueError` when none does."""
    served = next((surface.name for surface in surfaces if surface.collection == collection), None)
    if served is None:
        offered = ", ".join(surface.collection for surface in surfaces)
        msg = f"read_surfaces serves {offered or 'nothing'}, not this server's own collection {collection!r}."
        raise ValueError(msg)
    return served


def _admits(name: str, only: frozenset[str] | None) -> bool:
    """Whether the profile admits `name` (`only is None` or `name in only`)."""
    return only is None or name in only


def _switched_on(only: frozenset[str] | None, switches: RetrievalSwitches | None) -> frozenset[str] | None:
    """`only` without the graph tools when `switches` turn the graph off, `None` standing for the four read tools."""
    if switches is None or switches.graph:
        return only
    return (TOOL_NAMES if only is None else only) - {GRAPH_EXPAND, FIND_PATHS}


def _full_retrieval_handlers(
    search_handler: SupportsSearch[Search] | None,
    fetch_handler: FetchHandler | None,
    graph_expand_handler: GraphExpandHandler | None,
    find_paths_handler: GraphPathsHandler | None,
) -> RetrievalHandlers | None:
    """The four retrieval handlers as one `RetrievalHandlers`, or `None` if any is missing."""
    if search_handler is None or fetch_handler is None or graph_expand_handler is None or find_paths_handler is None:
        return None
    return RetrievalHandlers(search=search_handler, fetch=fetch_handler, graph_expand=graph_expand_handler, find_paths=find_paths_handler)


def _add_feedback(server: FastMCP, feedback_sink: FeedbackSink) -> None:
    """Register `submit_feedback`, recording through the injected sink."""

    async def submit_feedback(message: str, category: FeedbackCategory = "other", tool: str | None = None) -> str:
        await feedback_sink(category=category, message=message, tool=tool)
        return "Feedback recorded locally for the maintainer. Thank you: it helps refine these tools."

    server.add_tool(submit_feedback, name="submit_feedback", description=_FEEDBACK_DESCRIPTION)


def _add_eval_authoring(
    server: FastMCP,
    seed_repo_factory: SeedRepositoryFactory,
    datasets_dir: Path,
    thresholds: DifficultyThresholds,
    collection: str,
) -> None:
    """Register the opt-in `propose_eval_seeds` and `submit_eval_cases` tools, which answer bad input as a JSON error object."""
    default_collection = collection

    async def propose_eval_seeds(archetype: str, count: int, collection: str = default_collection) -> str:
        try:
            spec = resolve_archetype(archetype)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        limit = min(count, _MAX_SEED_COUNT)
        async with seed_repo_factory() as repo:
            seeds = await spec.seed(repo, collection=collection, page_ids=(), limit=limit)
        return json.dumps(
            {"archetype": archetype, "instructions": spec.instructions, "seeds": [seed.model_dump(mode="json") for seed in seeds]}
        )

    async def submit_eval_cases(cases_json: str, dataset: str = "authored") -> str:
        if _DATASET_NAME.fullmatch(dataset) is None:
            reason = f"invalid dataset name {dataset!r}: must match {_DATASET_NAME.pattern} (one segment of letters, digits, _ or -)"
            return json.dumps({"error": reason})
        try:
            submitted = _SUBMITTED_CASES_ADAPTER.validate_json(cases_json)
        except ValidationError as exc:
            reason = f"invalid cases_json: {exc.error_count()} validation error(s). Each item needs seed, question, answer, support_quotes"
            return json.dumps({"error": reason})
        accepted = [case for item in submitted if (case := _gate_case(item, thresholds)) is not None]
        path = datasets_dir / f"{dataset}.yaml"
        if accepted:
            _append_authored_cases(accepted, path)
        return json.dumps({"accepted": len(accepted), "rejected": len(submitted) - len(accepted), "dataset": str(path)})

    server.add_tool(propose_eval_seeds, name=PROPOSE_SEEDS, description=_PROPOSE_SEEDS_DESCRIPTION)
    server.add_tool(submit_eval_cases, name=SUBMIT_CASES, description=_SUBMIT_CASES_DESCRIPTION)


def _gate_case(item: _SubmittedCase, thresholds: DifficultyThresholds) -> AuthoredCase | None:
    """Run the quote check on one submitted case against its client-echoed seed, returning the case or `None`."""
    draft = SeedCaseDraft(question=item.question, answer=item.answer, support_quotes=item.support_quotes)
    return accept(draft, seed=item.seed, thresholds=thresholds)


def _append_authored_cases(accepted: list[AuthoredCase], path: Path) -> None:
    """Append accepted cases to the dataset YAML at `path`, rebuilding the dataset."""
    existing = [_authored_from_case(case) for case in load_dataset(path).cases] if path.exists() else []
    save_dataset(build_dataset([*existing, *accepted]), path)


def _authored_from_case(case: EvaluationCase) -> AuthoredCase:
    """Rebuild an `AuthoredCase` from a loaded dataset case, raising `ValueError` when it lacks its expected output or metadata."""
    if case.expected_output is None or case.metadata is None:
        msg = f"golden case {case.name!r} is missing its expected output or metadata"
        raise ValueError(msg)
    expected = ExpectedQa(answer=case.expected_output.answer, expected_source_ids=tuple(case.expected_output.cited_source_ids))
    return AuthoredCase(question=case.inputs, expected=expected, meta=case.metadata)


def _add_doc_authoring(server: FastMCP, doc_authoring_tools: AuthoringTools) -> None:
    """Register the opt-in documentation authoring tools, each forwarding to `doc_authoring_tools`."""

    async def knowledge_base_profile(knowledge_base: str) -> dict[str, object]:
        return await doc_authoring_tools.knowledge_base_profile(knowledge_base)

    async def skeleton(knowledge_base: str, page_type: str) -> dict[str, object]:
        return await doc_authoring_tools.skeleton(knowledge_base, page_type)

    async def write_note(knowledge_base: str, payload: dict[str, object], path: str = "") -> str:
        return await doc_authoring_tools.write_note(knowledge_base, payload, path)

    async def decide_page(
        knowledge_base: str, page_id: str, status: str = "", links: dict[str, list[str]] | None = None, reason: str = "", *, add: bool = False
    ) -> str:
        return await doc_authoring_tools.decide_page(knowledge_base, page_id, status, links, reason, add=add)

    async def resolve_entities(knowledge_base: str, entities: list[EntityName]) -> str:
        return await doc_authoring_tools.resolve_entities(knowledge_base, entities)

    async def read_page(knowledge_base: str, page_id: str) -> dict[str, JsonValue]:
        return await doc_authoring_tools.read_page(knowledge_base, page_id)

    server.add_tool(knowledge_base_profile, name="knowledge_base_profile", description=_KNOWLEDGE_BASE_PROFILE_DESCRIPTION)
    server.add_tool(skeleton, name="skeleton", description=_SKELETON_DESCRIPTION)
    server.add_tool(write_note, name="write_note", description=_WRITE_NOTE_DESCRIPTION)
    server.add_tool(decide_page, name="decide_page", description=_DECIDE_PAGE_DESCRIPTION)
    server.add_tool(resolve_entities, name="resolve_entities", description=_RESOLVE_ENTITIES_DESCRIPTION)
    server.add_tool(read_page, name="read_page", description=_READ_PAGE_DESCRIPTION)


def _register_single_source_tools(
    server: FastMCP,
    handlers: RetrievalHandlers,
    *,
    only: frozenset[str] | None = None,
    search_hybrid: bool | None = None,
    switches: RetrievalSwitches | None = None,
    descriptions: Mapping[str, str] | None = None,
    collection: str,
    envelope: EnvelopeHandler | None = None,
    k_source: Callable[[], int] | None = None,
    stamp: StampSource | None = None,
    facet_keys: tuple[str, ...] = (),
) -> None:
    """Add the single-source `search` and `fetch` tools, never the graph tools, which are registered separately."""
    _search_fetch_names = frozenset({SEARCH, FETCH})
    effective_only = _search_fetch_names if only is None else (_search_fetch_names & only)
    if not effective_only:
        return
    register_mcp_tools(
        server,
        handlers,
        only=effective_only,
        search_hybrid=search_hybrid,
        switches=switches,
        descriptions=descriptions,
        collection=collection,
        envelope=envelope,
        k_source=k_source,
        stamp=stamp,
        facet_keys=facet_keys,
    )


def _available_read_tools(
    *,
    handlers: RetrievalHandlers[Search] | None,
    fetch_handler: FetchHandler | None,
    graph_expand_handler: GraphExpandHandler | None,
    find_paths_handler: GraphPathsHandler | None,
    read_surfaces: tuple[ReadSurface, ...] | None,
    only: frozenset[str] | None,
) -> frozenset[str]:
    """The read tools this server registers, under the same conditions `_register_read_tools` uses."""
    routed = read_surfaces is not None
    wired = {
        SEARCH: routed or handlers is not None,
        FETCH: routed or fetch_handler is not None,
        GRAPH_EXPAND: graph_expand_handler is not None,
        FIND_PATHS: find_paths_handler is not None,
    }
    return frozenset(name for name, present in wired.items() if present and _admits(name, only))


def _add_flat_fetch(
    server: FastMCP,
    handler: FetchHandler,
    *,
    collection: str,
    descriptions: Mapping[str, str] | None = None,
    stamp: StampSource | None = None,
    tools: frozenset[str],
) -> None:
    """Register the flat `fetch` from one handler, reading every id form, its refusals naming only `tools`."""
    fetch = flat_fetch(handler, collection, stamp=stamp, reground=reground_phrase(tools), refuse=ToolError)
    description = fetch_description(can_search=SEARCH in tools, collection=collection)
    server.add_tool(fetch, name=FETCH, description=resolve_description(FETCH, description, descriptions))


def _add_graph_expand(
    server: FastMCP,
    handler: GraphExpandHandler,
    *,
    descriptions: Mapping[str, str] | None = None,
    stamp: StampSource | None = None,
    tools: frozenset[str] = TOOL_NAMES,
) -> None:
    """Register `graph_expand`, whose answers and description name only the tools in `tools`."""

    async def graph_expand(
        seed_ids: tuple[str, ...], hops: int = 2, rel_types: tuple[str, ...] | None = None, k: int = 20, *, include_superseded: bool = False
    ) -> GraphResult:
        message = GraphExpand(seed_ids=seed_ids, hops=hops, rel_types=rel_types, k=k, include_superseded=include_superseded)
        async with read_snapshot():
            result = await handler(message)
        return routed_graph_result(
            result,
            knowledge_base=None if stamp is None else await stamp(),
            next_steps=graph_next_steps(
                result.nodes,
                result.edges,
                truncated=result.window.truncated,
                tools=tools,
                seed_ids=seed_ids,
                hops=expansion_hops(hops),
                reach=result.reach,
            ),
        )

    base = graph_expand_description(can_search=SEARCH in tools, can_fetch=FETCH in tools)
    description = resolve_description(GRAPH_EXPAND, _extend_graph_expand_description(base), descriptions)
    server.add_tool(graph_expand, name=GRAPH_EXPAND, description=description)


def _add_find_paths(
    server: FastMCP,
    handler: GraphPathsHandler,
    *,
    descriptions: Mapping[str, str] | None = None,
    stamp: StampSource | None = None,
    tools: frozenset[str] = TOOL_NAMES,
) -> None:
    """Register `find_paths`, whose answers and description name only the tools in `tools`."""

    async def find_paths(
        from_id: str, to_id: str, max_hops: int = 4, rel_types: tuple[str, ...] | None = None, k: int = 10, *, include_superseded: bool = False
    ) -> GraphPathsResult:
        message = GraphPaths(
            from_id=from_id, to_id=to_id, max_hops=max_hops, rel_types=rel_types, k=k, include_superseded=include_superseded
        )
        async with read_snapshot():
            result = await handler(message)
        return routed_graph_paths(
            result,
            knowledge_base=None if stamp is None else await stamp(),
            next_steps=paths_next_steps(
                result.paths,
                from_id=from_id,
                to_id=to_id,
                truncated=result.window.truncated,
                tools=tools,
                max_hops=route_hops(max_hops),
                reach=result.reach,
            ),
        )

    base = find_paths_description(can_search=SEARCH in tools, can_fetch=FETCH in tools)
    description = resolve_description(FIND_PATHS, _extend_find_paths_description(base), descriptions)
    server.add_tool(find_paths, name=FIND_PATHS, description=description)


def _add_run_code(
    server: FastMCP,
    handlers: RetrievalHandlers,
    *,
    gateway: SqlGateway,
    embed_query: QueryEmbedder | None,
    describe_schema: DescribeSchemaTool,
    search_hybrid: bool | None,
    switches: RetrievalSwitches | None = None,
    collection: str,
    envelope: EnvelopeHandler | None = None,
    k_source: Callable[[], int] | None = None,
    stamp: StampSource | None = None,
    read_surfaces: tuple[ReadSurface, ...] | None = None,
    facet_keys: tuple[str, ...] = (),
) -> None:
    """Register the opt-in `run_code` tool backed by a Monty sandbox."""
    from pydantic_ai_harness.code_mode import CodeModeToolset  # noqa: PLC0415 (loads the Monty sandbox)

    sandbox_toolset = _build_sandbox_toolset(
        handlers,
        gateway=gateway,
        embed_query=embed_query,
        describe_schema=describe_schema,
        search_hybrid=search_hybrid,
        switches=switches,
        collection=collection,
        envelope=envelope,
        k_source=k_source,
        stamp=stamp,
        read_surfaces=read_surfaces,
        facet_keys=facet_keys,
    )
    code_mode_toolset = CodeModeToolset[None](wrapped=sandbox_toolset)

    async def run_code(code: str) -> str:
        return await _run_code(code, code_mode_toolset)

    description = _RUN_CODE_DESCRIPTION if k_source is None else _RUN_CODE_DESCRIPTION.replace(*_RUN_CODE_K_CLAUSE)
    if GRAPH_EXPAND not in sandbox_toolset.tools:
        description = description.replace(*_RUN_CODE_GRAPH_CLAUSE)
    server.add_tool(run_code, name=RUN_CODE, description=description)


def _build_sandbox_toolset(
    handlers: RetrievalHandlers,
    *,
    gateway: SqlGateway,
    embed_query: QueryEmbedder | None,
    describe_schema: DescribeSchemaTool,
    search_hybrid: bool | None,
    switches: RetrievalSwitches | None = None,
    collection: str,
    envelope: EnvelopeHandler | None = None,
    k_source: Callable[[], int] | None = None,
    stamp: StampSource | None = None,
    read_surfaces: tuple[ReadSurface, ...] | None = None,
    facet_keys: tuple[str, ...] = (),
) -> FunctionToolset[None]:
    """The toolset the code-mode sandbox binds, the same read surface the server serves at top level."""
    if read_surfaces is None:
        toolset = build_retrieval_toolset(
            handlers,
            only=frozenset({SEARCH, FETCH, GRAPH_EXPAND, FIND_PATHS}),
            search_hybrid=search_hybrid,
            switches=switches,
            collection=collection,
            envelope=envelope,
            k_source=k_source,
            stamp=stamp,
            can_sql=True,
            facet_keys=facet_keys,
        )
    else:
        # `add_routed_read_tools` adds `search` and `fetch` below, so the graph tools describe all four.
        toolset = build_retrieval_toolset(
            handlers,
            only=frozenset({GRAPH_EXPAND, FIND_PATHS}),
            search_hybrid=search_hybrid,
            switches=switches,
            collection=collection,
            descriptions={
                GRAPH_EXPAND: graph_expand_description(can_search=True, can_fetch=True),
                FIND_PATHS: find_paths_description(can_search=True, can_fetch=True),
            },
            can_sql=True,
        )
        add_routed_read_tools(
            toolset, read_surfaces, default_knowledge_base=_served_knowledge_base(read_surfaces, collection), k_source=k_source
        )
    toolset.add_function(
        make_sandbox_run_sql(gateway, embed_query), takes_ctx=False, name="run_sql", description=SANDBOX_RUN_SQL_DESCRIPTION
    )
    toolset.add_function(describe_schema, takes_ctx=False, name="describe_schema", description=DESCRIBE_SCHEMA_DESCRIPTION)
    return toolset


async def _run_code(code: str, code_mode_toolset: CodeModeToolsetT) -> str:
    """Run `code` in the code-mode sandbox without an agent and return the result as JSON, or a sandbox error message."""
    ctx: RunContext[None] = RunContext(deps=None, model=_NO_MODEL, usage=RunUsage(), tool_call_id="engram-run-code")
    ctx.tool_manager = ToolManager(toolset=code_mode_toolset)
    # Enter the bound copy on every call: `run_code` refuses to run outside it, `for_run` does not carry
    # an entered state into the copy, and each call gets its own REPL state.
    bound = await code_mode_toolset.for_run(ctx)
    async with bound:
        tools = await bound.get_tools(ctx)
        run_code_tool = tools[RUN_CODE]
        try:
            result = await bound.call_tool(RUN_CODE, {"code": code}, ctx, run_code_tool)
        except ModelRetry as exc:
            # A sandbox error is returned as the tool result.
            return str(exc)
    value = result.return_value if isinstance(result, ToolReturn) else result
    return json.dumps(value, default=str)
