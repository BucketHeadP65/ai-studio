"""What can go wrong in one agent's work, as typed constants, and the shape of a finding about it.

A symptom is what can be seen going wrong in a transcript, and `shows_in` names the pydantic-ai part kind it shows in. A
root cause is what is behind it. A surface is the thing a fix edits. `TAXONOMY` holds every symptom and root cause and is
the one place a name arriving as a string (an inspector model's output) is resolved to its row.
"""

from __future__ import annotations

from typing import Final

from ._models import Blame, CaughtBy, Finding, FoundBy, Horizon, RootCause, ShowsIn, Surface, SurfaceKind, Symptom, Taxonomy
from ._root_causes import ROOT_CAUSES
from ._text_symptoms import TEXT_SYMPTOMS
from ._tool_call_symptoms import TOOL_CALL_SYMPTOMS

TAXONOMY: Final = Taxonomy(root_causes=ROOT_CAUSES, symptoms=(*TOOL_CALL_SYMPTOMS, *TEXT_SYMPTOMS))

__all__ = [
    "ROOT_CAUSES",
    "TAXONOMY",
    "TEXT_SYMPTOMS",
    "TOOL_CALL_SYMPTOMS",
    "Blame",
    "CaughtBy",
    "Finding",
    "FoundBy",
    "Horizon",
    "RootCause",
    "ShowsIn",
    "Surface",
    "SurfaceKind",
    "Symptom",
    "Taxonomy",
]
