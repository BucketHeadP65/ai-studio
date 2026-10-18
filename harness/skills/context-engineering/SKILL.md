---
name: context-engineering
description: Use when designing prompts, agent workflows, tool sets, or skills, when an agent repeats actions, ignores instructions, picks the wrong tool, drifts over long sessions, or when a context window is bloating. Encodes the four failure modes (poisoning, distraction, confusion, clash), the four operating strategies (write, select, compress, isolate), and ten principles for treating LLM context as a finite, attention-budgeted resource.
---

# Context Engineering

## Iron Law

> **"good context engineering means finding the *smallest possible set of high-signal tokens* that maximize the likelihood of some desired outcome."** (Anthropic, *Effective Context Engineering for AI Agents*)

If you remember nothing else: **fewer, denser, peripheral.** Cut tokens. Make the ones left dense with signal. Place the load-bearing ones at the start or end, never the middle. Before asking "what would I add?", ask "what would I cut?".

---

## When NOT to use

- Static project facts (stack, conventions, commands): put in `CLAUDE.md`/`AGENTS.md`, not a skill.
- One-off task instructions: say it inline.
- Tool integration: MCP or a hook, not a context-engineering rewrite.
- "The model just got it wrong once": that's prompt engineering or eval, not context engineering. This skill fires when context **shape** is the problem.

---

## Vocabulary: the mental models worth memorising

These four framings, all from Anthropic, do most of the work. Internalise them before reading anything else.

| Term | Verbatim | Practical meaning |
|---|---|---|
| **Attention budget** | *"LLMs have an 'attention budget' that they draw on when parsing large volumes of context. Every new token introduced depletes this budget by some amount."* | Tokens cost attention quadratically (n² pairwise relationships). Doubling context more than doubles the work *per token*. |
| **Context rot** | *"as the number of tokens in the context window increases, the model's ability to accurately recall information from that context decreases."* | The advertised window is not the usable window. Plan around degradation, not the spec sheet. |
| **Goldilocks altitude** | *"The optimal altitude strikes a balance: specific enough to guide behavior effectively, yet flexible enough to provide the model with strong heuristics."* | Don't hardcode every edge case (brittle). Don't write vague platitudes (no signal). Aim for the band where the model can generalise. |
| **Just-in-time** | *"agents built with the 'just in time' approach maintain lightweight identifiers (file paths, stored queries, web links, etc.) and use these references to dynamically load data into context at runtime using tools."* | Pass an ID, not the whole document. Mirrors how humans use file systems and bookmarks. |

Andrej Karpathy's complementary framing: *"the delicate art and science of filling the context window with just the right information for the next step."* (LangChain attribution.)

---

## The Four Failure Modes

Every long-context failure you will see in production maps to one of these. The Breunig taxonomy is canonical. The verbatim definitions are below, with the cited evidence and the corresponding fix.

| Mode | Verbatim definition (Breunig) | Symptom you'll see | Cited evidence | Primary fix |
|---|---|---|---|---|
| **Poisoning** | *"a hallucination or other error makes it into the context, where it is repeatedly referenced."* | Agent pursues impossible goals, cites constraints that never existed | Gemini 2.5 technical report: Pokémon agent corrupted goal state | Restart with clean window, correct the source (system prompt, retrieved doc), quarantine in a sub-agent |
| **Distraction** | *"a context grows so long that the model over-focuses on the context, neglecting what it learned during training."* | Agent repeats prior actions instead of synthesising fresh, loops | Gemini Pokémon onset **>100K tokens**, Databricks: Llama 3.1 405b degrades at **~32K** | Compact / summarise, offload state to files, spawn sub-agent with fresh window |
| **Confusion** | *"superfluous content in the context is used by the model to generate a low-quality response."* | Picks the wrong tool on a routine task, conflates similar APIs | Berkeley FCL: every model degrades past 1 tool, **Llama 3.1 8B fails at 46 tools, succeeds at 19** even though all fit in 16K | Prune the tool setup, rewrite descriptions to be non-overlapping, RAG over tool descriptions if you genuinely need many |
| **Clash** | *"you accrue new information and tools in your context that conflicts with other information in the context."* | Contradicts earlier decisions, outputs inconsistent across turns | Microsoft+Salesforce sharded-prompts: **−39% average**, **o3 dropped 98.1% to 64.1%** when sharded | Front-load the complete spec at turn one, consolidate state at decision points |

The same one-page mapping with concrete fixes lives in [reference.md](reference.md): case studies of each mode and which of the four operating strategies (below) applies.

---

## The Four Operating Strategies

LangChain's synthesis of the production patterns. Every concrete fix in this skill is one of these four (or a blend).

1. **Write**: save context outside the window (scratchpad, NOTES.md, long-term memory store). Frees the active window without losing state.
2. **Select**: pull the right thing in at the right time (RAG over docs, RAG over tools, scratchpad reads).
3. **Compress**: keep only the tokens that earn their keep (summarisation, trimming, structured pruning like Provence).
4. **Isolate**: split work across sub-agents or sandboxes so each operates on a clean window.

> "Production agents typically blend all four." (LangChain, *Context Engineering for Agents*)

---

## The Ten Principles

### 1. Treat context as a budget, not a buffer

Quadratic attention means every token costs more than the last one. Anthropic puts it bluntly: *"Context, therefore, must be treated as a finite resource with diminishing marginal returns."* The right reflex is *"what would I cut?"*, not *"what would I add?"*.

### 2. Position matters: periphery, not middle

Lost-in-the-Middle (Liu et al., 2024) showed a U-shaped recall curve: models attend best to the start and end of context. Critical instructions, the current task, and load-bearing constraints belong at the periphery. The middle is the death zone.

✅ Place the spec at the top **and** restate the current goal at the bottom of your message.
❌ Bury the actual question in paragraph 7 of a 14-paragraph prompt.

### 3. Just-in-time over eager loading

Don't paste a whole file when a snippet will do. Don't load a doc "in case." Pass an identifier (path, URL, ID, ticket number) and let the agent fetch on demand.

✅ *"The full report is at `reports/q3.md`, read it if you need section 4."*
❌ Pasting all 12K tokens of `q3.md` into the prompt, then asking a 1K-token question about section 4.

### 4. Progressive disclosure for skills, references, and reference material

Three tiers (this is the Anthropic skill model exactly):

- **Always loaded:** name + description (~50 tokens per skill)
- **Loaded on selection:** SKILL.md / reference doc body (≤500 lines)
- **Loaded on demand:** bundled files / scripts the body explicitly references

Keep `AGENTS.md` / `CLAUDE.md` ≤300 lines and `SKILL.md` ≤500 lines. Split mutually-exclusive paths into separate referenced files.

### 5. Front-load specs, do not drip-feed requirements

> *"LLMs often make assumptions in early turns and prematurely attempt final solutions, which they then over-rely on."* (Microsoft+Salesforce sharded-prompts study, paraphrased, 2025)

A complete spec at turn one is materially better than three turns of clarification. The sharding study found **39% average degradation** on multi-turn assembly. **o3 fell from 98.1% to 64.1%** when the same spec was distributed across turns instead of stated up front. If you find yourself answering *"and another thing…"* repeatedly, stop and re-state the full spec.

### 6. Isolate sub-tasks in sub-agents with clean windows

> *"Each subagent might explore extensively, using tens of thousands of tokens or more, but returns only a condensed, distilled summary of its work (often 1,000-2,000 tokens)."* (Anthropic)

Anthropic's multi-agent research system reported **+90.2%** vs. single-agent Claude Opus 4, at **~15 times the token cost**. Use sub-agents when parallel exploration earns the spend (broad search, large file audits), not for every multi-step task. Poorly suited where agents must share fast-evolving state.

### 7. Offload working state to files, not to context

A `NOTES.md` or scratchpad persists coherence across many tool calls without consuming the active window. Claude Code uses a to-do list. Anthropic's Pokémon agent maintained tallies *"for the last 1,234 steps I've been training my Pokémon in Route 1."* Anthropic's "think" tool reported up to **+54%** on long-horizon, policy-heavy, sequential-decision tasks.

✅ Tool: `record_decision(decision, reasoning, file="NOTES.md")`, agent self-summarises critical state.
❌ Repeating the same state in every turn's prompt because "the model might forget."

### 8. Prune the tool setup, favour non-overlapping descriptions and semantic returns

> *"If a human engineer can't definitively say which tool should be used in a given situation, an AI agent can't be expected to do better."* (Anthropic, *Writing tools for agents*)

The numbers from the Berkeley Function-Calling Leaderboard and follow-on work:

- Past **~30 tools**, descriptions begin to overlap and the model's selection accuracy drops.
- Past **~100 tools**, models are *"virtually guaranteed to fail"* on selection.
- Llama 3.1 8B: **46 tools fails, 19 succeeds** in the same 16K window.
- The "Less is More" paper reports **+44% accuracy, −18% power, +77% speed** with dynamic tool selection.
- Applying RAG to tool *descriptions* alone has been reported to triple selection accuracy (LangChain).

Operationally: aim for ≤~20 tools in any one agent's setup. If two tools' descriptions could plausibly answer the same prompt, neither will be picked reliably. Tools should return semantic identifiers (`name`, `file_type`, `path`), not raw UUIDs or MIME types.

### 9. Compact carefully: recall first, precision second

> *"Start by maximizing recall to ensure your compaction prompt captures every relevant piece of information from the trace."* (Anthropic)

Compaction is lossy. *"overly aggressive compaction can result in the loss of subtle but critical context whose importance only becomes apparent later."* Never auto-summarise away anomalies, decisions, or surprises. Preserve verbatim:

- Decisions made and *why*
- Constraints discovered the hard way
- Tool errors that surprised the agent
- Anomalies the user explicitly cared about

The lightest-touch form is **tool-result clearing** (drop old tool outputs, keep the assistant's reasoning). Claude Code's auto-compact triggers at **95%** of the window, a reasonable default.

### 10. Diverse canonical examples beat exhaustive rules

> *"teams will often stuff a laundry list of edge cases into a prompt in an attempt to articulate every possible rule the LLM should follow for a particular task. We do not recommend this."* (Anthropic)

A few well-chosen examples convey behaviour better than a wall of `if/else`. If you find yourself enumerating exceptions, you're writing brittle prompt logic, encode the *pattern*, not every case. Anthropic's recommendation: *"curate a set of diverse, canonical examples that effectively portray the expected behavior."*

---

## Tool design: the second context surface

Tool descriptions live in the system prompt and are loaded every turn. They are context too. Anthropic's *Writing tools for agents* gives the playbook:

| Rule | Verbatim | Concrete |
|---|---|---|
| **Few, thoughtful, high-impact** | *"build a few thoughtful tools targeting specific high-impact workflows."* | Don't 1:1-wrap every API endpoint |
| **Consolidate workflows** | Replace `list_users` + `list_events` + `create_event` with `schedule_event` | One tool per *user intent*, not per *resource* |
| **Namespace by service / resource** | *"Namespacing tools by service (e.g., `asana_search`, `jira_search`) and by resource (e.g., `asana_projects_search`, `asana_users_search`)."* | Prevents collisions and disambiguates picks |
| **Unambiguous param names** | *"instead of a parameter named `user`, try a parameter named `user_id`."* | The model has to reason about ID provenance |
| **Semantic returns** | *"Merely resolving arbitrary alphanumeric UUIDs to more semantically meaningful language significantly improves Claude's precision."* | Return `name="alice@..."`, not `uuid="b3f4..."` |
| **Pagination + truncation defaults** | *"pagination, range selection, filtering, and/or truncation with sensible default parameter values."* | Default to small pages, let the agent ask for more |
| **Dual response formats** | *"You can enable both [natural language and technical identifiers] by exposing a simple `response_format` enum parameter."* | `concise` vs `detailed`, Slack tools used **~⅓** of tokens with `concise` |
| **Actionable errors** | *"prompt-engineer your error responses to clearly communicate specific and actionable improvements, rather than opaque error codes or tracebacks."* | Errors are the model's only learning signal mid-run |
| **Cap response size** | Claude Code defaults to **25,000 tokens** per tool result | Prevent a single tool call from poisoning the window |

---

## Decision Cookbook

| Situation | Right move |
|---|---|
| Need to reference a large file or doc | Pass the path, not the contents. Read on demand. |
| Tool setup creeping past 20 | Group tools into separate agents/skills, or RAG over descriptions. |
| Agent looping on the same action | Distraction: compact or restart with a clean window. Add a `NOTES.md`. |
| Adding a new instruction late in a long session | Repeat it at the bottom of the latest message, the periphery wins. |
| Building a long-horizon agent (hours of work) | Sub-agent for noisy retrieval, coordinator stays clean, persistent scratchpad. |
| Spec keeps growing across turns | Stop. Restate the full spec at turn one in a fresh window. |
| Hallucination has entered context | Poisoning: restart, don't try to "argue it out." Fix the source doc/prompt. |
| New rule has many edge cases | One canonical example > five rules. |
| Skill description routing badly | Front-load the use case, include exact trigger phrases users say, third-person. |
| Long output you'll quote later | Save to a file, reference the path on next turn. |
| Need data from a third-party API | Wrap as a tool with semantic returns and `concise`/`detailed` formats. |

---

## Good vs Bad: counterexamples

The discipline is easier to recognise in pairs.

| ✅ Earns its keep | ❌ Junk drawer |
|---|---|
| `read_file("reports/q3.md", section="4")` returns the section the agent asked for | Pasting all of `q3.md` into the prompt because "it's relevant background" |
| Tool returns `{name: "alice@example.com", role: "owner"}` | Tool returns `{uuid: "b3f4-…", type_id: 7}` and a separate `lookup_role(7)` tool |
| `schedule_event(attendee, slot, room)`, one tool per intent | `list_users`, `list_events`, `create_event`, `find_room`, `list_rooms`, five tools per workflow |
| Fresh sub-agent with a 2-paragraph brief and a single deliverable | Coordinator agent that loops with the same 80K-token history for an hour |
| Spec restated in full at turn 1 of a new window | Drip-feeding "and another thing, also handle X" across 12 turns |
| `NOTES.md` updated with `decision: chose Postgres because…` | Agent repeating its own conclusions in every turn's preamble "to remember" |
| 18 well-named, non-overlapping tools | 47 tools where `search_users` and `find_people` both exist |
| Compaction prompt: *"preserve every decision, constraint, and surprise verbatim"* | Compaction prompt: *"summarise the conversation"* (loses anomalies) |
| Description: *"Use when designing prompts, agent workflows…"* | Description: *"Helps with context"* |

---

## Red Flags

If you see any of these in a running session, the context is leaking signal, apply the relevant principle.

- [ ] Pasting full files when a 10-line snippet would do (Principle 3)
- [ ] Repeating the same instruction across turns (*"remember, X"*), front-load instead (Principle 5)
- [ ] Tool setups over ~20 tools with overlapping descriptions (Principle 8)
- [ ] System prompt enumerating every edge case instead of showing examples (Principle 10)
- [ ] Agent burning turns re-discovering things it already knew (Principle 7, offload to file)
- [ ] Compaction that drops the *one* anomaly the user cared about (Principle 9)
- [ ] `AGENTS.md` / `CLAUDE.md` > 300 lines / `SKILL.md` > 500 lines without supporting files (Principle 4)
- [ ] Multi-turn drip-feeding of requirements when the spec was knowable up front (Clash, Principle 5)
- [ ] Tools returning UUIDs / MIME types / opaque IDs instead of names (Tool design)
- [ ] Critical instruction buried in the middle of a long prompt (Principle 2)
- [ ] Sub-agent spawned for a task one agent could do, paying 15 times the tokens for nothing (Principle 6)
- [ ] An agent's history references "facts" you cannot find in any source: poisoning, restart

Each red flag means step back and apply the named principle.

---

## Related Skills

- **`writing-skills`**: every skill is a context-engineering artifact. Description always loaded, body on selection, bundled files on demand. A skill that violates these principles is a context tax, not an asset.
- **`agent-ux`**: how anything an agent reads is written: tool descriptions, instructions, results, errors. Context engineering decides what enters the window, agent-ux decides how it reads once there.
- **`writing-agents`**: the role an agent holds: identity, skills, the smallest tool set, budget, report contract. Principle 8 (prune the tool setup) applied to one agent definition.

---

## Further reading

- [reference.md](reference.md): extended case studies (Pokémon agent, sharded-prompt study, Berkeley FCL), Anthropic's three long-horizon techniques (compaction / structured notes / sub-agents), the full tool-design playbook with code patterns, and the strategy-to-failure mapping table.

---

## Sources

- [Anthropic: Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Anthropic: Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
- [Anthropic: Building effective agents](https://www.anthropic.com/research/building-effective-agents)
- [Anthropic: Equipping agents for the real world with Agent Skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills)
- [Liu et al.: Lost in the Middle (TACL 2024 / arXiv:2307.03172)](https://arxiv.org/abs/2307.03172)
- [Chroma Research: Context Rot (18 LLMs, 194,480 calls, 4 controlled experiments)](https://www.trychroma.com/research/context-rot)
- [Drew Breunig: How Long Contexts Fail](https://www.dbreunig.com/2025/06/22/how-contexts-fail-and-how-to-fix-them.html)
- [Drew Breunig: How to Fix Your Context](https://www.dbreunig.com/2025/06/26/how-to-fix-your-context.html)
- [LangChain: Context Engineering for Agents](https://www.langchain.com/blog/context-engineering-for-agents)
- [Berkeley Function-Calling Leaderboard](https://gorilla.cs.berkeley.edu/leaderboard.html)
- [Microsoft + Salesforce: Sharded prompts study (arXiv:2505.06120)](https://arxiv.org/abs/2505.06120)
- [Gan & Sun: RAG MCP (arXiv:2505.03275)](https://arxiv.org/abs/2505.03275)
- [Provence context pruner (arXiv:2501.16214)](https://arxiv.org/abs/2501.16214)
