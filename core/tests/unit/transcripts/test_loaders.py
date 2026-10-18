"""The loader registry: a source's scheme picks the loader, the rest of the source is what it opens."""

from __future__ import annotations

import re

from datetime import UTC, datetime

import pytest

from pydantic_ai.messages import ModelRequest, UserPromptPart

from core.ai.transcripts.contracts import RunEmptyError, SchemeUnknownError, Step, Transcript, TranscriptLoaders, unreadable_reason
from core.ai.transcripts.loaders import scheme_and_locator

AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
A_TRANSCRIPT = Transcript(
    source="test:run", steps=(Step(messages=(ModelRequest(parts=[UserPromptPart(content="hello", timestamp=AT)]),), ordinals=(0,)),)
)


class Recording:
    """A loader that remembers what it was asked to open."""

    def __init__(self) -> None:
        self.opened: list[str] = []

    async def __call__(self, locator: str) -> Transcript:
        self.opened.append(locator)
        return A_TRANSCRIPT


async def test_load_hands_the_locator_to_the_loader_registered_for_the_scheme() -> None:
    loader = Recording()

    got = await TranscriptLoaders(by_key={"test": loader}).load("test:run-1")

    assert loader.opened == ["run-1"]
    assert got is A_TRANSCRIPT


async def test_a_locator_keeps_its_own_colons() -> None:
    loader = Recording()

    await TranscriptLoaders(by_key={"test": loader}).load("test:a:b")

    assert loader.opened == ["a:b"]


async def test_load_refuses_a_source_with_no_scheme() -> None:
    with pytest.raises(ValueError, match="<scheme>:<locator>"):
        await TranscriptLoaders(by_key={"test": Recording()}).load("run-1")


async def test_load_refuses_a_scheme_nothing_is_registered_for_as_an_unknown_source_not_a_missing_run() -> None:
    with pytest.raises(SchemeUnknownError, match="'other'") as raised:
        await TranscriptLoaders(by_key={"test": Recording()}).load("other:run-1")

    assert not isinstance(raised.value, LookupError)
    assert unreadable_reason(raised.value) == "unknown source"


def test_unreadable_reason_names_each_kind_of_loader_failure() -> None:
    assert unreadable_reason(RunEmptyError("no step")) == "no steps"
    assert unreadable_reason(LookupError("gone")) == "run gone"
    assert unreadable_reason(FileNotFoundError("gone")) == "run gone"
    assert unreadable_reason(ValueError("no scheme")) == "unknown source"


def test_scheme_and_locator_splits_at_the_first_colon() -> None:
    assert scheme_and_locator("test:a:b") == ("test", "a:b")


@pytest.mark.parametrize("source", ["run-1", ":run-1"])
def test_scheme_and_locator_refuses_a_source_with_no_scheme(source: str) -> None:
    with pytest.raises(ValueError, match=re.escape(f"a source reads <scheme>:<locator>, not {source!r}")):
        scheme_and_locator(source)
