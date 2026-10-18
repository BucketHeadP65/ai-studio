---
name: agent-threads
description: "Use when a vault or project carries owner note threads on its markdown files (the Agent Threads Obsidian plugin, a .agent-threads folder, or any tool that writes the same files), when asked to read, answer, reply to or resolve a note or a thread, or when a file's .threads.json file comes up. Gives you verbs for the threads so you never edit a thread file by hand."
---

# Agent threads

The owner leaves note threads on markdown files, from Obsidian (the Agent Threads plugin) or from any tool that writes
the same files. A thread is a comment anchored to a span of text, with a status (open or resolved) and replies by the
owner or by you, the agent. Threads are how the owner asks you for something on a specific passage, and how you answer.

## Where the threads live

A file's threads sit in a thread file at `<home>/.agent-threads/<path under home>.threads.json`. The home is the nearest
ancestor folder of the file that holds a `.agent-threads` folder, else the root you were opened at. Threads for
`docs/adr/x.md` live at `.agent-threads/docs/adr/x.md.threads.json`.

## The verbs

The verbs are in `scripts/notes.py`, in the folder you loaded this skill from. Run it with `python3` from the vault or
project root. The examples use `.agents/skills/agent-threads/scripts/notes.py`. If you loaded the skill from another
folder, use that folder in its place.

List every open thread under the root.

    $ python3 .agents/skills/agent-threads/scripts/notes.py list
    docs/adr/0004.md  e5bfe9de-6e3e-4ef0-8db1-f21d6ec027d9  open  line 3  on "stays raw TypeScript"
        Why raw and not a build step? Say it in the ADR.
    Next: show FILE for the replies, reply FILE NOTE_ID TEXT to answer, resolve FILE NOTE_ID when it is settled.

`list --all` includes resolved threads. A thread whose span no longer exists in the file shows `anchor lost`.

Pass `--root` after the verb to point at a vault or project from outside it (the project root you were opened at, or
any other path).

    $ python3 project/.agents/skills/agent-threads/scripts/notes.py list --root project
    docs/adr/0004.md  e5bfe9de-6e3e-4ef0-8db1-f21d6ec027d9  open  line 3  on "stays raw TypeScript"
        Why raw and not a build step? Say it in the ADR.
    Next: show FILE for the replies, reply FILE NOTE_ID TEXT to answer, resolve FILE NOTE_ID when it is settled.

Reply as the agent.

    $ python3 .agents/skills/agent-threads/scripts/notes.py reply docs/adr/0004.md e5bfe9de-6e3e-4ef0-8db1-f21d6ec027d9 "Added a paragraph under Rationale, every consumer compiles it."
    Replied as agent on e5bfe9de-6e3e-4ef0-8db1-f21d6ec027d9 in docs/adr/0004.md, written to .agent-threads/docs/adr/0004.md.threads.json.

Show one file's threads with their replies.

    $ python3 .agents/skills/agent-threads/scripts/notes.py show docs/adr/0004.md
    docs/adr/0004.md  e5bfe9de-6e3e-4ef0-8db1-f21d6ec027d9  open  line 3  on "stays raw TypeScript"
        Why raw and not a build step? Say it in the ADR.
        reply (agent, 2026-09-28T19:24:03.646316Z): Added a paragraph under Rationale, every consumer compiles it.

Resolve a thread, or open it again.

    $ python3 .agents/skills/agent-threads/scripts/notes.py resolve docs/adr/0004.md e5bfe9de-6e3e-4ef0-8db1-f21d6ec027d9
    Resolved e5bfe9de-6e3e-4ef0-8db1-f21d6ec027d9 on docs/adr/0004.md, written to .agent-threads/docs/adr/0004.md.threads.json.

    $ python3 .agents/skills/agent-threads/scripts/notes.py reopen docs/adr/0004.md e5bfe9de-6e3e-4ef0-8db1-f21d6ec027d9
    Reopened e5bfe9de-6e3e-4ef0-8db1-f21d6ec027d9 on docs/adr/0004.md, written to .agent-threads/docs/adr/0004.md.threads.json.

A wrong note id exits 2 and names the ids that exist. A wrong file exits 2 and names the root it looked under.

    $ python3 .agents/skills/agent-threads/scripts/notes.py reply docs/adr/0004.md nope hello
    No note nope on docs/adr/0004.md. Notes on docs/adr/0004.md: e5bfe9de-6e3e-4ef0-8db1-f21d6ec027d9 (open).

## The rules

- Read the thread and the passage it points at before you act. The span is the owner's context.
- Reply when the thread asks something of you, and say what you did in the file, not what you plan to do.
- Resolve only a thread you addressed. If the owner's point still stands, leave it open or reopen it.
- Never edit a `.threads.json` file by hand. The verbs write the exact format the owner's tools read.
- Leave archived notes alone, the verbs refuse them.

## What the owner sees

The Obsidian panel picks up a write within three seconds. Open threads list first, resolved ones collapse and dim,
and your replies are styled as the agent's. Any other tool that reads the same thread files sees the same threads.

## The thread file, for reference

The thread file after the reply above, as the script wrote it (one line, no trailing newline).

    {"version":1,"notes":[{"id":"e5bfe9de-6e3e-4ef0-8db1-f21d6ec027d9","anchor":{"exact":"stays raw TypeScript","prefix":" TypeScript\n\nThe client library ","suffix":", with no build step.\n\n## Ration"},"text":"Why raw and not a build step? Say it in the ADR.","created_at":"2026-09-28T09:12:03.512Z","archived":false,"replies":[{"author":"agent","text":"Added a paragraph under Rationale, every consumer compiles it.","at":"2026-09-28T19:24:03.646316Z"}],"status":"open"}]}
