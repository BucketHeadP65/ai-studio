---
name: writing-skills
description: Use when authoring a new skill or extending an existing one with new content, choosing whether a skill is the right deliverable, deciding who reads it, writing the routing description, structuring the body, picking the right navigation aid, and bundling supporting files. Encodes Anthropic's official skill-authoring best practices and the eval-driven authoring loop, and complements `context-engineering` and `agent-ux`.
---

# Writing Skills

## Iron Law

> **A skill that violates context-engineering principles is a context tax, not an asset.** Every skill description ships in the system prompt of every session forever. Its body loads on every invocation. Earn that space.

If you can't state in one line *what triggers it* and *what it changes about behaviour*, don't ship it.

**Required reading**: the [`context-engineering`](../context-engineering/SKILL.md) skill is the prerequisite. Skills are the canonical example of progressive disclosure done right (or wrong). Apply principles 1-4 (budget, periphery, just-in-time, progressive disclosure) to every skill you author.

---

## Who reads a skill

A skill is read by whoever holds the role that does the job: a subagent, a harness agent, or you when the job is yours
(you design workflows and write tests too). Write it for an expert stranger, someone who knows the craft and not this
repository. That is what standalone means: the reader applies the skill in another project without reading anything here.

The official guidance says it in two sentences. "State what to do rather than narrating how or why." "Only add context
Claude doesn't already have." So a skill carries principles, moves, checks and one example, in the reader's vocabulary, and
never:

- an incident or a "lesson learned" (the principle goes in, the story goes to memory or the log)
- a workaround for a defect (that is a guard or a fix, filed where fixes are filed)
- tool mechanics (they ride with the tool's own description)
- a library, a version, a file path, a log entry, a date
- a sentence about why the skill exists or how it is organised

Test every sentence: if the reader obeyed it, what would they do differently, and is that a design move or a dodge? Write it
the way `agent-ux` writes anything an agent reads. Not sure which surface a piece of knowledge belongs on? `placing-knowledge`
decides before you write.

---

## Phase 0: Decide if a skill is the right deliverable

Three choices for any reusable instruction:

| Artifact | Loaded | Use for |
|---|---|---|
| **Inline prompt** | Once | One-off task, this conversation only |
| **AGENTS.md / CLAUDE.md** | According to the client's instruction scope | Project-wide static facts: stack, conventions, commands |
| **Skill** | Description always. Body on selection | A *workflow* or *discipline* that fires on specific triggers |

**Don't write a skill when:**
- The content is static project knowledge: put it in AGENTS.md / CLAUDE.md (≤300 lines)
- It's a one-time instruction: just say it inline
- It's tool integration: use MCP or hooks
- The "trigger" is vague ("when working on the backend"): no auto-route will land it

**Write a skill when:**
- A multi-step procedure repeats across sessions
- A user keeps re-pasting the same playbook
- Auto-trigger on natural-language cues would save explicit invocation

### Generic & reusable by default

A skill captures a **pattern** (TDD, debugging, structured-output design, context engineering): a workflow that fires the same way in *any* project that has the trigger. If your skill body references "this codebase", "our SDK", a specific service name, or hard-coded file paths, you have written project knowledge dressed up as a skill. Move it to `CLAUDE.md` / `AGENTS.md`, where always-loaded project facts belong.

Defaulting to generic is not pedantry. It is what makes a skill compound. A generic skill works in next year's project. A `do-X-in-the-foo-service` skill is dead the day the foo service is decommissioned. And every project-specific skill loaded in your description budget is a tax paid by every unrelated session.

**Project-specific skills are a deliberate exception**, not a habit. They are legitimate when a project has a *workflow* (not a fact) that only makes sense in its domain: pedagogical workshops with bespoke pedagogy, an internal-tooling playbook, a release process unique to one product. When you write one:

- Put it inside the project: `.agents/skills/` for Codex, `.claude/skills/` for Claude Code. For a shared skill, use one source folder with relative symlinks from both discovery directories, preserving scripts and references together.
- Name the project clearly in the description (e.g. *"Use when ... in the onboarding workshop"*) so auto-routing can disambiguate.
- Make it deletable: when the project is archived, removing the skill must not affect any other work.
- Cross-link from the project-specific skill to whichever generic skills it composes with, so future readers find the reusable layer underneath.

If you find yourself writing a project-specific skill, double-check whether the lesson you are encoding is *really* project-specific or whether you have just not abstracted it yet. A pattern phrased generically (`workshop-demo-writing`) is more reusable than the same pattern phrased specifically (`onboarding-workshop-demos`), even when only one project uses it today.

### Research before encoding external knowledge

A skill that encodes external knowledge (a framework, an SDK, a vendor API, a CLI tool) is only as accurate as the docs it was written against. Before drafting or expanding such a skill:

- Read the current official documentation for the topic (via the relevant docs MCP server such as `context7` or `microsoft`, the vendor's site, or an installed reference skill family like `pydantic-ai-*`).
- Confirm version-specific API names, parameter signatures, and behaviour against the version the consuming project actually uses.
- Check for deprecations or migrations since the last time you touched the skill.
- Prefer a runnable example from the source over a recalled signature.
- **For tools that promise round-trip operations against an external service** (sync CLIs, bidirectional mirrors, "pull/push" pairs): fetch the service's OpenAPI spec (often at `<base_url>/v1/openapi.json` or `<base_url>/openapi.json`) and verify the **write** endpoints exist before designing the user-facing workflow around them. Read and write paths are routinely asymmetric: read endpoints are public, write endpoints are UI-only, or are gated behind a different scope or a different namespace prefix. Probing in the design phase costs one HTTP call. Discovering the asymmetry after building the push pipeline costs an afternoon and a rewrite.

Skills about an evolving tool drift the moment that tool rev's a major or minor version. Re-validate the skill's body against current docs whenever you notice the consuming project bumping its pin, and whenever you are about to add a new section that names specific APIs. The eval-driven loop below catches most drift only if the baseline being evaluated against is current.

The same discipline applies one level up: when authoring teaching material *built on* a framework (workshop demos, tutorials, blog posts), the same docs-first step belongs in the authoring loop. Reusable knowledge is only as good as the source it was last validated against.

### Discipline, workflow, or library

Not every skill is a peer. Three types, with different uses:

| Type | What it is | Discovery |
|---|---|---|
| **Discipline** | a methodology that fires when the situation matches (`context-engineering`, `agent-ux`, `designing-workflows`, this skill) | Match the situation through a precise description |
| **Workflow** | a procedure with phases and side effects (commits, file writes, dispatches) | Explicit invocation, so the caller chooses when the workflow runs |
| **Library** | a single-purpose reference or sub-discipline read on demand | A supporting file when only one workflow needs it. An explicitly invoked skill when independently useful |

Preserve an existing invocation policy when changing packaging or adding client support. Default reusable references
to libraries and reserve automatic selection for disciplines that should fire from a
natural-language trigger. Claude Code uses `/skill-name`. Codex uses `$skill-name`. For explicit-only invocation,
set `disable-model-invocation: true` for Claude Code and `policy.allow_implicit_invocation: false` in
`agents/openai.yaml` for Codex. These are client-specific controls, not interchangeable permission boundaries.

**A discipline only one workflow uses** is not a top-level skill. Put it as a supporting `.md` file inside that workflow's
directory and have the workflow read it on demand, which keeps its description out of the system prompt entirely.

---

## Anatomy of SKILL.md

### Frontmatter

The minimum that works:

```yaml
---
name: my-skill
description: Use when [trigger], to [outcome]. [Optional one-liner about what it encodes.]
---
```

Portable frontmatter (use only what you need):

| Field | Purpose | Notes |
|---|---|---|
| `name` | Identifier | ≤64 chars, lowercase letters, digits and hyphens. No leading, trailing or consecutive hyphens |
| `description` | **Routing signal** | ≤1024 chars, third-person, action verbs, trigger phrases |
| `metadata` | Optional descriptive metadata | Keep values relevant to the skill |

Put client-specific behavior in the owning client's configuration. Claude Code's `allowed-tools`, `user-invocable`,
`context: fork`, `agent`, `model`, `argument-hint`, and `hooks` do not configure Codex. Codex UI and invocation policy
belong in `agents/openai.yaml`. Its agent model, sandbox, and tool configuration belong in Codex agent/config files.
Check the current client's documentation before using optional fields. An unsupported field or a prose tool list is
not an enforced permission rule. Keep shared instructions executable with the tools the current client actually exposes.

### Body

> **Triggering info belongs in the description, NEVER in the body.** No `## Use this skill when`, `## When this skill fires`, `## When to use` sections. The description (always loaded) is the *only* routing mechanism. The body (loaded *after* invocation) is for the procedure. Restating triggers in the body is pure context tax. Every reader has already routed by the time they see the body.
>
> Anthropic's official `skill-creator` makes this explicit:
>
> > **description**: When to trigger, what it does. This is the primary triggering mechanism: include both what the skill does AND specific contexts for when to use it. **All "when to use" info goes here, not in the body.**
>
> A `## When NOT to use` section IS allowed. It disambiguates from sibling skills *during* invocation, which is a different job from triggering.

The shortest form that captures the discipline. Sections, each only if non-empty:

1. **Iron Law / core principle**: one quoted line, sets the foundation
2. **When NOT to use**: pre-empts misuse (the only allowed gating section)
3. **Navigation aid** *(required, see next subsection)*: the named anchor that matches the work's shape
4. **Rationalisations table**: `Excuse | Reality` rows, addresses how the agent will try to weasel out
5. **Red flags / STOP checklist**: present-tense observations of violation symptoms
6. **One canonical example**: beats five mediocre ones. Complete, runnable, with WHY comments. Invented and generic,
   intuitive enough to read as a small story (a menu checker, a mail room), never the real system's names or the
   incident that taught the lesson dressed up as an example
7. **Related skills**: breadcrumbs to compose with
8. **Sources**: primary references, no fluff

### Navigation aid: match the anchor to the work's shape

> **Every skill body must contain at least one named navigation aid that matches the dominant shape of the work it guides. Forcing the wrong shape (a procedural path on a decision skill, a decision tree on a discipline skill) produces fake steps or fake branches and degrades the skill.**

There are four shapes. Pick the one that fits the dominant work, then add a secondary aid only if the skill is genuinely hybrid.

| Shape | Primary aid | Heading convention | When to use | Examples |
|---|---|---|---|---|
| **Procedural**: linear or loop work | **Reasoning path**: numbered steps OR a named loop | `## Reasoning path`, `## Process`, `## The Loop`, `## Phases`, `## Authoring loop` | Steps must execute in order. Skipping breaks the discipline | `writing-agents` (the role, in order), `superpowers:test-driven-development`, `superpowers:brainstorming`, `superpowers:systematic-debugging`, `superpowers:executing-plans` |
| **Decision/reference**: "which primitive do I pick" | **Decision tree**: flowchart, dichotomous key, or branched table | `## Decision tree`, `## Picking the right X`, `## The X decision`, ASCII flowchart | Right choice depends on context the skill must surface for the agent | `placing-knowledge` (the surfaces table), `designing-workflows` section 3 (one worker, several turns, or its own node) |
| **Discipline/principle**: judgment-driven | **Principle catalogue + counterexamples table** (good vs bad, educational vs theatrical) | `## Principles`, `## Philosophy`, `## What separates X from Y` | No single procedure. Teaching the *bad* shapes is how the lesson lands | `context-engineering` (4 failure modes + 10 principles), `agent-ux` (11 principles + counterexamples), `writing-skills` (this skill) |
| **Pure reference**: API lookup | **Tables / signature catalogues** | `## Catalogue`, `## API reference`, `## Hooks lifecycle map` | Body answers "what's the API?", not "how do I think?" | `context-engineering/reference.md` (case studies and the tool-design playbook) |

**Hybrid skills** are common. Most skills have a dominant shape plus a secondary aid:

- `placing-knowledge` is dominantly *decision* (the surfaces table) plus a *red flags* table.
- `writing-agents` is dominantly *procedural* (the role, in order) plus a *counterexamples* table.
- `designing-workflows` is dominantly *discipline* (Iron Law + principles) plus a *checklist* before landing.

Pick the dominant shape first, name the section accordingly, then add the secondary aid below it.

**Reasoning-path discipline (for procedural skills)**: a reasoning path is more than a step list. It encodes *how the agent should think* through the work: what to search for first, what to verify, when to stop. Borrow from prompt engineering: `read inputs → form hypothesis → verify against evidence → act → confirm result`. Make the path concrete to the skill (the authoring loop below is `baseline → draft → eval → iterate → stress-test`), not generic.

**The anti-pattern**: a body that's just prose with neither a path, tree, principle catalogue, nor lookup table. The agent has nothing to anchor reasoning on under load and falls back to general behaviour, exactly what the skill was meant to prevent.

### Length targets

| Skill type | Target | Examples |
|---|---|---|
| Workflow / process | 300-900 words | `executing-plans`, `requesting-code-review` |
| Discipline / methodology | 1000-1600 words | `tdd`, `systematic-debugging`, `context-engineering` |
| Meta / reference | up to ~3000 words, with bundled files | `superpowers:writing-skills` |

If a skill body is over 500 lines, split mutually-exclusive paths into supporting files referenced one level deep.

---

## The Description: routing signal craft

For an automatically discoverable skill, the name and description are available before its body. **The description is
the routing signal.** If the skill does not appear, check the current client's skill list, discovery paths, and invocation
policy before changing the prose. Reopen the session if its listing is stale.

**Rules:**

1. **Third person.** "Use when…" or imperative. Never "I help you…".
2. **Front-load the use case.** First clause = the trigger. Last clause = optional extra context.
3. **Include the exact phrases users say.** If users say "PDF", the description must say "PDF", not "documents".
4. **Action verbs.** "Encodes", "Guides", "Validates", not "Handles", "Helps with".
5. **Don't summarise the workflow.** A description that summarises the procedure causes Claude to follow the description instead of reading the body.

**Bad becomes Good:**

| ❌ Bad | ✅ Good | Why |
|---|---|---|
| `Helps with skills` | `Use when creating, editing, or auditing a Claude skill — chooses the right artifact, writes the routing description, bundles files.` | Action verbs, concrete triggers |
| `Use for TDD — write test first, watch it fail, write minimal code, refactor` | `Use when implementing any feature or bugfix, before writing implementation code` | Don't summarise the workflow |
| `For testing` | `Use when writing or expanding unit/integration tests, designing testable code, or evaluating whether a test is over-coupled to implementation.` | Specific triggers, multiple entry points |

If you're stuck, write five descriptions and pick the one a stranger would route correctly without seeing the body.

---

## Progressive disclosure

A skill is a directory, not a file. Package assets the body references *only when needed*.

```
my-skill/
├── SKILL.md              # Entry point. ≤500 lines.
├── reference.md          # Heavy API or pattern reference. Loaded on demand.
├── examples.md           # I/O pairs. Loaded on demand.
├── playbook.md           # Long procedure. Loaded on demand.
└── scripts/
    ├── validate.py       # Executes, never enters context
    └── helper.sh         # Executes, never enters context
```

Reference one level deep: `SKILL.md → reference.md`. **Do not chain** `SKILL.md → advanced.md → details.md`. Claude may partial-read nested files.

Use scripts (`bash`, `python`) for deterministic operations. Tokens you don't spend on shape-shifting prose are tokens you keep for the actual problem.

Include trigger sentences in SKILL.md so Claude knows the file exists:

```markdown
For the full pattern catalogue, see [reference.md](reference.md).
For runnable examples, see [examples.md](examples.md).
Resolve `scripts/validate.py` beside this `SKILL.md`, then run `python3 /resolved/skill/scripts/validate.py` to verify.
```

---

## Authoring loop (eval-driven)

Skills are software. Write them like software.

1. **Baseline.** Pose 3 representative tasks to each supported agent client *without* the skill. Capture outputs.
2. **Draft.** Write the smallest SKILL.md that should change those outcomes.
3. **Eval.** Re-run the same 3 tasks with the skill active. Compare.
4. **Iterate.** If the skill didn't fire, fix the description. If it fired but didn't help, fix the body. If it over-fired, narrow the description.
5. **Check the clients and models your work uses.** Verify discovery, invocation, supporting paths, and any client-specific behavior as well as the quality of the response.

Use `superpowers:dispatching-parallel-agents` for parallel evaluation runs.

### Sourcing verbatim quotes when rewriting an existing skill

When the skill encodes external knowledge (a research paper, a vendor article, an SDK changelog), the body's strength is in **verbatim quotes + concrete numbers + runnable examples**, not in the author's recall. Dispatch one sub-agent per source and ask each for raw material (quotes, examples, methodology, numerical claims), then synthesise. The coordinator's context stays uncluttered. Partial returns from a failing agent are still useful.

A self-contained brief that returns useful material:

```text
I'm rewriting the `<skill-name>` skill that needs concrete examples and quotes
from <SOURCE>. Please WebFetch <URL> and extract:

1. AT LEAST 8–12 verbatim quotes (with quote marks) on the load-bearing claims.
2. Concrete examples — code snippets, before/after pairs, scenarios with numbers.
3. Specific numerical claims — token counts, percentages, performance numbers,
   with conditions.
4. Direct "do this / don't do this" patterns.
5. Vivid metaphors / mental models the source uses.

Return everything in a single structured response — under 3000 words.
Cite each quote's source URL.
```

The output is raw material: quotes inline, numbers inline, examples inline. Synthesis (Iron Law, navigation aid, body shape) stays the coordinator's job because it requires seeing all sources at once. **Pre-validate any verbatim claim against the source before lifting it.** Sub-agents sometimes paraphrase under WebFetch fair-use limits and label the result "verbatim".

---

## Anti-patterns

| Anti-pattern | Why it bites | Fix |
|---|---|---|
| Description that summarises the workflow | Claude follows the description instead of reading the body | State the trigger only. Let the body teach the procedure |
| Long laundry-list system instructions | Buries critical guidance in the middle | One canonical example > five rules |
| Nested `@file.md` references | Claude partial-reads | Keep references one level from SKILL.md |
| Time-sensitive content ("as of 2025…") | Goes stale, confuses future readers | Use timeless phrasing. Mark deprecations explicitly |
| Many overlapping skills | Routing collapses. Auto-invoke picks wrong one | Merge or distinguish descriptions sharply |
| Body over 500 lines without supporting files | Loaded body itself becomes the context tax | Split into reference files |
| Magic numbers in bundled scripts without comments | Future-you can't tell why `30` was chosen | Document every constant: `TIMEOUT = 30  # slow networks` |
| Hard-coded client directories or implicit working directory | Scripts and references fail in another client or checkout | Resolve supporting paths beside the loaded `SKILL.md`, pass the project root explicitly when needed |
| Description in first person | Reads as fluff, doesn't trigger | Third-person imperative |
| `## Use this skill when` / `## When this skill fires` in the body | Restates the always-loaded description. Pure duplication for a body that only loads *after* routing already happened | Move all triggering info into `description`. Keep only `## When NOT to use` for sibling-skill disambiguation |
| New skill that duplicates an existing one | User confusion, divided attention | Search existing skills first. Extend rather than fork |
| No named navigation aid in the body | Agent reads prose under load, finds no anchor, falls back to general behaviour, defeats the skill | Add a named section that matches the work's shape (path / tree / principles+counterexamples / catalogue) |
| Procedural skill with a flowchart instead of numbered steps | Agent treats steps as optional branches | Procedures get numbered steps or a named loop. Flowcharts are for decision skills |
| Decision skill with numbered steps instead of branches | Agent walks the steps in order even when they don't apply | Decision skills get a tree or branched table |

---

## Pre-flight checklist

Before committing a new skill:

- [ ] **Search first.** Does an existing skill cover this? If yes, extend it. Don't duplicate.
- [ ] **Description routes correctly.** Read 3 plausible user messages: does the description make Claude pick this skill?
- [ ] **Description ≤1024 chars, third-person, includes user-language triggers.**
- [ ] **No triggering sections in the body** (no `## Use this skill when` / `## When this skill fires` / `## When to use`). Triggers live in `description` only. The body may keep `## When NOT to use` for sibling-skill disambiguation.
- [ ] **Iron Law in the first 5 lines** of the body.
- [ ] **Navigation aid present and matched to work shape.** Procedural gets named reasoning path / loop. Decision/reference gets decision tree / branched table. Discipline gets principle catalogue + counterexamples. Pure reference gets lookup table / signature catalogue. Hybrid gets dominant aid first, secondary below.
- [ ] **One canonical example** if the skill teaches a pattern.
- [ ] **"When NOT to use" section** if misuse is plausible.
- [ ] **Body ≤500 lines.** If longer, supporting files are in place.
- [ ] **No nested file references.** SKILL.md links to file.md, never file.md links to file2.md.
- [ ] **Client compatibility.** Every supported client discovers the same source. Scripts and relative references resolve. Optional metadata is configured for the client that implements it.
- [ ] **No time-sensitive language** that will rot.
- [ ] **Cross-link to related skills** at the end.
- [ ] **Sources** for any non-obvious claim.
- [ ] **Eval against 3 tasks** showed measurable behaviour change.

---

## Related skills

- **`context-engineering`**: the prerequisite. Every skill should respect its principles, especially progressive disclosure and "front-load complete specs".
- **`agent-ux`**: the register: every sentence a skill's reader obeys is UI copy, written from the reader's role.
- **`placing-knowledge`**: decides whether what you hold is a skill at all, or a memory, a log entry, an agent's role, or a line in CLAUDE.md.
- **`writing-agents`**: the role that holds the skill: identity, tools, budget, report contract.
- **`superpowers:writing-skills`**: the official, more comprehensive (~3200 words) version with bundled `anthropic-best-practices.md` and `testing-skills-with-subagents.md`. Read it when authoring a complex skill.
- **`superpowers:test-driven-development`**: the eval-driven authoring loop above is TDD adapted for prompts.

---

## Sources

- [Anthropic: Skill authoring best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
- [Anthropic: Equipping agents for the real world with Agent Skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills)
- [Anthropic: Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Claude Code Skills Documentation](https://code.claude.com/docs/en/skills)
- [Codex Skills Documentation](https://learn.chatgpt.com/docs/build-skills)
- [skill-creator on GitHub](https://github.com/anthropics/skills/blob/main/skills/skill-creator/SKILL.md)
- [Anthropic Skills Repository](https://github.com/anthropics/skills)
