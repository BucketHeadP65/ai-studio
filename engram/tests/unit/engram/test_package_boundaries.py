"""Structural gates on the library package's import surface.

Four ratchets — three walked over ``src/engram``, one over this test tier:

* the app spine stays out — no module under the library may import the CLI entry points, the composition root
  (``container`` / ``resolver`` / ``settings``), ``wireup``, the ``studio`` application package, or the retired
  ``features`` tree, so the library stays importable without wireup or the application settings tree;
* the evals-to-retrieval bridge stays one-directional — ``engram.evals.shared.serve_bridge`` is the anti-corruption
  seam evals reaches retrieval through, so retrieval itself must never import it. Retrieval may use other
  ``engram.evals`` modules; the bridge specifically would close the funnel into a cycle.
* ``templates/`` stays behind its one resolver: no module under the library reaches into a template directly, either
  by importing ``engram.init.infrastructure.templates`` (the resolver module itself), by importing its names from
  ``engram.init.contracts``, or by constructing a path rooted at ``templates`` as a bare literal, except the call
  sites that legitimately resolve a template: the resolver module itself, ``engram.init.contracts`` (which exports
  it), ``engram.testing.specs`` (the fixture module that reads the research template for parity tests),
  and ``engram.kb.domain.schema`` (``KnowledgeBaseSchemaMissingError``'s message names the known templates, so refusing a schema has to
  list them). ``templates/`` carries no Python, so the only way code reaches into it is a literal path segment. The
  walk only flags a string constant that names ``templates`` as its own root (``"templates"`` exactly, or
  ``"templates/..."``), never prose that merely mentions the word.
* the library test tier stays out too — no file under ``tests/unit/engram`` may import ``studio``, so the tier
  exercises the library through library surfaces alone. Resource shapes come from ``engram.config``; a spec whose
  subject is the app belongs in the studio project, under its ``tests/unit/knowledge_base``.

The bridge gate sees both import spellings: relative forms (the idiom inside ``retrieval/``) are resolved against the
importing file's own package before the comparison, so ``from ..evals.shared import serve_bridge`` is caught exactly
as its absolute twin is.
"""

import ast
import re

from pathlib import Path

_LIBRARY_SRC = Path(__file__).resolve().parents[3] / "src" / "engram"

_RETRIEVAL_SRC = _LIBRARY_SRC / "retrieval"

_LIBRARY_TESTS = Path(__file__).resolve().parent

_SERVE_BRIDGE = "engram.evals.shared.serve_bridge"

_TEMPLATES_RESOLVER = "engram.init.infrastructure.templates"

_TEMPLATES_NAMES = frozenset({"engram.init.contracts.Template", "engram.init.contracts.list_templates", "engram.init.contracts.load_template"})

_BUNDLES_EXCEPTIONS = {
    _LIBRARY_SRC / "init" / "infrastructure" / "templates.py",
    _LIBRARY_SRC / "init" / "contracts.py",
    _LIBRARY_SRC / "testing" / "specs.py",
    _LIBRARY_SRC / "kb" / "domain" / "schema.py",
}

_TEMPLATES_PATH_LITERAL = re.compile(r"^templates(/|$)")

_FORBIDDEN_ROOTS = {
    "cli",
    "cli_evals",
    "cli_init",
    "cli_note",
    "cli_support",
    "container",
    "features",
    "resolver",
    "settings",
    "studio",
    "wireup",
}


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _package_of(path: Path) -> tuple[str, ...]:
    """The dotted package a module's relative imports resolve against — its parent package, `__init__.py` included."""
    return tuple(path.relative_to(_LIBRARY_SRC.parent).with_suffix("").parts)[:-1]


def _absolute_target(package: tuple[str, ...], node: ast.ImportFrom) -> str | None:
    """The absolute module a `from ...` clause names, resolving a relative form against `package`.

    Returns `None` when the clause names nothing resolvable inside the tree — a bare absolute `from . import`
    with no module, or a relative level that climbs past the top-level package.
    """
    if node.level == 0:
        return node.module
    kept = len(package) - (node.level - 1)
    if kept <= 0:
        return None
    tail = node.module.split(".") if node.module else []
    return ".".join([*package[:kept], *tail])


def _modules_named(source: str, package: tuple[str, ...]) -> set[str]:
    """Every absolute module `source`'s imports name, including the module a `from X import y` reaches through `y`.

    `package` is the dotted package the source belongs to, against which its relative imports are resolved.
    """
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            target = _absolute_target(package, node)
            if target is None:
                continue
            modules.add(target)
            modules.update(f"{target}.{alias.name}" for alias in node.names)
    return modules


def _imported_modules(path: Path) -> set[str]:
    """Every absolute module `path`'s imports name, with its relative imports resolved against its own package."""
    return _modules_named(path.read_text(encoding="utf-8"), _package_of(path))


def test_library_never_imports_the_app_spine():
    assert _LIBRARY_SRC.is_dir(), f"the library tree is not at {_LIBRARY_SRC} — the walk below would pass vacuously"
    offenders = {
        str(path.relative_to(_LIBRARY_SRC)): sorted(_imported_roots(path) & _FORBIDDEN_ROOTS)
        for path in sorted(_LIBRARY_SRC.rglob("*.py"))
        if _imported_roots(path) & _FORBIDDEN_ROOTS
    }
    assert offenders == {}


def test_runtime_slices_never_import_the_testing_toolkit():
    """`engram.testing` serves consumers' test suites, never the runtime library.

    A runtime slice importing it would drag test tooling into production imports and invert the
    dependency direction.
    """
    assert _LIBRARY_SRC.is_dir(), f"the library tree is not at {_LIBRARY_SRC} — the walk below would pass vacuously"
    offenders = [
        str(path.relative_to(_LIBRARY_SRC))
        for path in sorted(_LIBRARY_SRC.rglob("*.py"))
        if "testing" not in path.relative_to(_LIBRARY_SRC).parts
        and any(m == "engram.testing" or m.startswith("engram.testing.") for m in _imported_modules(path))
    ]
    assert offenders == []


def _templates_path_literals(path: Path) -> list[str]:
    """Every string constant in `path` that names `templates` as its own root path segment."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _TEMPLATES_PATH_LITERAL.match(node.value)
    ]


def test_no_module_reaches_into_templates():
    assert _LIBRARY_SRC.is_dir(), f"the library tree is not at {_LIBRARY_SRC} — the walk below would pass vacuously"
    candidates = [path for path in sorted(_LIBRARY_SRC.rglob("*.py")) if path not in _BUNDLES_EXCEPTIONS]
    import_offenders = sorted(
        str(path.relative_to(_LIBRARY_SRC))
        for path in candidates
        if any(m == _TEMPLATES_RESOLVER or m.startswith(f"{_TEMPLATES_RESOLVER}.") or m in _TEMPLATES_NAMES for m in _imported_modules(path))
    )
    assert import_offenders == [], f"only {_TEMPLATES_RESOLVER}'s own exceptions may import it: {import_offenders}"
    literal_offenders = {str(path.relative_to(_LIBRARY_SRC)): _templates_path_literals(path) for path in candidates}
    literal_offenders = {name: literals for name, literals in literal_offenders.items() if literals}
    assert literal_offenders == {}, f"a template path built from a bare literal, not through {_TEMPLATES_RESOLVER}"


def test_retrieval_never_imports_the_evals_serve_bridge():
    assert _RETRIEVAL_SRC.is_dir(), f"the retrieval tree is not at {_RETRIEVAL_SRC} — the walk below would pass vacuously"
    offenders = sorted(
        str(path.relative_to(_LIBRARY_SRC)) for path in sorted(_RETRIEVAL_SRC.rglob("*.py")) if _SERVE_BRIDGE in _imported_modules(path)
    )
    assert offenders == [], f"{_SERVE_BRIDGE} is the evals-to-retrieval seam; retrieval must not import it back: {offenders}"


def test_the_library_test_tier_never_imports_the_app():
    assert _LIBRARY_TESTS.is_dir(), f"the library test tier is not at {_LIBRARY_TESTS} — the walk below would pass vacuously"
    offenders = sorted(
        str(path.relative_to(_LIBRARY_TESTS)) for path in sorted(_LIBRARY_TESTS.rglob("*.py")) if "studio" in _imported_roots(path)
    )
    assert offenders == [], f"these library-tier specs import the app; take the shapes from engram.config or move the spec: {offenders}"


def test_the_bridge_gate_sees_relative_imports():
    """Negative control: the walk above is green because retrieval is clean, not because a spelling is invisible to it."""
    package = _package_of(_RETRIEVAL_SRC / "queries.py")
    assert _SERVE_BRIDGE in _modules_named("from ..evals.shared import serve_bridge", package)
    assert _SERVE_BRIDGE in _modules_named("from ..evals.shared.serve_bridge import ServeBridge", package)
    assert _SERVE_BRIDGE in _modules_named("from engram.evals.shared import serve_bridge", package)
    assert _SERVE_BRIDGE not in _modules_named("from .queries import SearchHandler", package)
