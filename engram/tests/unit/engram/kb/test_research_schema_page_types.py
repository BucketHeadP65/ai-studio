import pytest

from engram.testing.specs import research_spec


@pytest.mark.parametrize(
    ("page_type", "prefix", "folder"),
    [("talk", "talk.", "Wiki/Talks"), ("article", "art.", "Wiki/Articles"), ("post", "post.", "Wiki/Posts"), ("flag", "flag.", "Wiki/Flags"), ("stance", "stance.", "Wiki/Stances")],
)
def test_the_new_kinds_are_declared(page_type: str, prefix: str, folder: str) -> None:
    pt = research_spec().page_type(page_type)
    assert pt is not None
    assert pt.id_prefix == prefix
    assert pt.folder == folder


def test_digest_kinds_carry_digest_of_claims_and_the_typed_edges() -> None:
    spec = research_spec()
    for page_type in ("talk", "article", "post"):
        pt = spec.page_type(page_type)
        assert pt is not None
        fields = {field.name: field for field in pt.frontmatter_fields}
        assert fields["digest_of"].required is True
        assert pt.claims_enabled is True
        assert set(pt.edge_fields) == {"builds_on", "alternative_to", "applies_to", "mentions", "part_of"}


def test_paper_gains_an_optional_digest_of() -> None:
    paper = research_spec().page_type("paper")
    assert paper is not None
    digest_of = next(field for field in paper.frontmatter_fields if field.name == "digest_of")
    assert digest_of.required is False


@pytest.mark.parametrize("page_type", ["paper", "talk", "article", "post"])
def test_the_four_digest_kinds_carry_an_optional_singular_part_of(page_type: str) -> None:
    """A section page names its lead page, singular, so `part_of` is a `link`, never a `link-list`."""
    pt = research_spec().page_type(page_type)
    assert pt is not None
    part_of = next(field for field in pt.frontmatter_fields if field.name == "part_of")
    assert part_of.type == "link"
    assert part_of.required is False
    assert "part_of" in pt.edge_fields


def test_part_of_is_declared_as_a_relation() -> None:
    relation = next(r for r in research_spec().relations if r.slug == "part_of")
    assert relation.domain == "paper | talk | article | post"
    assert relation.range == "paper | talk | article | post"
    assert relation.symmetric is False


def test_flag_carries_about_kind_and_the_four_statuses() -> None:
    flag = research_spec().page_type("flag")
    assert flag is not None
    assert set(flag.edge_fields) == {"about", "resolved_by"}
    assert set(flag.status_values) == {"pending", "accepted", "dismissed", "completed"}
    assert "flag_kind" in {field.name for field in flag.frontmatter_fields}


def test_the_new_relations_are_declared() -> None:
    slugs = {relation.slug for relation in research_spec().relations}
    assert {"mentions", "about"} <= slugs


def test_flag_kind_choices_are_declared() -> None:
    flag = research_spec().page_type("flag")
    assert flag is not None
    flag_kind_field = next(field for field in flag.frontmatter_fields if field.name == "flag_kind")
    assert set(flag_kind_field.choices) == {"idea", "feature", "risk", "question"}


def test_flag_carries_the_decision_block() -> None:
    spec = research_spec()
    flag = spec.page_type("flag")
    assert flag is not None
    assert flag.decision is not None
    assert flag.decision.open == "pending"
    assert {outcome.status for outcome in flag.decision.outcomes} == {"accepted", "dismissed", "completed"}
    completed = next(outcome for outcome in flag.decision.outcomes if outcome.status == "completed")
    assert completed.link == "resolved_by"
    stance = spec.page_type("stance")
    assert stance is not None
    assert spec.decision_page_types == (flag, stance)


def test_flag_kind_outside_the_vocabulary_is_rejected() -> None:
    from engram.kb.contracts import parse_frontmatter
    from engram.kb.exceptions import FrontmatterError

    data: dict[str, object] = {
        "id": "flag.x",
        "type": "flag",
        "title": "A flag",
        "status": "pending",
        "flag_kind": "bogus",
        "about": ["[[paper.example]]"],
    }
    with pytest.raises(FrontmatterError, match="choices"):
        parse_frontmatter(data, research_spec())
