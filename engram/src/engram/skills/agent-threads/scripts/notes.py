#!/usr/bin/env python3
# Part of the agent-threads skill. The Agent Threads Obsidian plugin overwrites a vault's copy of this file on load.
"""Owner note threads on markdown files: list them, show one file's, reply as the agent, resolve, reopen.

Usage, from the knowledge base or project root (or pass --root):

    notes.py list [--all]              every open thread under the root (--all adds resolved ones)
    notes.py show FILE                 every thread on FILE with its replies
    notes.py reply FILE NOTE_ID TEXT   append a reply as the agent
    notes.py resolve FILE NOTE_ID      close the thread
    notes.py reopen FILE NOTE_ID       open it again

FILE is a path relative to the root. A file's threads live at
`<home>/.agent-threads/<path under home>.threads.json`, where home is the nearest ancestor of FILE (up to the
root) that holds a `.agent-threads` folder, else the root. The thread file is written exactly the way the Agent
Threads Obsidian plugin writes it (same field order, compact JSON, no trailing newline) through a temp file replaced into place.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, List, TypedDict

THREADS_DIR = ".agent-threads"
SUFFIX = ".threads.json"
SKIPPED_DIRS = {".git", ".obsidian", ".venv", "node_modules"}


class _AnchorRequired(TypedDict):
    exact: str


class Anchor(_AnchorRequired, total=False):
    """A text-quote selector on the source markdown, the thread file's on-disk shape."""

    prefix: str
    suffix: str


class Reply(TypedDict):
    """One reply in a note's thread, the thread file's on-disk shape."""

    author: str
    text: str
    at: str


class _NoteRequired(TypedDict):
    id: str
    anchor: Anchor
    text: str
    created_at: str


class Note(_NoteRequired, total=False):
    """One owner annotation, the thread file's on-disk shape."""

    archived: bool
    replies: List[Reply]
    status: str


class Sheet(TypedDict):
    """The thread file's whole content: a version and every note on the file."""

    version: int
    notes: List[Note]


def home_for(root: Path, rel: Path) -> Path:
    """The nearest ancestor of `rel` (up to `root`) that holds a `.agent-threads` folder, else `root`.

    Args:
        root: The knowledge base or project root.
        rel: The file's path relative to `root`.

    Returns:
        The home directory for `rel`.
    """
    current = (root / rel).parent
    while True:
        if (current / THREADS_DIR).is_dir():
            return current
        if current == root or root not in current.parents:
            return root
        current = current.parent


def _sidecar_under(home: Path, root: Path, rel: Path) -> Path:
    """Where `rel`'s thread file would sit under `home`, an ancestor of `root / rel` up to and including `root`."""
    under_home = (root / rel).relative_to(home)
    return home / THREADS_DIR / under_home.with_name(under_home.name + SUFFIX)


def sidecar_for(root: Path, rel: Path) -> Path:
    """Where `rel`'s thread file lives under its home, the write target for a mutation.

    Args:
        root: The knowledge base or project root.
        rel: The file's path relative to `root`.

    Returns:
        The thread file path under `rel`'s home.
    """
    return _sidecar_under(home_for(root, rel), root, rel)


def _candidate_homes(root: Path, rel: Path) -> List[Path]:
    """Every ancestor directory of `rel` up to and including `root`, nearest first, whether or not it holds `.agent-threads`."""
    homes: List[Path] = []
    current = (root / rel).parent
    while True:
        homes.append(current)
        if current == root or root not in current.parents:
            return homes
        current = current.parent


def read_order(root: Path, rel: Path) -> List[Path]:
    """Every thread file path a read of `rel` consults, in order: the resolved home first, then every other candidate home.

    Args:
        root: The knowledge base or project root.
        rel: The file's path relative to `root`.

    Returns:
        The thread file paths, nearest home first, with no duplicates.
    """
    resolved = sidecar_for(root, rel)
    order = [resolved]
    for home in _candidate_homes(root, rel):
        candidate = _sidecar_under(home, root, rel)
        if candidate not in order:
            order.append(candidate)
    return order


def load(sidecar: Path) -> Sheet:
    """The thread file's sheet with version always 1, or an empty one when the file is absent.

    Args:
        sidecar: The thread file to read.

    Returns:
        The sheet the thread file holds.

    Raises:
        SystemExit: With code 2 when the thread file is not a JSON object with a `notes` list.
    """
    if not sidecar.is_file():
        return {"version": 1, "notes": []}
    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raw = None
    if not isinstance(raw, dict) or not isinstance(raw.get("notes"), list):
        print(f"Unreadable thread file {sidecar}.", file=sys.stderr)
        sys.exit(2)
    return {"version": 1, "notes": raw["notes"]}


def load_first(root: Path, rel: Path) -> Sheet:
    """The sheet at the first existing thread file in `rel`'s read order, or an empty one when none exists.

    Args:
        root: The knowledge base or project root.
        rel: The file's path relative to `root`.

    Returns:
        The sheet for `rel`.
    """
    for path in read_order(root, rel):
        if path.is_file():
            return load(path)
    return {"version": 1, "notes": []}


def dump(sheet: Sheet) -> str:
    """The sheet as the exact on-disk text: fixed field order, defaults filled in, compact, no newline.

    Args:
        sheet: The sheet to serialise.

    Returns:
        The thread file's text.
    """
    ordered: Sheet = {
        "version": 1,
        "notes": [
            {
                "id": note["id"],
                "anchor": {
                    "exact": note["anchor"]["exact"],
                    "prefix": note["anchor"].get("prefix", ""),
                    "suffix": note["anchor"].get("suffix", ""),
                },
                "text": note["text"],
                "created_at": note["created_at"],
                "archived": note.get("archived", False),
                "replies": [{"author": reply["author"], "text": reply["text"], "at": reply["at"]} for reply in note.get("replies", [])],
                "status": note.get("status", "open"),
            }
            for note in sheet["notes"]
        ],
    }
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


def write(sidecar: Path, sheet: Sheet) -> None:
    """Write `sheet` through a temp file in the thread file's directory, replaced into place.

    Args:
        sidecar: The thread file to write.
        sheet: The sheet to write into it.
    """
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=sidecar.name, suffix=".tmp", dir=sidecar.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temp:
            temp.write(dump(sheet))
        Path(temp_name).replace(sidecar)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def now() -> str:
    """The current UTC time in the thread file's timestamp form.

    Returns:
        The ISO 8601 timestamp ending in `Z`.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _flexible_pattern(exact: str) -> re.Pattern[str]:
    tokens = re.split(r"(\s+)", exact)
    escaped = (r"\s+" if token.isspace() else re.escape(token) for token in tokens)
    return re.compile("".join(escaped), re.DOTALL)


def _prefix_matches(text: str, start: int, prefix: str) -> bool:
    return not prefix or text[max(0, start - len(prefix)) : start].endswith(prefix)


def _suffix_matches(text: str, end: int, suffix: str) -> bool:
    return not suffix or text[end : end + len(suffix)].startswith(suffix)


_HEADING_TAIL = re.compile(r"(?:^|\n)#{1,6} \Z")
_HEADING_LINE = re.compile(r"#{1,6} ")


def _starts_heading_line(text: str, start: int) -> bool:
    line_start = text.rfind("\n", 0, start) + 1
    return _HEADING_LINE.fullmatch(text, line_start, start) is not None


def _single_hit(text: str, matches: List[re.Match[str]], anchor: Anchor) -> int | None:
    """The start of the one match left after narrowing by prefix and suffix, heading, prefix alone or suffix alone, else `None`."""
    if len(matches) == 1:
        return matches[0].start()
    if len(matches) > 1:
        prefix = anchor.get("prefix", "")
        suffix = anchor.get("suffix", "")
        pools = [
            [m for m in matches if _prefix_matches(text, m.start(), prefix) and _suffix_matches(text, m.end(), suffix)],
            [m for m in matches if _starts_heading_line(text, m.start())] if _HEADING_TAIL.search(prefix) else [],
            [m for m in matches if _prefix_matches(text, m.start(), prefix)] if prefix else [],
            [m for m in matches if _suffix_matches(text, m.end(), suffix)] if suffix else [],
        ]
        for pool in pools:
            if len(pool) == 1:
                return pool[0].start()
    return None


def resolve_line(text: str, anchor: Anchor) -> int | None:
    """The 1-based line where `anchor` sits in `text` now, `None` when it no longer resolves.

    Args:
        text: The markdown file's current text.
        anchor: The note's text-quote selector.

    Returns:
        The line number, or `None` when the anchor matches no single place.
    """
    exact = anchor.get("exact", "")
    if not exact:
        return None
    start = _single_hit(text, list(re.finditer(re.escape(exact), text)), anchor)
    if start is None:
        start = _single_hit(text, list(_flexible_pattern(exact).finditer(text)), anchor)
    return None if start is None else text.count("\n", 0, start) + 1


def _where(root: Path, rel: Path, note: Note) -> str:
    target = root / rel
    if not target.is_file():
        return "file missing"
    line = resolve_line(target.read_text(encoding="utf-8"), note["anchor"])
    return "anchor lost" if line is None else f"line {line}"


def _homes(root: Path) -> Iterator[Path]:
    """Every directory under `root` (root included) that holds a `.agent-threads` folder, root first."""
    if (root / THREADS_DIR).is_dir():
        yield root
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIPPED_DIRS and not (name.startswith(".") and name != THREADS_DIR))
        current = Path(dirpath)
        if current != root and THREADS_DIR in dirnames:
            yield current


def _threads(root: Path) -> Iterator[tuple[Path, Note]]:
    """Every note under every home, as (root-relative file path, note). Thread files come in path order, notes in the order they appear in each file."""
    for home in _homes(root):
        notes_dir = home / THREADS_DIR
        if not notes_dir.is_dir():
            continue
        for sidecar in sorted(notes_dir.rglob(f"*{SUFFIX}")):
            under_home = sidecar.relative_to(notes_dir)
            rel = (home / under_home.with_name(under_home.name[: -len(SUFFIX)])).relative_to(root)
            for note in load(sidecar)["notes"]:
                if not note.get("archived", False):
                    yield rel, note


def _short(text: str, width: int = 80) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 3] + "..."


def _print_thread(root: Path, rel: Path, note: Note, *, replies: bool) -> None:
    status = note.get("status", "open")
    print(f'{rel}  {note["id"]}  {status}  {_where(root, rel, note)}  on "{_short(note["anchor"]["exact"], 40)}"')
    print(f"    {_short(note['text'])}")
    if replies:
        for reply in note.get("replies", []):
            print(f"    reply ({reply['author']}, {reply['at']}): {_short(reply['text'], 200)}")


def cmd_list(root: Path, *, include_resolved: bool) -> int:
    """Print every thread under `root`, one line each, open only unless `include_resolved`.

    Args:
        root: The knowledge base or project root.
        include_resolved: Whether resolved threads are printed too.

    Returns:
        The exit code, always 0.
    """
    shown = [(rel, note) for rel, note in _threads(root) if include_resolved or note.get("status", "open") == "open"]
    if not shown:
        print(f"No {'threads' if include_resolved else 'open threads'} under {root}.")
        return 0
    for rel, note in shown:
        _print_thread(root, rel, note, replies=False)
    print("Next: show FILE for the replies, reply FILE NOTE_ID TEXT to answer, resolve FILE NOTE_ID when it is settled.")
    return 0


def cmd_show(root: Path, rel: Path) -> int:
    """Print every thread on `rel`, replies included, open threads before resolved ones.

    Args:
        root: The knowledge base or project root.
        rel: The file's path relative to `root`.

    Returns:
        The exit code, always 0.
    """
    sheet = load_first(root, rel)
    live = [note for note in sheet["notes"] if not note.get("archived", False)]
    if not live:
        print(f"No threads on {rel}.")
        return 0
    live.sort(key=lambda note: (note.get("status", "open") != "open", note["created_at"]))
    for note in live:
        _print_thread(root, rel, note, replies=True)
    return 0


def _find(sheet: Sheet, rel: Path, note_id: str) -> Note | None:
    for note in sheet["notes"]:
        if note["id"] == note_id:
            if note.get("archived", False):
                print(f"Note {note_id} on {rel} is archived, leave it.", file=sys.stderr)
                return None
            return note
    if sheet["notes"]:
        listing = ", ".join(f"{note['id']} ({note.get('status', 'open')})" for note in sheet["notes"])
        print(f"No note {note_id} on {rel}. Notes on {rel}: {listing}.", file=sys.stderr)
    else:
        print(f"No note {note_id} on {rel}, the file has no threads.", file=sys.stderr)
    return None


def _mutate(root: Path, rel: Path, note_id: str, message: str, apply: Callable[[Note], None]) -> int:
    sidecar = sidecar_for(root, rel)
    sheet = load_first(root, rel)
    note = _find(sheet, rel, note_id)
    if note is None:
        return 2
    apply(note)
    write(sidecar, sheet)
    print(f"{message}, written to {sidecar.relative_to(root)}.")
    return 0


def cmd_reply(root: Path, rel: Path, note_id: str, text: str) -> int:
    """Append `text` as an agent reply on `note_id`'s thread, minting its timestamp.

    Args:
        root: The knowledge base or project root.
        rel: The file's path relative to `root`.
        note_id: The note whose thread gets the reply.
        text: The reply text.

    Returns:
        The exit code, 0 when written and 2 when the note is missing or archived.
    """

    def apply(note: Note) -> None:
        note.setdefault("replies", []).append({"author": "agent", "text": text, "at": now()})

    return _mutate(root, rel, note_id, f"Replied as agent on {note_id} in {rel}", apply)


def cmd_status(root: Path, rel: Path, note_id: str, status: str) -> int:
    """Set `note_id`'s thread status to `status` (`open` or `resolved`).

    Args:
        root: The knowledge base or project root.
        rel: The file's path relative to `root`.
        note_id: The note whose status changes.
        status: The new status, `open` or `resolved`.

    Returns:
        The exit code, 0 when written and 2 when the note is missing or archived.
    """

    def apply(note: Note) -> None:
        note["status"] = status

    verb = "Resolved" if status == "resolved" else "Reopened"
    return _mutate(root, rel, note_id, f"{verb} {note_id} on {rel}", apply)


def _parser() -> argparse.ArgumentParser:
    # --root has two dests because a shared dest would let the subparser's default overwrite the top parser's value.
    root_help = "the vault or project root (default, the working directory)"
    parser = argparse.ArgumentParser(
        prog="notes.py",
        description="Owner note threads on markdown files.",
        epilog="Pass --root after the verb (for example, notes.py list --root PATH).",
    )
    parser.add_argument("--root", dest="root_before", default=None, help=root_help)
    verbs = parser.add_subparsers(dest="verb", required=True)
    listing = verbs.add_parser("list", help="every open thread under the root")
    listing.add_argument("--all", action="store_true", help="include resolved threads")
    listing.add_argument("--root", dest="root", default=None, help=root_help)
    show = verbs.add_parser("show", help="every thread on one file")
    show.add_argument("file")
    show.add_argument("--root", dest="root", default=None, help=root_help)
    reply = verbs.add_parser("reply", help="append a reply as the agent")
    reply.add_argument("file")
    reply.add_argument("note_id")
    reply.add_argument("text")
    reply.add_argument("--root", dest="root", default=None, help=root_help)
    for name in ("resolve", "reopen"):
        verb = verbs.add_parser(name, help=f"{name} a thread")
        verb.add_argument("file")
        verb.add_argument("note_id")
        verb.add_argument("--root", dest="root", default=None, help=root_help)
    return parser


def main(argv: List[str] | None = None) -> int:
    """Parse `argv` (or `sys.argv` when `None`) and run the named verb, returning its exit code.

    Args:
        argv: The command-line arguments after the program name, or `None` for `sys.argv`.

    Returns:
        The verb's exit code, 2 when the file or its thread file cannot be read.
    """
    args = _parser().parse_args(argv)
    root = Path(args.root or args.root_before or ".").resolve()
    try:
        if args.verb == "list":
            return cmd_list(root, include_resolved=args.all)
        rel = Path(args.file)
        if not (root / rel).is_file():
            print(f"No file at {rel} under {root}.", file=sys.stderr)
            return 2
        if args.verb == "show":
            return cmd_show(root, rel)
        if args.verb == "reply":
            return cmd_reply(root, rel, args.note_id, args.text)
        return cmd_status(root, rel, args.note_id, "resolved" if args.verb == "resolve" else "open")
    except SystemExit as exc:
        # load() exits on an unreadable thread file, and main() returns that code instead of raising.
        return exc.code if isinstance(exc.code, int) else 1


if __name__ == "__main__":
    sys.exit(main())
