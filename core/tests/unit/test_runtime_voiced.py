"""`RuntimeVoiced`: a literal capability voice re-shaped so it renders after the identity.

pydantic-ai renders literal instruction strings ahead of every function-shaped block, so a
mounted capability that speaks in a bare string would open the prompt before the agent's
identity. The wrapper turns the string into a callable — same words, later layer — and fails
loud when the wrapped capability stops speaking in a literal, so an obsolete wrapper is removed
rather than silently passing through.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from core.ai.voice import RuntimeVoiced

if TYPE_CHECKING:
    from collections.abc import Callable


class _Literal(AbstractCapability[None]):
    def get_instructions(self) -> str:
        return "Spoken as a literal."


class _Silent(AbstractCapability[None]):
    pass


class _FunctionShaped(AbstractCapability[None]):
    def get_instructions(self) -> Callable[[RunContext[None]], str]:
        return lambda _ctx: "Already function-shaped."


def _ctx() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


def test_a_literal_voice_becomes_function_shaped_with_the_same_words():
    compose = RuntimeVoiced(_Literal()).get_instructions()
    assert callable(compose)  # a literal here would render ahead of the agent's identity
    assert compose(_ctx()) == "Spoken as a literal."


def test_a_silent_capability_stays_silent():
    assert RuntimeVoiced(_Silent()).get_instructions() is None


def test_an_already_function_shaped_voice_refuses_the_wrapper():
    with pytest.raises(TypeError, match="Drop its RuntimeVoiced wrapper"):
        RuntimeVoiced(_FunctionShaped()).get_instructions()
