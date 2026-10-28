"""Running this app against a database other than the one it was written on.

Both fixes here have the same shape: a value that was true of one deployment was written as a
literal, so a second deployment could not exist. `store_connection` demanded TLS of every database
it opened, which a local Postgres does not offer; `reader_dsn` and the two `*_env` builders read
engram's settings file and nothing else, so pointing this app somewhere else meant editing a file
that belongs to another work stream -- and, for a visitor cloning the repo, a file holding
somebody else's credentials that they must never touch at all.

The override is one full DSN per database rather than host/port/user parts, because a DSN is what
every consumer here already takes, and a part-wise override is four chances to half-replace the
file and disagree with it.

The translation tests are the subtle half. One override string is read by two different clients:
`asyncpg` directly, in this process, and engram's SQLAlchemy engine in a subprocess. libpq spells
the transport `sslmode=disable`, and SQLAlchemy's asyncpg dialect forwards an unrecognised query
key straight into `asyncpg.connect()` -- which has no `sslmode` argument. So an override handed to
engram verbatim dies on a `TypeError` naming a keyword the visitor never wrote.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import asyncpg
import pytest

from core.settings import DatabaseSettings

import harness.adapters.store

from harness.adapters.knowledge_base import knowledge_base_env, knowledge_base_stdio_transport
from harness.adapters.store import (
    KNOWLEDGE_BASE_DB,
    KNOWLEDGE_BASE_READER_ENV,
    STORE_DB,
    STORE_READER_ENV,
    knowledge_base_reader_dsn,
    reader_dsn,
    ssl_terms,
    store_admin_dsn,
    store_connection,
    store_writer_dsn,
    store_writer_pool_terms,
)
from harness.settings import STUDIO_REPO

LOCAL_STORE = "postgresql://knowledge_bases:knowledge_bases@localhost:5434/engram_workflows?sslmode=disable"
LOCAL_KNOWLEDGE_BASE = "postgresql://knowledge_bases:knowledge_bases@localhost:5434/knowledge_bases?sslmode=disable"

FILE_SETTINGS = DatabaseSettings(
    url="postgresql+asyncpg://admin:secret@cloud.example:5432/knowledge_bases?ssl=require",
    reader_url="postgresql+asyncpg://reader:secret@cloud.example:5432/knowledge_bases?ssl=require",
)


@pytest.fixture(autouse=True)
def _no_ambient_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither override leaks in from the shell that started pytest.

    Autouse because the whole point of these is that they are read from the environment: a
    developer who exported one to point a shell elsewhere would otherwise watch the file-fallback tests fail
    for a reason that has nothing to do with the code.

    The settings stand-in is here for the same reason. The fallback path reads the OWNER's real
    `application_settings.json`, so asserting against it would be asserting against one laptop's
    cloud host -- and on a machine without that file the fallback tests would not run at all.
    """
    for name in (KNOWLEDGE_BASE_READER_ENV, STORE_READER_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(harness.adapters.store, "_database", lambda: FILE_SETTINGS)


def test_the_store_dsn_comes_from_the_environment_when_it_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full DSN in `APP_STORE_READER_URL` wins over the settings file, verbatim."""
    monkeypatch.setenv(STORE_READER_ENV, LOCAL_STORE)

    assert reader_dsn(STORE_DB) == LOCAL_STORE


def test_the_knowledge_base_dsn_comes_from_the_environment_when_it_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same for the knowledge base, under its own name: the two databases are overridden independently."""
    monkeypatch.setenv(KNOWLEDGE_BASE_READER_ENV, LOCAL_KNOWLEDGE_BASE)

    assert reader_dsn(KNOWLEDGE_BASE_DB) == LOCAL_KNOWLEDGE_BASE


def test_an_override_for_one_database_does_not_answer_for_the_other() -> None:
    """The store's override must not be handed to a knowledge base reader, and the reverse."""
    assert reader_dsn(STORE_DB) != reader_dsn(KNOWLEDGE_BASE_DB)


def test_without_an_override_the_dsn_still_comes_from_engrams_settings() -> None:
    """The owner's path is unchanged: the file's `reader_url`, retargeted at the named database."""
    dsn = reader_dsn(STORE_DB)

    assert dsn == f"postgresql://reader:secret@cloud.example:5432/{STORE_DB}"


def test_the_admin_dsn_targets_the_knowledge_base_database_off_the_files_own_url() -> None:
    """`store_admin_dsn` retargets the file's own (writer) URL at the knowledge base, not the reader URL."""
    assert store_admin_dsn() == f"postgresql+asyncpg://admin:secret@cloud.example:5432/{KNOWLEDGE_BASE_DB}?ssl=require"


def test_the_admin_dsn_differs_from_the_reader_dsn() -> None:
    """The admin credential reaches `memory.*`, which the reader role the rest of this app uses cannot."""
    assert store_admin_dsn() != knowledge_base_reader_dsn()


def test_the_files_sslmode_survives_into_the_pool_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A settings file naming `sslmode=disable` boots against a local Postgres with no env override.

    Only `sslmode` travels: it is the one term `ssl_terms` reads. Every other query key is still
    dropped, because asyncpg forwards unknown DSN params to the server as run-time settings.
    """
    local_file = DatabaseSettings(
        url="postgresql+asyncpg://knowledge_bases:knowledge_bases@localhost:5434/knowledge_bases?sslmode=disable",
        reader_url="postgresql+asyncpg://knowledge_bases:knowledge_bases@localhost:5434/knowledge_bases?sslmode=disable",
    )
    monkeypatch.setattr(harness.adapters.store, "_database", lambda: local_file)

    dsn = reader_dsn(STORE_DB)

    assert dsn == f"postgresql://knowledge_bases:knowledge_bases@localhost:5434/{STORE_DB}?sslmode=disable"
    assert ssl_terms(dsn) is False


def test_an_empty_override_is_no_override() -> None:
    """`APP_STORE_READER_URL=` in a sourced env file means "unset", not "connect to nothing"."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(STORE_READER_ENV, "")
        assert reader_dsn(STORE_DB).endswith(f"/{STORE_DB}")


def test_a_dsn_naming_no_sslmode_still_requires_tls() -> None:
    """The cloud default survives the fix: absent terms mean `require`, exactly as before."""
    assert ssl_terms("postgresql://reader@cloud.example:5432/knowledge_bases") == "require"


def test_sslmode_disable_turns_tls_off() -> None:
    """The whole point: a local Postgres offers no TLS, and `require` against one is fatal."""
    assert ssl_terms(LOCAL_STORE) is False


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full", "prefer", "allow"])
def test_every_other_sslmode_is_passed_through_verbatim(mode: str) -> None:
    """Anything but `disable` is asyncpg's own vocabulary, so it travels unedited."""
    assert ssl_terms(f"postgresql://reader@cloud.example:5432/knowledge_bases?sslmode={mode}") == mode


async def test_the_pool_is_opened_on_the_dsns_own_ssl_terms(monkeypatch: pytest.MonkeyPatch) -> None:
    """`store_connection` reads the terms off the DSN rather than demanding TLS of every database.

    The literal `ssl="require"` this replaces is why the app could not open a local store at all:
    the argument overrode anything the DSN said, so no DSN could turn it off.
    """
    recorded: dict[str, object] = {}
    sentinel = object()

    async def fake_create_pool(dsn: str, **kwargs: object) -> object:
        recorded["dsn"] = dsn
        recorded.update(kwargs)
        return sentinel

    monkeypatch.setenv(STORE_READER_ENV, LOCAL_STORE)
    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)

    pool = await store_connection()

    assert pool is sentinel
    assert recorded["dsn"] == LOCAL_STORE
    assert recorded["ssl"] is False


async def test_the_pool_still_requires_tls_against_the_owners_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    """No override, no `sslmode`: the cloud store is opened exactly as it always was."""
    recorded: dict[str, object] = {}

    async def fake_create_pool(dsn: str, **kwargs: object) -> object:
        recorded["dsn"] = dsn
        recorded.update(kwargs)
        return object()

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)

    await store_connection()

    assert recorded["ssl"] == "require"


def test_the_knowledge_base_subprocess_inherits_the_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """`studio mcp` is pointed at the visitor's knowledge base, on both handles, read-only as ever."""
    monkeypatch.setenv(KNOWLEDGE_BASE_READER_ENV, LOCAL_KNOWLEDGE_BASE)

    env = knowledge_base_env()

    assert env["APP_INFRASTRUCTURE__DATABASE__URL"] == env["APP_INFRASTRUCTURE__DATABASE__READER_URL"]
    assert urlsplit(env["APP_INFRASTRUCTURE__DATABASE__URL"]).path == f"/{KNOWLEDGE_BASE_DB}"


def test_the_knowledge_base_subprocess_runs_the_sibling_knowledge_base_cli() -> None:
    """The cross-process plugin spawns `studio mcp` from the sibling knowledge base checkout, naming the knowledge base it serves.

    The knowledge base CLI lives in the `studio` project beside this one, so the transport must run
    from that checkout (where `uv run studio` resolves) -- a transport still naming this
    checkout fails at spawn time, in a subprocess, where the error is a closed stdio pipe
    rather than an import that names what is missing.
    """
    transport = knowledge_base_stdio_transport(knowledge_base=Path("/knowledge-bases/served"))

    assert transport.command == "uv"
    assert transport.args == ["run", "studio", "mcp", "--knowledge-base", "/knowledge-bases/served"]
    assert "engram" not in transport.args
    assert transport.cwd == str(STUDIO_REPO)


def test_the_writer_dsn_inherits_the_store_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The save path writes into the visitor's store, not the owner's cloud one.

    The write path matters as much as the read one: an override that reached only the reader
    would send a visitor's authored page to a database they cannot even see.
    """
    monkeypatch.setenv(STORE_READER_ENV, LOCAL_STORE)

    assert urlsplit(store_writer_dsn()).hostname == "localhost"


def test_the_subprocess_dsn_names_the_driver_engram_connects_with(monkeypatch: pytest.MonkeyPatch) -> None:
    """The subprocess builds a SQLAlchemy engine, which needs the driver named in the scheme.

    A bare `postgresql://` there selects psycopg2 -- a synchronous driver engram does not install
    -- so the override has to arrive spelled the way the settings file spells it.
    """
    monkeypatch.setenv(KNOWLEDGE_BASE_READER_ENV, LOCAL_KNOWLEDGE_BASE)

    env = knowledge_base_env()

    assert env["APP_INFRASTRUCTURE__DATABASE__URL"].startswith("postgresql+asyncpg://")
    assert env["APP_INFRASTRUCTURE__DATABASE__READER_URL"].startswith("postgresql+asyncpg://")


def test_the_writer_dsn_names_the_driver_engram_connects_with(monkeypatch: pytest.MonkeyPatch) -> None:
    """The write path builds a SQLAlchemy engine too, so the override arrives respelled the same way."""
    monkeypatch.setenv(STORE_READER_ENV, LOCAL_STORE)

    dsn = store_writer_dsn()

    assert dsn.startswith("postgresql+asyncpg://")
    assert "sslmode" not in parse_qs(urlsplit(dsn).query)


def test_sslmode_never_reaches_engram(monkeypatch: pytest.MonkeyPatch) -> None:
    """SQLAlchemy's asyncpg dialect forwards an unknown query key into `asyncpg.connect()`.

    `connect()` has no `sslmode` argument, so a `disable` that travelled verbatim would kill the
    engine on a `TypeError` naming a keyword the visitor never typed. Its asyncpg spelling is
    the absence of the argument, so the key comes off.
    """
    monkeypatch.setenv(KNOWLEDGE_BASE_READER_ENV, LOCAL_KNOWLEDGE_BASE)

    env = knowledge_base_env()

    for slot in ("APP_INFRASTRUCTURE__DATABASE__URL", "APP_INFRASTRUCTURE__DATABASE__READER_URL"):
        assert "sslmode" not in parse_qs(urlsplit(env[slot]).query)


def test_a_required_transport_survives_the_translation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dropping `sslmode` must not mean dropping TLS: `require` becomes the `ssl` word asyncpg takes."""
    monkeypatch.setenv(STORE_READER_ENV, "postgresql://reader@managed.example:5432/engram_workflows?sslmode=require")

    assert parse_qs(urlsplit(store_writer_dsn()).query)["ssl"] == ["require"]


def test_without_an_override_the_subprocess_env_is_what_it_always_was() -> None:
    """The owner's cloud path is untouched by all of this -- same host, same `ssl=require`."""
    env = knowledge_base_env()

    assert urlsplit(env["APP_INFRASTRUCTURE__DATABASE__URL"]).hostname == "cloud.example"
    assert parse_qs(urlsplit(env["APP_INFRASTRUCTURE__DATABASE__URL"]).query)["ssl"] == ["require"]


def test_without_an_override_the_writer_keeps_the_owners_writer_role() -> None:
    """Only the override collapses the two roles; the file's writer handle answers the write path."""
    dsn = store_writer_dsn()

    assert urlsplit(dsn).username == "admin"
    assert urlsplit(dsn).path == f"/{STORE_DB}"


def test_the_schedules_pool_terms_keep_tls_when_the_dsn_says_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cloud DSN with no `sslmode` still requires TLS through the raw-asyncpg pool terms.

    `store_writer_dsn`'s SQLAlchemy respelling collapses "absent" and "disable" into one, so the
    pool terms must read the pre-translation URL -- absence downgraded to plaintext is the exact
    failure this test pins shut.
    """
    monkeypatch.setenv(STORE_READER_ENV, "postgresql://owner:secret@cloud.example.com:5432/engram_workflows")

    dsn, ssl = store_writer_pool_terms()

    assert dsn == "postgresql://owner:secret@cloud.example.com:5432/engram_workflows"
    assert ssl == "require"


def test_the_schedules_pool_terms_turn_tls_off_only_when_told_to(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit `sslmode=disable` is the one spelling that turns TLS off, and the query is shed."""
    monkeypatch.setenv(STORE_READER_ENV, "postgresql+asyncpg://knowledge_bases:knowledge_bases@localhost:5434/engram_workflows?sslmode=disable")

    dsn, ssl = store_writer_pool_terms()

    assert dsn == "postgresql://knowledge_bases:knowledge_bases@localhost:5434/engram_workflows"
    assert ssl is False
