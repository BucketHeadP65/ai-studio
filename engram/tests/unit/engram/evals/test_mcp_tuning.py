r"""Unit specs for the three write-capable MCP tuning tools.

Three OPT-IN tools the Claude Code client drives over MCP to act as the tuning "controller
brain", gated under the same `engram mcp --authoring` opt-in as the eval-authoring tools so the
default surface stays read-only:

* `diagnose(dataset, model)` evaluates the baseline policy on a named golden dataset and returns the
  structured `Diagnosis` (per-bucket metrics + worst cases + cost) as JSON, so the brain sees where
  retrieval is weak;
* `evaluate_policy(policy_json, dataset, model)` validates a proposed `RetrievalPolicy` at the
  boundary, evaluates it on the named dataset, and returns `{metrics, diagnosis}` as JSON;
* `submit_tuning(policy_json, name)` validates the policy and saves it to a `<name>.yaml`
  artifact under the policies directory, returning `{saved}` as JSON.

The tests pin the contract DI-friendly and transport-free, mirroring `test_mcp_authoring.py`: build
an in-memory `FastMCP`, register the tuning tools against a fake `PolicyEvaluatorFactory` (a
callable returning a fake `PolicyEvaluator` with a canned report built from the same `_Report` /
`_Case` stand-ins as `test_tuning.py` / `test_diagnosis.py`), and drive the tools through the
SDK's in-memory client to server session. The path-traversal guard and inbound-policy validation are
the headlines: an unsafe dataset / policy name is rejected, and an invalid-strategy policy JSON
surfaces a `ValidationError` as an actionable error rather than a torn-down session.
"""

from __future__ import annotations

import json

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session as connect_in_memory

from engram.evals.generation.contracts import AuthoredCase, EvaluationDataset, build_dataset, save_dataset
from engram.evals.shared.domain import Archetype, ExpectedQa, QaCaseMeta, QaInput, RetrievalAnswer
from engram.evals.tuning.infrastructure.mcp_tuning import DIAGNOSE, EVALUATE_POLICY, SUBMIT_TUNING, register_tuning_tools
from engram.evals.tuning.policy import RetrievalPolicy, baseline_policy, load_policy

if TYPE_CHECKING:
    from pathlib import Path

    from mcp.types import CallToolResult

    from engram.evals.rca.contracts import ReportLike
    from engram.evals.tuning.internal_contracts import PolicyEvaluator


# -- structural report stand-ins (the `diagnose` Protocol port, mirrors test_diagnosis.py) --


@dataclass(frozen=True)
class _Score:
    """A stand-in for one `EvaluationResult`; `diagnose` reads only `.value`."""

    value: float


@dataclass(frozen=True)
class _Case:
    """A stand-in for one `ReportCase`; carries the slice `diagnose` folds over plus the case name the port requires."""

    name: str
    inputs: QaInput
    metadata: QaCaseMeta | None
    expected_output: RetrievalAnswer | None
    output: RetrievalAnswer
    scores: dict[str, _Score]
    metrics: dict[str, float | int]
    total_duration: float


@dataclass(frozen=True)
class _Report:
    """A stand-in for an `EvaluationReport`; `diagnose` reads only `.cases`."""

    cases: list[_Case]


def _report(recall: float = 0.4) -> _Report:
    """A one-case canned report with the given recall, enough to fold into a `Diagnosis`."""
    return _Report(
        cases=[
            _Case(
                name="paper.a-0",
                inputs=QaInput(question="q-a"),
                metadata=QaCaseMeta(anchor_page_id="paper.a", difficulty="easy", archetype=Archetype.PINPOINT),
                expected_output=RetrievalAnswer(answer="ref", cited_source_ids=["paper.a"]),
                output=RetrievalAnswer(answer="got", cited_source_ids=["paper.a"]),
                scores={"recall@5": _Score(recall), "n_tool_calls": _Score(1.0)},
                metrics={"input_tokens": 100},
                total_duration=0.1,
            )
        ]
    )


# -- the faked measurement port + factory ------------------------------------


class _FakeEvaluator:
    """A `PolicyEvaluator` returning a canned report and recording each `(instructions, strategy)`.

    `report` is the canned `_Report` every `evaluate` returns; `evaluate_calls` records the
    `(instructions, strategy)` of each scored policy so a spec can assert which policy was measured.
    """

    def __init__(self, report: _Report) -> None:
        self.report = report
        self.evaluate_calls: list[tuple[str, str]] = []

    async def evaluate(self, policy: RetrievalPolicy, *, dataset: EvaluationDataset) -> ReportLike:
        del dataset  # the fake scores from its canned report, not the dataset's cases
        self.evaluate_calls.append((policy.instructions, policy.strategy))
        return self.report


@dataclass
class _FakeEvaluatorFactory:
    """A `PolicyEvaluatorFactory`: a callable returning `evaluator`, recording each model spec seen."""

    evaluator: _FakeEvaluator
    models: list[str | None]

    def __call__(self, model: str | None) -> PolicyEvaluator:
        self.models.append(model)
        return self.evaluator


# -- builders ----------------------------------------------------------------


def _factory(report: _Report | None = None) -> _FakeEvaluatorFactory:
    """A fake factory over a fresh fake evaluator carrying `report` (a default canned report)."""
    return _FakeEvaluatorFactory(evaluator=_FakeEvaluator(report or _report()), models=[])


def _dataset(tmp_path: Path, name: str = "golden") -> Path:
    """A `load_dataset`-loadable golden dataset YAML at `<tmp_path>/datasets/<name>.yaml`."""
    datasets_dir = tmp_path / "datasets"
    datasets_dir.mkdir(parents=True, exist_ok=True)
    dataset = build_dataset(
        [
            AuthoredCase(
                question=QaInput(question="q-a"),
                expected=ExpectedQa(answer="a", expected_source_ids=("paper.a",)),
                meta=QaCaseMeta(anchor_page_id="paper.a", difficulty="easy", archetype=Archetype.PINPOINT),
            )
        ]
    )
    save_dataset(dataset, datasets_dir / f"{name}.yaml")
    return datasets_dir / f"{name}.yaml"


def _server(tmp_path: Path, factory: _FakeEvaluatorFactory | None = None) -> FastMCP:
    """An in-memory server with only the three tuning tools registered over `tmp_path` dirs."""
    server = FastMCP(name="tuning-test")
    register_tuning_tools(
        server, evaluator_factory=factory or _factory(), datasets_dir=tmp_path / "datasets", policies_dir=tmp_path / "tuning"
    )
    return server


def _text(result: CallToolResult) -> str:
    """The first text block of a tool result (the tools return a single text payload)."""
    block = result.content[0]
    assert block.type == "text"
    return block.text


def _valid_policy_json(*, strategy: str = "search-hybrid", instructions: str = "Be precise.") -> str:
    """A JSON dump of a valid `RetrievalPolicy` (an eval-valid strategy)."""
    return RetrievalPolicy(instructions=instructions, strategy=strategy, tool_descriptions={}).model_dump_json()


def _invalid_strategy_policy_json(*, instructions: str = "Be precise.") -> str:
    """A raw policy JSON naming an out-of-set strategy (`code-mode`), built without going through the model.

    The `RetrievalPolicy` validator rejects `code-mode` at construction, so a valid case cannot dump it;
    this builds the payload by hand so the invalid strategy reaches the tool's `model_validate_json`
    boundary, where the rejection must fire.
    """
    return json.dumps({"instructions": instructions, "strategy": "code-mode", "tool_descriptions": {}})


# -- registration ------------------------------------------------------------


class TestRegistration:
    async def test_registers_the_three_tuning_tools(self, tmp_path: Path):
        server = _server(tmp_path)

        async with connect_in_memory(server) as client:
            await client.initialize()
            listing = await client.list_tools()

        names = {tool.name for tool in listing.tools}
        assert {DIAGNOSE, EVALUATE_POLICY, SUBMIT_TUNING} <= names

    async def test_descriptions_orient_the_diagnose_evaluate_submit_loop(self, tmp_path: Path):
        server = _server(tmp_path)

        async with connect_in_memory(server) as client:
            await client.initialize()
            listing = await client.list_tools()

        by_name = {tool.name: (tool.description or "") for tool in listing.tools}
        assert by_name[DIAGNOSE]
        assert by_name[EVALUATE_POLICY]
        assert by_name[SUBMIT_TUNING]
        blob = " ".join(by_name[name].lower() for name in (DIAGNOSE, EVALUATE_POLICY, SUBMIT_TUNING))
        assert "policy" in blob  # the loop tunes a retrieval policy
        assert "diagnose" in blob or "weak" in blob  # diagnose surfaces where retrieval is weak


# -- diagnose ----------------------------------------------------------------


class TestDiagnose:
    async def test_evaluates_the_baseline_policy_and_returns_a_diagnosis(self, tmp_path: Path):
        _dataset(tmp_path)
        factory = _factory(_report(recall=0.4))
        server = _server(tmp_path, factory)

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool(DIAGNOSE, {"dataset": "golden"})

        assert result.isError is False
        payload = json.loads(_text(result))
        # The Diagnosis shape: overall averages, per-bucket metrics, worst cases, and a cost summary.
        assert payload["overall"]["recall@5"] == 0.4
        assert payload["buckets"]
        assert "worst_cases" in payload
        assert "mean_duration_s" in payload
        # The baseline policy was the one measured (its canonical instructions + default strategy).
        (instructions, strategy) = factory.evaluator.evaluate_calls[0]
        assert instructions == baseline_policy().instructions
        assert strategy == baseline_policy().strategy

    async def test_threads_the_model_choice_through_to_the_factory(self, tmp_path: Path):
        _dataset(tmp_path)
        factory = _factory()
        server = _server(tmp_path, factory)

        async with connect_in_memory(server) as client:
            await client.initialize()
            await client.call_tool(DIAGNOSE, {"dataset": "golden", "model": "gpt-4o"})

        assert factory.models == ["gpt-4o"]

    async def test_default_model_is_test_model(self, tmp_path: Path):
        _dataset(tmp_path)
        factory = _factory()
        server = _server(tmp_path, factory)

        async with connect_in_memory(server) as client:
            await client.initialize()
            await client.call_tool(DIAGNOSE, {"dataset": "golden"})

        assert factory.models == ["test-model"]

    async def test_traversal_dataset_is_rejected_without_reading_outside_the_dir(self, tmp_path: Path):
        factory = _factory()
        server = _server(tmp_path, factory)

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool(DIAGNOSE, {"dataset": "../evil"})

        assert result.isError is False  # a bad name is an actionable answer, not a torn-down session
        payload = json.loads(_text(result))
        assert "error" in payload
        assert factory.evaluator.evaluate_calls == []  # the guard fired before any evaluation

    async def test_path_separator_dataset_is_rejected(self, tmp_path: Path):
        server = _server(tmp_path)

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool(DIAGNOSE, {"dataset": "nested/evil"})

        payload = json.loads(_text(result))
        assert "error" in payload


# -- evaluate_policy ---------------------------------------------------------


class TestEvaluatePolicy:
    async def test_returns_metrics_and_diagnosis_for_a_valid_policy(self, tmp_path: Path):
        _dataset(tmp_path)
        factory = _factory(_report(recall=0.7))
        server = _server(tmp_path, factory)

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool(
                EVALUATE_POLICY, {"policy_json": _valid_policy_json(strategy="search-keyword"), "dataset": "golden"}
            )

        assert result.isError is False
        payload = json.loads(_text(result))
        assert payload["metrics"]["recall@5"] == 0.7  # the per-run aggregate
        assert payload["diagnosis"]["overall"]["recall@5"] == 0.7  # the structured signal
        # The proposed (not the baseline) policy was the one measured.
        (instructions, strategy) = factory.evaluator.evaluate_calls[0]
        assert instructions == "Be precise."
        assert strategy == "search-keyword"

    async def test_invalid_strategy_policy_json_surfaces_a_validation_error(self, tmp_path: Path):
        _dataset(tmp_path)
        factory = _factory()
        server = _server(tmp_path, factory)
        # `code-mode` is a valid MCP profile but is excluded from the eval-valid strategy set, so the
        # RetrievalPolicy validator rejects it at the boundary.
        invalid = _invalid_strategy_policy_json()

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool(EVALUATE_POLICY, {"policy_json": invalid, "dataset": "golden"})

        assert result.isError is False  # a bad policy is an actionable answer, not a torn-down session
        payload = json.loads(_text(result))
        assert "error" in payload
        assert factory.evaluator.evaluate_calls == []  # the invalid policy never reached the evaluator

    async def test_malformed_policy_json_surfaces_an_actionable_error(self, tmp_path: Path):
        _dataset(tmp_path)
        server = _server(tmp_path)

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool(EVALUATE_POLICY, {"policy_json": "{not valid json", "dataset": "golden"})

        assert result.isError is False
        payload = json.loads(_text(result))
        assert "error" in payload

    async def test_traversal_dataset_is_rejected(self, tmp_path: Path):
        factory = _factory()
        server = _server(tmp_path, factory)

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool(EVALUATE_POLICY, {"policy_json": _valid_policy_json(), "dataset": "../evil"})

        payload = json.loads(_text(result))
        assert "error" in payload
        assert factory.evaluator.evaluate_calls == []


# -- submit_tuning ------------------------------------------------------


class TestSubmitTuning:
    async def test_writes_a_round_trippable_policy_artifact(self, tmp_path: Path):
        server = _server(tmp_path)
        policy_json = _valid_policy_json(strategy="search-hybrid", instructions="Cite carefully.")

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool(SUBMIT_TUNING, {"policy_json": policy_json, "name": "winner"})

        assert result.isError is False
        payload = json.loads(_text(result))
        written = tmp_path / "tuning" / "winner.yaml"
        assert payload["saved"] == str(written)
        assert written.exists()
        # The artifact round-trips back to an equal policy through the loop's load_policy.
        loaded = load_policy(written)
        assert loaded.instructions == "Cite carefully."
        assert loaded.strategy == "search-hybrid"

    async def test_invalid_strategy_policy_json_is_rejected_without_writing(self, tmp_path: Path):
        server = _server(tmp_path)
        invalid = _invalid_strategy_policy_json()

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool(SUBMIT_TUNING, {"policy_json": invalid, "name": "winner"})

        payload = json.loads(_text(result))
        assert "error" in payload
        assert not (tmp_path / "tuning" / "winner.yaml").exists()

    async def test_traversal_name_is_rejected_without_writing_outside_the_dir(self, tmp_path: Path):
        server = _server(tmp_path)
        policy_json = _valid_policy_json()

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool(SUBMIT_TUNING, {"policy_json": policy_json, "name": "../evil"})

        assert result.isError is False  # a bad name is an actionable answer, not a torn-down session
        payload = json.loads(_text(result))
        assert "error" in payload
        assert not (tmp_path / "evil.yaml").exists()  # nothing escaped policies_dir

    async def test_path_separator_name_is_rejected(self, tmp_path: Path):
        server = _server(tmp_path)

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool(SUBMIT_TUNING, {"policy_json": _valid_policy_json(), "name": "nested/evil"})

        payload = json.loads(_text(result))
        assert "error" in payload
        assert not (tmp_path / "tuning" / "nested").exists()
