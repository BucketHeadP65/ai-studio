r"""The three MCP recommendation tools, which let an MCP client run the recommendation loop by hand.

`diagnose_structure` runs the same diagnosis as `evals recommend` and returns a `StructuralDiagnosis`.
`what_if_edges` measures how many gold pairs candidate edges would connect, an upper bound on an add-relation edit.
`submit_recommendation` checks every proposal's edit against the current schema, then saves the recommendation
as `<name>.yaml` beside its draft ADR `<name>.adr.md`, which `evals apply-spec` later reads.
An unsafe name, a dataset name with no file, or a malformed or invalid payload is answered with a JSON `{error}`
object, never an exception.
A dataset or recommendation name must be one segment of letters, digits, `_` or `-`, so no tool reads or writes outside its folder.
"""

from __future__ import annotations

import json
import re

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.observability import get_logger
from pydantic import TypeAdapter, ValidationError

from engram.evals.generation.contracts import load_dataset
from engram.evals.rca.contracts import diagnose
from engram.evals.recommendation.domain.adr import render_draft_adr
from engram.evals.recommendation.domain.recommendation import CandidateEdge, CounterfactualResult, Recommendation, apply_op
from engram.evals.recommendation.infrastructure.recommendation_files import save_recommendation
from engram.evals.recommendation.service import remap_diagnosis, structural_diagnosis
from engram.evals.tuning.contracts import RetrievalPolicy, baseline_policy
from engram.kb.contracts import load_knowledge_base_schema

if TYPE_CHECKING:
    from pathlib import Path

    from mcp.server.mcpserver import MCPServer

    from engram.config import RecommendationSettings
    from engram.evals.commands import KnowledgeBaseGatewayFactory, PolicyEvaluatorFactory
    from engram.evals.recommendation.domain.detectors import EvidenceGrade

_logger = get_logger(__name__)

# The default model spec `diagnose_structure` scores against when the client names none: the
# network-free `TestModel`, so a bare call needs no Azure credentials.
_DEFAULT_MODEL = "test-model"

# The three recommendation tool names, which `register_recommendation_tools` registers together.
DIAGNOSE_STRUCTURE = "diagnose_structure"
WHAT_IF_EDGES = "what_if_edges"
SUBMIT_RECOMMENDATION = "submit_recommendation"

# The allowlist a `dataset` / `name` argument must match before it becomes a `<name>.yaml` path under
# its directory: one safe path segment of letters, digits, underscore, or hyphen. A name with a path
# separator, a `..`, or a leading dot is rejected, so a caller cannot read or write outside the
# directory (path-traversal guard).
_SAFE_NAME = re.compile(r"[A-Za-z0-9_-]+")

# The validators of the what-if payloads, built once. A malformed candidate edge or gold pair raises `ValidationError`.
_EDGES_ADAPTER = TypeAdapter(tuple[CandidateEdge, ...])
_PAIRS_ADAPTER = TypeAdapter(tuple[tuple[str, str], ...])

_DIAGNOSE_STRUCTURE_DESCRIPTION = (
    "Diagnose the structural (schema-level) weaknesses behind a retrieval evaluation run (step one of the "
    "diagnose -> probe -> submit recommendation loop). `dataset` names a dataset (one safe filename "
    "segment of letters, digits, _ or -, resolved under the datasets directory). `policy_json` is an "
    "optional RetrievalPolicy to run the baseline under (omit for the shipped baseline, which grades "
    "the evidence raw-baseline. Supplying a tuned policy grades it residual-of-tuned, so you "
    "propose schema surgery only for what policy tuning could not fix). `model` is the model under "
    "test (default test-model, the network-free backend). `collection` is the knowledge base partition. The "
    "reply is a StructuralDiagnosis as JSON: `section_splits` (gold pages whose canonical sections "
    "exceed the token budget, the restructure_sections signal), `missing_edges` (gold pages that should "
    "connect but cannot in the graph, with the unlifted prose links that would connect them, the "
    "add_relation signal), and `over_expansion` (cases that cite far more than their gold). Read this "
    "first, then probe candidate edges with what_if_edges and record a spec recommendation with submit_recommendation."
)

_WHAT_IF_EDGES_DESCRIPTION = (
    "Measure the reachability gain from injecting hypothetical typed edges into the knowledge base graph (the "
    "graph counterfactual, a judge-free upper bound on an add_relation fix). `edges_json` is a JSON "
    "array of candidate edges, each an object with `src_id`, `dst_id`, and `rel` (the relation slug the "
    "edge would carry). `pairs_json` is a JSON array of `[source_id, destination_id]` gold pairs to "
    "test. `max_hops` bounds the reachability horizon. `collection` is the knowledge base partition. The reply "
    "is a CounterfactualResult as JSON: `reachable_before` and `reachable_after` of `n_pairs` pairs, so "
    "you see how many previously unreachable gold pairs the candidate edges would connect. A malformed "
    "edges_json or pairs_json is returned as an actionable `{error}`, not a crash. Use the mined "
    "candidate links diagnose_structure surfaced as the edges to probe."
)

_SUBMIT_RECOMMENDATION_DESCRIPTION = (
    "Record a recommendation as a reusable artifact (the loop's final step, the analog of "
    "submit_tuning). `recommendation_json` is a Recommendation object: a knowledge-base-stamped baseline "
    "snapshot plus ranked spec proposals (each a typed restructure_sections or add_relation op with its "
    "evidence, falsifiable predictions, and per-page migration brief) and a free-form findings tail. "
    "`name` is one safe filename segment (letters, digits, _ or -) the recommendation is saved to as "
    "`<name>.yaml` under the recommendations directory, with a MADR-shaped draft ADR written beside it as "
    "`<name>.adr.md`. Every proposal's op is re-validated against the knowledge base's current schema before "
    "the write, so a fabricated or invalid edit (an unknown page type, a duplicate relation slug, a "
    "non-link edge field) is returned as an actionable `{error}` and nothing is written. Otherwise the "
    "reply is `{saved, adr}` with the two written paths. The recommendation is never auto-applied: a human "
    "reviews the draft ADR, then runs `studio evals apply-spec` to apply it."
)


@dataclass(frozen=True)
class _RecommendationToolDeps:
    """The dependencies the three recommendation tools share."""

    gateway_factory: KnowledgeBaseGatewayFactory
    evaluator_factory: PolicyEvaluatorFactory
    settings: RecommendationSettings
    datasets_dir: Path
    recommendations_dir: Path
    knowledge_base_path: Path


def register_recommendation_tools(  # noqa: PLR0913 (each arg is a distinct injected dep, bundling them would obscure the call contract)
    server: MCPServer,
    *,
    gateway_factory: KnowledgeBaseGatewayFactory,
    evaluator_factory: PolicyEvaluatorFactory,
    recommendation_settings: RecommendationSettings,
    datasets_dir: Path,
    recommendations_dir: Path,
    knowledge_base_path: Path,
) -> None:
    """Register the three opt-in MCP recommendation tools, closing over the injected dependencies.

    Each tool returns JSON and answers a bad input (an unsafe name, a dataset name with no file, a malformed
    payload, an edit the schema refuses) with an error object, never an exception. It reads no container and no
    environment.

    Args:
        server: The MCP server to register the three recommendation tools on.
        gateway_factory: Opens a fresh recommendation knowledge base gateway, one owner session, for each call.
        evaluator_factory: Builds a `PolicyEvaluator` for the `model` a call names. `diagnose_structure` scores
            the baseline run with it.
        recommendation_settings: The section token budget and hop horizon the diagnosis reads. Its hop horizon
            is also the default `max_hops` of `what_if_edges`.
        datasets_dir: The directory the named datasets are resolved under (one YAML per name).
        recommendations_dir: The directory recommendations are written into (one YAML plus its draft ADR per name).
        knowledge_base_path: The knowledge base root. Its schema gives the tools' default `collection` and
            checks every submitted edit.
    """
    deps = _RecommendationToolDeps(
        gateway_factory=gateway_factory,
        evaluator_factory=evaluator_factory,
        settings=recommendation_settings,
        datasets_dir=datasets_dir,
        recommendations_dir=recommendations_dir,
        knowledge_base_path=knowledge_base_path,
    )
    # The knowledge base's own collection, read once at registration, is the tools' `collection` default.
    default_collection = load_knowledge_base_schema(knowledge_base_path).retrieval.collection

    async def diagnose_structure_tool(
        dataset: str, policy_json: str = "", model: str = _DEFAULT_MODEL, collection: str = default_collection
    ) -> str:
        return await _run_diagnose_structure(deps, dataset=dataset, policy_json=policy_json, model=model, collection=collection)

    async def what_if_edges_tool(
        edges_json: str, pairs_json: str, max_hops: int = recommendation_settings.max_hops, collection: str = default_collection
    ) -> str:
        return await _run_what_if_edges(deps, edges_json=edges_json, pairs_json=pairs_json, max_hops=max_hops, collection=collection)

    async def submit_recommendation_tool(recommendation_json: str, name: str) -> str:
        return await _run_submit_recommendation(deps, recommendation_json=recommendation_json, name=name)

    server.add_tool(diagnose_structure_tool, name=DIAGNOSE_STRUCTURE, description=_DIAGNOSE_STRUCTURE_DESCRIPTION)
    server.add_tool(what_if_edges_tool, name=WHAT_IF_EDGES, description=_WHAT_IF_EDGES_DESCRIPTION)
    server.add_tool(submit_recommendation_tool, name=SUBMIT_RECOMMENDATION, description=_SUBMIT_RECOMMENDATION_DESCRIPTION)


# -- the tool bodies -----------------------------------------------------------------------------------


async def _run_diagnose_structure(deps: _RecommendationToolDeps, *, dataset: str, policy_json: str, model: str, collection: str) -> str:
    """Diagnose the dataset's run and answer the `StructuralDiagnosis`, its cases named by dataset case name, as JSON."""
    dataset_path = _dataset_file(dataset, deps.datasets_dir)
    if isinstance(dataset_path, str):
        return dataset_path
    resolved = _resolve_policy(policy_json)
    if resolved is None:
        return _policy_error()
    policy, evidence_grade = resolved
    loaded = load_dataset(dataset_path)
    name_by_question = {case.inputs.question: (case.name or case.inputs.question) for case in loaded.cases}
    report = await deps.evaluator_factory(model).evaluate(policy, dataset=loaded)
    base = diagnose(report)
    async with deps.gateway_factory() as gateway:
        structural = await structural_diagnosis(
            base, report, gateway, evidence_grade=evidence_grade, collection=collection, settings=deps.settings
        )
    return remap_diagnosis(structural, name_by_question).model_dump_json()


async def _run_what_if_edges(deps: _RecommendationToolDeps, *, edges_json: str, pairs_json: str, max_hops: int, collection: str) -> str:
    """Measure what the candidate edges add to reachability and answer a `CounterfactualResult` as JSON."""
    edges = _parse_edges(edges_json)
    if edges is None:
        return _payload_error("edges_json", "expected a JSON array of {src_id, dst_id, rel} candidate edges")
    pairs = _parse_pairs(pairs_json)
    if pairs is None:
        return _payload_error("pairs_json", "expected a JSON array of [source_id, destination_id] gold pairs")
    async with deps.gateway_factory() as gateway:
        before = await gateway.reachable_pairs(pairs, max_hops=max_hops, collection=collection)
        after = await gateway.reachable_pairs(pairs, max_hops=max_hops, collection=collection, extra_edges=edges)
    result = CounterfactualResult(
        n_pairs=len(pairs),
        reachable_before=sum(1 for flag in before if flag),
        reachable_after=sum(1 for flag in after if flag),
        max_hops=max_hops,
        candidate_edges=edges,
    )
    return result.model_dump_json()


async def _run_submit_recommendation(deps: _RecommendationToolDeps, *, recommendation_json: str, name: str) -> str:
    """Check every proposal's edit against the current schema, then write the recommendation and its draft ADR."""
    recommendation_path = _resolve_named(name, deps.recommendations_dir)
    if recommendation_path is None:
        return _name_error(name)
    recommendation = _parse_recommendation(recommendation_json)
    if recommendation is None:
        return _recommendation_error()
    spec = load_knowledge_base_schema(deps.knowledge_base_path)
    for proposal in recommendation.proposals:
        try:
            apply_op(spec, proposal.op)
        except ValueError as exc:
            _logger.info("recommendation tool rejected a spec-invalid proposal {proposal!r}: {problem}", proposal=proposal.id, problem=exc.__class__.__name__)
            return _op_error(proposal.id, exc)
    recommendation_path.parent.mkdir(parents=True, exist_ok=True)
    save_recommendation(recommendation, recommendation_path)
    adr_path = recommendation_path.with_name(recommendation_path.stem + ".adr.md")
    adr_path.write_text(render_draft_adr(recommendation), encoding="utf-8")
    return json.dumps({"saved": str(recommendation_path), "adr": str(adr_path)})


# -- parsing and error replies ---------------------------------------------------------------------------


def _resolve_policy(policy_json: str) -> tuple[RetrievalPolicy, EvidenceGrade] | None:
    """The policy to run and its evidence grade (`raw-baseline` for an empty `policy_json`), or None when the JSON is invalid."""
    if not policy_json:
        return baseline_policy(), "raw-baseline"
    policy = _parse_policy(policy_json)
    if policy is None:
        return None
    return policy, "residual-of-tuned"


def _parse_policy(policy_json: str) -> RetrievalPolicy | None:
    """Validate inbound policy JSON into a `RetrievalPolicy`, or None when it is malformed or invalid."""
    try:
        return RetrievalPolicy.model_validate_json(policy_json)
    except ValidationError as exc:
        _logger.info("recommendation tool rejected an invalid policy: {validation_errors} validation error(s)", validation_errors=exc.error_count())
        return None


def _parse_recommendation(recommendation_json: str) -> Recommendation | None:
    """Validate inbound recommendation JSON into a `Recommendation`, or None when it is malformed or invalid."""
    try:
        return Recommendation.model_validate_json(recommendation_json)
    except ValidationError as exc:
        _logger.info("recommendation tool rejected an invalid recommendation: {validation_errors} validation error(s)", validation_errors=exc.error_count())
        return None


def _parse_edges(edges_json: str) -> tuple[CandidateEdge, ...] | None:
    """Validate the inbound candidate-edge array, or None when it is malformed."""
    try:
        return _EDGES_ADAPTER.validate_json(edges_json)
    except ValidationError:
        return None


def _parse_pairs(pairs_json: str) -> tuple[tuple[str, str], ...] | None:
    """Validate the inbound gold-pair array, or None when it is malformed."""
    try:
        return _PAIRS_ADAPTER.validate_json(pairs_json)
    except ValidationError:
        return None


def _resolve_named(name: str, directory: Path) -> Path | None:
    """The `<name>.yaml` path under `directory`, or None when `name` is not one safe path segment."""
    if _SAFE_NAME.fullmatch(name) is None:
        return None
    return directory / f"{name}.yaml"


def _dataset_file(name: str, datasets_dir: Path) -> Path | str:
    """The file a dataset name names, or the JSON error for a name that is unsafe or has no file."""
    path = _resolve_named(name, datasets_dir)
    if path is None:
        return _name_error(name)
    if not path.is_file():
        return _missing_dataset_error(name)
    return path


def _name_error(name: str) -> str:
    """The actionable JSON error for an unsafe dataset / recommendation name (names the value and the rule)."""
    reason = f"invalid name {name!r}: must match {_SAFE_NAME.pattern} (one segment of letters, digits, _ or -)"
    return json.dumps({"error": reason})


def _missing_dataset_error(name: str) -> str:
    """The actionable JSON error for a safe dataset name with no file behind it."""
    return json.dumps({"error": f"no dataset named {name!r}: name a dataset that exists"})


def _policy_error() -> str:
    """The actionable JSON error for a malformed / out-of-set policy payload (names the required shape)."""
    reason = (
        "invalid policy_json: expected a RetrievalPolicy object with instructions, an eval-valid strategy, and optional tool_descriptions"
    )
    return json.dumps({"error": reason})


def _recommendation_error() -> str:
    """The actionable JSON error for a malformed recommendation payload (names the required shape)."""
    reason = "invalid recommendation_json: expected a Recommendation object (baseline snapshot, ranked spec proposals, findings tail)"
    return json.dumps({"error": reason})


def _op_error(proposal_id: str, exc: ValueError) -> str:
    """The actionable JSON error for a proposal whose edit the current schema refuses."""
    reason = f"invalid spec proposal {proposal_id!r}: {exc}"
    return json.dumps({"error": reason})


def _payload_error(field: str, detail: str) -> str:
    """The actionable JSON error for a malformed what-if payload (names the field and the required shape)."""
    return json.dumps({"error": f"invalid {field}: {detail}"})
