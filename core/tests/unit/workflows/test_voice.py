"""The engine's voice renders under the host's tool names and stays free of any host's vocabulary."""

from typing import get_args

from core.ai.workflows.contracts import Node, process_instructions

NODE_KINDS = tuple(str(node.model_fields["kind"].default) for node in get_args(get_args(Node)[0]))
"""Every verb the grammar has, read off the `Node` union itself so a sixth kind fails this guard."""


def test_renders_under_the_hosts_check_tool_name():
    text = process_instructions(check_tool="verify_spec")

    assert "`verify_spec` first, every time" in text
    assert "check_draft" not in text


def test_speaks_the_whole_grammar():
    text = process_instructions(check_tool="check_draft")

    assert len(NODE_KINDS) == 5
    for node_kind in NODE_KINDS:
        assert f"`{node_kind}`" in text, f"the voice never names {node_kind}, so an author never writes one"
    assert "five kinds of node" in text, "the count the voice states is the count the grammar has"
    assert "side effect takes its input from a GATE" in text
    assert "You never write orchestration code" in text


def test_names_the_orchestrator_gate_as_a_named_party_not_an_automatic_pass():
    """The gate a run clears unattended must read as a decider, or an author reaches for auto-approval instead."""
    text = process_instructions(check_tool="check_draft")

    assert "`orchestrator-gate`" in text
    assert "orchestrator" in text.lower()


def test_claims_nothing_about_what_a_host_has_registered():
    """The engine cannot see a host's registry, so it says nothing about it, empty or otherwise."""
    text = process_instructions(check_tool="check_draft")

    for claim in ("registers no steps", "no steps are registered", "no registered steps", "registry is empty", "nothing is registered"):
        assert claim not in text


def test_carries_no_host_vocabulary():
    """The library voice must work for any host: nothing studio-local may appear in it."""
    text = process_instructions(check_tool="check_draft")

    for host_word in ("saving-a-workflow", "spec_yaml", "graph panel", "shell"):
        assert host_word not in text
