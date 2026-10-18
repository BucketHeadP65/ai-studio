"""The chat payload describes the draft it is showing, or it describes nothing.

Found by running the real dashboard. `reply_payload` used to take `draft` from the model's
final message and `valid`/`issues`/`graph` from `conversation.last_workflow` -- whatever
`check_draft` happened to see last. Nothing bound the two, and in a live authoring turn they
came apart exactly as you would expect: the agent repaired its spec across eight `check_draft`
calls, reached VALID on the eighth, and then pasted an *earlier* YAML into its answer. The panel
rendered that stale YAML beside a green tick and a graph of the validated spec -- a graph whose
node list did not even match the YAML printed above it (nine nodes shown, eight in the graph).

Pressing Save then refused with ten findings the screen had just said did not exist. Nothing was
corrupted -- `verify_savable` is the one plugin and it held -- but "valid" meant "some spec was
valid once", which is not a claim worth showing anyone.

So the payload is derived from the draft. Validation is milliseconds and the spec is right there;
remembering a verdict about a different object was never cheaper than re-deriving the true one.
"""

from __future__ import annotations

import yaml

from core.ai.workflows.contracts import StepRegistry, Workflow

from harness.features.authoring.domain import Draft
from harness.features.authoring.service import reply_payload
from harness.session import Conversation, NoSessionLog, Runtime
from tests.conftest import CallLog, register_quarterly_steps
from tests.support.state import a_throwaway_state


class _NoStore:
    """`reply_payload` never touches the store; the session just has to hold one."""


class _NoAgent:
    """Likewise the agent."""


def _session(registry: StepRegistry, conversation: Conversation) -> Runtime:
    return Runtime(registry=registry, library=_NoStore(), agent=_NoAgent(), conversation=conversation, session_log=NoSessionLog(), state=a_throwaway_state())  # type: ignore[arg-type]


def _yaml(spec: Workflow) -> str:
    return yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False)


def test_a_draft_that_does_not_validate_is_not_reported_valid(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """The live failure, minimised: a good `last_workflow` and a bad draft on screen together."""
    broken = quarterly_report.model_copy(update={"nodes": quarterly_report.nodes[:1]})
    conversation = Conversation(
        draft=Draft(spec_yaml=_yaml(broken)),
        last_workflow=quarterly_report,  # what check_draft last saw -- a DIFFERENT, valid spec
        last_issues=(),
    )
    payload = reply_payload(_session(registry, conversation), "here you go")

    assert payload["valid"] is False, "the tick describes the draft on screen, not a spec it replaced"
    assert payload["issues"], "and the reason is shown rather than left to the save button to discover"


def test_the_graph_drawn_is_the_graph_of_the_draft_shown(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """A graph of one spec above the YAML of another is worse than no graph."""
    trimmed = quarterly_report.model_copy(update={"nodes": quarterly_report.nodes[:2]})
    conversation = Conversation(draft=Draft(spec_yaml=_yaml(trimmed)), last_workflow=quarterly_report, last_issues=())
    payload = reply_payload(_session(registry, conversation), "here you go")

    graph = payload["graph"]
    assert isinstance(graph, dict)
    nodes = graph["nodes"]
    assert isinstance(nodes, list)
    drawn = {node["id"] for node in nodes if isinstance(node, dict) and isinstance(node["id"], str)}
    assert {node.id for node in trimmed.nodes} <= drawn
    assert "publish" not in drawn, "a node the displayed draft does not contain must not be drawn"


def test_a_valid_draft_is_reported_valid(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """The happy path still says yes -- the fix is binding the verdict, not withholding it."""
    conversation = Conversation(draft=Draft(spec_yaml=_yaml(quarterly_report)), last_workflow=None, last_issues=("stale",))
    payload = reply_payload(_session(registry, conversation), "here you go")

    assert payload["valid"] is True
    assert not payload["issues"], "a stale finding about a spec nobody is looking at is not a finding"


def test_a_draft_that_is_not_even_a_spec_says_so_instead_of_crashing(registry: StepRegistry) -> None:
    """The agent can put anything in `spec_yaml`; the panel still has to answer."""
    conversation = Conversation(draft=Draft(spec_yaml="nodes: [oh dear"), last_workflow=None)
    payload = reply_payload(_session(registry, conversation), "here you go")

    assert payload["valid"] is False
    assert payload["issues"]
    assert payload["graph"] is None


def test_no_draft_at_all_is_not_a_verdict(registry: StepRegistry) -> None:
    """Most turns carry no proposal; those must not render as invalid."""
    payload = reply_payload(_session(registry, Conversation()), "just answering a question")

    assert payload["valid"] is False
    assert not payload["issues"], "nothing was proposed, so there is nothing to complain about"
    assert payload["graph"] is None


def test_the_findings_shown_are_this_drafts_findings(quarterly_report: Workflow, call_log: CallLog) -> None:
    """Not the remembered ones: a draft naming work this run lacks says exactly that."""
    thin = StepRegistry()
    register_quarterly_steps(thin, call_log, exclude=frozenset({"publish"}))
    conversation = Conversation(draft=Draft(spec_yaml=_yaml(quarterly_report)), last_workflow=None, last_issues=())
    payload = reply_payload(_session(thin, conversation), "here you go")

    assert payload["valid"] is False
    issues = payload["issues"]
    assert isinstance(issues, list)
    assert any(isinstance(issue, str) and "publish" in issue for issue in issues)
