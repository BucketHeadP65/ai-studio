"""The canonical retrieval tool names and the per-tool retry budget of the pydantic-ai toolset.

The toolset builders and the strategy axis read every tool name from here.
"""

from __future__ import annotations

SEARCH = "search"
"""The tool that finds candidate units."""
FETCH = "fetch"
"""The tool that reads one unit's full text."""
GRAPH_EXPAND = "graph_expand"
"""The tool that walks the knowledge graph out from a seed set."""
FIND_PATHS = "find_paths"
"""The tool that finds the typed paths between two nodes."""

TOOL_NAMES = frozenset({SEARCH, FETCH, GRAPH_EXPAND, FIND_PATHS})
"""The four structured retrieval tools a knowledge base exposes, the set the toolset-builder subset filters work over."""

RUN_CODE = "run_code"
"""The code-mode tool, a sandbox in which the model drives the retrieval tools by writing code. It is not in `TOOL_NAMES`."""

RUN_SQL = "run_sql"
"""The read-only SQL tool. The agent surface registers it, not the retrieval toolset."""

TOOL_MAX_RETRIES = 3
"""How many times each tool the pydantic-ai toolset registers may answer `ModelRetry` in one run. The MCP tools carry no cap."""
