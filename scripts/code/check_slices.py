#!/usr/bin/env python3
"""Report imports that cross a converted slice's boundary, and database or network code where the standard forbids it.

Usage:
    python3 scripts/code/check_slices.py --src DIR [--src DIR ...] --slice NAME [--slice NAME ...] [--assembly NAME ...]
        [--infrastructure NAME ...]

`--src` is a source root that holds top-level packages, such as `harness/src`. `--slice` is the
dotted name of a slice already brought to the standard, such as `harness.features.scheduling`.
`--assembly` names a composition module, such as `harness.container`, that may also import a
slice's `wiring`, `controller` and `infrastructure` modules. `--infrastructure` names a converted
slice that is itself an application's infrastructure, such as `harness.adapters`, where database and
network imports are allowed in every module. Each finding prints as `path:line: message`. The exit
status is 1 when anything is found and 0 when nothing is.

Standard library only, so it runs from any Python 3.13 without a virtual environment.
"""

import argparse
import ast
import sys

from dataclasses import dataclass
from pathlib import Path

SKIPPED_FOLDERS = frozenset({"__pycache__", ".venv", "vendor"})
PUBLIC = frozenset({"contracts"})
ASSEMBLY_REACH = frozenset({"contracts", "wiring", "controller", "infrastructure"})
DATABASE_AND_NETWORK = ("sqlalchemy", "psycopg", "psycopg2", "asyncpg", "httpx", "requests", "aiohttp", "urllib.request")
DOMAIN_FORBIDDEN = (*DATABASE_AND_NETWORK, "alembic", "fastapi", "starlette", "wireup", "pydantic_ai")


@dataclass(frozen=True)
class Module:
    """One Python module under a source root, named the way it is imported."""

    name: str
    path: Path
    is_package: bool


@dataclass(frozen=True)
class ImportLine:
    """One import statement, with its target made absolute."""

    base: str
    names: tuple[str, ...]
    line: int


@dataclass(frozen=True)
class Finding:
    """One place in one file that breaks the standard."""

    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        """The finding as `path:line: message`.

        Returns:
            The printable line.
        """
        return f"{self.path}:{self.line}: {self.message}"


def modules_under(root: Path) -> list[Module]:
    """Every module under a source root, skipping the folders nobody writes by hand.

    Args:
        root: A source root such as `harness/src`.

    Returns:
        The modules, in path order.
    """
    modules = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if SKIPPED_FOLDERS & set(relative.parts):
            continue
        parts = list(relative.with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        if parts:
            modules.append(Module(".".join(parts), path, is_package))
    return modules


def absolute_base(node: ast.ImportFrom, module: Module) -> str:
    """The absolute module a `from ... import` statement reads from.

    Args:
        node: The import statement.
        module: The module the statement is in.

    Returns:
        The dotted module name, with a relative import resolved against `module`.
    """
    if node.level == 0:
        return node.module or ""
    package = module.name if module.is_package else module.name.rpartition(".")[0]
    parts = package.split(".") if package else []
    if node.level > 1:
        parts = parts[: len(parts) - (node.level - 1)]
    return ".".join([*parts, node.module] if node.module else parts)


def import_lines(module: Module) -> list[ImportLine]:
    """Every import in a module, wherever it sits, `TYPE_CHECKING` blocks and function bodies included.

    Args:
        module: The module to read.

    Returns:
        The imports, in the order they appear.
    """
    tree = ast.parse(module.path.read_text(encoding="utf-8"), filename=str(module.path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(ImportLine(alias.name, (), node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append(ImportLine(absolute_base(node, module), tuple(alias.name for alias in node.names), node.lineno))
    return sorted(found, key=lambda item: item.line)


def is_within(name: str, slice_name: str) -> bool:
    """Whether a dotted module name is the slice itself or sits under it.

    Args:
        name: A dotted module name.
        slice_name: The slice's dotted name.

    Returns:
        True for the slice and every module below it.
    """
    return name == slice_name or name.startswith(f"{slice_name}.")


def entered(statement: ImportLine, slice_name: str) -> list[str]:
    """The first module below the slice that an import reaches, one entry per target.

    Args:
        statement: The import.
        slice_name: The slice's dotted name.

    Returns:
        Each module name one level below the slice, or an empty string for the slice package itself.
    """
    if statement.base == slice_name:
        return list(statement.names) or [""]
    if statement.base.startswith(f"{slice_name}."):
        return [statement.base[len(slice_name) + 1 :].split(".")[0]]
    return []


def matches(candidates: list[str], prefixes: tuple[str, ...]) -> str | None:
    """The first candidate module that is one of `prefixes` or sits under one.

    Args:
        candidates: Dotted module names an import reaches.
        prefixes: Dotted module names to look for.

    Returns:
        The matching candidate, or None when nothing matches.
    """
    for candidate in candidates:
        for prefix in prefixes:
            if candidate == prefix or candidate.startswith(f"{prefix}."):
                return candidate
    return None


def init_findings(module: Module, slice_name: str) -> list[Finding]:
    """A finding when a slice's `__init__.py` holds anything but its docstring.

    Args:
        module: The slice package's own module.
        slice_name: The slice's dotted name.

    Returns:
        One finding, or none.
    """
    body = ast.parse(module.path.read_text(encoding="utf-8"), filename=str(module.path)).body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    if body:
        return [Finding(module.path, body[0].lineno, f"`{slice_name}/__init__.py` must hold only its docstring")]
    return []


def boundary_findings(module: Module, statements: list[ImportLine], slice_name: str, *, assembly: bool) -> list[Finding]:
    """The imports in a module outside the slice that reach past the slice's public modules.

    Args:
        module: A module outside the slice.
        statements: Its imports.
        slice_name: The slice's dotted name.
        assembly: Whether the module is a composition module, which may also reach the assembly modules.

    Returns:
        The findings.
    """
    allowed = ASSEMBLY_REACH if assembly else PUBLIC
    findings = []
    for statement in statements:
        for name in entered(statement, slice_name):
            if name not in allowed:
                target = f"{slice_name}.{name}" if name else slice_name
                findings.append(Finding(module.path, statement.line, f"imports `{target}`, only `{slice_name}.contracts` is public"))
    return findings


def inside_findings(module: Module, statements: list[ImportLine], slice_name: str, *, infrastructure: bool = False) -> list[Finding]:
    """The imports in a module inside the slice that bring database, network or framework code where it may not go.

    Args:
        module: A module inside the slice.
        statements: Its imports.
        slice_name: The slice's dotted name.
        infrastructure: Whether the whole slice is an application's infrastructure, so every module outside
            its `domain` may import database and network code.

    Returns:
        The findings.
    """
    below = module.name[len(slice_name) + 1 :] if module.name != slice_name else ""
    in_domain = below == "domain" or below.startswith("domain.")
    in_infrastructure = infrastructure or below == "infrastructure" or below.startswith("infrastructure.")
    findings = []
    for statement in statements:
        candidates = [statement.base, *(f"{statement.base}.{name}" for name in statement.names)]
        if in_domain and (hit := matches(candidates, DOMAIN_FORBIDDEN)) is not None:
            findings.append(Finding(module.path, statement.line, f"the domain imports `{hit}`"))
        elif not in_infrastructure and (hit := matches(candidates, DATABASE_AND_NETWORK)) is not None:
            findings.append(Finding(module.path, statement.line, f"imports `{hit}` outside `{slice_name}.infrastructure`"))
    return findings


def check(roots: list[Path], slices: list[str], assembly: set[str], infrastructure: set[str] | None = None) -> list[Finding]:
    """Every finding for the converted slices across the given source roots.

    Args:
        roots: The source roots to read.
        slices: The dotted names of the converted slices.
        assembly: The dotted names of the composition modules.
        infrastructure: The dotted names of the converted slices that are an application's infrastructure.
            None means there are none.

    Returns:
        The findings, by file and line.
    """
    infrastructure = infrastructure or set()
    modules = [module for root in roots for module in modules_under(root)]
    imports = {module.name: import_lines(module) for module in modules}
    findings = []
    for slice_name in slices:
        own = [module for module in modules if module.name == slice_name and module.is_package]
        if not own:
            findings.append(Finding(Path(slice_name), 1, f"no package `{slice_name}` under the source roots"))
            continue
        findings.extend(init_findings(own[0], slice_name))
        for module in modules:
            if is_within(module.name, slice_name):
                findings.extend(inside_findings(module, imports[module.name], slice_name, infrastructure=slice_name in infrastructure))
            else:
                findings.extend(boundary_findings(module, imports[module.name], slice_name, assembly=module.name in assembly))
    return sorted(findings, key=lambda finding: (str(finding.path), finding.line))


def main(argv: list[str] | None = None) -> int:
    """Check the converted slices named on the command line and print what breaks the standard.

    Args:
        argv: The command line arguments, or None to read them from the process.

    Returns:
        1 when anything was found, 0 when nothing was.
    """
    parser = argparse.ArgumentParser(description="Report imports that cross a converted slice's boundary.")
    parser.add_argument("--src", action="append", type=Path, required=True, help="a source root holding top-level packages")
    parser.add_argument("--slice", action="append", required=True, help="the dotted name of a converted slice")
    parser.add_argument("--assembly", action="append", default=[], help="the dotted name of a composition module")
    parser.add_argument(
        "--infrastructure",
        action="append",
        default=[],
        help="the dotted name of a converted slice that is itself an application's infrastructure, "
        "where database and network imports are allowed in every module",
    )
    arguments = parser.parse_args(argv)
    findings = check(arguments.src, arguments.slice, set(arguments.assembly), set(arguments.infrastructure))
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
