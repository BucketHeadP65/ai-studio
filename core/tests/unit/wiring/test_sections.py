import pytest
import wireup

from pydantic import BaseModel

from core.wiring.exceptions import SectionOptionalError, SectionsAmbiguousError
from core.wiring.sections import section, section_type_of, sections_of


class Database(BaseModel):
    """A section nested inside another."""

    url: str


class Resources(BaseModel):
    """A section holding a section."""

    database: Database


class Retrieval(BaseModel):
    """A section of the root."""

    hybrid: bool = True


class Settings(BaseModel):
    """A settings root: two nested sections, a list of models, a mapping of models and a scalar."""

    resources: Resources
    retrieval: Retrieval
    entries: list[Retrieval]
    named: dict[str, Retrieval]
    retries: int


class OptionalSection(BaseModel):
    """A settings root whose section may be missing."""

    retrieval: Retrieval | None = None


class AmbiguousSection(BaseModel):
    """A settings root whose field names two models."""

    either: Retrieval | Database


def settings() -> Settings:
    return Settings(
        resources=Resources(database=Database(url="postgresql://nowhere/none")),
        retrieval=Retrieval(),
        entries=[Retrieval()],
        named={"one": Retrieval()},
        retries=3,
    )


class TestSectionTypeOf:
    def test_a_model_is_its_own_section(self):
        assert section_type_of(Retrieval) is Retrieval

    def test_a_scalar_is_not_a_section(self):
        assert section_type_of(int) is None

    def test_a_list_of_models_is_not_a_section(self):
        assert section_type_of(list[Retrieval]) is None

    def test_a_mapping_of_models_is_not_a_section(self):
        assert section_type_of(dict[str, Retrieval]) is None

    def test_an_optional_model_still_names_one_model(self):
        assert section_type_of(Retrieval | None) is Retrieval

    def test_a_union_of_two_models_names_no_one_type(self):
        assert section_type_of(Retrieval | Database) is None


class TestSectionsOf:
    async def test_every_section_resolves_under_its_own_type(self):
        built = settings()

        container = wireup.create_async_container(injectables=sections_of(built))

        assert await container.get(Resources) is built.resources
        assert await container.get(Database) is built.resources.database
        assert await container.get(Retrieval) is built.retrieval

    def test_a_scalar_a_list_and_a_mapping_are_not_registered(self):
        assert len(sections_of(settings())) == 3

    def test_an_optional_section_is_refused_by_name(self):
        with pytest.raises(SectionOptionalError, match="retrieval"):
            sections_of(OptionalSection())

    def test_a_field_naming_two_models_is_refused_by_name(self):
        with pytest.raises(SectionsAmbiguousError, match="either"):
            sections_of(AmbiguousSection(either=Retrieval()))


class TestSection:
    async def test_one_value_registered_under_the_type_a_consumer_declares(self):
        retrieval = Retrieval(hybrid=False)

        container = wireup.create_async_container(injectables=[section(retrieval, as_type=Retrieval)])

        assert await container.get(Retrieval) is retrieval
