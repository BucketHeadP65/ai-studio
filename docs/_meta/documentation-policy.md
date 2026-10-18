---
id: meta.documentation-policy
aliases: [meta.documentation-policy]
type: meta
title: Documentation policy
---
# Documentation policy

## spec-doc
The ratified what and why, design.md in the initiative folder, written when the direction is agreed. Approved means build on it.

### Problem
The gap in one or two paragraphs, what is missing or wrong today and for whom. No solution language.

### User Stories
As-a, I-want sentences per actor. Drop the section when the work has no user-facing surface.

### Acceptance Criteria
Checkboxes, each independently verifiable by a command or an observable behavior. No vague verbs.

### Constraints
The rules the implementation must not break, compatibility, conventions, platforms, budgets.

### Decisions
The ratified choices, numbered D1, D2, each with the reason it won over its alternative.

### Out of Scope
What was considered and deliberately excluded, so a later reader does not reopen it.

## plan-doc
The task-by-task implementation plan, plan.md in the initiative folder, written from the approved design.

### Goal
One sentence, what exists when the plan is done.

### Global Constraints
The design's binding rules copied verbatim, one line each. Every task inherits them.

### Tasks
Bite-sized tasks a fresh implementer can execute, files, interfaces, steps with exact code, test commands, a commit per task.

## architecture-doc
The how, architecture.md in the initiative folder, only when the design needs its own structural document.

### Scope
Which parts of the system this touches and which it leaves alone.

### Modified Files
A table, file, change, purpose. Every file the work creates or edits.

### Component Design
The pieces and their interfaces, what each does, how they talk, what data crosses. A diagram when it clarifies.

### Integration Points
Where this meets existing systems, APIs consumed, events emitted, contracts honored.

### Decisions
Structural choices with reasons, numbered like the design's.

## brainstorming-doc
The ideation record, analysis.md in the initiative folder, written while exploring, before a design is ratified. Only when the work needs exploration.

### Context
What prompted this and what is true today. Name the repos, docs and prior decisions the reader needs.

### Findings
What investigation established, one fact per bullet, each verifiable in the code or docs it names.

### Options
The routes considered with their trade-offs and which is favored. Rejected options say why they lost.

### Risks and Open Questions
What could invalidate the direction and what is still undecided. Park questions here rather than deciding silently.

### Out of Scope
What this initiative deliberately does not touch.

## adr
One decision per ADR, written as proposed for the owner to accept. Name the design it absorbs in `absorbs` and cite the initiative folder in Context. The title names the decision in plain words, with no ADR prefix and no number, the id and the file carry those. Plain punctuation throughout, no em dashes, no semicolons.

### Context
The situation that forced a decision, in the present tense, with the constraints that ruled options out.

### Decision
The choice, stated once, with the reason it won over its alternative. Numbered D1, D2 when a design carried several.

### Consequences
What becomes easier, what becomes harder, and what the repo must keep doing to honor the decision.
