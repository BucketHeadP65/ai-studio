"""Unit tests for the description override seam (`core.ai.retrieval.descriptions`)."""

from __future__ import annotations

from core.ai.retrieval.descriptions import resolve_description


def test_none_mapping_keeps_the_default():
    assert resolve_description("search", "shipped default", None) == "shipped default"


def test_missing_key_keeps_the_default():
    assert resolve_description("search", "shipped default", {"fetch": "override"}) == "shipped default"


def test_present_key_returns_the_override():
    assert resolve_description("search", "shipped default", {"search": "override"}) == "override"
