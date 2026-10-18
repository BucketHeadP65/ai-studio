---
name: writing-agents
description: Use when creating or revising an agent definition, a Claude Code or Codex subagent, a harness custom agent or profile, or the role a workflow step file describes. Covers the role (identity and job, the skills it holds, the smallest tool set, model, budget), the routing description with examples, the report contract, and the never-do list. Complements writing-skills (what a role holds) and agent-ux (how anything the agent reads is written).
---

# Writing agents

## Iron Law

> An agent definition is a role, not a person. It says who sits there, what the one job is, what the role holds and can reach,
> what it may spend, and what it hands back. The craft the role needs is a skill it holds. How the system is built stays out.

## When NOT to use

A discipline that several roles share is a skill, write it with `writing-skills` and let the roles name it. A single prompt
for one turn is not an agent. The words a role reads on a tool or a result are `agent-ux`'s job.

## The role, in order

Write these sections, in this order, and nothing else.

1. **Identity and job.** Two or three sentences. Who this is and the one job it does, written the way a good coding agent's
   system prompt opens. No inventory of tools, no map of the system, no reasons.
2. **Skills held.** The disciplines the job needs, by name. A Claude Code subagent preloads them through `skills:`. A Codex
   agent names the relevant skills in `developer_instructions` and asks the agent to read them when needed. This is not
   automatic preloading. A harness role holds the libraries its project names. A workflow step carries the discipline in
   its own instructions until it can name a skill. Hold few. A role that holds everything reads nothing.
3. **The smallest tool set.** Only what the job needs, and each tool's reach stated where the tool is described. A reviewer
   reads and never writes. An implementer writes and never commits. Acts that change what runs next (run, message, schedule)
   belong to an orchestrator's role alone.
4. **Model and budget.** The model tier the job needs: judgment and design get the capable tier, transcription and mechanical
   fixes get the cheap one. A budget the role knows from its first turn, requests and tool calls. When it is nearly gone, the
   role answers with what it has. Running out never means approve.
5. **The routing description.** Third person. The trigger first, in the words the caller uses, then what the role produces.
   For a Claude Code subagent, `<example>` blocks with context, the user's line, the assistant's line, and a short commentary,
   so the router sees the shape of a delegation. The description is the only routing signal. Never summarise the body in it.
6. **The report contract.** What the role hands back, its shape, its length, and the three endings it may have: done, done
   with concerns, could not (blocked, or could not judge). A role that can only say done will say done.
7. **The never list.** No subagents unless the role is an orchestrator. No commits unless told. No change to what runs next
   from inside a step. No guessing where it stands, its root and its reach are stated in section 3.

## Checklist

- [ ] The identity opens the body and says the one job.
- [ ] Every skill named exists, and none of them is this role's own job restated.
- [ ] Every tool is needed by the job, and its reach is stated beside it.
- [ ] A reviewer holds reads only. Acts sit with the orchestrator alone.
- [ ] The model matches the judgment the job needs, not the default.
- [ ] The budget is in the definition, not in a warning.
- [ ] The description routes from three plausible caller messages, read cold.
- [ ] The report has three endings, and could not is one of them.
- [ ] No sentence explains the system or the design. If the role obeyed it, what would it do differently?
- [ ] The definition uses the target client's file format, available model and tools, and actual permission controls.

## Choose the client's definition format

Keep the role's prose reusable. Encode its configuration in the format the target client reads.

| Target | Project file | Role instructions and controls |
|---|---|---|
| Claude Code | `.claude/agents/<name>.md` | YAML frontmatter plus a Markdown body. `skills` preloads skills, `tools` selects available tools |
| Codex | `.codex/agents/<name>.toml` | `name`, `description`, and `developer_instructions`. Supported options include `model`, `model_reasoning_effort`, and `sandbox_mode` |
| Harness | The project's declared custom-agent or profile location | Inspect its schema and existing definitions before writing |

For Codex, place the role body in `developer_instructions`. A Claude frontmatter field such as `skills:` or `tools:`
is not a Codex configuration field. Use the client's supported sandbox and tool controls to enforce restrictions.
The role's prose describes its job but does not create a security boundary. Preserve the user's model choice and use
only model IDs and tools available in the target environment.

Check the current [Claude Code agent schema](https://code.claude.com/docs/en/sub-agents) or
[Codex agent schema](https://learn.chatgpt.com/docs/agent-configuration/subagents) when producing an installable definition.

## One canonical example

A role that checks tomorrow's menu before it goes to the printer. This example illustrates the role's prose, not an
installable client configuration. Wrap it in the chosen client's format, naming real tools, models, and any available skills.

```markdown
# Menu checker

Routing description: Use when a drafted menu needs a verdict before printing. Returns approval, corrections, or cannot judge. Read-only.

You check tomorrow's menu before it is printed. One job: say whether the draft can go to print as it is, and if not, what
the kitchen must change.

You can open the allergen register and look up any dish by name. You cannot change a dish, and you cannot stop the
printer, the head chef does both.

You have six questions to the kitchen. Read the whole draft before you ask one. When the questions are nearly gone,
answer with what you know.

Answer approve, send back, or cannot judge, and name each dish you mean. Approve nothing you did not read to the last
line. Answer cannot judge when the draft is blank, torn, or not a menu, and say what you saw.
```

## Counterexamples

| System-designer thinking | Role thinking |
|---|---|
| "You have the keys to the whole kitchen" | The register and the dish list, because checking needs two things |
| "This agent implements the verification stage of the menu pipeline" | "You check tomorrow's menu before it is printed" |
| "The kitchen enforces a question quota" | "You have six questions to the kitchen. When they are nearly gone, answer with what you know." |
| A description that walks through how to check a menu | A description that says when to hand the checker a menu and what comes back |
| approve or send back | approve, send back, or cannot judge |
| "Use your judgment about changing a dish if it seems wrong" | The chef changes dishes. The checker answers. |

## Related skills

- `agent-ux` for every sentence the role reads: tools, results, errors, instructions.
- `writing-skills` for the disciplines a role holds.
- `context-engineering` for why the role holds few tools and few skills.
- `designing-workflows` for which role a workflow step needs and what it passes on.
- `placing-knowledge` for what belongs in the role and what belongs elsewhere.
