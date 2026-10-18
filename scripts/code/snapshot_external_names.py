#!/usr/bin/env python3
"""Write the names and shapes this repository shows the outside world, to compare before and after a wave.

Usage:
    python3 scripts/code/snapshot_external_names.py OUT_DIR

Writes five JSON files into OUT_DIR: the harness and studio OpenAPI documents, the `harness` and
`studio` command trees, and the studio MCP server's tools with their argument and result schemas.
Every `description` and `summary` is left out, because docstrings are rewritten on purpose and
those fields are read from them. The stack must be running, because the OpenAPI documents are
read from it. Compare two snapshots with `diff -r BEFORE AFTER`.

Standard library only. The command trees are read through each project's own environment.
"""

import argparse
import json
import queue
import subprocess
import sys
import threading
import urllib.request

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
OPENAPI = {"harness-openapi.json": "http://127.0.0.1:8770/openapi.json", "studio-openapi.json": "http://127.0.0.1:8100/openapi.json"}
COMMAND_TREES = {"harness-cli.json": ("harness", "harness.cli"), "studio-cli.json": ("studio", "studio.knowledge_base.cli")}
DESCRIPTIVE_KEYS = frozenset({"description", "summary"})
MCP_TIMEOUT_SECONDS = 60
COMMAND_TREE_PROGRAM = """
import json
import sys

import typer.main

from {module} import app


def walk(command, path):
    params = [
        {{
            "name": param.name,
            "opts": sorted([*param.opts, *param.secondary_opts]),
            "type": param.type.name,
            "required": param.required,
            "multiple": param.multiple,
            "flag": bool(getattr(param, "is_flag", False)),
        }}
        for param in command.params
    ]
    rows = [{{"command": " ".join(path), "params": sorted(params, key=lambda item: item["name"] or "")}}]
    for name, child in sorted(getattr(command, "commands", {{}}).items()):
        rows.extend(walk(child, [*path, name]))
    return rows


json.dump(walk(typer.main.get_command(app), ["{script}"]), sys.stdout)
"""


def without_descriptions(value: object) -> object:
    """A copy of a JSON value with every `description` and `summary` text removed, at any depth.

    Only a key whose value is text is removed, so a schema property that happens to be named
    `description` stays.

    Args:
        value: A value parsed from JSON.

    Returns:
        The same value without the descriptive texts.
    """
    if isinstance(value, dict):
        return {
            key: without_descriptions(item) for key, item in value.items() if not (key in DESCRIPTIVE_KEYS and isinstance(item, str))
        }
    if isinstance(value, list):
        return [without_descriptions(item) for item in value]
    return value


def openapi_document(url: str) -> object:
    """One running application's OpenAPI document, without its descriptions.

    Args:
        url: Where the application serves `openapi.json`.

    Returns:
        The document.
    """
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
        return without_descriptions(json.load(response))


def command_tree(project: str, module: str) -> object:
    """Every command and option of one project's command line, read through that project's environment.

    Args:
        project: The project folder, such as `studio`.
        module: The module that defines the Typer application `app`.

    Returns:
        One entry per command, with its options.
    """
    program = COMMAND_TREE_PROGRAM.format(module=module, script=project)
    completed = subprocess.run(
        ["uv", "run", "--directory", str(REPO / project), "python", "-c", program], capture_output=True, text=True, check=True
    )
    return json.loads(completed.stdout)


def mcp_reply(lines: "queue.Queue[str]", request_id: int) -> dict:
    """The MCP server's reply to one request, skipping its notifications.

    Args:
        lines: The server's output, one JSON message per line.
        request_id: The id of the request to wait for.

    Returns:
        The reply.

    Raises:
        TimeoutError: When no reply arrives in time.
    """
    while True:
        try:
            line = lines.get(timeout=MCP_TIMEOUT_SECONDS)
        except queue.Empty as error:
            msg = f"the MCP server did not answer request {request_id}"
            raise TimeoutError(msg) from error
        message = json.loads(line)
        if message.get("id") == request_id:
            return message


def mcp_tools() -> object:
    """The studio MCP server's tools, each with its argument and result schemas.

    Returns:
        The tools, sorted by name, without descriptions.
    """
    command = [str(REPO / "studio" / ".venv" / "bin" / "studio"), "mcp", "--knowledge-base", str(REPO / "docs")]
    server = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    requests, replies = server.stdin, server.stdout
    if requests is None or replies is None:
        msg = "the MCP server was started without pipes"
        raise RuntimeError(msg)
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in replies], daemon=True).start()

    def send(message: dict[str, object]) -> None:
        requests.write(json.dumps(message) + "\n")
        requests.flush()

    try:
        client = {"name": "snapshot", "version": "1"}
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": client}})
        mcp_reply(lines, 1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = mcp_reply(lines, 2)["result"]["tools"]
    finally:
        server.terminate()
        server.wait(timeout=10)
    shapes = [{"name": tool["name"], "input": tool.get("inputSchema"), "output": tool.get("outputSchema")} for tool in tools]
    return without_descriptions(sorted(shapes, key=lambda tool: tool["name"]))


def main(argv: list[str] | None = None) -> int:
    """Write the five snapshot files into the folder named on the command line.

    Args:
        argv: The command line arguments, or None to read them from the process.

    Returns:
        0 once every file is written.
    """
    parser = argparse.ArgumentParser(description="Snapshot the names and shapes this repository shows the outside world.")
    parser.add_argument("out_dir", type=Path, help="the folder to write the snapshot into")
    arguments = parser.parse_args(argv)
    arguments.out_dir.mkdir(parents=True, exist_ok=True)
    snapshots: dict[str, object] = {name: openapi_document(url) for name, url in OPENAPI.items()}
    snapshots.update({name: command_tree(project, module) for name, (project, module) in COMMAND_TREES.items()})
    snapshots["knowledge-base-mcp-tools.json"] = mcp_tools()
    for name, snapshot in snapshots.items():
        (arguments.out_dir / name).write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {arguments.out_dir / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
