---
name: placing-knowledge
description: Use when you are about to write down something you learned, decided, or were told (a rule, a preference, a lesson, a bug, a workaround, a mistake you made, a procedure) and must pick where it goes, a skill, an agent definition, a tool description, AGENTS.md / CLAUDE.md or a rule, memory, a log, an ADR, a tech debt entry, or a pull request. Decides the surface by who reads it and what they would do differently, then routes to the skill that writes that surface.
---

# Placing knowledge

## Iron Law

> Every surface has one reader. Write for that reader, on that surface, and nowhere else. A sentence that would not change
> what its reader does belongs to no surface. A sentence for a different reader belongs on a different surface.

## When NOT to use

Once the surface is chosen, this skill is done. Writing the content is the routed skill's job (`writing-skills`,
`writing-agents`, `agent-ux`, the knowledge base's own `documenting`). Deciding what a project should document at all is the
knowledge base's `documenting` skill, not this one.

## The three questions

Ask them in order, for every piece of knowledge, before writing a word.

1. Who reads it? An agent holding a role and doing a job (you included, when the job is yours). The coding agent of this
   repository. You, next session. The owner and the coding agents who ship fixes. The router that picks skills and agents.
2. When does it load? Every session. On a trigger. On delegation. On every request. On demand.
3. If the reader obeyed it, what would they do differently? No answer means the author is talking. Delete the sentence, or
   move the reason to the commit, the ADR, or memory.

## The surfaces

| Surface | Reader | Loads | Goes there | Never there | Write it with |
|---|---|---|---|---|---|
| Skill | whoever holds the role that does the job, you included | description every session, body on invocation | one discipline, standalone: principles, moves, checks, one example, in the reader's vocabulary | incidents, bug workarounds, tool mechanics, library names and versions, file paths, log pointers, dates, project facts | `writing-skills`, `agent-ux` |
| Agent definition | the router (its description), then the agent itself (its body is the whole system prompt) | on delegation | the role: identity, the job, the skills it holds, the smallest tool set, model, budget, report contract, what it never does | project facts, tool mechanics, reasons for the design | `writing-agents`, `agent-ux` |
| Tool description, capability instructions | the agent holding the tool | every request | what you get, when to reach for it, how it composes with its siblings, how far it reaches | how it is built, why it exists | `agent-ux` |
| AGENTS.md / CLAUDE.md, client-specific rules | the coding agent of this repository | according to the client's instruction scope | commands it cannot guess, conventions that differ from the defaults, how to test, repository etiquette, environment quirks, under 200 lines | procedures (a skill), a concern of one part of the code (nested instructions or a client-specific path rule), history, anything the code shows, anything a linked file can hold | one test per line: would removing it cause a mistake? If not, cut it |
| Memory | you, next session | according to the runtime's memory discovery and recall rules | the owner's preferences, your recurring mistakes with the why, project state the repository cannot show, references | what the repository records, what AGENTS.md / CLAUDE.md already says, what belongs in a skill for another reader | the runtime's memory rules, one fact per file, an index line |
| Log, ADR, tech debt, pull request | the owner and the coding agents who ship fixes | on demand | a bug with its evidence, a workaround with its guard, a decision with its reasons, a debt with its cost | nothing to exclude, this is where everything else goes | the knowledge base's `documenting` for ADRs and logs, the repository's tech debt page |

Codex discovers `AGENTS.md`. Claude Code discovers `CLAUDE.md`. For a repository used by both, keep one shared body
with a counterpart symlink in each relevant directory. Codex builds its initial instruction chain from the repository
root to its working directory. When work enters another package, read that package's instructions explicitly.
Claude's `.claude/rules/` path matching is client-specific, so shared package rules belong in the package instruction file.

## Sorting a lesson

When something went wrong and you want to keep the lesson, it is never one thing. Split it.

- The principle the reader applies next time, timeless, true in another project too: a skill. If it is only true here, or only
  true because of one run, it is not a principle.
- The defect: the log, then a guard or a fix, then an upstream issue when the fault is not ours.
- The owner's correction of you: memory, as feedback, with the why.
- The decision: an ADR.
- The fact about this repository that every session needs: AGENTS.md / CLAUDE.md or a client-specific rule, and only then.

## One lesson, five homes

A bakery's night baker once left the ovens on because the timer sat on the wrong ledge and nobody saw it. The morning
crew wants to "write down the lesson". It is five things.

- The principle, "a timer lives where the person who must hear it stands", goes in the bakers' handbook. It is true in
  every bakery.
- The broken ledge goes on the repair list, with a photo, and a hook is screwed in the same week.
- "The owner wants the night baker told twice, once at the oven and once at the doorway" goes in the manager's own notebook,
  it is about this owner.
- "We decided the night shift ends at six, not five" goes in the decisions file, with the reason.
- "The back latch sticks, lift it while turning the key" goes on the one card every new hire reads, because every shift
  needs it and nothing else would show it.

Nobody writes "on the 12th the ovens stayed on" into the handbook. The handbook holds what the next baker does
differently, the notebook and the repair list hold what happened.

## Red flags

| You are writing | It means |
|---|---|
| A library name, a version, a file path, a log id in a skill | tool mechanics or a scar, not a discipline. Move it. |
| "Because on <date> a run did X" | an incident. The principle stays, the story goes to memory or the log. |
| A rule that only exists because of one bad run | a workaround wearing a rule's clothes. Guard the fault where fixes live. |
| A sentence that explains why the surface is organised this way | the author talking. Delete it. |
| A "lessons learned" section anywhere an agent reads | the wrong surface. Sort the lesson. |
| A procedure in AGENTS.md / CLAUDE.md | a skill. |
| A project fact in a skill | AGENTS.md / CLAUDE.md, or nowhere. |

## Related skills

- `writing-skills` writes the skill once this skill has chosen it.
- `writing-agents` writes the role.
- `agent-ux` is the register for anything an agent reads, on every surface above.
- `context-engineering` is why every surface has a budget and a load time.
- `documenting` (the studio plugin) is the knowledge base's own ceremony for ADRs, designs, plans and logs.
