"""The three-way gate mode a run launches under.

`gate_mode` is the whole grammar: `"wait"` holds every person gate for a person, `"auto"` passes
every gate itself with nobody asked, `"orchestrate"` hands a person gate to this run's own
orchestrator instead of waiting it. A declared orchestrator gate decides the same way under
every one of the three. The mode only changes what happens to a gate that would otherwise wait
on a person.
"""

from __future__ import annotations

from typing import Literal

GateMode = Literal["wait", "orchestrate", "auto"]

GATE_MODES: tuple[GateMode, ...] = ("wait", "orchestrate", "auto")
"""Every mode a launch may ask for, in the order a preview offers them: the person first."""

DEFAULT_GATE_MODE: GateMode = "wait"
"""What a launch that names no mode runs under: every person gate waits for a person."""

