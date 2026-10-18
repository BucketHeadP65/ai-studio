"""`FileCustomSteps`: a custom step lands disabled, never active, until it is.

Mirrors `test_selfauthoring_library.py`'s pending-gate parity for capabilities, over the step
store instead: `write()` always lands the manifest entry disabled, `load_active()` registers
nothing for a step that was never activated, and only `activate()`/`deactivate()` flip it,
each attested through the same activations `FileCustomCapabilities` writes through.
"""

from __future__ import annotations

import re

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import RegistrationError, StepRegistry

from harness.features.selfauthoring.exceptions import StepValidationError
from harness.features.selfauthoring.infrastructure.steps import FileCustomSteps, validate_step_file
from tests.support.selfauthoring import FakeActivationLog
from tests.support.staging import staged_names

if TYPE_CHECKING:
    from pathlib import Path

_VALID_SOURCE = """
async def double(x: int) -> int:
    '''Doubles x.'''
    return x * 2
"""

_TWO_FUNCTION_SOURCE = """
async def double(x: int) -> int:
    return x * 2


async def triple(x: int) -> int:
    return x * 3
"""

_MISSING_ANNOTATION_SOURCE = """
async def double(x) -> int:
    return x * 2
"""

_ANY_ANNOTATION_SOURCE = """
from typing import Any


async def double(x: Any) -> int:
    return x * 2
"""

_NO_ASYNC_FUNCTION_SOURCE = """
def double(x: int) -> int:
    return x * 2
"""

_UNDERSCORE_ONLY_SOURCE = """
async def _double(x: int) -> int:
    return x * 2
"""

_BROKEN_SOURCE = "this is not python at all !!!"


def _store(tmp_path: Path) -> tuple[FileCustomSteps, FakeActivationLog]:
    activations = FakeActivationLog()
    return FileCustomSteps(tmp_path / "steps", activations=activations), activations


def test_a_cleanly_validated_write_lands_disabled(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)

    record = store.write("double", _VALID_SOURCE)

    assert record.status == "disabled", "pending is the only landing state, even for valid code"
    assert record.last_error is None
    assert record.function_names == ["double"]


def test_every_public_async_function_is_registered(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)

    record = store.write("arithmetic", _TWO_FUNCTION_SOURCE)

    assert record.function_names == ["double", "triple"]


def test_missing_annotation_is_refused_with_the_registrys_own_message(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)

    record = store.write("double", _MISSING_ANNOTATION_SOURCE)

    assert record.status == "disabled"
    assert record.last_error == "work 'double' has unannotated parameters: x"


def test_any_annotation_is_refused_with_the_registrys_own_message(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)

    record = store.write("double", _ANY_ANNOTATION_SOURCE)

    assert record.last_error == "work 'double' has Any annotations: x"


def test_a_module_with_no_public_async_function_is_a_refusal_not_a_silent_no_op(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)

    record = store.write("double", _NO_ASYNC_FUNCTION_SOURCE)

    assert record.last_error == "a step module must define at least one public async function"


def test_a_leading_underscore_function_does_not_count_as_public(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)

    record = store.write("double", _UNDERSCORE_ONLY_SOURCE)

    assert record.last_error == "a step module must define at least one public async function"


def test_a_write_that_fails_to_import_also_lands_disabled(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)

    record = store.write("broken", _BROKEN_SOURCE)

    assert record.status == "disabled"
    assert record.last_error is not None


def test_reauthoring_an_active_step_attests_the_deactivation(tmp_path: Path) -> None:
    """Overwriting an ACTIVE record is itself a deactivation -- the reviewed code is gone."""
    store, activations = _store(tmp_path)
    store.write("double", _VALID_SOURCE)
    store.activate("double")
    activations.events.clear()

    store.write("double", _TWO_FUNCTION_SOURCE)

    assert [(event.name, event.action, event.via, event.kind) for event in activations.events] == [
        ("double", "deactivate", "agent", "step_activated")
    ]


def test_reauthoring_a_pending_step_attests_nothing(tmp_path: Path) -> None:
    store, activations = _store(tmp_path)
    store.write("double", _VALID_SOURCE)
    activations.events.clear()

    store.write("double", _TWO_FUNCTION_SOURCE)

    assert activations.events == []


def test_activating_a_step_with_a_stored_error_is_refused_naming_it(tmp_path: Path) -> None:
    store, activations = _store(tmp_path)
    store.write("double", _MISSING_ANNOTATION_SOURCE)

    with pytest.raises(ValueError, match=re.escape("work 'double' has unannotated parameters: x")):
        store.activate("double")

    assert activations.events == [], "the refusal must not attest anything -- nothing changed"
    [record] = store.list_all()
    assert record.status == "disabled"


def test_a_disabled_authored_step_is_not_loaded(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)
    store.write("double", _VALID_SOURCE)

    registry = StepRegistry()
    loaded = store.load_active(registry)

    assert loaded == (), "written but never activated -- not loaded this run nor any later one"
    assert "double" not in registry


def test_activation_loads_it_and_is_attested(tmp_path: Path) -> None:
    store, activations = _store(tmp_path)
    store.write("double", _VALID_SOURCE)

    assert store.activate("double") is True

    registry = StepRegistry()
    loaded = store.load_active(registry)
    assert loaded == ("double",)
    assert "double" in registry
    assert [(event.name, event.action, event.via, event.kind) for event in activations.events] == [
        ("double", "activate", "cli", "step_activated")
    ]


def test_activating_an_unknown_name_is_reported_and_not_attested(tmp_path: Path) -> None:
    store, activations = _store(tmp_path)

    assert store.activate("nobody") is False
    assert activations.events == []


def test_deactivation_stops_loading_and_is_attested(tmp_path: Path) -> None:
    store, activations = _store(tmp_path)
    store.write("double", _VALID_SOURCE)
    store.activate("double")
    activations.events.clear()

    assert store.deactivate("double") is True

    registry = StepRegistry()
    assert store.load_active(registry) == ()
    assert [(event.name, event.action, event.via, event.kind) for event in activations.events] == [
        ("double", "deactivate", "cli", "step_activated")
    ]


def test_the_default_activation_log_is_used_when_none_is_given(tmp_path: Path) -> None:
    store = FileCustomSteps(tmp_path / "steps", activations=FakeActivationLog())
    store.write("double", _VALID_SOURCE)

    assert store.activate("double") is True
    registry = StepRegistry()
    assert store.load_active(registry) == ("double",)


def test_two_active_steps_colliding_on_a_function_name_fail_soft(tmp_path: Path) -> None:
    """One bad step never blocks the rest.

    The second registration fails, its error is recorded, and the first step's function stays
    registered.
    """
    store, _activations = _store(tmp_path)
    store.write("first", _VALID_SOURCE)
    store.write("second", _VALID_SOURCE)
    store.activate("first")
    store.activate("second")

    registry = StepRegistry()
    loaded = store.load_active(registry)

    assert loaded == ("double",), "only the first step to register 'double' wins this run"
    records = {record.name: record for record in store.list_all()}
    assert records["second"].last_error is not None


def test_validate_step_file_returns_the_registrys_message_verbatim(tmp_path: Path) -> None:
    path = tmp_path / "double.py"
    path.write_text(_MISSING_ANNOTATION_SOURCE, encoding="utf-8")

    with pytest.raises(StepValidationError, match=r"^work 'double' has unannotated parameters: x$"):
        validate_step_file(path)


def test_validate_step_file_wraps_the_registrys_error_type(tmp_path: Path) -> None:
    """The registry's own front plugin (`register()`) is the validator.

    Confirm the same `RegistrationError` message a plain registry raises for this shape of
    mistake also surfaces here, verbatim.
    """

    async def double(x) -> int:  # type: ignore[no-untyped-def]
        return x * 2

    with pytest.raises(RegistrationError) as registry_excinfo:
        StepRegistry().register(double)
    registry_message = str(registry_excinfo.value)

    path = tmp_path / "double.py"
    path.write_text(_MISSING_ANNOTATION_SOURCE, encoding="utf-8")
    with pytest.raises(StepValidationError, match=re.escape(registry_message)):
        validate_step_file(path)
# --- delete() -------------------------------------------------------------------------------


def test_delete_removes_a_disabled_step_its_file_and_attests(tmp_path: Path) -> None:
    store, activations = _store(tmp_path)
    store.write("double", _VALID_SOURCE)

    deleted = store.delete("double")

    assert deleted
    assert store.list_all() == []
    assert not (tmp_path / "steps" / "double.py").exists()
    assert (activations.events[-1].name, activations.events[-1].action) == ("double", "delete")


def test_delete_refuses_an_active_step_naming_the_fix(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)
    store.write("double", _VALID_SOURCE)
    store.activate("double")

    with pytest.raises(ValueError, match="disable"):
        store.delete("double")


def test_delete_of_an_unknown_name_is_false(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)

    assert not store.delete("ghost")


def test_the_manifest_is_staged_under_a_hidden_temp_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The manifest goes through the one shared atomic write, which creates its temp file exclusively."""
    store, _ = _store(tmp_path)
    staged = staged_names(monkeypatch)

    store.write("double", _VALID_SOURCE)

    assert staged, "the manifest is swapped into place"
    assert all(re.fullmatch(r"\.manifest\.json\.[0-9a-f]{32}\.tmp", name) for name in staged)
