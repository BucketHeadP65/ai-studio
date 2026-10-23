r"""Unit specs for the opt-in MCP eval-authoring tools on the serve-slice server factory.

Two OPT-IN authoring tools the Claude Code client drives over MCP to generate golden eval cases,
gated EXACTLY like `run_code` so the default `engram mcp` surface stays read-only:

* `propose_eval_seeds(archetype, count, collection)` returns the structural seeds (gold ids + source
  bodies + shape) plus the per-archetype drafting `instructions` as JSON, the client uses them to
  draft cases (it must NOT invent gold ids, those come from the seed);
* `submit_eval_cases(cases_json, dataset)` runs the quote check (`accept`) on each submitted
  case, keeps the ones whose support quotes are verbatim in the seed's source bodies, and appends the
  accepted cases to a dataset YAML, returning `{accepted, rejected}` as JSON.

The tests pin the contract the same DI-friendly, transport-free way the rest of the serve MCP suite
does (`tests/unit/serve/test_mcp_server.py`): build the factory from fakes + a tmp datasets dir, then
drive the tools through the SDK's in-memory client to server session (`connect_in_memory`). The
read-only guarantee is the headline: a server with no authoring deps (or any profile that does not
name the authoring tokens) exposes NEITHER tool.
"""

from __future__ import annotations

import json

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from mcp.client import Client

from engram.config import RecommendationSettings
from engram.evals.recommendation.infrastructure.mcp_recommendation import DIAGNOSE_STRUCTURE, SUBMIT_RECOMMENDATION, WHAT_IF_EDGES
from engram.evals.shared.domain import AnchorHash, Archetype, Seed
from engram.evals.tuning.infrastructure.mcp_tuning import DIAGNOSE, EVALUATE_POLICY, SUBMIT_TUNING
from engram.retrieval.infrastructure.mcp_server import _MAX_SEED_COUNT, PROPOSE_SEEDS, SUBMIT_CASES, create_mcp_server
from engram.retrieval.queries import DescribeSchemaHandler
from engram.testing.fakes.gateway import FakeSqlGateway
from engram.testing.fakes.recommendation import FakeKnowledgeBaseGateway
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from mcp.server.mcpserver import MCPServer
    from mcp.types import CallToolResult

    from engram.evals.generation.contracts import EvaluationDataset, SupportsSeeding
    from engram.evals.rca.contracts import ReportLike
    from engram.evals.shared.domain import QaDifficulty
    from engram.evals.tuning.internal_contracts import PolicyEvaluator
    from engram.evals.tuning.policy import RetrievalPolicy
    from engram.retrieval.domain import SchemaDescription

_COLLECTION = "workdocs"
"""The made-up collection every door in this module is built over: there is no default one."""

# A known phrase the canned seed's source body contains, so a verbatim support quote passes the
# quote check and a fabricated one fails.
_VERBATIM_PHRASE = "graph index construction incurs substantial token cost"
_SOURCE_BODY = f"Background. {_VERBATIM_PHRASE} during entity and relation extraction. The end."


def _schema() -> SchemaDescription:
    """A one-object M-Schema for the always-on describe handler (mirrors the sibling suite)."""
    from engram.retrieval.domain import ColumnInfo, ObjectInfo, SchemaDescription

    return SchemaDescription(
        objects=(
            ObjectInfo(
                name="api.cards",
                kind="view",
                comment="L1 card scan.",
                columns=(ColumnInfo(name="id", data_type="text", comment="Page id."),),
            ),
        )
    )


def _describe_handler() -> DescribeSchemaHandler:
    """The real (pure) describe handler over a fake gateway scripted with a one-object schema."""
    return DescribeSchemaHandler(FakeSqlGateway(schema=_schema()))


def _pinpoint_seed() -> Seed:
    """A single-source pinpoint seed whose source body carries the known verbatim phrase."""
    return Seed(
        archetype=Archetype.PINPOINT,
        gold_ids=("paper.graph-cost",),
        source_bodies=(_SOURCE_BODY,),
        seed_key="pinpoint:paper.graph-cost",
        anchors=(AnchorHash(unit_id="paper.graph-cost", body_hash="h-a"),),
    )


class _FakeSeeder:
    """A structural `SupportsSeeding`: every seeder method returns the same canned seeds.

    Only `pinpoint_seeds` is exercised by the tests, but all five methods are implemented so the fake
    satisfies the `SupportsSeeding` protocol structurally (no subclassing). `seeds` is a public
    attribute so a test can reassign it between scenarios; `calls` records each invocation's
    `(collection, page_ids, limit)` for assertions.
    """

    def __init__(self, seeds: list[Seed]) -> None:
        self.seeds = seeds
        self.calls: list[tuple[str, tuple[str, ...], int]] = []

    async def _record(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        del salt, band, anchor_page_type
        self.calls.append((collection, page_ids, limit))
        return self.seeds[:limit]

    async def pinpoint_seeds(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        return await self._record(collection=collection, page_ids=page_ids, limit=limit, salt=salt, band=band, anchor_page_type=anchor_page_type)

    async def survey_seeds(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        return await self._record(collection=collection, page_ids=page_ids, limit=limit, salt=salt, band=band, anchor_page_type=anchor_page_type)

    async def trace_seeds(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        return await self._record(collection=collection, page_ids=page_ids, limit=limit, salt=salt, band=band, anchor_page_type=anchor_page_type)

    async def synthesis_seeds(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        return await self._record(collection=collection, page_ids=page_ids, limit=limit, salt=salt, band=band, anchor_page_type=anchor_page_type)

    async def passage_seeds(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        return await self._record(collection=collection, page_ids=page_ids, limit=limit, salt=salt, band=band, anchor_page_type=anchor_page_type)


def _seed_repo_factory(seeder: SupportsSeeding):
    """A `SeedRepositoryFactory`: an async-CM yielding `seeder` (one repo per command, the real shape)."""

    @asynccontextmanager
    async def factory() -> AsyncGenerator[SupportsSeeding]:
        yield seeder

    return factory


# The authoring profile names both authoring tokens plus the always-on base tokens; the always-on
# describe_schema / run_sql ride along regardless.
_AUTHORING_ONLY = frozenset({"search", "fetch", PROPOSE_SEEDS, SUBMIT_CASES})


def _authoring_server(seeder: SupportsSeeding, datasets_dir: Path, *, only: frozenset[str] | None = _AUTHORING_ONLY) -> MCPServer:
    """A server wired with the authoring deps and a profile that opts the authoring tools in."""
    return create_mcp_server(
        FakeSqlGateway(), _describe_handler(), seed_repo_factory=_seed_repo_factory(seeder), datasets_dir=datasets_dir, only=only,
        collection=_COLLECTION,
    )


class _NeverEvaluator:
    """A `PolicyEvaluator` stand-in for the tuning-gate specs (registration-only, never called)."""

    async def evaluate(self, policy: RetrievalPolicy, *, dataset: EvaluationDataset) -> ReportLike:  # pragma: no cover (gate-only)
        del policy, dataset
        msg = "the tuning-gate specs only list tools; they never evaluate a policy"
        raise AssertionError(msg)


def _evaluator_factory(model: str | None) -> PolicyEvaluator:
    """A `PolicyEvaluatorFactory`: returns the never-called evaluator (the gate specs only list tools)."""
    del model
    return _NeverEvaluator()


def _knowledge_base_gateway_factory():
    """A `KnowledgeBaseGatewayFactory`: an async-CM yielding a canned recommendation gateway (registration-only here)."""

    @asynccontextmanager
    async def factory() -> AsyncGenerator[FakeKnowledgeBaseGateway]:
        yield FakeKnowledgeBaseGateway()

    return factory


def _text(result: CallToolResult) -> str:
    """The first text block of a tool result (the tools return a single text payload)."""
    block = result.content[0]
    assert block.type == "text"
    return block.text


def _case_payload(
    *, quote: str, question: str = "What does graph index construction incur?", answer: str = "Substantial token cost."
) -> dict[str, object]:
    """One submit_eval_cases item: the seed dump plus the model-drafted question/answer/quote."""
    return {"seed": _pinpoint_seed().model_dump(mode="json"), "question": question, "answer": answer, "support_quotes": [quote]}


async def _tool_names(server: MCPServer) -> frozenset[str]:
    async with Client(server) as client:
        listing = await client.list_tools()
    return frozenset(tool.name for tool in listing.tools)


class TestAuthoringToolsAreReadOnlyByDefault:
    """The default `engram mcp` surface (no authoring deps, `only=None`) exposes NEITHER tool."""

    async def test_default_surface_has_neither_authoring_tool(self):
        server = create_mcp_server(FakeSqlGateway(), _describe_handler(), collection=_COLLECTION)

        names = await _tool_names(server)

        assert PROPOSE_SEEDS not in names
        assert SUBMIT_CASES not in names

    async def test_profile_without_authoring_tokens_excludes_both_even_when_deps_wired(self, tmp_path: Path):
        # The deps are wired, but the profile does not name the authoring tokens — opt-in is preserved.
        server = _authoring_server(_FakeSeeder([_pinpoint_seed()]), tmp_path, only=frozenset({"search"}))

        names = await _tool_names(server)

        assert PROPOSE_SEEDS not in names
        assert SUBMIT_CASES not in names

    async def test_named_in_only_but_deps_absent_still_excludes_both(self):
        # Even when the profile names the authoring tokens, the tools need their deps wired (mirror run_code).
        server = create_mcp_server(FakeSqlGateway(), _describe_handler(), only=_AUTHORING_ONLY, collection=_COLLECTION)

        names = await _tool_names(server)

        assert PROPOSE_SEEDS not in names
        assert SUBMIT_CASES not in names


class TestAuthoringToolsOptIn:
    """Wired deps + a profile that names the tokens registers BOTH authoring tools."""

    async def test_both_tools_register_when_opted_in_with_deps(self, tmp_path: Path):
        server = _authoring_server(_FakeSeeder([_pinpoint_seed()]), tmp_path)

        names = await _tool_names(server)

        assert PROPOSE_SEEDS in names
        assert SUBMIT_CASES in names

    async def test_descriptions_orient_the_propose_draft_submit_flow(self, tmp_path: Path):
        server = _authoring_server(_FakeSeeder([_pinpoint_seed()]), tmp_path)

        async with Client(server) as client:
            listing = await client.list_tools()

        by_name = {tool.name: (tool.description or "") for tool in listing.tools}
        assert by_name[PROPOSE_SEEDS]
        assert by_name[SUBMIT_CASES]
        blob = (by_name[PROPOSE_SEEDS] + " " + by_name[SUBMIT_CASES]).lower()
        assert "verbatim" in blob  # the server verifies quotes verbatim
        assert "gold" in blob  # gold ids come from the seed, the client must not invent them


class TestProposeEvalSeeds:
    async def test_returns_seeds_with_gold_ids_source_bodies_and_instructions(self, tmp_path: Path):
        seeder = _FakeSeeder([_pinpoint_seed()])
        server = _authoring_server(seeder, tmp_path)

        async with Client(server) as client:
            result = await client.call_tool(PROPOSE_SEEDS, {"archetype": "pinpoint", "count": 3})

        assert result.is_error is False
        payload = json.loads(_text(result))
        assert payload["archetype"] == "pinpoint"
        assert payload["instructions"]  # the per-archetype drafting prompt rides along
        (seed,) = payload["seeds"]
        assert seed["gold_ids"] == ["paper.graph-cost"]  # gold ids from the structural seed
        assert _VERBATIM_PHRASE in seed["source_bodies"][0]  # the source body the client drafts from
        # The fake recorded the dispatch: the pinpoint seeder, the server's own collection, count as the limit.
        assert seeder.calls == [(_COLLECTION, (), 3)]

    async def test_collection_argument_threads_through_to_the_seeder(self, tmp_path: Path):
        seeder = _FakeSeeder([_pinpoint_seed()])
        server = _authoring_server(seeder, tmp_path)

        async with Client(server) as client:
            await client.call_tool(PROPOSE_SEEDS, {"archetype": "pinpoint", "count": 2, "collection": "cooking"})

        assert seeder.calls == [("cooking", (), 2)]

    async def test_unknown_archetype_returns_an_actionable_error_not_a_crash(self, tmp_path: Path):
        server = _authoring_server(_FakeSeeder([_pinpoint_seed()]), tmp_path)

        async with Client(server) as client:
            result = await client.call_tool(PROPOSE_SEEDS, {"archetype": "bogus", "count": 1})

        assert result.is_error is False  # an unknown name is an actionable answer, not a torn-down session
        payload = json.loads(_text(result))
        assert "error" in payload
        assert "bogus" in payload["error"]  # names the bad value so the client re-grounds it


class TestSubmitEvalCases:
    async def test_verbatim_case_is_accepted_and_written_to_the_dataset(self, tmp_path: Path):
        from engram.evals.generation.contracts import load_dataset

        server = _authoring_server(_FakeSeeder([_pinpoint_seed()]), tmp_path)
        cases_json = json.dumps([_case_payload(quote="substantial token cost")])

        async with Client(server) as client:
            result = await client.call_tool(SUBMIT_CASES, {"cases_json": cases_json})

        assert result.is_error is False
        payload = json.loads(_text(result))
        assert payload["accepted"] == 1
        assert payload["rejected"] == 0

        written = tmp_path / "authored.yaml"
        assert written.exists()
        dataset = load_dataset(written)
        (case,) = dataset.cases
        assert case.expected_output is not None
        assert case.expected_output.cited_source_ids == ["paper.graph-cost"]  # gold ids from the seed
        assert case.metadata is not None
        assert case.metadata.archetype is Archetype.PINPOINT  # the seed's archetype rides onto the case

    async def test_fabricated_quote_is_rejected_by_the_gate(self, tmp_path: Path):
        server = _authoring_server(_FakeSeeder([_pinpoint_seed()]), tmp_path)
        cases_json = json.dumps([_case_payload(quote="a phrase that is nowhere in any source body")])

        async with Client(server) as client:
            result = await client.call_tool(SUBMIT_CASES, {"cases_json": cases_json})

        assert result.is_error is False
        payload = json.loads(_text(result))
        assert payload["accepted"] == 0
        assert payload["rejected"] == 1  # the quote check dropped the fabricated quote

    async def test_mixed_batch_accepts_the_faithful_and_rejects_the_fabricated(self, tmp_path: Path):
        server = _authoring_server(_FakeSeeder([_pinpoint_seed()]), tmp_path)
        cases_json = json.dumps(
            [
                _case_payload(quote="substantial token cost", question="Q-good"),
                _case_payload(quote="not in the sources at all", question="Q-bad"),
            ]
        )

        async with Client(server) as client:
            result = await client.call_tool(SUBMIT_CASES, {"cases_json": cases_json})

        payload = json.loads(_text(result))
        assert payload["accepted"] == 1
        assert payload["rejected"] == 1

    async def test_named_dataset_argument_chooses_the_yaml_filename(self, tmp_path: Path):
        server = _authoring_server(_FakeSeeder([_pinpoint_seed()]), tmp_path)
        cases_json = json.dumps([_case_payload(quote="substantial token cost")])

        async with Client(server) as client:
            await client.call_tool(SUBMIT_CASES, {"cases_json": cases_json, "dataset": "manual"})

        assert (tmp_path / "manual.yaml").exists()
        assert not (tmp_path / "authored.yaml").exists()

    async def test_appends_to_an_existing_dataset_rather_than_overwriting(self, tmp_path: Path):
        from engram.evals.generation.contracts import load_dataset

        server = _authoring_server(_FakeSeeder([_pinpoint_seed()]), tmp_path)
        first = json.dumps([_case_payload(quote="substantial token cost", question="First?", answer="A1")])
        second = json.dumps([_case_payload(quote="entity and relation extraction", question="Second?", answer="A2")])

        async with Client(server) as client:
            await client.call_tool(SUBMIT_CASES, {"cases_json": first})
            await client.call_tool(SUBMIT_CASES, {"cases_json": second})

        dataset = load_dataset(tmp_path / "authored.yaml")
        assert len(dataset.cases) == 2  # the second submission appended, it did not clobber the first
        questions = {case.inputs.question for case in dataset.cases}
        assert questions == {"First?", "Second?"}

    async def test_malformed_payload_returns_an_actionable_error_not_a_crash(self, tmp_path: Path):
        server = _authoring_server(_FakeSeeder([_pinpoint_seed()]), tmp_path)

        async with Client(server) as client:
            result = await client.call_tool(SUBMIT_CASES, {"cases_json": "{not valid json"})

        assert result.is_error is False  # a bad payload is an actionable answer, not a torn-down session
        payload = json.loads(_text(result))
        assert "error" in payload

    async def test_payload_missing_required_fields_returns_an_actionable_error(self, tmp_path: Path):
        server = _authoring_server(_FakeSeeder([_pinpoint_seed()]), tmp_path)
        # Valid JSON, but each item is missing question/answer/support_quotes — a pydantic ValidationError.
        cases_json = json.dumps([{"seed": _pinpoint_seed().model_dump(mode="json")}])

        async with Client(server) as client:
            result = await client.call_tool(SUBMIT_CASES, {"cases_json": cases_json})

        assert result.is_error is False
        payload = json.loads(_text(result))
        assert "error" in payload


class TestSubmitEvalCasesDatasetGuard:
    """`dataset` is a caller-supplied filename segment, so a traversal payload must be rejected, not written."""

    async def test_traversal_dataset_is_rejected_without_writing_outside_the_dir(self, tmp_path: Path):
        # The quote is verbatim, so the gate would accept the case — proving the guard fires BEFORE the write.
        server = _authoring_server(_FakeSeeder([_pinpoint_seed()]), tmp_path)
        cases_json = json.dumps([_case_payload(quote="substantial token cost")])

        async with Client(server) as client:
            result = await client.call_tool(SUBMIT_CASES, {"cases_json": cases_json, "dataset": "../evil"})

        assert result.is_error is False  # a bad name is an actionable answer, not a torn-down session
        payload = json.loads(_text(result))
        assert "error" in payload
        assert not (tmp_path.parent / "evil.yaml").exists()  # nothing escaped datasets_dir

    async def test_path_separator_dataset_is_rejected(self, tmp_path: Path):
        server = _authoring_server(_FakeSeeder([_pinpoint_seed()]), tmp_path)
        cases_json = json.dumps([_case_payload(quote="substantial token cost")])

        async with Client(server) as client:
            result = await client.call_tool(SUBMIT_CASES, {"cases_json": cases_json, "dataset": "nested/evil"})

        assert result.is_error is False
        payload = json.loads(_text(result))
        assert "error" in payload
        assert not (tmp_path / "nested").exists()  # no nested directory was created

    async def test_absolute_path_dataset_is_rejected(self, tmp_path: Path):
        server = _authoring_server(_FakeSeeder([_pinpoint_seed()]), tmp_path)
        cases_json = json.dumps([_case_payload(quote="substantial token cost")])
        escape_target = tmp_path.parent / "abs-evil"

        async with Client(server) as client:
            result = await client.call_tool(SUBMIT_CASES, {"cases_json": cases_json, "dataset": str(escape_target)})

        assert result.is_error is False
        payload = json.loads(_text(result))
        assert "error" in payload
        assert not escape_target.with_suffix(".yaml").exists()

    async def test_valid_dataset_name_still_writes_inside_the_dir(self, tmp_path: Path):
        server = _authoring_server(_FakeSeeder([_pinpoint_seed()]), tmp_path)
        cases_json = json.dumps([_case_payload(quote="substantial token cost")])

        async with Client(server) as client:
            result = await client.call_tool(SUBMIT_CASES, {"cases_json": cases_json, "dataset": "authored"})

        payload = json.loads(_text(result))
        assert payload["accepted"] == 1  # a safe name still writes
        assert (tmp_path / "authored.yaml").exists()


class TestProposeEvalSeedsCountClamp:
    """`count` flows straight into the seeder LIMIT, so a huge value must be clamped server-side."""

    async def test_huge_count_is_clamped_to_the_server_maximum(self, tmp_path: Path):
        seeder = _FakeSeeder([_pinpoint_seed()])
        server = _authoring_server(seeder, tmp_path)

        async with Client(server) as client:
            result = await client.call_tool(PROPOSE_SEEDS, {"archetype": "pinpoint", "count": 10_000})

        assert result.is_error is False
        (_collection, _page_ids, limit) = seeder.calls[0]
        assert limit == _MAX_SEED_COUNT  # the LIMIT the seeder saw is bounded, not the 10_000 the client asked for

    async def test_count_within_the_ceiling_threads_through_unchanged(self, tmp_path: Path):
        seeder = _FakeSeeder([_pinpoint_seed()])
        server = _authoring_server(seeder, tmp_path)

        async with Client(server) as client:
            await client.call_tool(PROPOSE_SEEDS, {"archetype": "pinpoint", "count": 3})

        assert seeder.calls[0][2] == 3  # a count under the ceiling is not touched by the clamp


class TestTuningToolsGate:
    """The three write-capable tuning tools gate on their deps being wired.

    They ride the same `--authoring` opt-in as the eval-authoring tools, but on a separate deps-presence
    gate: the evaluator factory AND a policies directory (AND the datasets directory) must be wired, else
    none registers, so a bare `mcp` server stays read-only. The application's `--authoring` CLI path resolves
    the deps and passes them through, firing the gate.
    """

    _TUNING_TOOLS = frozenset({DIAGNOSE, EVALUATE_POLICY, SUBMIT_TUNING})

    async def test_default_surface_has_no_tuning_tools(self, tmp_path: Path):
        del tmp_path  # the default surface wires no tuning deps at all
        server = create_mcp_server(FakeSqlGateway(), _describe_handler(), collection=_COLLECTION)

        names = await _tool_names(server)

        assert self._TUNING_TOOLS.isdisjoint(names)

    async def test_three_tools_register_when_evaluator_factory_and_policies_dir_are_wired(self, tmp_path: Path):
        server = create_mcp_server(
            FakeSqlGateway(),
            _describe_handler(),
            policy_evaluator_factory=_evaluator_factory,
            datasets_dir=tmp_path / "datasets",
            policies_dir=tmp_path / "tuning",
            collection=_COLLECTION,
        )

        names = await _tool_names(server)

        assert names >= self._TUNING_TOOLS

    async def test_evaluator_factory_without_policies_dir_registers_none(self, tmp_path: Path):
        # The factory is wired but the policies directory is absent — the gate needs BOTH (and datasets_dir).
        server = create_mcp_server(
            FakeSqlGateway(), _describe_handler(), policy_evaluator_factory=_evaluator_factory, datasets_dir=tmp_path / "datasets",
            collection=_COLLECTION,
        )

        names = await _tool_names(server)

        assert self._TUNING_TOOLS.isdisjoint(names)

    async def test_policies_dir_without_evaluator_factory_registers_none(self, tmp_path: Path):
        # The policies directory is wired but the evaluator factory is absent — the gate needs BOTH.
        server = create_mcp_server(
            FakeSqlGateway(), _describe_handler(), datasets_dir=tmp_path / "datasets", policies_dir=tmp_path / "tuning",
            collection=_COLLECTION,
        )

        names = await _tool_names(server)

        assert self._TUNING_TOOLS.isdisjoint(names)

    async def test_tuning_gate_is_independent_of_the_authoring_gate(self, tmp_path: Path):
        # Wiring the authoring deps alone exposes the authoring tools but NOT the tuning tools.
        server = _authoring_server(_FakeSeeder([_pinpoint_seed()]), tmp_path)

        names = await _tool_names(server)

        assert PROPOSE_SEEDS in names  # the authoring tools are wired
        assert self._TUNING_TOOLS.isdisjoint(names)  # but the tuning tools are not


class TestRecommendationToolsGate:
    """The three write-capable recommendation tools gate on their deps being wired.

    They ride the same `--authoring` opt-in as the tuning tools, but on a separate deps-presence
    gate: the knowledge-base-gateway factory, the evaluator factory, the recommendation settings, the datasets and
    recommendations directories, AND the knowledge base path must ALL be wired, else none registers, so a bare
    `mcp` server stays read-only. The application's `--authoring` CLI path resolves the deps and fires the gate.
    """

    _RECOMMENDATION_TOOLS = frozenset({DIAGNOSE_STRUCTURE, WHAT_IF_EDGES, SUBMIT_RECOMMENDATION})

    async def test_default_surface_has_no_recommendation_tools(self, tmp_path: Path):
        del tmp_path  # the default surface wires no recommendation deps at all
        server = create_mcp_server(FakeSqlGateway(), _describe_handler(), collection=_COLLECTION)

        names = await _tool_names(server)

        assert self._RECOMMENDATION_TOOLS.isdisjoint(names)

    async def test_three_tools_register_when_all_recommendation_deps_are_wired(self, tmp_path: Path):
        (tmp_path / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
        server = create_mcp_server(
            FakeSqlGateway(),
            _describe_handler(),
            gateway_factory=_knowledge_base_gateway_factory(),
            policy_evaluator_factory=_evaluator_factory,
            recommendation_settings=RecommendationSettings(),
            datasets_dir=tmp_path / "datasets",
            recommendations_dir=tmp_path / "recommendations",
            knowledge_base_path=tmp_path,
            collection=_COLLECTION,
        )

        names = await _tool_names(server)

        assert names >= self._RECOMMENDATION_TOOLS

    async def test_missing_gateway_factory_registers_none(self, tmp_path: Path):
        # Every recommendation dep but the knowledge-base-gateway factory is wired — the gate needs all of them.
        server = create_mcp_server(
            FakeSqlGateway(),
            _describe_handler(),
            policy_evaluator_factory=_evaluator_factory,
            recommendation_settings=RecommendationSettings(),
            datasets_dir=tmp_path / "datasets",
            recommendations_dir=tmp_path / "recommendations",
            knowledge_base_path=tmp_path,
            collection=_COLLECTION,
        )

        names = await _tool_names(server)

        assert self._RECOMMENDATION_TOOLS.isdisjoint(names)

    async def test_missing_recommendations_dir_registers_none(self, tmp_path: Path):
        # The recommendations directory (the write target) is absent — the gate needs it too.
        server = create_mcp_server(
            FakeSqlGateway(),
            _describe_handler(),
            gateway_factory=_knowledge_base_gateway_factory(),
            policy_evaluator_factory=_evaluator_factory,
            recommendation_settings=RecommendationSettings(),
            datasets_dir=tmp_path / "datasets",
            knowledge_base_path=tmp_path,
            collection=_COLLECTION,
        )

        names = await _tool_names(server)

        assert self._RECOMMENDATION_TOOLS.isdisjoint(names)

    async def test_recommendation_gate_is_independent_of_the_authoring_gate(self, tmp_path: Path):
        # Wiring the authoring deps alone exposes the authoring tools but NOT the recommendation tools.
        server = _authoring_server(_FakeSeeder([_pinpoint_seed()]), tmp_path)

        names = await _tool_names(server)

        assert PROPOSE_SEEDS in names  # the authoring tools are wired
        assert self._RECOMMENDATION_TOOLS.isdisjoint(names)  # but the recommendation tools are not
