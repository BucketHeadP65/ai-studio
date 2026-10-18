"""The ingestion policy page: rules in frontmatter over the spec's defaults, guidance in the body."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.markdown import split_frontmatter

from engram.kb.contracts import SourceQueuePolicy, read_policy, render_policy, write_policy
from engram.kb.infrastructure.policy_page import POLICY_PATH, SourceQueuePolicyPage
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from pathlib import Path


def test_no_page_means_the_spec_defaults_and_no_guidance(tmp_path: Path) -> None:
    page = read_policy(tmp_path, research_spec())

    assert page == SourceQueuePolicyPage(rules=SourceQueuePolicy(), guidance="", present=False)


def test_the_page_round_trips_and_overrides_the_defaults_field_by_field(tmp_path: Path) -> None:
    rules = SourceQueuePolicy(max_age_years=6, source_types=("paper", "article"))

    path = write_policy(tmp_path, rules, "Take papers on memory for agents.")
    page = read_policy(tmp_path, research_spec())

    assert path == tmp_path / POLICY_PATH
    assert page.present is True
    assert page.rules == rules
    assert page.rules.per_source_cap == 5
    assert page.guidance == "Take papers on memory for agents."


def test_per_pass_cap_round_trips_and_defaults_to_unset(tmp_path: Path) -> None:
    rules = SourceQueuePolicy(per_pass_cap=12)

    write_policy(tmp_path, rules, "Take papers on memory for agents.")
    page = read_policy(tmp_path, research_spec())

    assert page.rules.per_pass_cap == 12
    assert SourceQueuePolicy().per_pass_cap is None


def test_the_rendered_page_reads_as_a_meta_note() -> None:
    front, body = split_frontmatter(render_policy(SourceQueuePolicy(hop_depth=2), "Guidance here."))

    assert front["id"] == "meta.ingestion-policy"
    assert front["type"] == "meta"
    assert front["title"] == "Ingestion policy"
    assert front["hop_depth"] == 2
    assert front["never"] == ["profile", "encyclopedia", "homepage", "social-post", "repository"]
    assert body.strip() == "# Ingestion policy\n\nGuidance here."


def test_a_page_with_invalid_yaml_is_read_back_without_raising(tmp_path: Path) -> None:
    """An unclosed flow sequence, the shape a hand edit in Obsidian produces, never a raw exception out of read_policy."""
    (tmp_path / "_meta").mkdir()
    (tmp_path / POLICY_PATH).write_text(
        "---\nid: meta.ingestion-policy\ntype: meta\ntitle: Ingestion policy\nnever: [profile\n---\n\n# Ingestion policy\n\nx\n",
        encoding="utf-8",
    )

    page = read_policy(tmp_path, research_spec())

    assert page.present is True
    assert page.parse_error is not None
    assert page.rules == SourceQueuePolicy()
    assert page.guidance == ""


def test_a_rule_outside_its_closed_set_is_reported_not_raised(tmp_path: Path) -> None:
    (tmp_path / "_meta").mkdir()
    (tmp_path / POLICY_PATH).write_text(
        "---\nid: meta.ingestion-policy\ntype: meta\ntitle: Ingestion policy\nnever: [forum]\n---\n\n# Ingestion policy\n\nx\n",
        encoding="utf-8",
    )

    page = read_policy(tmp_path, research_spec())

    assert page.present is True
    assert page.parse_error is not None
    assert "never" in page.parse_error
    assert page.rules == SourceQueuePolicy()


def test_frontmatter_that_is_not_a_mapping_is_reported_not_read_as_defaults(tmp_path: Path) -> None:
    (tmp_path / "_meta").mkdir()
    (tmp_path / POLICY_PATH).write_text("---\n- one\n- two\n---\n\nbody\n", encoding="utf-8")

    page = read_policy(tmp_path, research_spec())

    assert page.present is True
    assert page.parse_error is not None
    assert page.rules == SourceQueuePolicy()
    assert page.guidance == ""


def test_an_unknown_rule_key_is_carried_not_dropped(tmp_path: Path) -> None:
    (tmp_path / "_meta").mkdir()
    (tmp_path / POLICY_PATH).write_text(
        "---\nid: meta.ingestion-policy\ntype: meta\ntitle: Ingestion policy\nhopdepth: 4\n---\n\n# Ingestion policy\n\nx\n",
        encoding="utf-8",
    )

    page = read_policy(tmp_path, research_spec())

    assert page.parse_error is None
    assert page.rules.hop_depth == 1
    assert page.unknown_rules == ("hopdepth",)


def test_a_page_that_names_only_one_rule_keeps_the_rest_from_the_spec(tmp_path: Path) -> None:
    (tmp_path / "_meta").mkdir()
    (tmp_path / POLICY_PATH).write_text(
        "---\nid: meta.ingestion-policy\ntype: meta\ntitle: Ingestion policy\nhop_depth: 3\n---\n\n# Ingestion policy\n\nOnly depth.\n",
        encoding="utf-8",
    )

    page = read_policy(tmp_path, research_spec())

    assert page.rules.hop_depth == 3
    assert page.rules.per_source_cap == 5
    assert page.guidance == "Only depth."
    assert page.unknown_rules == ()
