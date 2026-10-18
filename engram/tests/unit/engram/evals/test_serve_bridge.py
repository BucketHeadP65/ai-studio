"""Unit specs for the evals slice's serve_bridge anti-corruption seam.

`engram.evals.shared.serve_bridge` is the single home for the Evaluation to Retrieval
edge: it is the only evals module allowed to import from `engram.retrieval.tools`,
`engram.retrieval.queries`, and `engram.retrieval.envelope`, re-exporting the symbols evals
needs so a refactor of the retrieval tool, query, or disclosure surface changes only that one file.

Two specs guard the seam:

* the re-export spec asserts every symbol evals uses is exposed by the bridge and is the
  same object as in its retrieval origin module;
* the architecture-invariant spec walks every `*.py` under the library evals package
  (`engram.evals`) other than `serve_bridge.py` and asserts none imports `engram.retrieval.tools`,
  `engram.retrieval.queries`, or `engram.retrieval.envelope` directly. The application's HTTP-mounting
  module for evals lives outside this library and is a separate edge, outside that package.
"""

from __future__ import annotations

import ast

from pathlib import Path

import engram.evals

from engram.retrieval import capability as serve_capability
from engram.retrieval import envelope as serve_envelope
from engram.retrieval import queries as serve_queries
from engram.retrieval import tools as serve_tools

# The names the bridge re-exports, grouped by their origin module. The toolset builders and handler ports stay in retrieval.
TOOLS_NAMES = (
    "TOOL_NAMES",
    "FIND_PATHS",
    "GRAPH_EXPAND",
    "SEARCH",
    "build_retrieval_toolset",
    "RetrievalToolset",
    "RetrievalHandlers",
    # The next-move builders and the result-status rule a guarded trial rebuilds a filtered result
    # with, plus the leveled fetch's own result type.
    "FetchEnvelope",
    "envelope_next_steps",
    "search_next_steps",
    "search_result_status",
)
QUERIES_NAMES = (
    "FetchHandler",
    "GraphExpandHandler",
    "GraphPathsHandler",
    "SearchHandler",
    "graph_next_steps",
    "paths_next_steps",
)
# The leveled-fetch and ref-grammar seam: the eval harness threads the same handler into the toolset
# build the MCP server threads into its registration, and a trial canonicalizes a ref through the
# same grammar the retrieval surface reads.
ENVELOPE_NAMES = ("EnvelopeHandler", "BadRefError", "qualify_ref", "parse_ref")
# The SQL tools an evaluation run with the SQL switch gives the agent under test.
CAPABILITY_NAMES = ("SqlKnowledgeBase",)

# The retrieval modules the bridge alone may import, among the library evals package's own modules.
FORBIDDEN_SERVE_MODULES = ("engram.retrieval.tools", "engram.retrieval.queries", "engram.retrieval.envelope", "engram.retrieval.contracts")


def test_serve_bridge_re_exports_every_symbol_evals_uses() -> None:
    from engram.evals.shared import serve_bridge

    for name in TOOLS_NAMES:
        bridged = getattr(serve_bridge, name)
        assert bridged is not None
        assert bridged is getattr(serve_tools, name)


    for name in QUERIES_NAMES:
        bridged = getattr(serve_bridge, name)
        assert bridged is not None
        assert bridged is getattr(serve_queries, name)

    for name in ENVELOPE_NAMES:
        bridged = getattr(serve_bridge, name)
        assert bridged is not None
        assert bridged is getattr(serve_envelope, name)

    for name in CAPABILITY_NAMES:
        bridged = getattr(serve_bridge, name)
        assert bridged is not None
        assert bridged is getattr(serve_capability, name)


def _imports_forbidden_serve_module(source: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and any(node.module == mod or node.module.startswith(f"{mod}.") for mod in FORBIDDEN_SERVE_MODULES)
        ):
            return True
        if isinstance(node, ast.Import) and any(
            alias.name == mod or alias.name.startswith(f"{mod}.") for alias in node.names for mod in FORBIDDEN_SERVE_MODULES
        ):
            return True
    return False


def test_only_serve_bridge_imports_the_retrieval_edge() -> None:
    package_dir = Path(engram.evals.__file__).parent
    bridge_path = package_dir / "shared" / "serve_bridge.py"

    violators = [
        path.relative_to(package_dir).as_posix()
        for path in sorted(package_dir.rglob("*.py"))
        if path != bridge_path and _imports_forbidden_serve_module(path.read_text(encoding="utf-8"))
    ]

    assert violators == [], f"these evals modules import the retrieval edge directly instead of via serve_bridge: {violators}"
