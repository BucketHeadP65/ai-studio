"""Indexing any registered knowledge base from a process that serves the registry of knowledge bases.

It runs `studio index` against the knowledge base's own root, so an index never scans one knowledge base's files
under another's collection. An index that fails is reported in the result and never raised, and the caller
decides what the failure means.
"""

from __future__ import annotations

import asyncio
import sys

from typing import TYPE_CHECKING, Protocol

from engram.authoring.contracts import ReindexResult, reindex_result_from_subprocess

if TYPE_CHECKING:
    from collections.abc import Sequence

    from engram.authoring.contracts import AuthoringKnowledgeBase


class CaptureRunner(Protocol):
    """Run one command and report its exit code together with the tail of its combined output."""

    async def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        """Run `argv` and wait for it to exit.

        Args:
            argv: The command and its arguments.

        Returns:
            The command's exit code and the tail of its combined stdout and stderr.
        """
        ...


INDEX_ERROR_TAIL = 1500
"""The most characters of output a failed index's result carries, taken from the end of the output."""


async def run_captured(argv: Sequence[str]) -> tuple[int, str]:
    """The default `CaptureRunner`: run `argv` with stdout and stderr merged.

    Args:
        argv: The command and its arguments.

    Returns:
        The exit code, `-1` when there is none, and the last `INDEX_ERROR_TAIL` characters of the output.
    """
    process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    output, _ = await process.communicate()
    text = output.decode(errors="replace").strip()
    return process.returncode if process.returncode is not None else -1, text[-INDEX_ERROR_TAIL:]


class SubprocessReindex:
    """Reindex a knowledge base by running `studio index --knowledge-base <path>` under this interpreter and environment.

    It works for any knowledge base, because the subprocess resolves its own root from `--knowledge-base` and never
    reuses this process's reader.
    """

    def __init__(self, run: CaptureRunner = run_captured) -> None:
        """Hold the command runner.

        Args:
            run: Runs one command and reports its exit code and output tail.
        """
        self._run = run

    async def __call__(self, knowledge_base: AuthoringKnowledgeBase) -> ReindexResult:
        """Reindex `knowledge_base`'s own checkout, reading the outcome from the subprocess's exit code and output tail.

        Args:
            knowledge_base: The knowledge base whose checkout is indexed.

        Returns:
            The outcome of the index run.
        """
        argv = (sys.executable, "-m", "studio.knowledge_base.cli", "index", "--knowledge-base", str(knowledge_base.path))
        code, tail = await self._run(argv)
        return reindex_result_from_subprocess(knowledge_base, code, tail)
