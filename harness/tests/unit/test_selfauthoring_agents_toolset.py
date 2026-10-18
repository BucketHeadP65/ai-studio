"""`AgentCreationToolset`: what the authoring agent is actually told, in its own words.

The one honesty claim this slice lives or dies by: unlike `StepCreation` (next environment-build) and
the vendor `CapabilityCreation` (next agent run), a stored agent resolves the moment it's
activated -- `AGENT_GUIDANCE` and `write_custom_agent`'s own return string both have to say exactly
that, not the step's restart claim or the vendor's next-run claim.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harness.features.selfauthoring.infrastructure.activation_log import NoActivations
from harness.features.selfauthoring.infrastructure.agents import FileCustomAgents
from harness.features.selfauthoring.tools import AGENT_GUIDANCE, AgentCreation, AgentCreationToolset
from tests.support.selfauthoring import FakeActivationLog

if TYPE_CHECKING:
    from pathlib import Path

BRIEFING = "Read the ledger and say whether the quarter looks healthy."

_VALID_DOCUMENT = f"instructions: {BRIEFING!r}\n"

_UNKNOWN_CAPABILITY = f"instructions: {BRIEFING!r}\ncapabilities:\n  - nonexistent_capability_xyz\n"


def _toolset(tmp_path: Path) -> AgentCreationToolset[None]:
    return AgentCreationToolset[None](FileCustomAgents(tmp_path / "agents", activations=FakeActivationLog()))


async def test_authoring_a_valid_document_states_pending_and_immediate_resolution_honestly(tmp_path: Path) -> None:
    reply = await _toolset(tmp_path).write_custom_agent("weigher", _VALID_DOCUMENT)

    assert "harness activate agent weigher" in reply
    assert "no restart" in reply
    assert "waiting for the next turn" in reply


async def test_authoring_a_document_naming_an_unknown_capability_reports_it(tmp_path: Path) -> None:
    reply = await _toolset(tmp_path).write_custom_agent("broken", _UNKNOWN_CAPABILITY)

    assert "failed validation" in reply
    assert "nonexistent_capability_xyz" in reply
    assert "write_custom_agent again" in reply


async def test_list_custom_agents_reports_status_and_model(tmp_path: Path) -> None:
    toolset = _toolset(tmp_path)
    await toolset.write_custom_agent("weigher", f"model: openai:gpt-5\ninstructions: {BRIEFING!r}\n")

    reply = await toolset.list_custom_agents()

    assert "weigher [disabled] openai:gpt-5" in reply


async def test_list_custom_agents_reports_none_yet(tmp_path: Path) -> None:
    reply = await _toolset(tmp_path).list_custom_agents()

    assert reply == "No custom agents yet."


def test_agent_creation_guidance_states_the_pending_and_timing_contract() -> None:
    assert "harness activate agent <name>" in AGENT_GUIDANCE
    assert "no restart" in AGENT_GUIDANCE


def test_agent_creation_defaults_to_the_module_guidance(tmp_path: Path) -> None:
    capability = AgentCreation[None](directory=tmp_path / "agents", activations=NoActivations())

    assert capability.get_instructions() == AGENT_GUIDANCE


def test_agent_creation_guidance_can_be_overridden(tmp_path: Path) -> None:
    capability = AgentCreation[None](directory=tmp_path / "agents", guidance="custom text", activations=NoActivations())

    assert capability.get_instructions() == "custom text"


def test_agent_creation_guidance_can_be_omitted(tmp_path: Path) -> None:
    capability = AgentCreation[None](directory=tmp_path / "agents", guidance="", activations=NoActivations())

    assert capability.get_instructions() is None
