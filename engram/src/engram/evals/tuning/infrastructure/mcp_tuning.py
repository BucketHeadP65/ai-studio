"""The three MCP tuning tools, which let an MCP client run the tuning loop by hand.

`diagnose` scores the shipped baseline policy on a named dataset and returns its `Diagnosis`.
`evaluate_policy` validates and scores a proposed `RetrievalPolicy` and returns `{metrics, diagnosis}`.
`submit_tuning` validates a policy and saves it as the `<name>.yaml` file `evals apply-policy` later reads.
An unsafe name or a malformed or out-of-set policy is answered with a JSON `{error}` object, never an exception.
A dataset or policy name must be one segment of letters, digits, `_` or `-`, so no tool reads or writes outside its folder.
"""

from __future__ import annotations

import json
import re

from typing import TYPE_CHECKING

from core.observability import get_logger
from pydantic import ValidationError

from engram.evals.generation.contracts import load_dataset
from engram.evals.rca.contracts import diagnose
from engram.evals.tuning.policy import RetrievalPolicy, baseline_policy, save_policy

if TYPE_CHECKING:
    from pathlib import Path

    from mcp.server.mcpserver import MCPServer

    from engram.evals.commands import PolicyEvaluatorFactory

_logger = get_logger(__name__)

# The default model spec the tuning tools score against when the client names none: the
# network-free `TestModel`.
_DEFAULT_MODEL = "test-model"

# The three tuning tool names, which `register_tuning_tools` registers together.
DIAGNOSE = "diagnose"
EVALUATE_POLICY = "evaluate_policy"
SUBMIT_TUNING = "submit_tuning"

# The allowlist a `dataset` / `name` argument must match before it becomes a `<name>.yaml` path under
# its directory: one safe path segment of letters, digits, underscore, or hyphen. A name with a path
# separator, a `..`, or a leading dot is rejected, so a caller cannot read or write outside the
# directory (path-traversal guard).
_SAFE_NAME = re.compile(r"[A-Za-z0-9_-]+")

_DIAGNOSE_DESCRIPTION = (
    "Diagnose where retrieval is weak on a dataset (step one of the diagnose -> propose -> "
    "evaluate -> submit tuning loop). `dataset` names a dataset (one safe filename segment "
    "of letters, digits, _ or -, resolved under the datasets directory). `model` is the model under "
    "test (default test-model, the network-free backend). The tool scores the shipped BASELINE policy "
    "on the dataset and returns the structured signal as JSON: `overall` (the per-metric averages, "
    "recall@k / precision@k / mrr / n_tool_calls), `buckets` (the per-(archetype, difficulty) means, so "
    "you see which retrieval shapes are weak), `worst_cases` (the lowest-recall cases with their gold "
    "vs cited ids, to inspect concrete failures), and the cost summary (mean duration + mean tokens). "
    "Read this first, then craft a RetrievalPolicy that targets the weak buckets and test it with "
    "evaluate_policy."
)

_EVALUATE_POLICY_DESCRIPTION = (
    "Evaluate a proposed retrieval policy on a dataset (the loop's measurement step). "
    "`policy_json` is a JSON object with `instructions` (the agent-under-test system prompt), "
    "`strategy` (one of full | search-hybrid | search-keyword | graph-only | keyword-graph, the tool "
    "subset and search leg, code-mode is NOT an eval strategy), and an optional `tool_descriptions` "
    "map (per-tool MCP description overrides, omit for the shipped descriptions). `dataset` names the "
    "dataset (one safe filename segment, under the datasets directory) and `model` is the model "
    "under test (default test-model). The reply is `{metrics, diagnosis}` as JSON: `metrics` is the "
    "per-metric averages (compare its recall@k to the baseline from diagnose) and `diagnosis` is the "
    "full structured signal. A malformed or out-of-set policy is returned as an actionable `{error}` "
    "(an unknown strategy is rejected here), not a crash. Iterate until the metrics beat the baseline, "
    "then record the winner with submit_tuning."
)

_SUBMIT_TUNING_DESCRIPTION = (
    "Record a winning retrieval policy as a reusable artifact (the loop's final step). `policy_json` is "
    "the same RetrievalPolicy object evaluate_policy scores (instructions + strategy + optional "
    "tool_descriptions). `name` is one safe filename segment (letters, digits, _ or -) the policy is "
    "saved to as `<name>.yaml` under the policies directory, the artifact `evals apply-policy` later "
    "consumes. The policy is re-validated before the write, so a malformed or out-of-set policy is "
    "returned as an actionable `{error}` and nothing is written. Otherwise the reply is `{saved}` with "
    "the written path. Submit only the policy whose metrics beat the baseline in evaluate_policy."
)


def register_tuning_tools(
    server: MCPServer, *, evaluator_factory: PolicyEvaluatorFactory, datasets_dir: Path, policies_dir: Path
) -> None:
    """Register the three opt-in MCP tuning tools, closing over the injected deps.

    Each tool returns JSON and converts a bad input (an unsafe dataset / policy name, a malformed or
    out-of-set policy) into an actionable error object rather than tearing down the session.

    Args:
        server: The MCP server to register the three tuning tools on.
        evaluator_factory: Builds a `PolicyEvaluator` bound to the per-call `model` choice. The built
            evaluator scores a policy against a dataset into a report.
        datasets_dir: The directory the named datasets are resolved under (one YAML per name).
        policies_dir: The directory winning policy files are written into (one YAML per name).
    """

    async def diagnose_tool(dataset: str, model: str = _DEFAULT_MODEL) -> str:
        dataset_path = _dataset_path(dataset, datasets_dir)
        if isinstance(dataset_path, str):
            return dataset_path
        report = await evaluator_factory(model).evaluate(baseline_policy(), dataset=load_dataset(dataset_path))
        return diagnose(report).model_dump_json()

    async def evaluate_policy_tool(policy_json: str, dataset: str, model: str = _DEFAULT_MODEL) -> str:
        dataset_path = _dataset_path(dataset, datasets_dir)
        if isinstance(dataset_path, str):
            return dataset_path
        policy = _parse_policy(policy_json)
        if policy is None:
            return _policy_error()
        report = await evaluator_factory(model).evaluate(policy, dataset=load_dataset(dataset_path))
        signal = diagnose(report)
        return json.dumps({"metrics": dict(signal.overall), "diagnosis": signal.model_dump(mode="json")})

    async def submit_tuning_tool(policy_json: str, name: str) -> str:
        policy_path = _resolve_named(name, policies_dir)
        if policy_path is None:
            return _name_error(name)
        policy = _parse_policy(policy_json)
        if policy is None:
            return _policy_error()
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        save_policy(policy, policy_path)
        return json.dumps({"saved": str(policy_path)})

    server.add_tool(diagnose_tool, name=DIAGNOSE, description=_DIAGNOSE_DESCRIPTION)
    server.add_tool(evaluate_policy_tool, name=EVALUATE_POLICY, description=_EVALUATE_POLICY_DESCRIPTION)
    server.add_tool(submit_tuning_tool, name=SUBMIT_TUNING, description=_SUBMIT_TUNING_DESCRIPTION)


def _resolve_named(name: str, directory: Path) -> Path | None:
    """The `<name>.yaml` path under `directory`, or `None` when `name` is not one safe path segment."""
    if _SAFE_NAME.fullmatch(name) is None:
        return None
    return directory / f"{name}.yaml"


def _parse_policy(policy_json: str) -> RetrievalPolicy | None:
    """Validate inbound policy JSON into a `RetrievalPolicy`, or `None` when it is malformed or invalid."""
    try:
        return RetrievalPolicy.model_validate_json(policy_json)
    except ValidationError as exc:
        _logger.info("tuning tool rejected an invalid policy: {validation_errors} validation error(s)", validation_errors=exc.error_count())
        return None


def _dataset_path(name: str, datasets_dir: Path) -> Path | str:
    """The dataset file `name` names, or the JSON error answering a name that is unsafe or has no file."""
    path = _resolve_named(name, datasets_dir)
    if path is None:
        return _name_error(name)
    if not path.is_file():
        return json.dumps({"error": f"no dataset named {name!r}: name a dataset that exists"})
    return path


def _name_error(name: str) -> str:
    """The actionable JSON error for an unsafe dataset / policy name (names the value and the rule)."""
    reason = f"invalid name {name!r}: must match {_SAFE_NAME.pattern} (one segment of letters, digits, _ or -)"
    return json.dumps({"error": reason})


def _policy_error() -> str:
    """The actionable JSON error for a malformed / out-of-set policy payload (names the required shape)."""
    reason = (
        "invalid policy_json: expected a RetrievalPolicy object with instructions, an eval-valid strategy, and optional tool_descriptions"
    )
    return json.dumps({"error": reason})
