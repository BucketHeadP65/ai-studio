"""The contract as a port, tested against a host that has never heard of a workflow.

`FakeHost` is deliberately not a `WorkflowLibrary` and does not try to be: its tools are
called `find_row`, `read_row`, and `store_row`, its arguments are `text`/`top`/`key`, and its
answers come back under keys of its own choosing. If the port works against this, the claim
that it adapts *somebody else's* store is doing real work rather than describing a wrapper
around our own types.

The test that matters most is `test_save_refuses_...`: it asserts on what did **not** happen.
`store_row` is reachable by the port and invisible to the model, so "an unexecutable workflow
cannot be written through this port" is checkable by watching the host's call log rather than
by trusting a prompt.
"""

from __future__ import annotations

import json

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import pytest
import yaml

from pydantic import TypeAdapter
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.toolsets import CombinedToolset, FunctionToolset
from pydantic_ai.usage import RunUsage, UsageLimits

from core.ai.workflows.contract_port import SEARCH_TOOL, LibraryVocabulary, WorkflowWrite, cards_from_mapping, render_cards
from core.ai.workflows.contracts import (
    Briefing,
    FileEvidenceLog,
    FromNode,
    FromParameter,
    InlineStep,
    LibraryError,
    Outcome,
    ParameterSpec,
    RunEvidence,
    RunNode,
    SavedWorkflow,
    Shape,
    StepRegistry,
    Workflow,
    WorkflowContract,
    WorkflowIntent,
    WorkflowPresentation,
    spec_identity,
)
from core.ai.workflows.domain.vocabulary import SameAs
from core.ai.workflows.infrastructure.library_collection import WORKFLOW_COLLECTION
from tests.unit.workflows.conftest import CallLog, register_quarterly_steps

if TYPE_CHECKING:
    from pathlib import Path

# --- a foreign host --------------------------------------------------------------------


@dataclass
class FakeHost:
    """Somebody else's store: its own tool names, its own words, its own answer shapes."""

    rows: dict[str, str] = field(default_factory=dict)
    steps: dict[str, dict] = field(default_factory=dict)
    calls: list[tuple[str, Mapping[str, object]]] = field(default_factory=list)
    stored: list[dict[str, object]] = field(default_factory=list)
    """Every `store_row` write, decoded back to data: `{"spec": ..., "uses": [...]}` per call."""
    refuse_store: str | None = None
    """Scriptable: when set, `store_row` raises `LibraryError` with this message instead of writing."""
    catalog_rows: dict[str, str] | None = None
    """Scriptable: when set, `list_rows` answers from this instead of `rows` -- lets a test prove
    the catalog fallback reads its own listing rather than repeating the search."""

    def toolset(self) -> FunctionToolset[None]:
        toolset: FunctionToolset[None] = FunctionToolset()
        toolset.add_function(self.find_row)
        toolset.add_function(self.list_rows)
        toolset.add_function(self.read_row)
        toolset.add_function(self.store_row)
        toolset.add_function(self.send_survey)
        toolset.add_function(self.find_step)
        toolset.add_function(self.read_step)
        toolset.add_function(self.save_step)
        return toolset

    @property
    def tools_called(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.calls)

    def args_for(self, tool: str) -> Mapping[str, object]:
        return next(args for name, args in self.calls if name == tool)

    async def find_row(self, text: str, top: int) -> dict[str, object]:
        """Search rows. Note the vocabulary: `text`/`top`, and hits under `rows`."""
        self.calls.append(("find_row", {"text": text, "top": top}))
        hits = [
            {"key": key, "name": key.replace("-", " "), "blurb": document[:40], "rank": 0.9, "state": "verified"}
            for key, document in sorted(self.rows.items())
        ]
        return {"rows": hits[:top]}

    async def list_rows(self, top: int) -> dict[str, object]:
        """Every row, unranked, plus how many there are. Note the vocabulary: `top`, hits under `rows`."""
        self.calls.append(("list_rows", {"top": top}))
        source = self.rows if self.catalog_rows is None else self.catalog_rows
        hits = [
            {"key": key, "name": key.replace("-", " "), "blurb": document[:40], "rank": 0.0, "state": "verified"}
            for key, document in sorted(source.items())
        ]
        return {"total": len(source), "rows": hits[:top]}

    async def read_row(self, key: str) -> dict[str, object]:
        """Return one row's content under `document`, not `spec`."""
        self.calls.append(("read_row", {"key": key}))
        return {"document": self.rows.get(key, "")}

    async def store_row(self, key: str, doc: str, label: str, note: str, guide: str, who: str, uses: list[str] | None = None) -> dict[str, object]:
        """Keep a row. Six positional concepts, none of them named the way we name them."""
        self.calls.append(("store_row", {"key": key, "label": label, "who": who, "guide": guide, "note": note}))
        if self.refuse_store is not None:
            raise LibraryError(self.refuse_store)
        self.rows[key] = doc
        self.stored.append({"spec": yaml.safe_load(doc), "uses": uses or []})
        return {"saved": key}

    async def send_survey(self, feedback: str) -> str:
        """A real tool that is irrelevant here -- the thing `hide` exists for."""
        self.calls.append(("send_survey", {"feedback": feedback}))
        return "thanks"

    async def find_step(self, text: str, top: int) -> dict[str, object]:
        """Search reusable steps. Same shape as `find_row`, scoped to `steps`."""
        self.calls.append(("find_step", {"text": text, "top": top}))
        hits = [
            {"key": key, "name": key.replace("-", " "), "blurb": str(document)[:40], "rank": 0.9, "state": "verified"}
            for key, document in sorted(self.steps.items())
        ]
        return {"rows": hits[:top]}

    async def read_step(self, key: str) -> dict[str, object]:
        """Return one reusable step's content under `document`, not `step`."""
        self.calls.append(("read_step", {"key": key}))
        return {"document": self.steps.get(key, {})}

    async def save_step(self, key: str, doc: dict[str, object], label: str, note: str, guide: str, who: str) -> dict[str, object]:
        """Keep a reusable step, the same shape `store_row` keeps a workflow in."""
        self.calls.append(("save_step", {"key": key, "label": label, "who": who, "guide": guide, "note": note}))
        self.steps[key] = doc
        return {"saved": key}


def _card(row: Mapping[str, object]) -> SavedWorkflow:
    return SavedWorkflow(
        id=str(row["key"]),
        title=str(row["name"]),
        summary=str(row["blurb"]),
        score=float(str(row["rank"])),
        provisional=str(row["state"]) != "verified",
    )


def _to_cards(raw: object) -> Sequence[SavedWorkflow]:
    if not isinstance(raw, Mapping):
        msg = f"expected a mapping, got {type(raw).__name__}"
        raise TypeError(msg)
    return cards_from_mapping(raw, key="rows", card=_card)


def _to_spec(raw: object) -> Workflow:
    if not isinstance(raw, Mapping) or not raw.get("document"):
        msg = "the row carries no document"
        raise ValueError(msg)
    return Workflow.model_validate(yaml.safe_load(str(raw["document"])))


def _write_args(write: WorkflowWrite) -> dict[str, object]:
    return {
        "key": f"wf-{write.workflow.name}",
        "doc": yaml.safe_dump(write.workflow.model_dump(mode="json"), sort_keys=False),
        "label": write.title,
        "note": write.summary,
        "guide": write.applicability,
        "who": write.saved_by,
        "uses": list(write.uses) if write.uses is not None else None,
    }


def _to_id(raw: object) -> str:
    if not isinstance(raw, Mapping) or not raw.get("saved"):
        msg = "the write returned no id"
        raise ValueError(msg)
    return str(raw["saved"])


_BROUGHT_STEP = TypeAdapter(InlineStep)


def _to_step(raw: object) -> InlineStep:
    if not isinstance(raw, Mapping) or not raw.get("document"):
        msg = "the row carries no step"
        raise ValueError(msg)
    return _BROUGHT_STEP.validate_python(raw["document"])
def _yaml(spec: Workflow) -> str:
    """`spec` as the YAML text `save_workflow`'s `spec_yaml` argument now takes."""
    return yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False)


READ_ONLY = LibraryVocabulary(
    search_tool="find_row",
    fetch_tool="read_row",
    search_args=lambda task, limit: {"text": task, "top": limit},
    fetch_args=lambda workflow_id: {"key": workflow_id},
    to_cards=_to_cards,
    to_spec=_to_spec,
)
BROKEN_SEARCH = replace(READ_ONLY, search_tool="no_such_tool")
"""A host whose search verb is not there -- stands in for any store that is simply down."""

WRITABLE = LibraryVocabulary(
    search_tool="find_row",
    fetch_tool="read_row",
    search_args=lambda task, limit: {"text": task, "top": limit},
    fetch_args=lambda workflow_id: {"key": workflow_id},
    to_cards=_to_cards,
    to_spec=_to_spec,
    put_tool="store_row",
    put_args=_write_args,
    to_id=_to_id,
)

WRITABLE_WITH_SHELF = replace(
    WRITABLE,
    search_steps_tool="find_step",
    fetch_step_tool="read_step",
    put_step_tool="save_step",
    step_args=lambda step_id: {"key": step_id},
    to_step=_to_step,
)
"""`WRITABLE`, plus a library of steps -- same host, same words, one more thing it keeps."""
WITH_CATALOG = replace(WRITABLE, catalog_tool="list_rows", catalog_args=lambda limit: {"top": limit})
"""The same writable host, plus a listing verb -- for the case a search by word finds nothing.

Declares no `catalog_total`, even though `list_rows` answers one: a vocabulary opts in to
reading it, so this is the "total unknown" case on purpose."""

WITH_CATALOG_TOTAL = replace(WITH_CATALOG, catalog_total=lambda raw: raw.get("total"))
"""The same host, with the vocabulary now reading the total `list_rows` already answers."""

BROKEN_CATALOG = replace(WITH_CATALOG, catalog_tool="no_such_tool")
"""A host whose catalog verb is not there -- stands in for a listing endpoint that is simply down."""


# --- a model that makes exactly the calls a test wants -----------------------------------


@dataclass
class Script:
    """A model that issues the given tool calls, then reports what last came back.

    Reporting the last tool return (or retry prompt) as its final text is what lets a test
    assert on the *content* of a refusal without reading private state.
    """

    calls: list[ToolCallPart]
    tools_seen: tuple[str, ...] = ()

    def model(self) -> FunctionModel:
        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            self.tools_seen = tuple(sorted(tool.name for tool in info.function_tools))
            turn = sum(1 for message in messages if isinstance(message, ModelResponse))
            if turn < len(self.calls):
                return ModelResponse(parts=[self.calls[turn]])
            return ModelResponse(parts=[TextPart(_last_feedback(messages))])

        return FunctionModel(respond)


def _last_feedback(messages: list[ModelMessage]) -> str:
    for message in reversed(messages):
        for part in reversed(message.parts):
            if isinstance(part, ToolReturnPart) and part.outcome == "retried":
                return f"RETRY {part.model_response_str()}"
            if isinstance(part, ToolReturnPart):
                return f"RETURN {part.model_response_str()}"
    return "nothing came back"


def _agent(script: Script, port: WorkflowContract[None]) -> Agent[None, str]:
    return Agent(script.model(), deps_type=type(None), output_type=str, capabilities=[port])


LIMITS = UsageLimits(request_limit=6)


# --- tests -----------------------------------------------------------------------------


async def test_the_model_sees_the_contract_plus_only_the_host_tools_you_exposed(registry: StepRegistry) -> None:
    """`store_row` is in the host surface and invisible to the model: that is the design."""
    host = FakeHost()
    port = WorkflowContract[None](
        source=host.toolset(), vocabulary=WRITABLE, expose=("find_row",), hide=("send_survey",), registry=registry
    )
    script = Script(calls=[])
    await _agent(script, port).run("say nothing", usage_limits=LIMITS)

    assert script.tools_seen == ("adapt_workflow", "fetch_workflow", "find_row", "save_workflow", "search_workflows")
    assert "store_row" not in script.tools_seen
    assert "read_row" not in script.tools_seen


async def test_search_and_fetch_translate_the_hosts_vocabulary(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """Our words go in, the host's words come out -- and the reverse, after the call."""
    host = FakeHost(rows={"wf-quarterly-report": yaml.safe_dump(quarterly_report.model_dump(mode="json"))})
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=registry)
    script = Script(
        calls=[
            ToolCallPart("search_workflows", {"task": "close the quarter", "limit": 3}),
            ToolCallPart("fetch_workflow", {"workflow_id": "wf-quarterly-report"}),
        ]
    )
    result = await _agent(script, port).run("find and read one", usage_limits=LIMITS)

    assert host.args_for("find_row") == {"text": "close the quarter", "top": 3}
    assert host.args_for("read_row") == {"key": "wf-quarterly-report"}
    assert "quarterly-report v3" in result.output
    assert "gates: approve" in result.output
    assert "FITS THIS RUN" in result.output


async def test_the_ledger_puts_earned_trust_on_the_search_and_fetch_surface(
    quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path
) -> None:
    """The evidence a field test found invisible, now on both reads.

    A live probe asked two doors "which workflow has earned the most trust" and both honestly
    declined: the record sat in the ledger and nothing on the agent's surface exposed it.
    """
    ledger = FileEvidenceLog(tmp_path / "evidence.jsonl")
    await ledger.append(
        RunEvidence(
            workflow_id="wf-quarterly-report",
            spec_hash=spec_identity(quarterly_report),
            workflow_version=quarterly_report.version,
            outcome="completed",
            result=Outcome(status="succeeded", summary="the report was published"),
            runner="test", gates_auto=(), gates_orchestrated=()
        )
    )
    host = FakeHost(rows={"wf-quarterly-report": yaml.safe_dump(quarterly_report.model_dump(mode="json"))})
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=registry, evidence_log=ledger)
    script = Script(
        calls=[
            ToolCallPart("search_workflows", {"task": "close the quarter", "limit": 3}),
            ToolCallPart("fetch_workflow", {"workflow_id": "wf-quarterly-report"}),
        ]
    )
    result = await _agent(script, port).run("find and read one", usage_limits=LIMITS)

    returns = {part.tool_name: str(part.content) for message in result.all_messages() for part in message.parts if isinstance(part, ToolReturnPart)}
    assert "earned: machine-confirmed, 1 runs with evidence" in returns["search_workflows"], "the card carries what the id earned"
    assert "earned: machine-confirmed, 1 runs with evidence" in returns["fetch_workflow"], "the fetch carries the hash-keyed record"


async def test_without_a_ledger_no_trust_is_invented(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """A store with no ledger beside it says nothing about trust -- absence, not a made-up tier."""
    host = FakeHost(rows={"wf-quarterly-report": yaml.safe_dump(quarterly_report.model_dump(mode="json"))})
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=registry)
    script = Script(
        calls=[
            ToolCallPart("search_workflows", {"task": "close the quarter", "limit": 3}),
            ToolCallPart("fetch_workflow", {"workflow_id": "wf-quarterly-report"}),
        ]
    )
    result = await _agent(script, port).run("find and read one", usage_limits=LIMITS)

    returns = " ".join(str(part.content) for message in result.all_messages() for part in message.parts if isinstance(part, ToolReturnPart))
    assert "earned:" not in returns


async def test_a_fetched_spec_says_whether_this_run_can_execute_it(quarterly_report: Workflow, call_log: CallLog) -> None:
    """The port answers the fit question itself; it used to point at a tool that did not exist."""
    thin = StepRegistry()
    register_quarterly_steps(thin, call_log, exclude=frozenset({"publish"}))
    host = FakeHost(rows={"wf-quarterly-report": yaml.safe_dump(quarterly_report.model_dump(mode="json"))})
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=thin)
    script = Script(calls=[ToolCallPart("fetch_workflow", {"workflow_id": "wf-quarterly-report"})])
    result = await _agent(script, port).run("read it", usage_limits=LIMITS)

    assert "DOES NOT FIT THIS RUN: no step named publish" in result.output
    assert "do not keep re-reading it" in result.output


async def test_a_fetched_spec_comes_back_as_usable_yaml(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """A fetch round-trips: what comes out parses back into the spec that went in.

    This is the regression that produced a confidently wrong answer. The tool is named for the
    spec but returned only a summary, so an agent asked "does this saved workflow run here?" had
    nothing but prose to hand the validator -- which correctly refused it as malformed YAML, once
    per workflow, and the agent reported the entire library broken. Asserting on the round trip
    rather than on the presence of the word `yaml` is the point: a summary that merely *looks*
    like a spec would pass a substring check and fail a person.
    """
    host = FakeHost(rows={"wf-quarterly-report": yaml.safe_dump(quarterly_report.model_dump(mode="json"))})
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=registry)
    script = Script(calls=[ToolCallPart("fetch_workflow", {"workflow_id": "wf-quarterly-report"})])
    result = await _agent(script, port).run("read it", usage_limits=LIMITS)

    fetched = json.loads(result.output.removeprefix("RETURN "))
    assert Workflow.model_validate(yaml.safe_load(fetched["spec_yaml"])) == quarterly_report
    assert f"{quarterly_report.name} v{quarterly_report.version}" in fetched["summary"]
    assert "FITS THIS RUN" in fetched["fit"]
    assert fetched["fits_here"] is True


async def test_a_fetch_is_a_structured_return_a_program_can_subscript(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """The CodeMode lesson, applied to our own tool: `r["spec_yaml"]`, not regex surgery.

    These contract tools live inside the sandbox, where a program consumes the return. A
    spec fenced inside prose forced exactly the string surgery that killed the sandbox
    programs -- so the return is data with named fields, and the prose lives in them.
    """
    host = FakeHost(rows={"wf-quarterly-report": yaml.safe_dump(quarterly_report.model_dump(mode="json"))})
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=registry)
    script = Script(calls=[ToolCallPart("fetch_workflow", {"workflow_id": "wf-quarterly-report"})])
    result = await _agent(script, port).run("read it", usage_limits=LIMITS)

    fetched = json.loads(result.output.removeprefix("RETURN "))
    assert set(fetched) == {"summary", "fit", "fits_here", "spec_yaml", "staleness"}
    assert "```" not in fetched["spec_yaml"], "the YAML is the field's whole value, not fenced prose"


async def test_without_a_registry_fits_here_is_unknown_not_false(quarterly_report: Workflow) -> None:
    host = FakeHost(rows={"wf-quarterly-report": yaml.safe_dump(quarterly_report.model_dump(mode="json"))})
    port = WorkflowContract[None](source=host.toolset(), vocabulary=READ_ONLY, expose=())
    script = Script(calls=[ToolCallPart("fetch_workflow", {"workflow_id": "wf-quarterly-report"})])
    result = await _agent(script, port).run("read it", usage_limits=LIMITS)

    fetched = json.loads(result.output.removeprefix("RETURN "))
    assert fetched["fits_here"] is None, "no registry means unknown, and unknown is not a no"


async def test_without_a_registry_the_port_admits_it_cannot_judge_fit(quarterly_report: Workflow) -> None:
    host = FakeHost(rows={"wf-quarterly-report": yaml.safe_dump(quarterly_report.model_dump(mode="json"))})
    port = WorkflowContract[None](source=host.toolset(), vocabulary=READ_ONLY, expose=())
    script = Script(calls=[ToolCallPart("fetch_workflow", {"workflow_id": "wf-quarterly-report"})])
    result = await _agent(script, port).run("read it", usage_limits=LIMITS)

    assert "fit: unknown from here" in result.output


async def test_the_port_reaches_a_tool_the_model_cannot_see(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """`expose=()` hides every host tool from the model; the port still calls two of them."""
    host = FakeHost(rows={"wf-quarterly-report": yaml.safe_dump(quarterly_report.model_dump(mode="json"))})
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=registry)
    script = Script(calls=[ToolCallPart("fetch_workflow", {"workflow_id": "wf-quarterly-report"})])
    await _agent(script, port).run("read it", usage_limits=LIMITS)

    assert script.tools_seen == ("adapt_workflow", "fetch_workflow", "save_workflow", "search_workflows")
    assert host.tools_called == ("read_row",)


async def test_save_refuses_a_spec_this_run_cannot_execute_and_writes_nothing(quarterly_report: Workflow, call_log: CallLog) -> None:
    """The assertion that matters is the negative one: the host was never asked to store."""
    thin = StepRegistry()
    register_quarterly_steps(thin, call_log, exclude=frozenset({"publish"}))
    host = FakeHost()
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=thin)
    script = Script(
        calls=[
            ToolCallPart(
                "save_workflow",
                {
                    "spec_yaml": _yaml(quarterly_report),
                    "applicability": "Use at quarter end. Not for ad-hoc questions.",
                    "title": "Quarterly report",
                },
            )
        ]
    )
    result = await _agent(script, port).run("save it", usage_limits=LIMITS)

    assert "REFUSED" in result.output
    assert "unknown_work" in result.output
    assert host.calls == []
    assert host.rows == {}


async def test_a_store_refusal_comes_back_as_a_result_the_model_can_act_on(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """A `LibraryError` on the write is a correction, not a crash -- both doors say the same thing.

    The store can refuse a write the validator cannot see (re-minting a version it has already
    superseded, for one), and its message names the way forward. Untranslated, it propagated out
    of the tool function and killed the whole chat turn -- the one reader capable of following
    "fetch the newest version and adapt that" never saw it.
    """
    host = FakeHost(refuse_store="'wf-quarterly-report' is already superseded by a later version. Fetch the newest and adapt that.")
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=registry)
    script = Script(
        calls=[
            ToolCallPart(
                "save_workflow",
                {
                    "spec_yaml": _yaml(quarterly_report),
                    "applicability": "Use at quarter end. Not for ad-hoc questions.",
                    "title": "Quarterly report",
                },
            )
        ]
    )
    result = await _agent(script, port).run("save it", usage_limits=LIMITS)

    assert "REFUSED" in result.output
    assert "superseded" in result.output
    assert host.rows == {}, "nothing was written"


async def test_repeated_adapt_refusals_never_crash_the_turn(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """A correction loop ends with the model reporting, not with `UnexpectedModelBehavior`.

    Live, a fourth refused `adapt_workflow` call hit pydantic-ai's retry ceiling and killed the
    whole chat turn -- the person watched the conversation die instead of reading what was wrong.
    Refusals are results now, so there is no ceiling to hit and the last word is the model's.
    """
    host = FakeHost(rows={"wf-quarterly-report": yaml.safe_dump(quarterly_report.model_dump(mode="json"))})
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=registry)
    bad_call = ToolCallPart(
        "adapt_workflow",
        {"workflow_id": "wf-quarterly-report", "ops": [{"op": "remove_node", "id": "ghost"}], "applicability": "Use when."},
    )
    script = Script(calls=[bad_call, bad_call, bad_call, bad_call])
    result = await _agent(script, port).run("adapt it", usage_limits=LIMITS)

    assert "RETURN" in result.output, "the fourth refusal reads exactly like the first"
    assert "refused" in result.output
    assert "ghost" in result.output
    assert "store_row" not in host.tools_called, "nothing was ever written"


async def test_save_writes_through_the_hosts_own_write_tool(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """A valid spec crosses: adapted into the host's words, and its id read back, not guessed."""
    host = FakeHost()
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=registry, saved_by="agent:test")
    script = Script(
        calls=[
            ToolCallPart(
                "save_workflow",
                {
                    "spec_yaml": _yaml(quarterly_report),
                    "applicability": "Use at quarter end. Not for ad-hoc questions.",
                    "title": "Quarterly report",
                },
            )
        ]
    )
    result = await _agent(script, port).run("save it", usage_limits=LIMITS)

    assert host.tools_called == ("store_row",)
    assert host.args_for("store_row")["label"] == "Quarterly report"
    assert host.args_for("store_row")["who"] == "agent:test"
    assert "saved as wf-quarterly-report" in result.output
    stored = Workflow.model_validate(yaml.safe_load(host.rows["wf-quarterly-report"]))
    assert stored == quarterly_report


def _writes_captured(kept: list[WorkflowWrite]) -> LibraryVocabulary:
    """`WRITABLE`, keeping every write the port makes as the typed value it actually crossed as."""

    def put_args(write: WorkflowWrite) -> dict[str, object]:
        kept.append(write)
        return _write_args(write)

    return replace(WRITABLE, put_args=put_args)


_ASKED_FOR = {
    "request": "Give me a decision brief on whether to keep the current retrieval policy",
    "expected_result": "One brief I can read in two minutes, with the counterevidence kept.",
    "assumptions": ["The knowledge base to read is the one this session already names"],
}
"""What a person stated, in the shape the save tool takes it: incomplete on purpose."""


async def test_a_save_carrying_an_intent_writes_it_beside_the_workflow(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """What was asked for travels with the save rather than being dropped on the way to the library."""
    kept: list[WorkflowWrite] = []
    host = FakeHost()
    port = WorkflowContract[None](source=host.toolset(), vocabulary=_writes_captured(kept), expose=(), registry=registry)
    script = Script(
        calls=[
            ToolCallPart(
                "save_workflow",
                {
                    "spec_yaml": _yaml(quarterly_report),
                    "applicability": "Use at quarter end. Not for ad-hoc questions.",
                    "title": "Quarterly report",
                    "intent": _ASKED_FOR,
                },
            )
        ]
    )
    result = await _agent(script, port).run("save it", usage_limits=LIMITS)

    assert "saved as wf-quarterly-report" in result.output
    assert len(kept) == 1
    assert kept[0].intent == WorkflowIntent.model_validate(_ASKED_FOR)


async def test_a_save_nobody_stated_an_intent_for_writes_none(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """Absent stays absent: no request is composed on the person's behalf."""
    kept: list[WorkflowWrite] = []
    host = FakeHost()
    port = WorkflowContract[None](source=host.toolset(), vocabulary=_writes_captured(kept), expose=(), registry=registry)
    script = Script(
        calls=[
            ToolCallPart(
                "save_workflow",
                {"spec_yaml": _yaml(quarterly_report), "applicability": "Use at quarter end. Not for ad-hoc questions."},
            )
        ]
    )
    await _agent(script, port).run("save it", usage_limits=LIMITS)

    assert len(kept) == 1
    assert kept[0].intent is None


async def test_an_adaptation_carrying_an_intent_writes_it_with_the_child(quarterly_report: Workflow, registry: StepRegistry) -> None:
    kept: list[WorkflowWrite] = []
    host = FakeHost(rows={"wf-quarterly-report": yaml.safe_dump(quarterly_report.model_dump(mode="json"))})
    port = WorkflowContract[None](source=host.toolset(), vocabulary=_writes_captured(kept), expose=(), registry=registry)
    script = Script(
        calls=[
            ToolCallPart(
                "adapt_workflow",
                {
                    "workflow_id": "wf-quarterly-report",
                    "ops": [{"op": "set_description", "to": "Close the quarter, and say what moved."}],
                    "applicability": "Use at quarter end. Not for ad-hoc questions.",
                    "intent": _ASKED_FOR,
                },
            )
        ]
    )
    await _agent(script, port).run("adapt it", usage_limits=LIMITS)

    assert len(kept) == 1
    assert kept[0].intent == WorkflowIntent.model_validate(_ASKED_FOR)


async def test_save_refuses_a_card_nobody_could_choose_by(quarterly_report: Workflow, registry: StepRegistry) -> None:
    host = FakeHost()
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=registry)
    script = Script(calls=[ToolCallPart("save_workflow", {"spec_yaml": _yaml(quarterly_report), "applicability": "  "})])
    result = await _agent(script, port).run("save it", usage_limits=LIMITS)

    assert "applicability is empty" in result.output
    assert host.calls == []


async def test_the_card_falls_back_to_what_the_spec_already_says(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """Title and summary are optional because the spec names and describes itself.

    Asking a model to restate them was an invitation to fold every argument into the spec
    object, which is exactly what one did -- four times in a row, until the retries ran out.
    """
    host = FakeHost()
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=registry)
    script = Script(
        calls=[ToolCallPart("save_workflow", {"spec_yaml": _yaml(quarterly_report), "applicability": "At quarter end only."})]
    )
    await _agent(script, port).run("save it", usage_limits=LIMITS)

    written = host.args_for("store_row")
    assert written["label"] == "Quarterly report"  # from spec.name
    assert written["note"] == quarterly_report.description


async def test_a_read_only_host_offers_no_way_to_save(registry: StepRegistry) -> None:
    """Declaring no write slots is more honest than a save tool that fails when called."""
    host = FakeHost()
    port = WorkflowContract[None](source=host.toolset(), vocabulary=READ_ONLY, expose=(), registry=registry)
    script = Script(calls=[])
    await _agent(script, port).run("say nothing", usage_limits=LIMITS)

    assert port.can_save is False
    assert script.tools_seen == ("fetch_workflow", "search_workflows")
    note = port.get_instructions()
    assert note is not None
    assert "read-only" in note(RunContext(deps=None, model=TestModel(), usage=RunUsage()))


async def test_a_writable_host_without_a_registry_still_offers_no_save() -> None:
    """No way to verify means no way to write: a store of unrunnable procedures is a landfill."""
    host = FakeHost()
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=())
    script = Script(calls=[])
    await _agent(script, port).run("say nothing", usage_limits=LIMITS)

    assert port.can_save is False
    assert "save_workflow" not in script.tools_seen


async def test_the_save_gesture_can_demand_protocol_level_approval(registry: StepRegistry) -> None:
    """Over ACP the save button becomes a permission request, and the flag rides the tool itself.

    `requires_approval` is pydantic-ai's own tool marking -- the ACP adapter reads it off the
    toolset and turns the call into a client-side permission prompt, which is the save covenant
    ("deciding to keep something is theirs") expressed at the protocol level. The default stays
    False: the HTTP studio renders its own Save button, and a deferred-approval save there would
    wait the chat loop on a question nothing renders.
    """
    host = FakeHost()
    port = WorkflowContract[None](
        source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=registry, save_requires_approval=True
    )
    toolset = port.get_toolset()
    assert isinstance(toolset, CombinedToolset)
    contract = next(inner for inner in toolset.toolsets if isinstance(inner, FunctionToolset))
    assert contract.tools["save_workflow"].requires_approval, "the write gesture asks the human"
    assert not contract.tools["search_workflows"].requires_approval, "reads never do"
    assert not contract.tools["adapt_workflow"].requires_approval, "adapt only produces a draft; the write is still save"

    unmarked = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=registry)
    plain = unmarked.get_toolset()
    assert isinstance(plain, CombinedToolset)
    default_contract = next(inner for inner in plain.toolsets if isinstance(inner, FunctionToolset))
    assert not default_contract.tools["save_workflow"].requires_approval


async def test_save_refuses_malformed_yaml_with_the_validators_findings(registry: StepRegistry) -> None:
    """`spec_yaml` is parsed and checked here -- a broken draft is refused, not a schema 422."""
    host = FakeHost()
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=registry)
    script = Script(
        calls=[ToolCallPart("save_workflow", {"spec_yaml": "name: [unterminated", "applicability": "Whenever."})]
    )
    result = await _agent(script, port).run("save it", usage_limits=LIMITS)

    assert "RETURN" in result.output, "the refusal is a tool result, never a retry that counts toward a ceiling"
    assert "not well-formed" in result.output
    assert host.calls == []


async def test_save_refuses_yaml_that_is_not_a_spec_at_all(registry: StepRegistry) -> None:
    """Well-formed YAML that fails the `Workflow` schema is refused the same way, not a crash."""
    host = FakeHost()
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=registry)
    script = Script(calls=[ToolCallPart("save_workflow", {"spec_yaml": "just: a mapping", "applicability": "Whenever."})])
    result = await _agent(script, port).run("save it", usage_limits=LIMITS)

    assert "RETURN" in result.output, "the refusal is a tool result, never a retry that counts toward a ceiling"
    assert "not well-formed" in result.output
    assert host.calls == []


def test_the_write_doors_stay_always_on_without_a_capability_id(registry: StepRegistry) -> None:
    """The default: no `save_capability_id` set, both write doors are plain, always-visible tools."""
    host = FakeHost()
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=registry)

    toolset = port.get_toolset()
    assert isinstance(toolset, CombinedToolset)
    contract = next(inner for inner in toolset.toolsets if isinstance(inner, FunctionToolset))
    for door in ("save_workflow", "adapt_workflow"):
        assert contract.tools[door].tool_def.defer_loading is False
        assert contract.tools[door].tool_def.capability_id is None


async def test_both_write_doors_are_hidden_behind_the_given_capability_id(registry: StepRegistry) -> None:
    """`save_capability_id` ties both write doors to a deferred capability -- `search`/`fetch` stay on.

    Both, because both take the same grammar the capability teaches: an `adapt_workflow` offered
    without it invited edit ops in a guessed shape, which live burned every retry on one field
    error at a time.
    """
    host = FakeHost()
    port = WorkflowContract[None](
        source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=registry, save_capability_id="saving-a-workflow"
    )
    ctx = RunContext(deps=None, model=TestModel(), usage=RunUsage())

    toolset = port.get_toolset()
    assert toolset is not None
    tools = await toolset.get_tools(ctx)

    for door in ("save_workflow", "adapt_workflow"):
        assert tools[door].tool_def.defer_loading is True
        assert tools[door].tool_def.capability_id == "saving-a-workflow"
    assert tools["search_workflows"].tool_def.defer_loading is False
    assert tools["fetch_workflow"].tool_def.defer_loading is False


def test_a_half_declared_write_surface_is_a_construction_error() -> None:
    with pytest.raises(ValueError, match="all three of put_tool"):
        LibraryVocabulary(
            search_tool="find_row",
            fetch_tool="read_row",
            search_args=lambda task, limit: {"text": task, "top": limit},
            fetch_args=lambda workflow_id: {"key": workflow_id},
            to_cards=_to_cards,
            to_spec=_to_spec,
            put_tool="store_row",
        )


async def test_a_broken_host_search_is_reported_as_broken_not_as_empty(registry: StepRegistry) -> None:
    """An unchecked store is not an empty one, and the difference must survive to the model."""
    host = FakeHost()
    port = WorkflowContract[None](source=host.toolset(), vocabulary=BROKEN_SEARCH, expose=(), registry=registry)
    script = Script(calls=[ToolCallPart("search_workflows", {"task": "anything"})])
    result = await _agent(script, port).run("look for one", usage_limits=LIMITS)

    assert "LIBRARY SEARCH UNAVAILABLE" in result.output
    assert "NOT 'nothing matched'" in result.output
    assert "no candidate" not in result.output  # never the wording used for a real empty result


async def test_an_untranslatable_answer_is_a_retry_not_a_half_parsed_card(registry: StepRegistry) -> None:
    """The boundary holds: a row with no document yields a retry naming the failure."""
    host = FakeHost(rows={})
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=registry)
    script = Script(calls=[ToolCallPart("fetch_workflow", {"workflow_id": "wf-missing"})])
    result = await _agent(script, port).run("read a missing one", usage_limits=LIMITS)

    assert "RETRY" in result.output
    assert "did not yield a valid workflow spec" in result.output
    assert "Do not invent one" in result.output


# --- the catalog fallback: nothing matched by word, offer the whole library ----------------------


async def test_the_search_tool_asks_for_the_persons_own_words(registry: StepRegistry) -> None:
    """The argument doc asks for the request verbatim, not a rewrite into a topic."""
    host = FakeHost()
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=registry)
    toolset = port.get_toolset()
    assert toolset is not None
    ctx = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    tools = await toolset.get_tools(ctx)

    description = tools[SEARCH_TOOL].tool_def.parameters_json_schema["properties"]["task"]["description"]
    assert "in their own words" in description


async def test_zero_hits_with_a_catalog_tool_offers_the_whole_shelf(registry: StepRegistry) -> None:
    """A search that matches nothing by word is not a dead end when the vocabulary can list everything."""
    host = FakeHost(rows={}, catalog_rows={"wf-quarterly-report": "doc", "wf-audit": "doc2"})
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WITH_CATALOG, expose=(), registry=registry)
    script = Script(calls=[ToolCallPart("search_workflows", {"task": "biography of einstein"})])
    result = await _agent(script, port).run("look for one", usage_limits=LIMITS)

    assert "2 saved workflows follow." in result.output, "no catalog_total declared, so the total is unknown"
    assert "wf-quarterly-report" in result.output
    assert "wf-audit" in result.output
    assert "Narrow with words from the request, or fetch one by id with fetch_workflow." in result.output
    assert host.tools_called == ("find_row", "list_rows")


async def test_a_real_hit_never_triggers_the_catalog_fallback(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """Hits stay unchanged: the catalog tool is only reached when the search itself came up empty."""
    host = FakeHost(
        rows={"wf-quarterly-report": yaml.safe_dump(quarterly_report.model_dump(mode="json"))}, catalog_rows={"wf-other": "x"}
    )
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WITH_CATALOG, expose=(), registry=registry)
    script = Script(calls=[ToolCallPart("search_workflows", {"task": "close the quarter"})])
    result = await _agent(script, port).run("look for one", usage_limits=LIMITS)

    assert "wf-quarterly-report" in result.output
    assert "wf-other" not in result.output
    assert "saved workflows follow" not in result.output
    assert host.tools_called == ("find_row",), "the catalog tool is never called once the search already found something"


async def test_zero_hits_without_a_catalog_tool_keeps_the_old_line(registry: StepRegistry) -> None:
    """A vocabulary that never declared a catalog tool keeps today's zero-hit behavior exactly."""
    host = FakeHost(rows={})
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=registry)
    script = Script(calls=[ToolCallPart("search_workflows", {"task": "anything"})])
    result = await _agent(script, port).run("look for one", usage_limits=LIMITS)

    assert "no saved workflow matches" in result.output
    assert "saved workflows follow" not in result.output


async def test_an_empty_catalog_keeps_the_old_zero_hit_line(registry: StepRegistry) -> None:
    """A catalog tool that itself has nothing to offer is not dressed up as a listing."""
    host = FakeHost(rows={}, catalog_rows={})
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WITH_CATALOG, expose=(), registry=registry)
    script = Script(calls=[ToolCallPart("search_workflows", {"task": "anything"})])
    result = await _agent(script, port).run("look for one", usage_limits=LIMITS)

    assert "no saved workflow matches" in result.output
    assert "saved workflows follow" not in result.output
    assert host.tools_called == ("find_row", "list_rows"), "the catalog tool was still tried"


async def test_a_broken_catalog_call_is_reported_as_broken_not_as_empty(registry: StepRegistry) -> None:
    """The same LIBRARY SEARCH UNAVAILABLE shape a broken search tool gets -- a ModelRetry, not a dead end."""
    host = FakeHost(rows={})
    port = WorkflowContract[None](source=host.toolset(), vocabulary=BROKEN_CATALOG, expose=(), registry=registry)
    script = Script(calls=[ToolCallPart("search_workflows", {"task": "anything"})])
    result = await _agent(script, port).run("look for one", usage_limits=LIMITS)

    assert "LIBRARY SEARCH UNAVAILABLE" in result.output
    assert "no_such_tool" in result.output
    assert "NOT 'nothing matched'" in result.output


async def test_the_catalog_header_states_the_total_when_the_host_says_and_more_remain(registry: StepRegistry) -> None:
    """Ten rows at most, and a header naming how many the library actually holds."""
    catalog_rows = {f"wf-{i:02d}": "doc" for i in range(15)}
    host = FakeHost(rows={}, catalog_rows=catalog_rows)
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WITH_CATALOG_TOTAL, expose=(), registry=registry)
    script = Script(calls=[ToolCallPart("search_workflows", {"task": "anything"})])
    result = await _agent(script, port).run("look for one", usage_limits=LIMITS)

    assert "15 saved workflows, the first 10 by title follow." in result.output
    assert result.output.count(" [trusted] ") == 10, "exactly ten cards, even though fifteen exist"
    assert host.args_for("list_rows") == {"top": 10}


async def test_the_catalog_header_says_all_when_the_total_equals_what_is_shown(registry: StepRegistry) -> None:
    host = FakeHost(rows={}, catalog_rows={"wf-a": "doc", "wf-b": "doc"})
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WITH_CATALOG_TOTAL, expose=(), registry=registry)
    script = Script(calls=[ToolCallPart("search_workflows", {"task": "anything"})])
    result = await _agent(script, port).run("look for one", usage_limits=LIMITS)

    assert "2 saved workflows, all of them follow." in result.output


async def test_the_catalog_header_is_singular_for_one(registry: StepRegistry) -> None:
    host = FakeHost(rows={}, catalog_rows={"wf-a": "doc"})
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WITH_CATALOG, expose=(), registry=registry)
    script = Script(calls=[ToolCallPart("search_workflows", {"task": "anything"})])
    result = await _agent(script, port).run("look for one", usage_limits=LIMITS)

    assert "1 saved workflow follows." in result.output


def test_render_cards_with_applicability_false_drops_only_the_when_line() -> None:
    """The catalog listing's own knob: everything else about a card's line stays put.

    Purpose and produces are one short line each and are what a chooser scans a catalog by, so
    they survive the knob that drops the long applicability note.
    """
    card = SavedWorkflow(
        id="wf-a",
        title="A",
        summary="one line",
        applicability="Use when X. Not when Y.",
        provisional=False,
        presentation=WorkflowPresentation(purpose="Settle one contested claim.", produces="A note and what is still open."),
    )
    with_note = render_cards([card])
    without_note = render_cards([card], applicability=False)

    assert "when: Use when X. Not when Y." in with_note
    assert "when:" not in without_note
    assert "wf-a [trusted] A: one line" in without_note
    assert "purpose: Settle one contested claim." in without_note
    assert "produces: A note and what is still open." in without_note


def test_a_card_with_a_presentation_states_its_purpose_and_what_it_produces() -> None:
    """The author's own words, above the applicability note and in the order a chooser reads them."""
    card = SavedWorkflow(
        id="wf-a",
        title="A",
        summary="one line",
        applicability="Use when X. Not when Y.",
        presentation=WorkflowPresentation(purpose="Settle one contested claim.", produces="A note and what is still open."),
    )

    rendered = render_cards([card]).splitlines()

    assert rendered == [
        "wf-a [provisional] A: one line",
        "    purpose: Settle one contested claim.",
        "    produces: A note and what is still open.",
        "    when: Use when X. Not when Y.",
    ]


def test_a_card_with_no_presentation_states_no_purpose_at_all() -> None:
    """Nothing here composes prose a card does not carry."""
    card = SavedWorkflow(id="wf-a", title="A", summary="one line", applicability="Use when X.")

    rendered = render_cards([card])

    assert "purpose:" not in rendered
    assert "produces:" not in rendered


async def test_the_host_catalog_tool_is_not_among_the_agents_tools(registry: StepRegistry) -> None:
    """The catalog tool is reached by the port, the same way the search and fetch tools are."""
    host = FakeHost()
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WITH_CATALOG, expose=(), registry=registry)
    script = Script(calls=[])
    await _agent(script, port).run("say nothing", usage_limits=LIMITS)

    assert "list_rows" not in script.tools_seen


# --- a spec that brings its own step ----------------------------------------------------

BROUGHT = Workflow(
    name="judged-report",
    version=1,
    description="reconcile, then judge the ledger with a step this artifact carries",
    parameters={"quarter": ParameterSpec(description="which quarter", json_schema={"type": "string"})},
    inline=(
        Briefing(
            name="judge_ledger",
            instructions="Read the ledger and say in one line whether the quarter looks healthy, and why it looks that way.",
            inputs={"ledger": SameAs(like="reconcile")},
            output=Shape(json_schema={"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}),
        ),
    ),
    nodes=(
        RunNode(id="sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
        RunNode(id="costs", work="fetch_costs", inputs={"quarter": FromParameter(parameter="quarter")}),
        RunNode(id="ledger", work="reconcile", inputs={"sales": FromNode(node="sales"), "costs": FromNode(node="costs")}),
        RunNode(id="judge", work="judge_ledger", inputs={"ledger": FromNode(node="ledger")}),
    ),
    output=FromNode(node="judge"),
)


async def _never_run(task: Briefing, output_type: type, /, **inputs: object) -> object:  # pragma: no cover - saving never executes
    msg = "saving must not run a step"
    raise AssertionError(msg)


async def test_save_builds_the_steps_a_spec_brought_before_deciding(registry: StepRegistry) -> None:
    """The write gate covers invented steps too, on the same route and with the same check.

    Without an author the port would report the spec's own step as unknown work and refuse a
    perfectly good artifact -- so "can this run here" has to include "can I build what it brought".
    """
    host = FakeHost()
    port = WorkflowContract[None](
        source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=_performing(registry), saved_by="agent:test"
    )
    script = Script(
        calls=[
            ToolCallPart("save_workflow", {"spec_yaml": _yaml(BROUGHT), "applicability": "Use at quarter end. Not for ad-hoc questions."})
        ]
    )
    result = await _agent(script, port).run("save it", usage_limits=LIMITS)

    assert "saved as wf-judged-report" in result.output
    stored = Workflow.model_validate(yaml.safe_load(host.rows["wf-judged-report"]))
    assert stored == BROUGHT  # the briefing and the declared shape survive the write, verbatim
    assert stored.inline[0].name == "judge_ledger"


async def test_a_port_with_no_author_refuses_a_spec_that_brought_a_step(registry: StepRegistry) -> None:
    """And refuses it *cold*, writing nothing -- the artifact is fine, this run just cannot build it."""
    host = FakeHost()
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=registry)
    script = Script(
        calls=[
            ToolCallPart("save_workflow", {"spec_yaml": _yaml(BROUGHT), "applicability": "Use at quarter end. Not otherwise."})
        ]
    )
    result = await _agent(script, port).run("save it", usage_limits=LIMITS)

    assert "REFUSED. Nothing was written" in result.output
    assert "cannot perform a briefing" in result.output
    assert host.rows == {}


def _performing(steps: StepRegistry) -> StepRegistry:
    """The same steps, in a registry that can also perform a briefing.

    One argument where the port used to take `registry=` and `author=` side by side -- and had to
    `and` their two absences together before it could offer saving at all.
    """
    performing = StepRegistry(perform=_never_run)
    for name in steps.names:
        if (impl := steps.get(name)) is not None:
            performing.register(impl.function, name=name)
    return performing


async def test_a_fetched_spec_that_brings_its_own_step_is_reported_as_fitting(registry: StepRegistry) -> None:
    """A workflow carrying what it needs FITS. Reporting a missing step would be exactly backwards."""
    host = FakeHost(rows={"wf-judged-report": yaml.safe_dump(BROUGHT.model_dump(mode="json"))})
    port = WorkflowContract[None](source=host.toolset(), vocabulary=WRITABLE, expose=(), registry=_performing(registry))
    script = Script(calls=[ToolCallPart("fetch_workflow", {"workflow_id": "wf-judged-report"})])
    result = await _agent(script, port).run("read it", usage_limits=LIMITS)

    assert "brings its own steps: judge_ledger" in result.output
    assert "FITS THIS RUN" in result.output
    assert "DOES NOT FIT" not in result.output


# --- a library of steps --------------------------------------------------------------------


def _ctx() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


def _port(host: FakeHost, tmp_path: Path) -> WorkflowContract[None]:
    """A writable, performing port over `host`, its vocabulary naming the library tools too."""
    registry = StepRegistry(perform=_never_run)
    register_quarterly_steps(registry, CallLog())
    return WorkflowContract[None](
        source=host.toolset(),
        vocabulary=WRITABLE_WITH_SHELF,
        expose=(),
        registry=registry,
        saved_by="agent:test",
        evidence_log=FileEvidenceLog(tmp_path / "evidence.jsonl"),
    )


def _digest_step() -> Briefing:
    return Briefing(
        name="digest_source",
        instructions="Read the whole mirror and call write_note with one digest page that quotes its claims.",
        inputs={"source": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema={"type": "string"}),
    )


def _spec_carrying(step: InlineStep) -> Workflow:
    """A minimal spec that already brings `step` under `tasks` and runs it from one node."""
    return Workflow(
        name="digest-one",
        version=1,
        description="Digest one source.",
        parameters={"source": ParameterSpec(description="a source id", json_schema={"type": "string"})},
        inline=(step,),
        nodes=(RunNode(id="d", work=step.name, inputs={"source": FromParameter(parameter="source")}),),
        output=FromNode(node="d"),
    )


async def test_search_steps_and_fetch_step_read_the_hosts_own_steps(tmp_path: Path) -> None:
    host = FakeHost(steps={"st.digest_source.v1": _digest_step().model_dump(mode="json")})
    port = _port(host, tmp_path)
    found = await port._search_steps(_ctx(), "digest a mirror")
    assert "st.digest_source.v1" in found
    shown = await port._fetch_step(_ctx(), "st.digest_source.v1")
    assert "digest_source" in shown
    assert "write_note" in shown


async def test_a_step_search_with_no_hits_says_so_in_its_own_words(tmp_path: Path) -> None:
    """A step search that found nothing must not report on workflows -- it was not asked about them."""
    port = _port(FakeHost(steps={}), tmp_path)

    found = await port._search_steps(_ctx(), "nothing matches this")

    assert found == "(no reusable step matches. Plan fresh instead of forcing a bad fit)"
    assert "workflow" not in found


async def test_save_copies_used_steps_into_the_artifact_and_records_them(tmp_path: Path) -> None:
    host = FakeHost(steps={"st.digest_source.v1": _digest_step().model_dump(mode="json")})
    port = _port(host, tmp_path)
    spec = Workflow(
        name="digest-one",
        version=1,
        description="Digest one source.",
        parameters={"source": ParameterSpec(description="a source id", json_schema={"type": "string"})},
        nodes=(RunNode(id="d", work="digest_source", inputs={"source": FromParameter(parameter="source")}),),
        output=FromNode(node="d"),
    )
    said = await port._save(_ctx(), _yaml(spec), applicability="Use for one mirror. Do not use for a batch.", uses=["st.digest_source.v1"])
    assert "saved as" in said
    stored = host.stored[-1]
    written = Workflow.model_validate(stored["spec"])
    assert [task.name for task in written.inline] == ["digest_source"]
    assert stored["uses"] == ["st.digest_source.v1"]


async def test_save_refuses_a_used_step_that_collides_with_a_carried_one(tmp_path: Path) -> None:
    host = FakeHost(steps={"st.digest_source.v1": _digest_step().model_dump(mode="json")})
    port = _port(host, tmp_path)
    spec = _spec_carrying(_digest_step())
    said = await port._save(_ctx(), _yaml(spec), applicability="Use once. Do not use twice.", uses=["st.digest_source.v1"])
    assert "REFUSED" in said
    assert "digest_source" in said
    assert host.stored == []


async def test_save_refuses_a_uses_list_naming_the_same_step_twice(tmp_path: Path) -> None:
    """The plan promises a refusal, not a silent dedupe -- a repeat is a mistake worth surfacing."""
    host = FakeHost(steps={"st.digest_source.v1": _digest_step().model_dump(mode="json")})
    port = _port(host, tmp_path)
    spec = Workflow(
        name="digest-one",
        version=1,
        description="Digest one source.",
        parameters={"source": ParameterSpec(description="a source id", json_schema={"type": "string"})},
        nodes=(RunNode(id="d", work="digest_source", inputs={"source": FromParameter(parameter="source")}),),
        output=FromNode(node="d"),
    )
    said = await port._save(
        _ctx(), _yaml(spec), applicability="Use once. Do not use twice.", uses=["st.digest_source.v1", "st.digest_source.v1"]
    )
    assert "REFUSED" in said
    assert "more than once" in said
    assert host.stored == []


async def test_fetch_step_on_an_unknown_id_is_a_retry_not_a_crash(tmp_path: Path) -> None:
    """The adaptation boundary applies to the library too: an untranslatable answer is a named retry."""
    host = FakeHost()
    port = _port(host, tmp_path)
    with pytest.raises(ModelRetry, match="did not yield a valid reusable step"):
        await port._fetch_step(_ctx(), "st.missing.v1")


async def test_fetch_step_on_a_malformed_payload_is_a_retry_not_a_crash(tmp_path: Path) -> None:
    host = FakeHost(steps={"st.broken.v1": {"name": "not_a_real_step"}})
    port = _port(host, tmp_path)
    with pytest.raises(ModelRetry, match="did not yield a valid reusable step"):
        await port._fetch_step(_ctx(), "st.broken.v1")


def test_shelf_tools_appear_only_when_the_vocabulary_names_them() -> None:
    """`search_steps`/`fetch_step` are gated on the vocabulary alone, the same way `can_save` gates saving."""
    host = FakeHost()

    def contract_tool_names(vocabulary: LibraryVocabulary) -> Mapping[str, object]:
        toolset = WorkflowContract[None](source=host.toolset(), vocabulary=vocabulary, expose=()).get_toolset()
        assert isinstance(toolset, CombinedToolset)
        contract = next(inner for inner in toolset.toolsets if isinstance(inner, FunctionToolset))
        return contract.tools

    assert "search_steps" not in contract_tool_names(READ_ONLY)
    assert "fetch_step" not in contract_tool_names(READ_ONLY)
    assert "search_steps" not in contract_tool_names(WRITABLE)
    assert "fetch_step" not in contract_tool_names(WRITABLE)
    assert "search_steps" in contract_tool_names(WRITABLE_WITH_SHELF)
    assert "fetch_step" in contract_tool_names(WRITABLE_WITH_SHELF)


def test_the_production_vocabularys_put_args_carries_uses_through(quarterly_report: Workflow) -> None:
    """`WORKFLOW_COLLECTION.put_args` is where `uses` reaches `write_workflow`.

    Proven against the production mapping directly, not through the test-only stand-in whose
    own mapping hid this gap.
    """
    write = WorkflowWrite(
        workflow=quarterly_report, title="t", summary="s", applicability="a", saved_by="agent:test", uses=["st.digest_source.v1"]
    )
    put_args = WORKFLOW_COLLECTION.put_args
    assert put_args is not None
    assert put_args(write)["uses"] == ["st.digest_source.v1"]
