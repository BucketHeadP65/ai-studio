r"""Structural gates over the DI composition.

wireup accepts a registration whose key nothing can ever resolve (a type alias, a stale
provider), and a behavioural suite stays green over it. These gates assert the *structure*
of the composition directly:

* **every registration resolves** — the real `create_container` graph is built from fully
  explicit settings and every factory's DI key is resolved, with the IO leaves overridden:
  the two engines (their connectors await the pgvector codec registration, the composition's
  one deliberate resolve-time connection) by lazy engines over the same URLs, and the reader
  gateway + embedder by inert fakes. Nothing touches Postgres or Azure, and a silent-dead
  registration cannot survive the sweep;
* **every DI key is a runtime class** — a factory whose return annotation is not a real
  `type` (e.g. a PEP 695 alias) registers fine and resolves never; refused here;
* **the conventions hold mechanically** — every registration is an unqualified singleton with no
  declared lifetime, a class we own is registered under its port, and a factory function that
  provides a class we own says why it is not the class itself; wireup is imported only by the
  composition root and the `studio.knowledge_base.wiring` modules; and `os.environ` is written only
  at the documented fresh-knowledge_base bootstrap seam (`studio.knowledge_base.cli_init`).

The module list is derived from the filesystem (`src/studio/knowledge_base/wiring/*.py`), so a new slice's
wiring module is swept automatically, and one missing from `create_container` fails the sweep
when its keys do not resolve. The file-level gates walk all of `src/` — both halves of the app,
the DI-free agent included, so a stray registration outside the knowledge base half is caught too — and a
path they report is relative to that root (`studio/knowledge_base/container.py`,
`studio/knowledge_base/wiring/serve.py`, …).
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import re

from collections.abc import AsyncIterator, Iterator
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, get_args, get_origin, get_type_hints

from core.ai.embedding import BatchEmbedder
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.gateway import FakeSqlGateway
from engram.testing.specs import research_spec
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from wireup._annotations import InjectableDeclaration

from studio.knowledge_base import container as container_module
from studio.knowledge_base.container import create_container
from studio.knowledge_base.settings import (
    ApplicationSettings,
    AzureOpenAiSettings,
    CoverageSettings,
    DatabaseSettings,
    DifficultyThresholdsSettings,
    DocumentIntelligenceSettings,
    EvalsSettings,
    GatewaySettings,
    GenerationSettings,
    InfrastructureSettings,
    IngestionSettings,
    KnowledgeBasePathOrigin,
    KnowledgeBaseSettings,
    LogfireSettings,
    RecommendationSettings,
    RetrievalSettings,
    SettingsFile,
    SourceSettings,
    TuningSettings,
)
from studio.knowledge_base.wiring.contracts import ReaderEngine, ReaderSqlGateway

if TYPE_CHECKING:
    from types import ModuleType

_SRC = Path(__file__).parents[3] / "src"

# The one file allowed to write `os.environ`: the fresh-knowledge_base bootstrap seam.
_BOOTSTRAP_SEAM = _SRC / "studio" / "knowledge_base" / "cli_init.py"

# Every key `create_container` seeds: the fifteen sections `sections_of` derives from the settings
# tree, plus the two facts that are not settings fields. A removed one fails here loudly.
_SETTINGS_KEYS: tuple[type, ...] = (
    KnowledgeBasePathOrigin,
    SettingsFile,
    RetrievalSettings,
    IngestionSettings,
    SourceSettings,
    EvalsSettings,
    GenerationSettings,
    DifficultyThresholdsSettings,
    CoverageSettings,
    TuningSettings,
    RecommendationSettings,
    InfrastructureSettings,
    DatabaseSettings,
    AzureOpenAiSettings,
    DocumentIntelligenceSettings,
    LogfireSettings,
    GatewaySettings,
)

_SETTINGS_JSON: dict[str, object] = {
    "knowledge_bases": {},
    "retrieval": {"dialect": "pg_textsearch", "graph_backend": "age"},
    "ingestion": {"embedding_deployment": "text-embedding-3-large", "embedding_dimensions": 1024, "embedding_batch_size": 64},
    "sources": {},
    "evals": {"autogen": "queue", "datasets_dir": "evals/datasets", "rca_queries_path": "evals/rca/queries.sql"},
    "infrastructure": {
        "database": {
            "url": "postgresql+asyncpg://knowledge_bases:secret@localhost:5432/knowledge_bases",
            "reader_url": "postgresql+asyncpg://kb_reader:secret@localhost:5432/knowledge_bases",
        },
        "azure_openai": {"endpoint": "https://example.openai.azure.com"},
        "document_intelligence": {"endpoint": ""},
        # A dummy key: `ask_models_provider` builds its gateway-backed models at provide time
        # (clients stay lazy, no network), but refuses to build without a key at all.
        "gateway": {"api_key": "test-gateway-key", "base_url": "https://gateway-eu.pydantic.dev/proxy"},
        "harness": {"base_url": "http://127.0.0.1:8770", "timeout_seconds": 10.0},
        "logfire": {"write_token": "", "read_token": "", "base_url": "https://logfire-eu.pydantic.dev"},
    },
}


def _knowledge_base_root(tmp_path: Path) -> Path:
    """A research knowledge base on disk, the shape `KnowledgeBaseSettings` accepts."""
    knowledge_base_root = tmp_path / "knowledge_base"
    for required in ("Units", "_meta"):
        (knowledge_base_root / required).mkdir(parents=True)
    (knowledge_base_root / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
    return knowledge_base_root


def _settings(tmp_path: Path) -> ApplicationSettings:
    """A settings tree carrying every section, read from a file under `tmp_path` — no env, no network."""
    json_file = tmp_path / "application_settings.json"
    json_file.write_text(json.dumps(_SETTINGS_JSON | {"sources": {"zotero": {"data_dir": str(tmp_path)}}}), encoding="utf-8")
    return ApplicationSettings(json_file=json_file)


def _wiring_modules() -> list[ModuleType]:
    """Every slice wiring module on disk, imported — the filesystem is the source of truth."""
    wiring = _SRC / "studio" / "knowledge_base" / "wiring"
    names = sorted(f"studio.knowledge_base.wiring.{path.stem}" for path in wiring.glob("*.py") if path.stem not in {"__init__", "contracts"})
    return [importlib.import_module(name) for name in names]


def _declaration(name: str, entry: object) -> InjectableDeclaration:
    """The registration wireup attached to `entry`.

    wireup writes `__wireup_registration__` onto whatever it registers but declares no type for the
    attribute, so this reads it and narrows on the dataclass wireup actually stores.

    Raises:
        TypeError: when `entry` carries none, which means it is listed but never registered.
    """
    found = getattr(entry, "__wireup_registration__", None)
    if not isinstance(found, InjectableDeclaration):
        msg = f"{name} is listed but carries no wireup registration"
        raise TypeError(msg)
    return found


def _registrations(module: ModuleType) -> list[tuple[str, object]]:
    """Every entry of a wiring module's `REGISTRATIONS`, named for the message a failure prints."""
    listed: list[object] = module.REGISTRATIONS
    return [(f"{module.__name__}.{getattr(entry, '__name__', entry)}", entry) for entry in listed]


def _root_registrations() -> list[tuple[str, object]]:
    """The composition root's own providers, which are declared rather than listed."""
    return [
        (f"{container_module.__name__}.{name}", member)
        for name, member in inspect.getmembers(container_module)
        if hasattr(member, "__wireup_registration__") and getattr(member, "__module__", None) == container_module.__name__
    ]


def _all_registrations() -> list[tuple[str, object]]:
    """Every registration across the slice wiring modules plus the composition root's own."""
    found: list[tuple[str, object]] = []
    for module in _wiring_modules():
        found.extend(_registrations(module))
    found.extend(_root_registrations())
    return found


def _di_key(name: str, entry: object) -> object:
    """The type a registration registers under: `as_type` when it names one, else the class or the return."""
    declaration = _declaration(name, entry)
    if declaration.as_type is not None:
        return declaration.as_type
    if isinstance(entry, type):
        return entry
    key = get_type_hints(entry)["return"]
    return get_args(key)[0] if get_origin(key) in {AsyncIterator, Iterator} else key


class TestEveryRegistrationIsResolvable:
    """The container states only truths: each registered key is a class, and each resolves."""

    def test_every_wiring_module_exports_its_registrations(self):
        for module in _wiring_modules():
            listed = getattr(module, "REGISTRATIONS", None)
            assert isinstance(listed, list), f"{module.__name__} exports no REGISTRATIONS list"
            assert listed, f"{module.__name__} exports an empty REGISTRATIONS list"

    def test_every_listed_registration_is_registered(self):
        for name, entry in _all_registrations():
            _declaration(name, entry)

    def test_every_di_key_is_a_runtime_class(self):
        for name, factory in _all_registrations():
            key = _di_key(name, factory)
            assert isinstance(key, type), f"{name} registers under {key!r}, which is not a runtime class — nothing can ever resolve it"

    async def test_the_real_graph_resolves_every_registration(self, tmp_path: Path):
        settings = _settings(tmp_path)
        container = create_container(settings, knowledge_base=KnowledgeBaseSettings(path=_knowledge_base_root(tmp_path)))
        # The isinstance filter is a no-op given the class-key gate above; it narrows for the checker.
        keys = {key for name, factory in _all_registrations() if isinstance(key := _di_key(name, factory), type)}

        # The engine providers await the pgvector codec registration — the composition's one
        # deliberate resolve-time connection — so the sweep swaps them for lazy engines over
        # the same URLs (engine construction never connects); everything downstream still
        # resolves for real and merely stores them.
        database = settings.infrastructure.database
        with (
            container.override.injectable(target=AsyncEngine, new=create_async_engine(database.url)),
            container.override.injectable(
                target=ReaderEngine, new=ReaderEngine(create_async_engine(database.reader_url, poolclass=NullPool))
            ),
            container.override.injectable(target=ReaderSqlGateway, new=FakeSqlGateway()),
            container.override.injectable(target=BatchEmbedder, new=FakeEmbedding()),
        ):
            for key in sorted(keys, key=lambda klass: klass.__name__):
                resolved = await container.get(key)
                assert resolved is not None, f"nothing resolved for registered key {key.__name__}"

    async def test_every_settings_instance_registration_resolves(self, tmp_path: Path):
        container = create_container(_settings(tmp_path))

        for key in _SETTINGS_KEYS:
            resolved = await container.get(key)
            assert isinstance(resolved, key)

    async def test_the_knowledge_base_resolves_when_the_container_was_given_one(self, tmp_path: Path):
        knowledge_base = KnowledgeBaseSettings(path=_knowledge_base_root(tmp_path))
        container = create_container(_settings(tmp_path), knowledge_base=knowledge_base)

        assert await container.get(KnowledgeBaseSettings) is knowledge_base


class FactoryReason(StrEnum):
    """Why a registration is a factory function rather than the class itself."""

    ASSEMBLED = "the object is built from values the container does not register"
    MANY_BY_QUALIFIER = "several instances of one type, told apart by a qualifier"


# Every factory function whose DI key is a class this package defines, with why a class registration
# cannot serve. A key the package does not define needs no entry: there is no class here to register.
_ASSEMBLED: dict[str, FactoryReason] = {
    "studio.knowledge_base.wiring.refresh.harness_refresh_forward_provider": FactoryReason.ASSEMBLED,
    "studio.knowledge_base.wiring.conversion.document_converters_provider": FactoryReason.ASSEMBLED,
    "studio.knowledge_base.wiring.sources.connectors_provider": FactoryReason.ASSEMBLED,
    "studio.knowledge_base.wiring.serve.reader_engine_provider": FactoryReason.ASSEMBLED,
    "studio.knowledge_base.wiring.serve.reader_gateway_provider": FactoryReason.ASSEMBLED,
    "studio.knowledge_base.wiring.serve.admin_gateway_provider": FactoryReason.ASSEMBLED,
    "studio.knowledge_base.wiring.serve.harness_artifact_resolver_provider": FactoryReason.ASSEMBLED,
    "studio.knowledge_base.wiring.serve.note_retention_provider": FactoryReason.ASSEMBLED,
    "studio.knowledge_base.wiring.serve.knowledge_base_registry_provider": FactoryReason.ASSEMBLED,
    "studio.knowledge_base.wiring.serve.search_reranking_provider": FactoryReason.ASSEMBLED,
    "studio.knowledge_base.wiring.evals.evaluation_sql_tools_provider": FactoryReason.ASSEMBLED,
    "studio.knowledge_base.container.qa_router_for_knowledge_base": FactoryReason.ASSEMBLED,
    "studio.knowledge_base.container.qa_router_for_registry": FactoryReason.ASSEMBLED,
}


class TestRegistrationConventions:
    """The registration shape, pinned mechanically."""

    def test_every_registration_is_an_unqualified_singleton(self):
        for name, entry in _all_registrations():
            declaration = _declaration(name, entry)
            assert declaration.lifetime == "singleton", f"{name} is not a singleton"
            assert declaration.qualifier is None, f"{name} carries a qualifier, disambiguate with a wrapper type"

    def test_a_class_registration_names_a_port_or_itself(self):
        for name, entry in _all_registrations():
            if not isinstance(entry, type):
                continue
            key = _di_key(name, entry)
            assert isinstance(key, type), f"{name} registers under {key!r}, which is not a runtime class"

    def test_a_class_registration_resolves_its_constructor_annotations(self):
        # A class registration carries no decorator, so ruff's runtime-evaluated-decorators rule does
        # not hold its __init__ annotations at module scope. Resolving them here catches the move.
        unresolved: list[str] = []
        for name, entry in _all_registrations():
            if not isinstance(entry, type):
                continue
            try:
                get_type_hints(entry.__init__)
            except NameError as exc:
                unresolved.append(f"{name}: {exc}")
        assert unresolved == [], f"a registered class names a type that is not importable at module scope: {unresolved}"

    def test_a_factory_of_a_type_we_own_says_why_it_is_not_a_class(self):
        for name, entry in _all_registrations():
            if isinstance(entry, type):
                continue
            key = _di_key(name, entry)
            if not (isinstance(key, type) and getattr(key, "__module__", "").startswith("studio.")):
                continue
            assert name in _ASSEMBLED, f"{name} provides {key.__name__}, a class this package owns: register the class, or say why not"

    def test_no_assembled_entry_is_stale(self):
        listed = {name for name, _ in _all_registrations()}
        assert set(_ASSEMBLED) <= listed, f"stale entries: {sorted(set(_ASSEMBLED) - listed)}"


class TestDiStaysAtItsSeams:
    """File-level gates: the DI machinery appears only where the conventions say it may."""

    @staticmethod
    def _sources() -> list[tuple[Path, str]]:
        return [(path, path.read_text(encoding="utf-8")) for path in sorted(_SRC.rglob("*.py"))]

    def test_wireup_is_imported_only_at_the_sanctioned_seams(self):
        allowed = re.compile(
            r"(^studio/knowledge_base/container\.py"
            r"|^studio/knowledge_base/http/api\.py"
            r"|^studio/knowledge_base/http/controllers/[^/]+\.py"
            r"|^studio/knowledge_base/wiring/[^/]+\.py)$"
        )
        offenders = [
            str(path.relative_to(_SRC))
            for path, text in self._sources()
            if ("import wireup" in text or "from wireup import" in text) and not allowed.match(str(path.relative_to(_SRC)))
        ]
        assert offenders == [], f"wireup imported outside the composition roots, the controllers and the wiring modules: {offenders}"

    def test_injectable_is_declared_only_in_wiring_modules_and_the_root(self):
        allowed = re.compile(r"(^studio/knowledge_base/container\.py|^studio/knowledge_base/wiring/[^/]+\.py)$")
        offenders = [
            str(path.relative_to(_SRC))
            for path, text in self._sources()
            if "@injectable" in text.replace("@wireup.injectable", "@injectable") and not allowed.match(str(path.relative_to(_SRC)))
        ]
        assert offenders == [], f"@injectable declared outside a wiring module / the composition root: {offenders}"

    def test_no_registration_declares_a_lifetime(self):
        # Singleton is wireup's default. A declared lifetime is either noise or a non-singleton.
        offenders = [str(path.relative_to(_SRC)) for path, text in self._sources() if "lifetime=" in text]
        assert offenders == [], f"a registration declares a lifetime, and singleton is the default: {offenders}"

    def test_environment_is_written_only_at_the_bootstrap_seam(self):
        write = re.compile(r"os\.environ\[[^\]]+\]\s*=")
        offenders = [str(path.relative_to(_SRC)) for path, text in self._sources() if write.search(text) and path != _BOOTSTRAP_SEAM]
        assert offenders == [], f"os.environ written outside the documented {_BOOTSTRAP_SEAM.relative_to(_SRC)} bootstrap: {offenders}"


class TestTheAppConsumesTheLibrarysPublicSurface:
    """The app reaches the library only through names the library publishes.

    An underscore-prefixed name is the library's own business: importing one couples the app to a spelling the
    library is free to change, and no type-checker flags the drift. When the app needs such a value, the library
    publishes it under a public name instead. `studio/knowledge_base/wiring/` is exempt — the composition root is where the
    app is allowed to reach for whatever a registration needs to bind.
    """

    def test_no_app_module_imports_a_private_name_from_the_library(self):
        app_src = _SRC / "studio" / "knowledge_base"
        assert app_src.is_dir(), f"the app tree is not at {app_src} — the walk below would pass vacuously"
        offenders: dict[str, list[str]] = {}
        for path in sorted(app_src.rglob("*.py")):
            if path.parent.name == "wiring":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            private = [
                f"{node.module}.{alias.name}"
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module
                and (node.module == "engram" or node.module.startswith("engram."))
                for alias in node.names
                if alias.name.startswith("_")
            ]
            if private:
                offenders[str(path.relative_to(_SRC))] = sorted(private)
        assert offenders == {}, f"the app imported library privates; publish them in the library instead: {offenders}"
