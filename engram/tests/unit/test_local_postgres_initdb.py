"""The local Postgres's first boot: every initdb hook runs against the database the compose file creates."""

import re

from pathlib import Path

import yaml

_ENGRAM = Path(__file__).resolve().parents[2]
_COMPOSE = _ENGRAM / "docker-compose.yml"
_INITDB = _ENGRAM / "docker" / "postgres" / "initdb"

# A psql meta-command that switches database: `\connect name` or `\c name`.
_CONNECT = re.compile(r"^\s*\\(?:connect|c)\s+(\S+)")


def _compose_database() -> str:
    compose = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    return compose["services"]["postgres"]["environment"]["POSTGRES_DB"]


def test_the_initdb_hooks_exist() -> None:
    assert sorted(_INITDB.glob("*.sql")), f"no initdb hook under {_INITDB}"


def test_no_initdb_hook_connects_to_a_database_the_compose_file_does_not_create() -> None:
    database = _compose_database()
    for hook in sorted(_INITDB.glob("*.sql")):
        for number, line in enumerate(hook.read_text(encoding="utf-8").splitlines(), start=1):
            match = _CONNECT.match(line)
            assert match is None or match.group(1) == database, (
                f"{hook.name}:{number} connects to {match.group(1) if match else ''}, but the compose file creates {database}"
            )
