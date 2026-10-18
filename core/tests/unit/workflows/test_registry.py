"""Registration introspects the step's contract, and refuses steps that cannot state one."""

from __future__ import annotations

from typing import Any

import pytest

from core.ai.workflows.contracts import RegistrationError, StepRegistry


async def _sum(first: int, second: int, scale: float = 1.0) -> float:
    return (first + second) * scale


def test_contract_is_introspected() -> None:
    registry = StepRegistry()
    impl = registry.register(_sum, name="summer")
    assert impl.input_types == {"first": int, "second": int, "scale": float}
    assert impl.required == {"first", "second"}
    assert impl.output_type is float
    assert registry.get("summer") is impl
    assert "summer" in registry


def test_duplicate_name_refused() -> None:
    registry = StepRegistry()
    registry.register(_sum)
    with pytest.raises(RegistrationError, match="already registered"):
        registry.register(_sum)


def test_sync_function_refused() -> None:
    def sync_step(x: int) -> int:
        return x

    with pytest.raises(RegistrationError, match="async"):
        StepRegistry().register(sync_step)


def test_unannotated_parameter_refused() -> None:
    async def sloppy(x) -> int:  # type: ignore[no-untyped-def]
        return 0

    with pytest.raises(RegistrationError, match="unannotated"):
        StepRegistry().register(sloppy)


def test_missing_return_annotation_refused() -> None:
    async def no_return(x: int):  # type: ignore[no-untyped-def]
        return x

    with pytest.raises(RegistrationError, match="return annotation"):
        StepRegistry().register(no_return)


def test_any_annotation_refused() -> None:
    async def vague(x: Any) -> int:
        return 0

    with pytest.raises(RegistrationError, match="Any annotations"):
        StepRegistry().register(vague)


def test_replay_safety_is_off_unless_asserted() -> None:
    """The unsafe default. A step nobody vouched for must never be re-executed by a replay.

    Registration cannot verify the claim -- it would have to know whether the body writes a
    file -- so silence has to mean "no", the same way `grants` defaults to no tools.
    """
    registry = StepRegistry()
    assert registry.register(_sum, name="unmarked").replay_safe is False


def test_replay_safety_is_the_integrators_assertion() -> None:
    registry = StepRegistry()
    assert registry.register(_sum, name="marked", replay_safe=True).replay_safe is True
    impl = registry.get("marked")
    assert impl is not None
    assert impl.replay_safe is True, "the flag has to survive onto the stored implementation, not just the return value"
