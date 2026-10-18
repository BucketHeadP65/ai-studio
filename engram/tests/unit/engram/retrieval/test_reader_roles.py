"""The private reader role's name, password and URL are derived from the collection and the secret alone."""

import re

import pytest

from sqlalchemy.engine import make_url

from engram.retrieval.infrastructure.reader_roles import private_reader_url
from engram.retrieval.reader_roles import private_reader_password, private_reader_role


def test_the_role_name_is_a_plain_identifier_the_same_for_the_same_collection():
    role = private_reader_role("client-vault")

    assert re.fullmatch(r"kb_reader_[0-9a-f]{20}", role)
    assert private_reader_role("client-vault") == role
    assert private_reader_role("other-vault") != role


def test_the_password_follows_the_secret():
    role = private_reader_role("client-vault")

    assert private_reader_password(role, "one") == private_reader_password(role, "one")
    assert private_reader_password(role, "one") != private_reader_password(role, "two")


def test_an_empty_secret_gives_no_password():
    with pytest.raises(ValueError, match="reader secret"):
        private_reader_password(private_reader_role("client-vault"), "")


def test_the_url_swaps_only_the_user_and_the_password():
    url = make_url(private_reader_url("postgresql+asyncpg://kb_reader:pw@db.example:5434/knowledge_bases?sslmode=require", "client-vault", "one"))

    assert url.username == private_reader_role("client-vault")
    assert url.password == private_reader_password(private_reader_role("client-vault"), "one")
    assert (url.drivername, url.host, url.port, url.database, dict(url.query)) == (
        "postgresql+asyncpg",
        "db.example",
        5434,
        "knowledge_bases",
        {"sslmode": "require"},
    )
