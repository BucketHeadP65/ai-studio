"""The three-way gate mode a run launches under.

`gate_mode` is the whole grammar: `"wait"` holds every person gate for a person, `"auto"` passes
every gate itself with nobody asked, `"orchestrate"` hands a person gate to this run's own
orchestrator instead of waiting it. A declared orchestrator gate decides the same way under
every one of the three. The mode only changes what happens to a gate that would otherwise wait
on a person. `auto_approve_gates` is kept for old callers. Sending it `True` means `"auto"`, and
it must not be sent with `"orchestrate"`.
"""

from __future__ import annotations

from typing import Literal

GateMode = Literal["wait", "orchestrate", "auto"]

GATE_MODES: tuple[GateMode, ...] = ("wait", "orchestrate", "auto")
"""Every mode a launch may ask for, in the order a preview offers them: the person first."""

DEFAULT_GATE_MODE: GateMode = "wait"
"""What a launch that names no mode runs under: every person gate waits for a person."""


def effective_gate_mode(gate_mode: GateMode, *, auto_approve_gates: bool) -> GateMode:
    """The mode a launch runs under: `gate_mode`, or `auto_approve_gates` translated to it.

    `auto_approve_gates=True` means `"auto"`, so pairing it with `gate_mode="orchestrate"` asks for
    two different gate behaviors at once and is refused.

    Args:
        gate_mode: The mode the caller asked for.
        auto_approve_gates: The switch kept for old callers. `True` asks for `"auto"`.

    Returns:
        `"auto"` when `auto_approve_gates` is true, otherwise `gate_mode`.

    Raises:
        ValueError: `auto_approve_gates` is true and `gate_mode` asked for `"orchestrate"`.
    """
    if auto_approve_gates and gate_mode == "orchestrate":
        msg = (
            "auto_approve_gates true means every gate passes itself, and gate_mode orchestrate means "
            "this run's own orchestrator decides them instead. Send one, not both."
        )
        raise ValueError(msg)
    if auto_approve_gates:
        return "auto"
    return gate_mode
