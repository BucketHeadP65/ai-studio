---
id: harness-adr-0013-skills-registry
aliases: ["harness-adr-0013-skills-registry"]
type: adr
app: harness
status: proposed
tags: []
title: "One skills registry, seats name their skills, an authoring agent lists them, Claude Code reads the same files"
---

# One skills registry, seats name their skills, an authoring agent lists them, Claude Code reads the same files

**Status:** Proposed (2026-08-30).

## Context

Skills sit in four homes with four readers and no rule that says which agent reads which, three under the harness's own
package, six under this repository's `.claude/skills/`, one mirrored under the owner's `~/.claude/skills/`, and the rest
in a plugin that is being retired. The studio's chat agent mounts a whole directory at a time, so every skill in a
library reaches it and no other agent reaches any, and opening the studio on another project starts it with only three
skills. An agent that authors agents cannot see which skills exist, so it cannot give a new seat the discipline that
seat needs, and a workflow step cannot name a skill at all.

## Decision

**D1.** The registry is `harness/skills/`, beside `harness/blocks/`, data the app ships and versions with itself, read
fresh at every boot for every project. The three skills that lived under the harness package and the six that lived
under this repository's `.claude/skills/` move there. A mirror under `~/.claude/skills/` stays the owner's own, made by
symlink, documented, never made by the repository.

**D2.** A seat names its skills. `ProfileDoc.skills`, the authored agent document's `skills`, and `TaskSpec.skills` are
each a list of names. The harness turns a list into `Skills(SKILLS, include=names)` on that one seat, never a whole
directory. The chat agent's list is the active profile's, applied the way its doors already are, with the constant
`ORCHESTRATOR_SKILLS`, every registry skill named one by one, standing in when no profile is selected, so a skill added
to the registry reaches no seat until a seat names it.

**D3.** A name is refused where it is written. An authored agent or profile naming a skill outside the registry is
refused at `author_agent` or `author_profile`, with the names on file listed in the refusal. A step block naming an
unknown skill fails the blocks check and `pack`. A profile naming an unknown skill is refused again at selection. The
name space is the registry plus the project's own libraries.

**D4.** The authoring agent lists the registry before it names anything. `list_skills` answers one line per skill, name
and description, the same shape `list_authored_agents` already answers, mounted beside the author doors.

**D5.** `Skills` joins the known capabilities on the authoring path. `validate_agent_document` and `build_worker_agent`
accept it beside the owner's promoted capabilities, so a spec may still carry the upstream entry directly. The
`skills` field is the short form the harness expands into that entry with the registry's path.

**D6.** Claude Code reads the registry through symlinks, not a copy. Which skills Claude Code holds in this repository
is exactly which symlinks exist under `.claude/skills/`, the same rule as a seat naming its list.

**D7.** The catalog stays the one screen for browsing. `skills_catalog` keeps serving the projects station, reading
the registry as its builtin library and each project's own libraries beside it. The `list_skills` door of D4 reads the
same cards, so the screen and the authoring agent never disagree about what exists.

## Consequences

- A profile carries `skills`, a list of names, applied to the chat agent at selection.
- An authored agent document carries `skills`, a list of names, expanded into its own `Skills` mount at build time.
- A workflow step block carries `skills`, a list of names, mounted on that one step and nowhere else.
- With no profile selected, the chat agent's catalog is exactly `ORCHESTRATOR_SKILLS`, every registry skill named one
  by one.
- `list_skills` is a read door beside `author_agent` and `author_profile`. An agent that writes a seat lists the
  registry first, then names skills on the seat it is writing.
- `Skills` is a known capability on the authoring path, so a hand-written spec may still carry the upstream entry
  directly instead of the `skills` short form.
- Claude Code follows the symlink rule the docs already state. "A `<skill-name>` entry in the enterprise, personal, or
  project locations can be a symlink to a directory elsewhere on disk. Claude Code follows the symlink and reads
  `SKILL.md` from the target directory."

What was rejected, and why.

- An audience field on a skill, so the registry itself would say who may hold it. Rejected, a seat already names its
  own skills one by one, a second field saying the same thing a different way is a fact that can disagree with itself.
- Copies or links between projects. Rejected, one file per skill, one registry, so a change to a skill's content
  reaches every seat that names it without a second place to edit.
- Inheritance on opening a project, a project picking up skills just because it was opened. Rejected, a seat's list is
  the one place its skills are decided, and inheritance would let a project's skills change without anyone naming
  them.
- A Claude Code plugin for the registry. Rejected, a plugin manifest cannot point outside its own root, so the
  registry would have had to live inside `harness/`, which would then read as a Claude plugin, the one shape the owner
  ruled out for this directory.
