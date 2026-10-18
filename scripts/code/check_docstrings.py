#!/usr/bin/env python3
"""Report docstrings, comments and description arguments that break the code standard in the root CLAUDE.md.

Usage:
    python3 scripts/code/check_docstrings.py [--vocabulary] PATH [PATH ...]

A PATH is a Python file or a folder searched for Python files. Folders named `__pycache__`,
`.venv`, `migrations` and `vendor` are skipped. Each finding prints as `path:line: message`.
The exit status is 1 when anything is found and 0 when nothing is.

Standard library only, so it runs from any Python 3.13 without a virtual environment.
"""

import argparse
import ast
import io
import re
import sys
import tokenize

from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "docs"))

import validate_docs  # noqa: E402

SKIPPED_FOLDERS = frozenset({"__pycache__", ".venv", "migrations", "vendor"})
ANY_SECTION = re.compile(r"^\s*(Args|Arguments|Returns|Yields|Raises|Attributes|Examples?|Notes?|Warnings?|Todo):\s*$", re.MULTILINE)
ARGS_SECTION = re.compile(r"^\s*(Args|Arguments):\s*$", re.MULTILINE)
ARGUMENT_NAME = re.compile(r"^(\s+)(\*{0,2}\w+)((?: \([^)]*\))?:)", re.MULTILINE)
RETURNS_SECTION = re.compile(r"^\s*Returns:\s*$", re.MULTILINE)
YIELDS_SECTION = re.compile(r"^\s*Yields:\s*$", re.MULTILINE)
ENTRY_POINT_DECORATORS = frozenset({"command", "callback", "get", "post", "put", "patch", "delete", "head", "options", "websocket", "api_route"})
PROPERTY_DECORATORS = frozenset({"property", "cached_property", "setter", "getter", "deleter"})
TOOL_DECORATORS = frozenset({"tool", "tool_plain"})
"""The decorators that register a function as an agent tool."""
TOOL_REGISTRARS = frozenset({"add_function", "Tool"})
"""The calls whose first argument becomes an agent tool."""
ROUTE_REGISTRARS = frozenset({"add_api_route", "add_api_websocket_route", "add_route", "add_websocket_route"})
"""The calls whose second argument, or `endpoint`, becomes an HTTP route."""
CHECKED_DUNDERS = frozenset({"__call__"})
"""The double-underscore methods the check reads like any other method."""
DESCRIBING_KEYWORDS = frozenset({"description", "help"})
"""The keyword arguments whose string a person or a model reads as a description, such as `Field(description=...)` or `typer.Option(help=...)`."""
NO_VALUE = frozenset({"None", "NoReturn", "Never"})
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


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


def python_files(base: Path) -> list[Path]:
    """Every Python file at or under `base`, skipping the folders nobody writes by hand.

    Args:
        base: A Python file or a folder.

    Returns:
        The files, in path order.
    """
    if base.is_file():
        return [base] if base.suffix == ".py" else []
    return [path for path in sorted(base.rglob("*.py")) if not SKIPPED_FOLDERS & set(path.relative_to(base).parts)]


def is_dunder(name: str) -> bool:
    """Whether `name` is a double-underscore name such as `__init__`.

    Args:
        name: The function or method name.

    Returns:
        True for a name that starts and ends with two underscores.
    """
    return name.startswith("__") and name.endswith("__")


def is_private_name(name: str) -> bool:
    """Whether `name` is private: it starts with an underscore and is not a double-underscore name.

    Args:
        name: A function, class or module name.

    Returns:
        True for a private name.
    """
    return name.startswith("_") and not is_dunder(name)


def decorator_name(decorator: ast.expr) -> str:
    """The last dotted name of a decorator, whether or not it is called.

    Args:
        decorator: One entry of a definition's decorator list.

    Returns:
        The name, such as `property` for `@property` or `get` for `@router.get("")`.
    """
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Name):
        return target.id
    return ""


def is_entry_point(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether the function is a CLI command or an HTTP route, whose docstring is its help text.

    Args:
        node: The function definition.

    Returns:
        True when a called decorator such as `@app.command()` or `@router.get("")` registers it.
    """
    return any(
        isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr in ENTRY_POINT_DECORATORS
        for decorator in node.decorator_list
    )


def parameter_names(node: ast.FunctionDef | ast.AsyncFunctionDef, *, is_method: bool) -> list[str]:
    """The parameters a caller passes, leaving out a method's `self` or `cls`.

    Args:
        node: The function definition.
        is_method: Whether the function is defined directly in a class body.

    Returns:
        The parameter names, with `*` and `**` in front of the variadic ones.
    """
    arguments = node.args
    positional = [*arguments.posonlyargs, *arguments.args]
    if is_method and positional and positional[0].arg in {"self", "cls"}:
        positional = positional[1:]
    names = [argument.arg for argument in (*positional, *arguments.kwonlyargs)]
    if arguments.vararg is not None:
        names.append(f"*{arguments.vararg.arg}")
    if arguments.kwarg is not None:
        names.append(f"**{arguments.kwarg.arg}")
    return names


def returns_value(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether the function's return annotation promises a value.

    Args:
        node: The function definition.

    Returns:
        True unless the annotation is missing, `None`, `NoReturn` or `Never`.
    """
    returns = node.returns
    if returns is None:
        return False
    if isinstance(returns, ast.Constant):
        return returns.value is not None and str(returns.value) not in NO_VALUE
    if isinstance(returns, ast.Name):
        return returns.id not in NO_VALUE
    if isinstance(returns, ast.Attribute):
        return returns.attr not in NO_VALUE
    return True


def is_generator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether the function's own body yields, leaving out any function defined inside it.

    Args:
        node: The function definition.

    Returns:
        True when a `yield` or `yield from` belongs to this function.
    """
    stack = [child for child in ast.iter_child_nodes(node) if not isinstance(child, SCOPES)]
    while stack:
        child = stack.pop()
        if isinstance(child, (ast.Yield, ast.YieldFrom)):
            return True
        stack.extend(grandchild for grandchild in ast.iter_child_nodes(child) if not isinstance(grandchild, SCOPES))
    return False


def docstring_line(node: ast.AsyncFunctionDef | ast.FunctionDef | ast.ClassDef) -> int:
    """The line a definition's docstring starts on, or the definition's own line when it has none.

    Args:
        node: The definition.

    Returns:
        The line number.
    """
    if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
        return node.body[0].lineno
    return node.lineno


def is_one_line(docstring: str) -> bool:
    """Whether a cleaned docstring is a single line with no section.

    Args:
        docstring: The docstring as `ast.get_docstring` returns it.

    Returns:
        True for one line.
    """
    return len(docstring.splitlines()) <= 1 and not ANY_SECTION.search(docstring)


def _callable_name(node: ast.expr) -> str | None:
    """The function a reference names, `f` for both `f` and `self.f`."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def tool_names(tree: ast.Module) -> frozenset[str]:
    """The functions whose docstring is read by a model or shown as help, which a reader checks instead.

    A function counts when a `tool` or `tool_plain` decorator registers it, when it is the first argument
    of `add_function` or `Tool`, when it is listed in a `tools` keyword argument, when a route call such as
    `add_api_route` takes it, or when it is defined inside a function that returns it, as a tool or step
    factory does.

    Args:
        tree: The parsed module.

    Returns:
        The names of those functions, empty when the module has none.
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if isinstance(target, ast.Attribute) and target.attr in TOOL_DECORATORS:
                    names.add(node.name)
            nested = {child.name for child in node.body if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))}
            returned = {child.value.id for child in node.body if isinstance(child, ast.Return) and isinstance(child.value, ast.Name)}
            names.update(nested & returned)
        elif isinstance(node, ast.Call):
            called = _callable_name(node.func)
            if called in TOOL_REGISTRARS and node.args and (name := _callable_name(node.args[0])):
                names.add(name)
            if called in ROUTE_REGISTRARS:
                endpoint = next((keyword.value for keyword in node.keywords if keyword.arg == "endpoint"), None)
                endpoint = endpoint if endpoint is not None else (node.args[1] if len(node.args) > 1 else None)
                if endpoint is not None and (name := _callable_name(endpoint)):
                    names.add(name)
            for keyword in node.keywords:
                if keyword.arg == "tools" and isinstance(keyword.value, (ast.List, ast.Tuple)):
                    names.update(name for element in keyword.value.elts if (name := _callable_name(element)))
    return frozenset(names)


def imported_tool_names(tree: ast.Module) -> frozenset[str]:
    """The functions a module imports and then registers as agent tools, which live in another module.

    Args:
        tree: The parsed module.

    Returns:
        The imported names passed to `add_function` or `Tool`, or listed in a `tools` keyword argument.
    """
    imported = {alias.asname or alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
    registered = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _callable_name(node.func) in TOOL_REGISTRARS and node.args and isinstance(node.args[0], ast.Name):
            registered.add(node.args[0].id)
        for keyword in node.keywords:
            if keyword.arg == "tools" and isinstance(keyword.value, (ast.List, ast.Tuple)):
                registered.update(element.id for element in keyword.value.elts if isinstance(element, ast.Name))
    return frozenset(registered & imported)


def check_class(node: ast.ClassDef, path: Path, *, private: bool) -> list[Finding]:
    """The findings on one class's own docstring.

    Args:
        node: The class definition.
        path: The file it is in.
        private: Whether the class is private by its name or by where it is defined.

    Returns:
        The findings, empty when the docstring meets the standard.
    """
    docstring = ast.get_docstring(node)
    if private:
        if docstring is not None and not is_one_line(docstring):
            return [Finding(path, docstring_line(node), f"private `{node.name}` needs a one-line docstring or none")]
        return []
    if docstring is None:
        return [Finding(path, node.lineno, f"public `{node.name}` has no docstring")]
    return []


def check_function(node: ast.FunctionDef | ast.AsyncFunctionDef, path: Path, *, private: bool, is_method: bool) -> list[Finding]:
    """The findings on one function's or method's own docstring.

    Args:
        node: The function definition.
        path: The file it is in.
        private: Whether the function is private by its name or by where it is defined.
        is_method: Whether the function is defined directly in a class body.

    Returns:
        The findings, empty when the docstring meets the standard.
    """
    if node.name == "__init__" and is_method and not private:
        return constructor_findings(node, path)
    decorators = {decorator_name(decorator) for decorator in node.decorator_list}
    if (is_dunder(node.name) and node.name not in CHECKED_DUNDERS) or "overload" in decorators:
        return []
    docstring = ast.get_docstring(node)
    line = docstring_line(node)
    if private:
        if docstring is not None and not is_one_line(docstring):
            return [Finding(path, line, f"private `{node.name}` needs a one-line docstring or none")]
        return []
    if docstring is None:
        return [Finding(path, node.lineno, f"public `{node.name}` has no docstring")]
    if decorators & PROPERTY_DECORATORS or is_entry_point(node):
        return []
    findings = []
    parameters = parameter_names(node, is_method=is_method)
    if parameters and not ARGS_SECTION.search(docstring):
        findings.append(Finding(path, line, f"`{node.name}` takes {', '.join(parameters)} and has no Args section"))
    if is_generator(node):
        if not YIELDS_SECTION.search(docstring):
            findings.append(Finding(path, line, f"`{node.name}` is a generator and has no Yields section"))
    elif returns_value(node) and not RETURNS_SECTION.search(docstring):
        findings.append(Finding(path, line, f"`{node.name}` returns a value and has no Returns section"))
    return findings


def constructor_findings(node: ast.FunctionDef | ast.AsyncFunctionDef, path: Path) -> list[Finding]:
    """The findings on a public class's `__init__`, which names every parameter it takes in Args.

    Args:
        node: The `__init__` definition.
        path: The file it is in.

    Returns:
        The findings, empty when the constructor takes no parameters or documents each one.
    """
    parameters = parameter_names(node, is_method=True)
    if not parameters:
        return []
    docstring = ast.get_docstring(node)
    if docstring is None:
        return [Finding(path, node.lineno, f"public `__init__` takes {', '.join(parameters)} and has no docstring")]
    if not ARGS_SECTION.search(docstring):
        return [Finding(path, docstring_line(node), f"`__init__` takes {', '.join(parameters)} and has no Args section")]
    return []


def nested_bodies(statement: ast.stmt) -> list[list[ast.stmt]]:
    """The statement lists inside a compound statement, such as both branches of an `if`.

    Args:
        statement: Any statement.

    Returns:
        Each nested list of statements, empty for a simple statement.
    """
    bodies = [getattr(statement, field) for field in ("body", "orelse", "finalbody") if isinstance(getattr(statement, field, None), list)]
    bodies.extend(handler.body for handler in getattr(statement, "handlers", []))
    bodies.extend(case.body for case in getattr(statement, "cases", []))
    return bodies


def check_definitions(
    statements: list[ast.stmt], path: Path, *, private: bool, in_class: bool, tools: frozenset[str] = frozenset()
) -> list[Finding]:
    """The docstring findings on every class and function in `statements`, however deep.

    A function registered as an agent tool is left to the tool rule, which a reader checks.

    Args:
        statements: A module, class or compound statement body.
        path: The file it is in.
        private: Whether everything here is already private by where it is defined.
        in_class: Whether `statements` is a class body, so its functions are methods.
        tools: The names of the functions the module registers as agent tools.

    Returns:
        The findings, in the order the definitions appear.
    """
    findings = []
    for statement in statements:
        if isinstance(statement, ast.ClassDef):
            class_private = private or is_private_name(statement.name)
            findings.extend(check_class(statement, path, private=class_private))
            findings.extend(check_definitions(statement.body, path, private=class_private, in_class=True, tools=tools))
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function_private = private or is_private_name(statement.name)
            if statement.name not in tools:
                findings.extend(check_function(statement, path, private=function_private, is_method=in_class))
            findings.extend(check_definitions(statement.body, path, private=True, in_class=False, tools=tools))
        else:
            for body in nested_bodies(statement):
                findings.extend(check_definitions(body, path, private=private, in_class=in_class, tools=tools))
    return findings


def prose_findings(text: str, first_line: int, path: Path, *, vocabulary: bool) -> list[Finding]:
    """The banned symbols, dash clause breaks and, when asked, retired words in one piece of prose.

    Args:
        text: A docstring or a comment.
        first_line: The file line the text starts on.
        path: The file it is in.
        vocabulary: Whether to report this repository's retired words as well.

    Returns:
        The findings.
    """
    return [
        Finding(path, first_line + offset - 1, message)
        for offset, message in validate_docs.prose_findings(text, words=vocabulary, dashes=True)
    ]


def string_findings(tree: ast.Module, path: Path, *, vocabulary: bool) -> list[Finding]:
    """The prose findings in every docstring, attribute docstring and description argument of a module.

    Args:
        tree: The parsed module.
        path: The file it is in.
        vocabulary: Whether to report this repository's retired words as well.

    Returns:
        The findings.
    """
    findings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            findings.extend(prose_findings(mark_argument_names(node.value.value), node.lineno, path, vocabulary=vocabulary))
        elif isinstance(node, ast.keyword) and node.arg in DESCRIBING_KEYWORDS:
            for part in described_text(node.value):
                findings.extend(prose_findings(part.value, part.lineno, path, vocabulary=vocabulary))
    return findings


def described_text(value: ast.expr) -> list[ast.Constant]:
    """The literal string pieces of a description argument, including the fixed parts of an f-string.

    Args:
        value: The keyword argument's value.

    Returns:
        Each string constant in it, empty when the value is not written as a literal.
    """
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return [value]
    if isinstance(value, ast.JoinedStr):
        return [part for part in value.values if isinstance(part, ast.Constant) and isinstance(part.value, str)]
    return []


def mark_argument_names(docstring: str) -> str:
    """The docstring with each Args entry's parameter name written as a code span.

    A parameter name is an identifier, so the prose check never reads it as a word.

    Args:
        docstring: A docstring.

    Returns:
        The same text, line for line.
    """
    match = ARGS_SECTION.search(docstring)
    if match is None:
        return docstring
    section_end = ANY_SECTION.search(docstring, match.end())
    end = section_end.start() if section_end else len(docstring)
    entries = ARGUMENT_NAME.sub(r"\1`\2`\3", docstring[match.end() : end])
    return docstring[: match.end()] + entries + docstring[end:]


def comment_findings(source: str, path: Path, *, vocabulary: bool) -> list[Finding]:
    """The prose findings in every comment of a module.

    Args:
        source: The module's text.
        path: The file it is in.
        vocabulary: Whether to report this repository's retired words as well.

    Returns:
        The findings.
    """
    findings = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            findings.extend(prose_findings(token.string.lstrip("#"), token.start[0], path, vocabulary=vocabulary))
    return findings


def check_file(path: Path, base: Path, *, vocabulary: bool, registered: frozenset[str] = frozenset()) -> list[Finding]:
    """Every finding in one Python file.

    Args:
        path: The file.
        base: The folder or file the caller named, which decides whether the module path is private.
        vocabulary: Whether to report this repository's retired words as well.
        registered: The names other checked modules import and register as agent tools.

    Returns:
        The findings, in line order.
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as error:
        return [Finding(path, error.lineno or 1, f"cannot be parsed: {error.msg}")]
    relative = path.relative_to(base).parts if path != base else (path.name,)
    module_private = any(is_private_name(part) for part in (*relative[:-1], Path(relative[-1]).stem))
    findings = check_definitions(tree.body, path, private=module_private, in_class=False, tools=tool_names(tree) | registered)
    findings.extend(string_findings(tree, path, vocabulary=vocabulary))
    findings.extend(comment_findings(source, path, vocabulary=vocabulary))
    return sorted(findings, key=lambda finding: finding.line)


def main(argv: list[str] | None = None) -> int:
    """Check every path given on the command line and print what breaks the standard.

    Args:
        argv: The command line arguments, or None to read them from the process.

    Returns:
        1 when anything was found, 0 when nothing was.
    """
    parser = argparse.ArgumentParser(description="Report docstrings and comments that break the code standard.")
    parser.add_argument("paths", nargs="+", type=Path, help="Python files or folders to read")
    parser.add_argument("--vocabulary", action="store_true", help="also report this repository's retired words")
    arguments = parser.parse_args(argv)
    files = [(base, path) for base in arguments.paths for path in python_files(base)]
    registered: set[str] = set()
    for _, path in files:
        try:
            registered |= imported_tool_names(ast.parse(path.read_text(encoding="utf-8")))
        except SyntaxError:
            continue
    findings = [
        finding for base, path in files for finding in check_file(path, base, vocabulary=arguments.vocabulary, registered=frozenset(registered))
    ]
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
