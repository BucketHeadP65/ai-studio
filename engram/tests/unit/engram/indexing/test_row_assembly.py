"""The full, incremental and ingest paths build their rows from one shared row assembly."""

import ast
import importlib

from pathlib import Path

import engram.indexing.service as service_package

SERVICE = Path(service_package.__file__).parent


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}


def test_every_path_uses_the_same_row_builder() -> None:
    row_builder = importlib.import_module("engram.indexing.service.row_builder")
    for name in ("index", "incremental", "ingest"):
        module = importlib.import_module(f"engram.indexing.service.{name}")
        assert module.RowBuilder is row_builder.RowBuilder


def test_incremental_and_ingest_never_reach_into_the_full_path() -> None:
    for name in ("incremental.py", "ingest.py", "row_builder.py", "orientation.py"):
        assert "engram.indexing.service.index" not in _imported_modules(SERVICE / name), name


def test_the_full_path_imports_the_incremental_path_at_module_top() -> None:
    tree = ast.parse((SERVICE / "index.py").read_text())
    local_imports = [
        node.module
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
        for node in ast.walk(function)
        if isinstance(node, ast.ImportFrom)
    ]
    assert local_imports == []
