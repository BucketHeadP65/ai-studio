"""`harness flags`: list, show and resolve the library's maintenance flags from the shell.

Each verb opens the library through the `LibraryAccess` the command's context object holds, so a test hands it an
opener through `obj`.
"""

from __future__ import annotations

import asyncio
import json

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import FlagCard, MaintenanceFlag, StepRegistry
from typer.testing import CliRunner

from harness.cli import LibraryAccess, app
from tests.support.flags import (
    CHANGE,
    CHANGE_ID,
    DIFF,
    DIVERGED,
    DIVERGED_ID,
    SETTLED,
    SETTLED_ID,
    SETTLED_REASON,
    STUDIO_DOWN,
    UNREADABLE_MOVE,
    a_flag_library,
    studio_down,
)

if TYPE_CHECKING:
    from contextlib import AsyncExitStack

    from core.ai.workflows.contracts import LibraryCollection
    from typer.testing import Result

    from harness.settings import HarnessSettings
    from tests.support.fakes.library_collection import FakeCollection

runner = CliRunner()


@pytest.fixture
def flagged() -> tuple[FakeCollection, LibraryCollection]:
    return asyncio.run(a_flag_library())


def _opening(library: LibraryCollection) -> LibraryAccess:
    async def open_library(stack: AsyncExitStack, settings: HarnessSettings) -> tuple[LibraryCollection, StepRegistry]:
        del stack, settings
        return library, StepRegistry()

    return LibraryAccess(open_library=open_library)


def _flags(library: LibraryCollection, *arguments: str) -> Result:
    return runner.invoke(app, ["flags", *arguments], obj=_opening(library))


def _refused(result: Result) -> str:
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), "no traceback"
    lines = result.output.splitlines()
    assert len(lines) == 1, result.output
    return lines[0]


def test_list_prints_one_line_per_open_flag_newest_first(flagged: tuple[FakeCollection, LibraryCollection]) -> None:
    _, library = flagged

    result = _flags(library, "list")

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        f"{DIVERGED_ID}  diverged_copy  wf.digest.v3  finish  {DIVERGED.summary}",
        f"{CHANGE_ID}  repository_change  -  summarise  {CHANGE.summary}",
    ]


def test_list_narrows_by_kind_and_by_status(flagged: tuple[FakeCollection, LibraryCollection]) -> None:
    _, library = flagged

    diverged = _flags(library, "list", "--kind", "diverged_copy")
    resolved = _flags(library, "list", "--status", "resolved")

    assert diverged.output.splitlines() == [f"{DIVERGED_ID}  diverged_copy  wf.digest.v3  finish  {DIVERGED.summary}"]
    assert resolved.output.splitlines() == [f"{SETTLED_ID}  diverged_copy  wf.report.v2  finish  {SETTLED.summary}"]


def test_list_says_when_no_flag_matches(flagged: tuple[FakeCollection, LibraryCollection]) -> None:
    _, library = flagged

    result = _flags(library, "list", "--kind", "plan_rejected")

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["No open maintenance flags of kind plan_rejected."]


def test_list_json_parses_back_into_the_cards_the_route_answers(flagged: tuple[FakeCollection, LibraryCollection]) -> None:
    _, library = flagged

    result = _flags(library, "list", "--json")

    assert result.exit_code == 0, result.output
    cards = [FlagCard.model_validate(card) for card in json.loads(result.output)]
    assert cards == list(asyncio.run(library.flags(status="open")))
    assert DIFF in cards[0].evidence


def test_list_refuses_a_flag_page_that_does_not_parse_in_one_line(flagged: tuple[FakeCollection, LibraryCollection]) -> None:
    store, library = flagged
    del store.pages[CHANGE_ID]["frontmatter"]["step_hash"]

    line = _refused(_flags(library, "list"))

    assert line.startswith(f"the maintenance flag {CHANGE_ID!r} does not parse")
    assert line.endswith(f". {UNREADABLE_MOVE}")


@pytest.mark.parametrize("verb", [("show",), ("resolve", "--by", "owner", "--reason", "Applied.")], ids=["show", "resolve"])
def test_show_and_resolve_refuse_a_flag_page_that_does_not_parse_with_the_next_move(
    flagged: tuple[FakeCollection, LibraryCollection], verb: tuple[str, ...]
) -> None:
    store, library = flagged
    del store.pages[CHANGE_ID]["frontmatter"]["step_hash"]
    written = len(store.payloads)

    line = _refused(_flags(library, verb[0], CHANGE_ID, *verb[1:]))

    assert line.startswith(f"the maintenance flag {CHANGE_ID!r} does not parse")
    assert line.endswith(f". {UNREADABLE_MOVE}")
    assert len(store.payloads) == written


@pytest.mark.parametrize(
    "arguments",
    [["flags", "list"], ["flags", "show", DIVERGED_ID], ["flags", "resolve", DIVERGED_ID, "--by", "owner", "--reason", "Kept."]],
    ids=["list", "show", "resolve"],
)
def test_a_library_that_does_not_open_prints_what_did_not_answer_in_one_line(arguments: list[str]) -> None:
    result = runner.invoke(app, arguments, obj=LibraryAccess(open_library=studio_down))

    assert _refused(result) == f"The workflow library did not open: {STUDIO_DOWN}"


def test_show_prints_the_whole_flag_with_its_evidence_and_diff(flagged: tuple[FakeCollection, LibraryCollection]) -> None:
    _, library = flagged

    result = _flags(library, "show", DIVERGED_ID)

    assert result.exit_code == 0, result.output
    assert result.output == (
        f"{DIVERGED_ID}\n"
        "Diverged copy of finish in digest\n"
        "kind: diverged_copy\n"
        "status: open\n"
        "step: finish\n"
        "step version: st.finish.v2 (sha256:aaaa)\n"
        "workflow: wf.digest.v3\n"
        "raised: 2026-11-05T11:00:00+00:00 by library\n"
        f"summary: {DIVERGED.summary}\n"
        "\n"
        "## Evidence\n"
        "\n"
        f"{DIVERGED.evidence}\n"
        "\n"
        "## Proposed change\n"
        "\n"
        "(none)\n"
        "\n"
        f'Resolve it with: harness flags resolve {DIVERGED_ID} --by <your name> --reason "<why>"\n'
    )


def test_show_prints_a_resolved_flag_with_who_and_why_and_its_proposed_change(flagged: tuple[FakeCollection, LibraryCollection]) -> None:
    _, library = flagged
    asyncio.run(library.resolve_flag(CHANGE_ID, by="coding-agent", reason="Applied as version 5."))

    output = _flags(library, "show", CHANGE_ID).output

    assert "workflow: none, the flag is about the step\n" in output
    assert "resolved by: coding-agent\nreason: Applied as version 5.\n" in output
    assert f"## Proposed change\n\n{CHANGE.proposed_change}\n" in output
    assert "Resolve it with" not in output


def test_show_json_parses_back_into_the_flag(flagged: tuple[FakeCollection, LibraryCollection]) -> None:
    _, library = flagged

    result = _flags(library, "show", SETTLED_ID, "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["id"] == SETTLED_ID
    assert MaintenanceFlag.model_validate(payload["flag"]) == SETTLED.model_copy(
        update={"status": "resolved", "resolved_by": "owner", "decision_reason": SETTLED_REASON}
    )


def test_show_refuses_an_unknown_id_in_one_line_naming_it(flagged: tuple[FakeCollection, LibraryCollection]) -> None:
    _, library = flagged

    line = _refused(_flags(library, "show", "mf.diverged.nowhere.finish"))

    assert line == (
        "No maintenance flag mf.diverged.nowhere.finish. Find its id with `harness flags list`, "
        "or `harness flags list --status resolved` for a resolved one."
    )


def test_resolve_resolves_with_who_and_why_and_exits_0(flagged: tuple[FakeCollection, LibraryCollection]) -> None:
    _, library = flagged

    result = _flags(library, "resolve", DIVERGED_ID, "--by", "owner", "--reason", "The shorter digest is wanted.")

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [f"Resolved {DIVERGED_ID} by owner."]
    stored = asyncio.run(library.fetch_flag(DIVERGED_ID))
    assert stored is not None
    assert (stored.status, stored.resolved_by, stored.decision_reason) == ("resolved", "owner", "The shorter digest is wanted.")


def test_resolve_refuses_an_unknown_id_in_one_line_naming_it(flagged: tuple[FakeCollection, LibraryCollection]) -> None:
    store, library = flagged
    written = len(store.payloads)

    line = _refused(_flags(library, "resolve", "mf.diverged.nowhere.finish", "--by", "owner", "--reason", "Gone."))

    assert line == (
        "No maintenance flag mf.diverged.nowhere.finish. Find its id with `harness flags list`, "
        "or `harness flags list --status resolved` for a resolved one."
    )
    assert len(store.payloads) == written


def test_resolve_refuses_an_empty_reason_in_one_line_and_writes_nothing(flagged: tuple[FakeCollection, LibraryCollection]) -> None:
    store, library = flagged
    written = len(store.payloads)

    line = _refused(_flags(library, "resolve", DIVERGED_ID, "--by", "owner", "--reason", " "))

    assert line == f"{DIVERGED_ID} was not resolved: --by and --reason must not be empty. Say who resolves it and why, then run it again."
    assert len(store.payloads) == written
