---
name: agent-ux
description: Use when designing, naming, or reviewing anything an agent consumes (tools, toolsets, capabilities, instructions, result shapes, MCP surfaces, error messages). The agent is the user. Design its world like a pro UX designer, not like the system's architect.
---

# Agent UX: the agent is the user

## Iron Law

> **The agent is the user. Every tool name, description, result shape, instruction block, and error message is UI copy.**
> If a competent professional dropped into the agent's role would ask "what is this? why is it called that? which of these do I use?",
> the design is wrong. Faithfulness to the system's internal architecture is not a defense.

An agent-facing surface (tools, skills, instructions, errors, warnings) never tries to mirror the system's internal architecture.
It reflects only those aspects of the architecture the agent needs to use the tools and perform its job, modeled so the agent
understands its **resources** (what it can reach), **capabilities** (what it can do), **constraints** (budgets, caps, permissions),
and **requirements** (what its job demands). Everything else about the system is the implementer's business and stays out of the
agent's world.

## The principles

1. **Verbs, not protocols.** Agents get a small set of task verbs (`search`, `fetch`, `run`, `check`). A mechanism you built
   (disclosure, deferral, budgeting, caching) is never a tool name. If the mechanism changes what an existing verb returns, put it
   *inside* that verb. Don't mint a sibling. Ask: "would a person want a second button for this, or the same button behaving better?"

2. **The result teaches the next move.** A well-shaped result carries its own affordances: a listing carries the ids you'd fetch
   next. A truncated result says what was cut and how to get it. A budget stop names the exit ("narrow with search"), not just the
   refusal. The agent should never need out-of-band knowledge to know what it *can do* with what it just received.

3. **Descriptions are UI copy, in the agent's language.** A tool description says what you get, when to reach for it, and how it
   composes with its siblings ("start here, then fetch a hit's id"), never implementation provenance ("conformant with X",
   "backed by Y", spec/ADR names). Write it the way you'd write a tooltip for a professional, then cut a third.

4. **Layer instructions by ownership.** Core instructions = the agent's identity and craft (who it is, how it acts), stable,
   written like a good coding agent's system prompt. Tool mechanics arrive with the tool (its description) or the capability (its
   instruction block). Never leak one layer into another: identity prose that inventories tools rots. Tool blocks that philosophize
   about the system waste every request's tokens.

5. **One mental model, reused.** Before inventing an interaction pattern, find what the agent's framework already teaches and rhyme
   with it (pydantic-ai: catalog + one loader for visible-in-summary things, search for invisible things, `ModelRetry` with an
   actionable message for recoverable misses, result-shaped stubs for budget stops). A pattern the agent has already learned costs
   zero instructions.

6. **Accept what the agent already holds.** Ids from search results, refs from listings, bare names: every form the agent could
   plausibly have must work at the plugin (normalize inside, never demand the agent reformat). An error for a form you could have
   accepted is a design bug, not an agent mistake.

7. **Errors are conversations.** Every failure message answers: what happened, is retrying useful, what to do instead, in one
   sentence, imperative ("Re-ground the id with `search` and try again"). A refusal that just states the rule teaches nothing.
   Silence (empty result where an error belongs) teaches something false. The system's own corrections (an output validator's
   retry, a budget notice) must read as the system speaking, never as the person: a model that mistakes a validator for the owner
   answers the wrong party, reading a JSON parse error as something the person just said.
   Prefer a surface that needs no correction (plain-text answers plus a tool for structured hand-offs) over a format the model
   must be nagged into.

8. **Name from the agent's role, test from the agent's role.** Read the tool listing as the agent will see it: names, one-line
   descriptions, parameter schemas, nothing else. If two entries could be confused, or one requires knowing your architecture to
   pick, redesign. The final gate: run a real session and watch which tools the agent reaches for. Adoption failure is a UX finding,
   never a model deficiency to prompt around. The same gate applies to instructions: the prompt you composed is not the prompt the
   model receives (the framework may reorder your layers, e.g. literal instruction strings render ahead of function-shaped ones),
   so read one rendered request and check the identity still speaks first, with tool and capability blocks after it.

9. **No author in the room.** The surface never explains its own construction. "What is written here is only what no single
   surface can say for itself" is the author defending a refactor to the diff reviewer, smuggled into UI copy the agent pays for
   on every request. The agent cannot see your file layout or your capability split, so a sentence about them changes nothing it
   does. This is **rationale leakage**: the writer produces a change and its justification in one breath, and the nearest writable
   surface is the artifact itself, so the prompt ends up doubling as the PR description. The test for every sentence on an
   agent-facing surface: *if the agent obeyed this sentence, what would it do differently?* No answer means it is the author talking.
   The rationale moves to the commit or the ADR and the sentence is deleted. (Same law as "docstrings are contracts": the artifact
   carries the contract, never the paperwork.)

10. **Situate the agent.** Its role has a place and a reach: the shell's working directory, the files plugin's root, the knowledge
    bases it can search. State them where the plugin is described, in the plugin's own block, so the agent never probes (`pwd`, `ls -la`) or
    guesses (a made-up home directory) to learn where it stands. A reach that stops short of the job (a root that excludes the file
    the owner just named) is a design defect, not a model mistake. An agent left unsituated runs `find` over a home
    directory it invented before it thinks to ask where it stands.

11. **What the agent lived through stays in its memory.** A turn that fails or is cut off is still the agent's experience. The next
    turn's history carries it, closed with what happened (the call that was never answered, the error that ended the turn), so the
    agent can reason from it and the owner's next message lands on the conversation they both saw. Erasing the turn teaches the
    agent it never happened, and it repeats the mistake or contradicts what the owner just watched it say.

## Design checklist (run before shipping any agent-facing surface)

- [ ] Could this new tool be an improvement to an existing verb's result instead?
- [ ] Does every result carry the handle/affordance for the natural next action?
- [ ] Do descriptions name sibling tools and the order of use, in plain language, zero internal vocabulary?
- [ ] Are core instructions identity-only, with mechanics delivered by tool/capability blocks?
- [ ] Does every input form the agent might hold get accepted and normalized?
- [ ] Does every error say what to do next, and is retrying honest (works or explicitly futile)?
- [ ] Have you read the full tool listing cold, as the agent sees it?
- [ ] Have you read one rendered request and confirmed the identity opens the prompt, tool/capability blocks after it?
- [ ] Does every instruction sentence change what the agent would do, no design apologia, no prose about how the prompt is organized?
- [ ] Have you watched one live run and treated non-adoption as a design defect?
- [ ] Does the rendered request say where the agent runs and how far each plugin reaches, beside the plugin itself?
- [ ] After a failed or cut-off turn, does the next turn's history still carry it, closed with what happened?
- [ ] Can any system correction (validator retry, budget stop) be mistaken for the person's voice?

## Counterexamples: same machinery, two designs

| ❌ System-designer thinking | ✅ Agent-as-user thinking |
|---|---|
| `disclose(ref)` beside `search`/`fetch` | `fetch(id)` returns content + child summaries, no new tool |
| "Progressive disclosure: return this node's envelope…" | "Read a unit by id… call with no id to open the map" |
| "## Progressive disclosure" instruction block | "## Leveled reads … every level costs tokens" |
| Error: `BadRefError: ref did not match grammar` | "No unit found with id 'x'. Re-ground it with `search`." |
| Instructions explain the system's architecture | Instructions state the job, tools explain themselves |
| Budget stop raises an exception mid-run | Budget stop returns a result-shaped notice naming the exit |
| "This run's budget is 20000 tokens" announced upfront | The constraint speaks when it binds: the read that spends the budget returns the notice |
| "What is written here is only what no single surface can say for itself" | Open with the job: "You are a coding agent." |
| The agent runs `pwd` and `find /Users/<guess>` to learn where it is | The files plugin's block says "File paths are relative to /path, nothing outside it is reachable." |
| A failed turn vanishes, the next turn starts from the last success | The failed turn stays, its open call closed with "not answered, the turn ended here (error)." |
| The output validator says "Invalid JSON: expected value at line 1" and the model answers the "user" | Plain-text answers plus a `settle_draft` tool, nothing to validate |
