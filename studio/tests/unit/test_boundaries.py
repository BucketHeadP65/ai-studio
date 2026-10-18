"""Structural gate: `templates/` stays behind its one resolver.

`templates/` carries no Python (just each template's `knowledge-base-schema.json` and `_meta/*.md`), so the only
way a module reaches into it is through `engram.init.infrastructure.templates` (the resolver every template slug, reached through `engram.init.contracts`,
resolves through) or a bare path literal rooted at `templates`. Both are restricted to the
`studio init --template` surface: `cli_init.py` (the CLI command), `template_guidance.py` (the page copy both starts share), `http/controllers/templates.py`
(`GET /api/templates`, the create dialog's catalog) and `http/controllers/knowledge_bases.py` (the
create route, which starts a knowledge base from a named template). Everywhere else, a page type, status, or field comes
from the schema or the request, never a literal naming a template.
"""

import ast
import re

from pathlib import Path

_STUDIO_SRC = Path(__file__).resolve().parents[2] / "src" / "studio"

_TEMPLATES_RESOLVER = "engram.init.infrastructure.templates"

_TEMPLATES_NAMES = frozenset({"engram.init.contracts.Template", "engram.init.contracts.list_templates", "engram.init.contracts.load_template"})

_TEMPLATES_EXCEPTIONS = {
    _STUDIO_SRC / "knowledge_base" / "cli_init.py",
    _STUDIO_SRC / "knowledge_base" / "http" / "service.py",
    _STUDIO_SRC / "knowledge_base" / "http" / "controllers" / "templates.py",
    _STUDIO_SRC / "knowledge_base" / "template_guidance.py",
}

_TEMPLATES_PATH_LITERAL = re.compile(r"^templates(/|$)")


def _imported_modules(path: Path) -> set[str]:
    """Every absolute module `path`'s imports name, including the module a `from X import y` reaches through `y`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _template_path_literals(path: Path) -> list[str]:
    """Every string constant in `path` that names `templates` as its own root path segment."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _TEMPLATES_PATH_LITERAL.match(node.value)
    ]


def test_no_module_reaches_into_templates():
    assert _STUDIO_SRC.is_dir(), f"the app tree is not at {_STUDIO_SRC}, the walk below would pass vacuously"
    candidates = [path for path in sorted(_STUDIO_SRC.rglob("*.py")) if path not in _TEMPLATES_EXCEPTIONS]
    import_offenders = sorted(
        str(path.relative_to(_STUDIO_SRC))
        for path in candidates
        if any(m == _TEMPLATES_RESOLVER or m.startswith(f"{_TEMPLATES_RESOLVER}.") or m in _TEMPLATES_NAMES for m in _imported_modules(path))
    )
    assert import_offenders == [], f"only the studio init --template surface may import {_TEMPLATES_RESOLVER}: {import_offenders}"
    literal_offenders = {str(path.relative_to(_STUDIO_SRC)): _template_path_literals(path) for path in candidates}
    literal_offenders = {name: literals for name, literals in literal_offenders.items() if literals}
    assert literal_offenders == {}, f"a template path built from a bare literal, not through {_TEMPLATES_RESOLVER}"
