"""The rules the wiring holds, over every module the composition root composes."""

from __future__ import annotations

import ast
import re

from typing import TYPE_CHECKING

import pytest

from core.ai.history.contracts import PoolDatabase, PostgresStepStore, RequestParametersSink

from harness import settings as _settings
from harness.adapters.store import RecordDatabase, SessionsDatabase
from harness.adapters.wiring import RecordPoolOpener
from harness.container import WIRING_MODULES, create_container
from harness.domain.ports import ConversationHistory, SessionTranscript
from harness.features.authoring.internal_contracts import Chat
from harness.features.authoring.service import ChatService
from harness.main import CONTROLLERS
from harness.settings import HarnessSettings
from tests.support.container import answering_from_memory, rooted_at
from tests.support.fakes.pools import FakePool, FakeRecordDatabase, FakeRecordPool, FakeSessionsDatabase

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

SRC = _settings.ROOT

CONTAINER_NAMES = frozenset(
    {"AsyncContainer", "create_async_container", "get_app_container", "get_request_container", "inject_from_container"}
)
COMPOSITION_ROOTS = frozenset({"container.py", "main.py", "acp.py", "cli.py"})
WIRING_FILES = frozenset({"adapters/wiring.py"}) | {
    f"features/{slice_name}/wiring.py"
    for slice_name in ("authoring", "browsing", "inspection", "projects", "running", "scheduling", "selfauthoring", "sessions", "terminal")
}


def parsed_modules() -> dict[str, ast.Module]:
    """Every module under `src/harness`, keyed by its path relative to the package."""
    return {
        path.relative_to(SRC).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(SRC.rglob("*.py"))
        if "vendor" not in path.parts
    }


def wireup_imports() -> set[tuple[str, str]]:
    """Every name imported from `wireup` anywhere under the package, as a `(file, name)` pair."""
    found: set[tuple[str, str]] = set()
    for relative, module in parsed_modules().items():
        for node in ast.walk(module):
            if isinstance(node, ast.ImportFrom) and node.module is not None and node.module.split(".")[0] == "wireup":
                found |= {(relative, alias.name) for alias in node.names}
            if isinstance(node, ast.Import):
                found |= {(relative, alias.name) for alias in node.names if alias.name.split(".")[0] == "wireup"}
    return found


class TestWireupStaysAtItsSeams:
    def test_only_a_composition_root_names_a_container_type(self) -> None:
        offenders = sorted(
            f"{where}:{name}" for where, name in wireup_imports() if name in CONTAINER_NAMES and where not in COMPOSITION_ROOTS
        )

        assert offenders == []

    def test_wireup_is_imported_only_where_it_may_be(self) -> None:
        allowed = (
            COMPOSITION_ROOTS
            | WIRING_FILES
            | {
                f"features/{name}/controller.py"
                for name in (
                    "authoring",
                    "browsing",
                    "inspection",
                    "projects",
                    "running",
                    "scheduling",
                    "selfauthoring",
                    "sessions",
                    "terminal",
                )
            }
        )
        offenders = sorted({where for where, _ in wireup_imports()} - allowed)

        assert offenders == []

    def test_no_registration_declares_a_lifetime(self) -> None:
        passed = [
            f"{relative}:{node.lineno}"
            for relative, module in parsed_modules().items()
            for node in ast.walk(module)
            if isinstance(node, ast.Call) and any(keyword.arg == "lifetime" for keyword in node.keywords)
        ]

        assert passed == []

    def test_the_tree_was_actually_read(self) -> None:
        assert len(parsed_modules()) > 80


CACHED_BY_DESIGN: frozenset[tuple[str, str]] = frozenset(
    {
        # The shared settings file's database section, read once for a boot-time binding: which database each
        # caller connects to, under which role. Not this app's settings, and not a section of them.
        ("adapters/store.py", "_database")
    }
)
"""Every process-wide cached reader that stays, pinned exactly, so a new one cannot appear quietly."""


CACHING = frozenset({"cache", "lru_cache"})
"""The two decorators that keep one answer for the life of the process."""


def caches_forever(decorator: ast.expr) -> bool:
    """Whether this decorator caches its function process-wide, however it was spelled.

    All four spellings count: the bare name (`@cache`), the qualified one (`@functools.cache`), and
    either of those called (`@lru_cache()`, `@functools.lru_cache(maxsize=None)`). A gate that reads
    only the bare name is defeated by changing an import, which is not the property it is here for.
    """
    named = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(named, ast.Name):
        return named.id in CACHING
    return isinstance(named, ast.Attribute) and named.attr in CACHING


def cached_readers() -> set[tuple[str, str]]:
    """Every function under the package cached for the whole process, as a `(file, name)` pair.

    A cached function that takes an argument is a memo on what it was handed, and answers a fresh
    value for a fresh argument. One that takes none answers the same value for the life of the
    process, whatever changed underneath it -- which is what a settings reader must not be.
    `async def` counts the same as `def`: awaiting a cached coroutine function reads the same
    cached answer.
    """
    return {
        (relative, node.name)
        for relative, module in parsed_modules().items()
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and not node.args.args
        and not node.args.posonlyargs
        and not node.args.kwonlyargs
        and any(caches_forever(decorator) for decorator in node.decorator_list)
    }


class TestNothingReadsSettingsOncePerProcess:
    """A deployment's own settings are resolved from the container, never cached behind a call.

    A cached reader answers whatever the process started with, so an override a test sets, or a
    section a composition root hands in, never reaches it.
    """

    def test_no_cached_reader_appears_anywhere_under_the_package(self) -> None:
        offenders = sorted(f"{where}:{name}" for where, name in cached_readers() - CACHED_BY_DESIGN)

        assert offenders == []

    def test_every_pinned_reader_is_still_there(self) -> None:
        stale = sorted(f"{where}:{name}" for where, name in CACHED_BY_DESIGN - cached_readers())

        assert stale == [], "a pinned cached reader is gone -- remove it from CACHED_BY_DESIGN"


CONTROLLER_FILES = {controller.__name__: f"features/{controller.__module__.split('.')[2]}/controller.py" for controller in CONTROLLERS}
"""Where each mounted controller's class is written down, derived from the class itself."""


def constructor_fields(klass: ast.ClassDef) -> set[str]:
    """Every `self._x` the constructor assigns, which is every collaborator this controller was handed."""
    initialiser = next((node for node in klass.body if isinstance(node, ast.FunctionDef) and node.name == "__init__"), None)
    if initialiser is None:
        return set()
    return {
        target.attr
        for node in ast.walk(initialiser)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self"
    }


def fields_read_outside_the_constructor(klass: ast.ClassDef) -> set[str]:
    """Every `self._x` the class reads anywhere but its own constructor."""
    return {
        node.attr
        for member in klass.body
        if not (isinstance(member, ast.FunctionDef) and member.name == "__init__")
        for node in ast.walk(member)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and isinstance(node.ctx, ast.Load)
    }


def controller_class(name: str) -> ast.ClassDef:
    """The class definition `name` names, read out of the module the class itself points at."""
    module = parsed_modules()[CONTROLLER_FILES[name]]
    found = next(node for node in ast.walk(module) if isinstance(node, ast.ClassDef) and node.name == name)
    return found


class TestNoControllerHoldsACollaboratorNoRouteReads:
    """A controller takes what its routes use, and nothing else.

    A collaborator stored and never read is a dependency the container must build for no reason,
    and a reader of this class cannot tell it is dead. Caught here rather than in a review.
    """

    @pytest.mark.parametrize("name", sorted(CONTROLLER_FILES), ids=sorted(CONTROLLER_FILES))
    def test_every_field_it_stores_is_read(self, name: str) -> None:
        klass = controller_class(name)
        dead = sorted(constructor_fields(klass) - fields_read_outside_the_constructor(klass))

        assert dead == [], f"{name} stores {dead} and no route reads them"

    def test_every_mounted_controller_was_found(self) -> None:
        assert sorted(CONTROLLER_FILES) == sorted(controller.__name__ for controller in CONTROLLERS)


class TestEveryWiringModuleIsListed:
    @pytest.mark.parametrize("module", WIRING_MODULES, ids=lambda module: module.__name__)
    def test_it_exports_a_registration_list(self, module: ModuleType) -> None:
        assert isinstance(module.REGISTRATIONS, list)

    def test_the_container_builds(self) -> None:
        assert create_container(HarnessSettings()) is not None

    async def test_the_chat_port_answers_the_service(self, tmp_path: Path) -> None:
        """The one port a chat route declares resolves over the leaves a test answers with, so the wiring is whole.

        Building it is the check: the service is composed from the sessions this process holds, the
        record's own read, and the refresh a settled turn narrates through, and a registration
        missing under any of those three fails here rather than on the first request.
        """
        container = create_container(rooted_at(HarnessSettings(), tmp_path))
        with answering_from_memory(container):
            chat = await container.get(Chat)

        assert isinstance(chat, ChatService)
        await container.close()


POINTS_AT_A_SCHEMA = re.compile(r"set\s+search_path", re.IGNORECASE)
"""The statement that moves a connection to another schema, in any case and across line breaks."""

FRAGMENT_JOINT = re.compile(r"[\"']\s*[\"']")
"""Where two adjacent string literals meet. Python joins them into one statement, so the gate does too."""


def points_a_connection_at_a_schema(text: str) -> bool:
    """Whether this text runs a `SET search_path`, however the statement was spelled.

    Case, line breaks and adjacent string literals are folded away first. A setting name composed
    at runtime (`f"SET {setting} TO ..."`) is beyond a text gate and stays out of scope.
    """
    return POINTS_AT_A_SCHEMA.search(FRAGMENT_JOINT.sub("", text)) is not None


class TestTheTranscriptIsTheRecord:
    """Every session's transcript is the record over the sessions pool, and nothing else."""

    async def test_the_transcript_port_answers_the_record(self, tmp_path: Path) -> None:
        """One store, over the record's own pool, under the port every run is written through."""
        container = create_container(rooted_at(HarnessSettings(), tmp_path))
        with container.override({RecordDatabase: FakeRecordDatabase()}):
            store = await container.get(SessionTranscript)

        assert isinstance(store, PostgresStepStore)
        assert isinstance(store.database, PoolDatabase)
        await container.close()

    async def test_the_history_port_answers_the_same_record(self, tmp_path: Path) -> None:
        """The read a turn starts from is the record itself, so it cannot drift from what the runs are written to."""
        container = create_container(rooted_at(HarnessSettings(), tmp_path))
        with container.override({RecordDatabase: FakeRecordDatabase()}):
            history = await container.get(ConversationHistory)
            store = await container.get(SessionTranscript)

        assert isinstance(history, PostgresStepStore)
        assert isinstance(store, PostgresStepStore)
        read, written = history.database, store.database
        assert isinstance(read, PoolDatabase)
        assert isinstance(written, PoolDatabase)
        assert read.pool is written.pool, "one pool behind both ports"
        await container.close()

    async def test_the_request_parameters_sink_answers_the_same_record(self, tmp_path: Path) -> None:
        """What a request was sent with is written to the same store its run is, over the same pool."""
        container = create_container(rooted_at(HarnessSettings(), tmp_path))
        with container.override({RecordDatabase: FakeRecordDatabase()}):
            parameters = await container.get(RequestParametersSink)
            store = await container.get(SessionTranscript)

        assert isinstance(parameters, PostgresStepStore)
        assert isinstance(store, PostgresStepStore)
        read, written = parameters.database, store.database
        assert isinstance(read, PoolDatabase)
        assert isinstance(written, PoolDatabase)
        assert read.pool is written.pool, "one pool behind both ports"
        await container.close()

    def test_the_adapters_build_no_file_store(self) -> None:
        """The file store left the adapters with its factory: the wiring does not name it."""
        source = (SRC / "adapters" / "wiring.py").read_text(encoding="utf-8")

        assert "FileStepStore" not in source

    async def test_resolving_the_record_creates_its_tables(self, tmp_path: Path) -> None:
        """The factory provisions the record, so a root that resolves the store alone still finds its tables."""
        sessions = FakePool()
        record = FakePool()
        opener = FakeRecordPool(record)
        container = create_container(rooted_at(HarnessSettings(), tmp_path))
        with container.override({SessionsDatabase: FakeSessionsDatabase(sessions), RecordPoolOpener: opener}):
            await container.get(RecordDatabase)

        assert opener.asked_over == [sessions]
        assert any("CREATE TABLE IF NOT EXISTS runs" in statement for statement, _ in record.statements)
        await container.close()

    def test_only_the_wiring_opens_the_records_pool(self) -> None:
        """Every root reaches the record through the container, so no other module opens its pool."""
        offenders = sorted(
            relative
            for relative in parsed_modules()
            if relative != "adapters/wiring.py" and "open_record_pool" in (SRC / relative).read_text(encoding="utf-8")
        )

        assert offenders == []

    def test_no_module_sets_a_search_path_on_a_borrowed_connection(self) -> None:
        """A record connection is born in the record's schema: no module moves one there afterwards.

        A search path set on a borrowed connection lasts as long as the borrowing, so a pool two
        owners share cannot be divided that way. The record's pool declares its schema at connect
        time instead, which no release can undo.
        """
        offenders = sorted(
            relative for relative in parsed_modules() if points_a_connection_at_a_schema((SRC / relative).read_text("utf-8"))
        )

        assert offenders == []


POINTED_AT_A_SCHEMA: tuple[tuple[str, str], ...] = (
    ("plain", 'await connection.execute("SET search_path TO history")'),
    ("lowercase", 'await connection.execute("set search_path to history")'),
    ("split across lines", 'SQL = """\nSET\n  search_path TO history\n"""'),
    ("two literals", 'await connection.execute("SET search" "_path TO history")'),
)
"""Every spelling of the statement the gate above must catch, one per shape."""


class TestTheSearchPathGateCatchesEverySpelling:
    """The shapes a raw substring test would miss, each pinned so the gate cannot regress to one."""

    @pytest.mark.parametrize(("shape", "text"), POINTED_AT_A_SCHEMA, ids=[shape for shape, _ in POINTED_AT_A_SCHEMA])
    def test_it_is_caught(self, shape: str, text: str) -> None:
        assert points_a_connection_at_a_schema(text), f"the {shape} spelling slipped past the gate"

    def test_a_setting_named_at_runtime_is_out_of_reach(self) -> None:
        """Named because it is the gate's edge, not an oversight: no text gate can resolve a variable."""
        assert not points_a_connection_at_a_schema('await connection.execute(f"SET {setting} TO history")')
