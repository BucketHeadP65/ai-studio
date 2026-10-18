"""`FileCustomAgents`: a custom agent lands disabled, never active, until it is.

Mirrors `test_selfauthoring_steps_library.py`'s pending-gate parity for steps, over agent
documents instead: `write()` always lands the manifest entry disabled, validation is
`AgentSpec.model_validate` plus every capability name resolving against the capability registry,
and `resolve_active()` is the one plugin a `AgentStep` string reference reaches through -- refusing
an unknown name and a pending one with two different, honest messages.
"""

from __future__ import annotations

import re

from typing import TYPE_CHECKING

import pytest

from harness.features.selfauthoring.exceptions import AgentValidationError
from harness.features.selfauthoring.infrastructure.agents import FileCustomAgents, validate_agent_document
from tests.support.selfauthoring import FakeActivationLog

if TYPE_CHECKING:
    from pathlib import Path

BRIEFING = "Read the ledger and say whether the quarter looks healthy."

_VALID_DOCUMENT = f"instructions: {BRIEFING!r}\n"

_VALID_DOCUMENT_WITH_MODEL = f"model: openai:gpt-5\ninstructions: {BRIEFING!r}\n"

_NOT_YAML = "this is not: yaml: at all: [\n"

_NOT_A_MAPPING = "- just\n- a\n- list\n"

_FAILS_AGENTSPEC = "end_strategy: nonsense\n"

_UNKNOWN_CAPABILITY = f"instructions: {BRIEFING!r}\ncapabilities:\n  - nonexistent_capability_xyz\n"


def _store(tmp_path: Path) -> tuple[FileCustomAgents, FakeActivationLog]:
    activations = FakeActivationLog()
    return FileCustomAgents(tmp_path / "agents", activations=activations), activations


# --- write() and the pending gate -----------------------------------------------------------


def test_a_cleanly_validated_write_lands_disabled(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)

    record = store.write("weigher", _VALID_DOCUMENT)

    assert record.status == "disabled", "pending is the only landing state, even for a valid document"
    assert record.last_error is None


def test_the_documents_own_model_is_recorded_for_display(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)

    record = store.write("weigher", _VALID_DOCUMENT_WITH_MODEL)

    assert record.model == "openai:gpt-5"


def test_a_document_naming_no_model_records_none(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)

    record = store.write("weigher", _VALID_DOCUMENT)

    assert record.model is None


def test_invalid_yaml_is_refused_and_still_lands_disabled(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)

    record = store.write("broken", _NOT_YAML)

    assert record.status == "disabled"
    assert record.last_error is not None
    assert "YAML" in record.last_error


def test_yaml_that_is_not_a_mapping_is_refused_naming_what_it_actually_is(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)

    record = store.write("broken", _NOT_A_MAPPING)

    assert record.last_error is not None
    assert "mapping" in record.last_error
    assert "list" in record.last_error


def test_a_document_that_fails_agentspec_validation_is_refused_with_a_trimmed_message(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)

    record = store.write("broken", _FAILS_AGENTSPEC)

    assert record.last_error is not None
    assert "end_strategy" in record.last_error
    assert "https://errors.pydantic.dev" not in record.last_error, "trimmed to the point -- no schema-error URL"


def test_an_unknown_capability_name_is_refused_naming_it_and_the_known_ones(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)

    record = store.write("broken", _UNKNOWN_CAPABILITY)

    assert record.last_error is not None
    assert "nonexistent_capability_xyz" in record.last_error
    assert "WebSearch" in record.last_error, "the registry's own known names are named too"


def test_a_write_that_fails_validation_can_be_fixed_by_reauthoring_the_same_name(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)
    store.write("weigher", _NOT_YAML)

    record = store.write("weigher", _VALID_DOCUMENT)

    assert record.status == "disabled"
    assert record.last_error is None


def test_reauthoring_an_active_agent_attests_the_deactivation(tmp_path: Path) -> None:
    """Overwriting an ACTIVE record is itself a deactivation -- the reviewed document is gone."""
    store, activations = _store(tmp_path)
    store.write("weigher", _VALID_DOCUMENT)
    store.activate("weigher")
    activations.events.clear()

    store.write("weigher", _VALID_DOCUMENT_WITH_MODEL)

    assert [(event.name, event.action, event.via, event.kind) for event in activations.events] == [
        ("weigher", "deactivate", "agent", "agent_activated")
    ]


def test_reauthoring_a_pending_agent_attests_nothing(tmp_path: Path) -> None:
    """It was never active, so there is no deactivation to record."""
    store, activations = _store(tmp_path)
    store.write("weigher", _VALID_DOCUMENT)
    activations.events.clear()

    store.write("weigher", _VALID_DOCUMENT_WITH_MODEL)

    assert activations.events == []


def test_reauthoring_a_never_before_seen_name_attests_nothing(tmp_path: Path) -> None:
    store, activations = _store(tmp_path)

    store.write("weigher", _VALID_DOCUMENT)

    assert activations.events == []


# --- activation, attested through the shared activations ------------------------------------------


def test_activation_is_attested_with_the_agent_kind(tmp_path: Path) -> None:
    store, activations = _store(tmp_path)
    store.write("weigher", _VALID_DOCUMENT)

    assert store.activate("weigher") is True

    assert [(event.name, event.action, event.via, event.kind) for event in activations.events] == [
        ("weigher", "activate", "cli", "agent_activated")
    ]
    [record] = store.list_all()
    assert record.status == "active"


def test_deactivation_is_attested_with_the_agent_kind(tmp_path: Path) -> None:
    store, activations = _store(tmp_path)
    store.write("weigher", _VALID_DOCUMENT)
    store.activate("weigher")
    activations.events.clear()

    assert store.deactivate("weigher") is True

    assert [(event.name, event.action, event.via, event.kind) for event in activations.events] == [
        ("weigher", "deactivate", "cli", "agent_activated")
    ]


def test_activating_an_unknown_name_is_reported_and_not_attested(tmp_path: Path) -> None:
    store, activations = _store(tmp_path)

    assert store.activate("nobody") is False
    assert activations.events == []


def test_activating_a_document_with_a_stored_error_is_refused_naming_it(tmp_path: Path) -> None:
    store, activations = _store(tmp_path)
    store.write("broken", _UNKNOWN_CAPABILITY)

    with pytest.raises(ValueError, match=re.escape("nonexistent_capability_xyz")):
        store.activate("broken")

    assert activations.events == [], "the refusal must not attest anything -- nothing changed"
    [record] = store.list_all()
    assert record.status == "disabled", "still pending -- activation was refused, not silently skipped"


def test_the_default_activation_log_is_used_when_none_is_given(tmp_path: Path) -> None:
    store = FileCustomAgents(tmp_path / "agents", activations=FakeActivationLog())
    store.write("weigher", _VALID_DOCUMENT)

    assert store.activate("weigher") is True


# --- resolve_active(): the plugin an AgentStep string reference reaches through ----------------


def test_resolving_an_unknown_name_names_it(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)

    with pytest.raises(LookupError, match="nobody"):
        store.resolve_active("nobody")


def test_resolving_a_pending_name_names_the_activation_command(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)
    store.write("weigher", _VALID_DOCUMENT)

    with pytest.raises(LookupError, match=re.escape("harness activate agent weigher")):
        store.resolve_active("weigher")


def test_resolving_an_active_agent_returns_its_document(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)
    store.write("weigher", _VALID_DOCUMENT_WITH_MODEL)
    store.activate("weigher")

    document = store.resolve_active("weigher")

    assert document["model"] == "openai:gpt-5"
    assert document["instructions"] == BRIEFING


def test_resolving_the_same_name_after_deactivation_is_pending_again(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)
    store.write("weigher", _VALID_DOCUMENT)
    store.activate("weigher")
    store.deactivate("weigher")

    with pytest.raises(LookupError, match="activate agent weigher"):
        store.resolve_active("weigher")


# --- validate_agent_document(): the standalone front plugin, same one write() uses -------------


def test_validate_agent_document_returns_the_agentspec_on_success() -> None:
    agent_spec = validate_agent_document(_VALID_DOCUMENT_WITH_MODEL)

    assert agent_spec.model == "openai:gpt-5"
    assert agent_spec.instructions == BRIEFING


def test_validate_agent_document_raises_for_an_unknown_capability() -> None:
    with pytest.raises(AgentValidationError, match="nonexistent_capability_xyz"):
        validate_agent_document(_UNKNOWN_CAPABILITY)
# --- delete() -------------------------------------------------------------------------------


def test_delete_removes_a_disabled_agent_its_document_and_attests(tmp_path: Path) -> None:
    store, activations = _store(tmp_path)
    store.write("weigher", _VALID_DOCUMENT)

    deleted = store.delete("weigher")

    assert deleted
    assert store.list_all() == []
    assert not (tmp_path / "agents" / "weigher.yaml").exists()
    assert (activations.events[-1].name, activations.events[-1].action) == ("weigher", "delete")


def test_delete_refuses_an_active_agent_naming_the_fix(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)
    store.write("weigher", _VALID_DOCUMENT)
    store.activate("weigher")

    with pytest.raises(ValueError, match="disable"):
        store.delete("weigher")


def test_delete_of_an_unknown_name_is_false(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)

    assert not store.delete("ghost")
