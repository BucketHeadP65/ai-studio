"""No test that composes this application reaches a real database: every registration that opens one is answered.

`IO_LEAVES` answers the container's I/O leaves for every route and lifespan test. A registration whose factory reaches a
database DSN and is missing from it would open the database the settings file names, which on a developer's machine is
the live one. The first check reads the factories' source, a class's constructor and the lifespan's own body, and opens
nothing, so it runs before anything is resolved. The second enters a lifespan over `IO_LEAVES`, resolves every
registration, and walks what each holds.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import types
import typing

from collections.abc import AsyncGenerator, AsyncIterator, Generator, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import asyncpg
import pytest

from core.db import build_engine, open_asyncpg_pool
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import harness.adapters.store as store_module

from harness.adapters.store import (
    STORE_DB,
    DatabaseBinding,
    knowledge_base_reader_dsn,
    reader_dsn,
    sessions_writer_dsn,
    store_admin_dsn,
    store_writer_dsn,
)
from harness.container import WIRING_MODULES
from harness.main import make_lifespan
from tests.support.container import IO_LEAVES, an_app

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

DSN_PRIMITIVES = frozenset(
    {
        *(
            name
            for name, value in vars(store_module).items()
            if inspect.isfunction(value) and value.__module__ == store_module.__name__ and name.endswith(("_dsn", "_pool", "_pool_terms", "_connection"))
        ),
        "build_engine",
        "open_asyncpg_pool",
        "create_async_engine",
        "create_pool",
    }
)
"""Every function of the store adapter that derives a database DSN or opens a pool on one, and the engine and pool builders
of core, SQLAlchemy and asyncpg. `connect` and `read_infrastructure` stay out: both name things that open nothing."""

LIVE_PORT = 5434
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_WALK_DEPTH = 5
_TRAIL_DEPTH = 8


def _referenced(function: Callable[..., object]) -> list[tuple[str, object]]:
    """The names a function's source mentions, with what each is bound to in its module, module attributes included."""
    try:
        source = textwrap.dedent(inspect.getsource(function))
    except (OSError, TypeError):
        return []
    bound = getattr(function, "__globals__", {})
    found: list[tuple[str, object]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id in bound:
            found.append((node.id, bound[node.id]))
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and isinstance(bound.get(node.value.id), types.ModuleType):
            target = getattr(bound[node.value.id], node.attr, None)
            if target is not None:
                found.append((node.attr, target))
    return found


def _path_to_a_database(function: Callable[..., object], seen: set[int], trail: tuple[str, ...] = ()) -> tuple[str, ...] | None:
    """The chain of names from `function` to a DSN primitive, or `None` when its code never reaches one. A class is read from its constructor."""
    start = function.__init__ if inspect.isclass(function) else function
    if id(start) in seen or len(trail) > _TRAIL_DEPTH:
        return None
    seen.add(id(start))
    for name, target in _referenced(start):
        if name in DSN_PRIMITIVES:
            return (*trail, name)
        module = getattr(target, "__module__", "") or ""
        if not module.startswith(("harness", "core", "engram")):
            continue
        callee = target.__init__ if inspect.isclass(target) else target
        if inspect.isfunction(callee) and (path := _path_to_a_database(callee, seen, (*trail, name))) is not None:
            return path
    return None


def _registered_type(factory: Callable[..., object]) -> object:
    """The type a factory registers: its return type, or what it yields."""
    if inspect.isclass(factory):
        return factory
    returned = typing.get_type_hints(factory)["return"]
    if typing.get_origin(returned) in {AsyncIterator, AsyncGenerator, Iterator, Generator}:
        return typing.get_args(returned)[0]
    return returned


def _registrations() -> list[tuple[object, Callable[..., object]]]:
    return [(_registered_type(factory), factory) for module in WIRING_MODULES for factory in module.REGISTRATIONS]


def _unanswered(
    registrations: list[tuple[object, Callable[..., object]]], *, answered: frozenset[object], lifespan: Callable[..., object] | None
) -> list[str]:
    """Every registration that reaches a database and is not answered, and the lifespan when its own body opens one."""
    found = [
        f"{getattr(registered, '__name__', registered)} ({factory.__name__} reaches {' > '.join(path)})"
        for registered, factory in registrations
        if registered not in answered and (path := _path_to_a_database(factory, set())) is not None
    ]
    if lifespan is not None and (path := _path_to_a_database(lifespan, set())) is not None:
        found.append(f"the lifespan ({lifespan.__name__} reaches {' > '.join(path)})")
    return found


def _unanswered_database_registrations() -> list[str]:
    return _unanswered(_registrations(), answered=frozenset(registered for registered, _ in IO_LEAVES), lifespan=make_lifespan)


def test_every_registration_that_reaches_a_database_is_answered_by_the_test_leaves() -> None:
    assert _unanswered_database_registrations() == []


# Shapes the scan must flag. None of them is ever called, so nothing is opened.

_PROBE_URL = "postgresql://nobody@db.example/none"


class _OpensAStorePoolWhenBuilt:
    """A class registration whose constructor derives the store's DSN."""

    def __init__(self) -> None:
        self.dsn = store_writer_dsn()


def _builds_a_core_engine() -> AsyncEngine:
    return build_engine(_PROBE_URL)


def _builds_a_sqlalchemy_engine() -> AsyncEngine:
    return create_async_engine(_PROBE_URL)


async def _opens_a_core_pool() -> asyncpg.Pool:
    return await open_asyncpg_pool(_PROBE_URL, target="nowhere")


async def _opens_an_asyncpg_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(_PROBE_URL)


def _lifespan_opening_an_engine(container: object) -> Callable[[object], AbstractAsyncContextManager[None]]:
    """Shaped like `make_lifespan`: the database is opened inside the lifespan it returns."""
    del container

    @asynccontextmanager
    async def lifespan(app: object) -> AsyncGenerator[None]:
        del app
        engine = build_engine(_PROBE_URL)
        yield
        await engine.dispose()

    return lifespan


def test_a_class_registration_is_scanned_from_its_constructor() -> None:
    assert _unanswered([(_OpensAStorePoolWhenBuilt, _OpensAStorePoolWhenBuilt)], answered=frozenset(), lifespan=None) == [
        "_OpensAStorePoolWhenBuilt (_OpensAStorePoolWhenBuilt reaches store_writer_dsn)"
    ]


@pytest.mark.parametrize(
    ("factory", "primitive"),
    [
        (_builds_a_core_engine, "build_engine"),
        (_builds_a_sqlalchemy_engine, "create_async_engine"),
        (_opens_a_core_pool, "open_asyncpg_pool"),
        (_opens_an_asyncpg_pool, "create_pool"),
    ],
)
def test_a_database_opened_outside_the_store_adapter_is_found(factory: Callable[..., object], primitive: str) -> None:
    path = _path_to_a_database(factory, set())

    assert path is not None
    assert path[-1] == primitive


def test_a_database_the_lifespan_opens_in_its_own_body_is_found() -> None:
    assert _unanswered([], answered=frozenset(), lifespan=_lifespan_opening_an_engine) == [
        "the lifespan (_lifespan_opening_an_engine reaches build_engine)"
    ]


def test_the_scan_starts_from_the_lifespan_the_application_enters() -> None:
    assert _unanswered([], answered=frozenset(), lifespan=make_lifespan) == []


@dataclass(frozen=True)
class _Target:
    """Where a DSN points, the password left out."""

    host: str
    port: int | None
    database: str
    user: str


def _target(text: str) -> _Target | None:
    """Where a DSN string points, or `None` when the text is not a Postgres URL."""
    parts = urlsplit(text)
    if not parts.scheme.startswith("postgres"):
        return None
    return _Target(host=parts.hostname or "", port=parts.port, database=parts.path.lstrip("/"), user=parts.username or "")


def _live_targets() -> frozenset[_Target]:
    """Where this checkout's own settings point every database it opens, the DSNs read and never printed."""
    dsns = (store_writer_dsn(), store_admin_dsn(), sessions_writer_dsn(), knowledge_base_reader_dsn(), reader_dsn(STORE_DB))
    return frozenset(target for dsn in dsns if (target := _target(dsn)) is not None)


def _is_live(target: _Target, live: frozenset[_Target]) -> bool:
    return target in live or (target.host in LOCAL_HOSTS and target.port == LIVE_PORT)


def _what_is_live(value: object, live: frozenset[_Target], seen: set[int], depth: int = 0) -> str | None:
    """What in `value` reaches a database: a live DSN, an engine on one, or any real pool or connection."""
    if depth > _WALK_DEPTH or id(value) in seen or isinstance(value, (types.ModuleType, type, types.FunctionType, int, float, bool, bytes)):
        return None
    seen.add(id(value))
    if isinstance(value, str):
        target = _target(value)
        return "a live DSN" if target is not None and _is_live(target, live) else None
    if isinstance(value, (asyncpg.Pool, asyncpg.Connection)):
        return f"a real {type(value).__name__}"
    if isinstance(value, AsyncEngine):
        value = value.url
    if isinstance(value, URL):
        target = _Target(host=value.host or "", port=value.port, database=value.database or "", user=value.username or "")
        return "an engine on a live database" if _is_live(target, live) else None
    children: list[object] = []
    if isinstance(value, dict):
        children = list(value.values())
    elif isinstance(value, (list, tuple, set, frozenset)):
        children = list(value)
    else:
        children = list(getattr(value, "__dict__", {}).values())
        children += [getattr(value, slot) for slot in getattr(type(value), "__slots__", ()) if isinstance(slot, str) and hasattr(value, slot)]
    for child in children:
        if (found := _what_is_live(child, live, seen, depth + 1)) is not None:
            return found
    return None


def test_the_walk_finds_a_live_dsn_and_a_real_pool_but_not_the_test_infrastructure() -> None:
    live = frozenset({_Target(host="db.internal", port=5432, database="engram_workflows", user="writer")})

    @dataclass
    class Holder:
        dsn: str

    assert _what_is_live(Holder("postgresql://someone:secret@localhost:5434/anything"), live, set()) == "a live DSN"
    assert _what_is_live(Holder("postgresql+asyncpg://writer:other@db.internal:5432/engram_workflows"), live, set()) == "a live DSN"
    assert _what_is_live(Holder("postgresql+asyncpg://owner:pw@db.example:5432/kb"), live, set()) is None
    assert _what_is_live({"nested": [Holder("postgres://a@127.0.0.1:5434/b")]}, live, set()) == "a live DSN"


async def _holdings(state: Path, leaves: dict[type, object]) -> list[str]:
    """Every registration that holds a live database once a lifespan over `leaves` has started, by type name only."""
    live = _live_targets()
    app = an_app(state=state)
    container = app.state.wireup_container
    holding: list[str] = []
    with container.override(leaves):
        async with app.router.lifespan_context(app):
            for registered, _ in _registrations():
                resolved = await container.get(registered)
                if (found := _what_is_live(resolved, live, set())) is not None:
                    holding.append(f"{getattr(registered, '__name__', registered)} holds {found}")
    return holding


async def test_a_lifespan_over_the_test_leaves_holds_no_live_database(tmp_path: Path) -> None:
    assert _unanswered_database_registrations() == [], "a registration that reaches a database is unanswered, so nothing is resolved"

    assert await _holdings(tmp_path, dict(IO_LEAVES)) == []


async def test_a_leaf_that_holds_a_live_dsn_is_named(tmp_path: Path) -> None:
    assert _unanswered_database_registrations() == [], "a registration that reaches a database is unanswered, so nothing is resolved"
    leaking = DatabaseBinding(description=store_writer_dsn(), named=True)

    holding = await _holdings(tmp_path, {**dict(IO_LEAVES), DatabaseBinding: leaking})

    assert "DatabaseBinding holds a live DSN" in holding
