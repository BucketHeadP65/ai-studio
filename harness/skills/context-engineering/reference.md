# Context Engineering: Reference

Extended material for the [`context-engineering`](SKILL.md) skill. Read SKILL.md first, this file deepens the catalogue.

---

## Part 1: Failure mode case studies

### 1.1 Poisoning: the Pokémon goal corruption

**Source:** Gemini 2.5 Technical Report (DeepMind/Google), as recounted in Drew Breunig's *How Long Contexts Fail*.

**What happened:** Gemini 2.5 Pro played Pokémon for thousands of steps in a single agent loop with a >1M-token context window. Occasionally a hallucination about the game state (e.g. *"the leader is in the cave"* when it wasn't) entered the goal section of context. Once embedded, the agent referenced the false goal in subsequent steps, planning impossible paths and recycling futile strategies. The agent could not recover by reasoning. The bad fact was load-bearing in every later turn.

**Why it bites:** Long-running agents that copy-forward goals, beliefs, or summaries amplify any error introduced early. The error becomes part of the input distribution the model conditions on.

**The fix is structural, not local.** No prompt-engineering trick removes a poisoned belief from a 200K-token history reliably. Only:
- Restart the run with a clean window
- Quarantine retrieval/exploration in a sub-agent and pass back a verified summary
- Pin the source-of-truth (game state, ticket data) in a structured slot the agent reads each turn rather than recomputing

### 1.2 Distraction: context distraction beyond ~32K / ~100K tokens

**Sources:** Databricks long-context RAG study (Llama 3.1 405b), Gemini 2.5 technical report (Pokémon agent).

**The numbers:**
- **Llama 3.1 405b correctness degrades around 32K tokens**, well before the advertised 128K window.
- **Gemini 2.5 Pro Pokémon agent transitions from novel reasoning to repeated actions past ~100K tokens**, even though the window is >1M.
- Smaller models hit this onset *much* earlier. Bigger reasoning models hit it later.

**Mechanism (what the model "feels"):** As history grows, the model's attention increasingly anchors to the in-context history rather than to its training distribution. It starts replaying things it already did rather than synthesising fresh strategies.

**The fix is structural:**
- Compact the history (Anthropic's compaction approach)
- Offload state to a `NOTES.md` so the active window stays small
- Spawn a sub-agent for exploratory work and only return a 1-2K-token summary

**Operational note:** distraction looks superficially like a "lazy" model. It's not laziness. The prior of the in-context history dominates. The cure is to shrink the prior.

### 1.3 Confusion: Berkeley FCL and the >30-tool cliff

**Sources:** Berkeley Function-Calling Leaderboard (BFCL) v3, "RAG MCP" (Gan & Sun, arXiv:2505.03275), "Less is More" (arXiv:2411.15399), GeoEngine benchmark.

**Key findings:**
- Every model performs worse when given more than one tool. The performance-vs-tool-count curve slopes down from the very first added tool.
- Past **~30 tools**, descriptions begin overlapping enough that selection accuracy collapses.
- Past **~100 tools**, models are *"virtually guaranteed to fail"*.
- Llama 3.1 8B with the GeoEngine benchmark: **fails at 46 tools, succeeds at 19**, even though all 46 fit comfortably in the 16K context window. Capacity is not the bottleneck. *Cognitive surface* is.
- The "Less is More" paper: dynamic tool selection on Llama 3.1 8B improved accuracy by **44%**, cut power by **18%**, and improved speed by **77%**.
- Applying RAG to *tool descriptions* (not data) has been reported to triple selection accuracy (LangChain).

**Why this happens:** Two tools whose descriptions overlap force the model to disambiguate on the basis of a few words. Anthropic's bar (*"if a human engineer can't definitively say which tool should be used in a given situation, an AI agent can't be expected to do better"*) is the practical test.

**The fix:**
- Prune to ≤~20 tools per setup where possible
- Rewrite overlapping descriptions until a human reviewer picks the right one without context
- For larger surfaces, RAG over tool descriptions and surface only the top-k for the current request
- Or use a meta-tool reveal pattern: one "list categories" tool, then drill into a single category's tools per turn

### 1.4 Clash: the sharded-prompts study

**Source:** Microsoft + Salesforce 2025 paper on multi-turn / sharded conversations (arXiv:2505.06120, as cited by Drew Breunig).

**The numbers:**
- **−39% average performance** when the same spec is distributed across multi-turn ("sharded") form vs delivered up-front.
- **OpenAI o3 dropped from 98.1% (single-turn) to 64.1% (sharded)** on the same task.

**Mechanism (paraphrased from the study):** LLMs make assumptions in early turns, prematurely attempt final solutions, then over-rely on those answers. Once they take a wrong turn, they don't recover. Every subsequent turn is conditioned on the bad commitment.

**The fix:**
- Front-load the *complete* spec at turn one of a fresh window. The window may be more expensive on that turn but is cheaper overall.
- If new requirements emerge mid-task, restate the *full* spec in the next message rather than appending a delta.
- Recognise the symptom: if you find yourself saying *"and another thing…"* repeatedly, stop and re-state.

---

## Part 2: Anthropic's three long-horizon techniques

From *Effective Context Engineering for AI Agents*. The three are not exclusive, production agents typically blend them.

### 2.1 Compaction

> *"Compaction is the practice of taking a conversation nearing the context window limit, summarizing its contents, and reinitiating a new context window with the summary."* (Anthropic)

**When it fits:** the agent's task requires extensive back-and-forth dialogue (debugging, iterative authoring, multi-step research), and you need to maintain *conversational flow* across that span.

**The two-phase rule for designing a compaction prompt:**

1. **Maximise recall first.** Capture every decision, constraint, anomaly, and tool error verbatim. Better to keep too much than to lose the one detail that mattered.
2. **Then iterate toward precision.** Once recall is high, prune redundant tool outputs and routine acknowledgements.

**The lightest-touch form: tool-result clearing.** Drop the verbose tool outputs from earlier turns. Keep the assistant's own reasoning and conclusions. Most of the volume is in tool returns. Most of the value is in the assistant's synthesis.

**Production trigger:** Claude Code's auto-compact fires at **95% of the context window**.

**What you must preserve verbatim:**
- Decisions made and *why*
- Constraints discovered the hard way (especially mid-task)
- Tool errors that surprised the agent
- Anomalies the user explicitly cared about
- Architectural choices and unresolved bugs

### 2.2 Structured note-taking (agentic memory)

> *"Structured note-taking, or agentic memory, is a technique where the agent regularly writes notes persisted to memory outside of the context window."* (Anthropic)

**When it fits:** iterative development with clear milestones, long-running tasks where progress is incremental.

**Canonical examples:**
- Claude Code maintains a to-do list as part of its scratchpad
- A custom agent maintains `NOTES.md` and reads it back as needed
- The Pokémon agent kept tallies *"for the last 1,234 steps I've been training my Pokémon in Route 1, Pikachu has gained 8 levels toward the target of 10."*

**Pattern:** expose two tools to the agent (`record_note(topic, content)` and `read_notes(topic=None)`) and let the agent decide what to write. Critical: the *agent* decides what to record. Don't hardcode "summarise after every tool call." That becomes its own context tax.

**Anthropic's "think" tool (note: distinct from extended thinking)** reported up to **+54% improvement** on benchmarks for specialised agents. Three Anthropic-identified use cases:
1. Tool output analysis: process results before acting
2. Policy-heavy environments: follow detailed guidelines over many steps
3. Sequential decision making: actions build on previous ones, mistakes are costly

**Naming note (Breunig):** "think" overloads with extended thinking. "Scratchpad" is more precise.

### 2.3 Sub-agent architectures

> *"specialized sub-agents can handle focused tasks with clean context windows. The main agent coordinates with a high-level plan while subagents perform deep technical work or use tools to find relevant information."* (Anthropic)

**When it fits:** complex research / analysis / large-codebase work where parallel exploration pays. Anthropic's multi-agent research system reported **+90.2% vs single-agent Claude Opus 4** on internal evals, at **~15 times the token cost vs single-agent chat**.

**Pattern:**
- Lead/coordinator agent with a high-level plan, uses a stronger model (Anthropic used Claude Opus 4 as lead)
- Sub-agents with focused briefs and clean windows, can use a faster model (Anthropic used Claude Sonnet 4)
- Each sub-agent explores extensively (tens of thousands of tokens) and returns a 1-2K-token distilled summary
- Lead synthesises across sub-agent returns

**When NOT to use multi-agent:**
- Tasks where sub-agents must share fast-evolving state
- Simple sequential workflows (don't pay 15 times for nothing)
- Latency-sensitive paths (parallelism helps throughput, not single-task latency)

**Token cost framing:** the +90.2% / 15 times ratio is the right framing. If your task can't justify 15 times the cost, the architecture is wrong, not the models.

---

## Part 3: Tool design playbook (extended)

From *Writing Tools for Agents*. SKILL.md has the rule table, this section gives the patterns and their motivation.

### 3.1 Consolidate around user intent, not API endpoints

The most common Anthropic-flagged mistake: 1:1 wrapping. Each public API endpoint becomes a tool. The agent ends up orchestrating low-level calls.

**Refactors Anthropic gives explicitly:**
- `list_users` + `list_events` + `create_event` becomes `schedule_event(attendee, slot, room?)`
- `read_logs` becomes `search_logs(query, time_range)`, only return relevant lines
- `get_customer_by_id` + `list_transactions` + `list_notes` becomes `get_customer_context(customer_id)`

The agent's "verb" should match the user's intent. If a human says *"schedule a meeting"*, the tool should be `schedule_event`, not five primitives the agent has to glue together.

### 3.2 Namespacing: service and resource

> *"Namespacing tools by service (e.g., `asana_search`, `jira_search`) and by resource (e.g., `asana_projects_search`, `asana_users_search`)."*

Two reasons:
1. **Disambiguation under load.** When the agent has 30 tools, namespaced names are easier to pick correctly.
2. **Permission boundaries.** Service prefix maps to access scope. You can filter by prefix when delegating to sub-agents.

Anthropic notes that prefix-vs-suffix choice has *"non-trivial effects on tool-use evaluations"*, prefer prefix unless evals say otherwise.

### 3.3 Semantic returns, not opaque IDs

> *"Merely resolving arbitrary alphanumeric UUIDs to more semantically meaningful language significantly improves Claude's precision."*

Return shape comparison:

```python
# ❌ Forces the agent to chain a lookup tool to get any human-meaningful field
{"uuid": "b3f4-9c2d-1a", "type_id": 7, "owner_id": "u_xq91"}

# ✅ Each field carries meaning the model can reason about directly
{"name": "Q3 Roadmap", "kind": "doc", "owner": "alice@example.com"}
```

Where IDs are unavoidable (you'll feed them back to a write tool), expose **both** (the human-readable label *and* the ID) and document which one to pass to which tool.

### 3.4 The `response_format` enum pattern

> *"You can enable both [natural language and technical identifiers] by exposing a simple `response_format` enum parameter."*

```python
from enum import Enum

class ResponseFormat(str, Enum):
    CONCISE = "concise"      # human-readable, ~⅓ of detailed tokens
    DETAILED = "detailed"    # adds raw IDs, timestamps, internal fields
```

Anthropic's Slack tools used `concise` by default and reported **~⅓ the tokens** of `detailed`. The agent escalates to `detailed` only when it needs IDs to call a follow-up tool.

### 3.5 Pagination, range selection, truncation

> *"pagination, range selection, filtering, and/or truncation with sensible default parameter values."*

Defaults matter more than maxes. If your default `page_size=50` and the agent rarely needs more, the typical call is small. Don't make the agent specify limits every call. It forgets, and the rare large response poisons the window.

Anthropic's Claude Code default: **25,000 tokens per tool result** as a hard cap. A single tool call should not be able to fill the window.

When you truncate, **say so to the agent**:

```
Showing first 10 results of 247. Refine the query to narrow down.
```

The agent can act on this. It cannot act on a silent truncation.

### 3.6 Actionable errors

> *"prompt-engineer your error responses to clearly communicate specific and actionable improvements, rather than opaque error codes or tracebacks."*

The error message is the agent's only learning signal mid-run. Compare:

```
❌ HTTP 422: Unprocessable Entity
✅ The `slot` parameter must be ISO 8601 with timezone (e.g., "2026-05-08T14:00:00Z").
   You passed "May 8 2pm" — convert it to ISO format and retry.
```

The good error tells the agent *which arg, what shape, how to fix*. It's a teaching moment, not a stack trace.

---

## Part 4: Strategy-to-failure mapping

LangChain's four strategies (Write / Select / Compress / Isolate) versus Breunig's four failure modes, which strategies apply to which mode. Multiple strategies usually apply. This table shows the *primary* fix per mode.

| Failure mode | Primary | Also useful | Why |
|---|---|---|---|
| Poisoning | Isolate (sub-agent / fresh window) | Compress (re-summarise from sources, not from the poisoned trace) | The bad fact is in the trace, you need a window that doesn't contain it |
| Distraction | Compress (summarise) + Write (offload to file) | Isolate (sub-agent for sub-task) | The active window is too long, shrink it |
| Confusion | Select (RAG over tools / docs) | Compress (prune the tool setup) | Surface only what's relevant *now* |
| Clash | Write the full spec into a clean window | Compress (consolidate state) | Front-load completely, restart if needed |

---

## Part 5: Patterns library

### 5.1 Structured-context dictionary

Maintain context as a **dictionary** keyed by purpose, then assemble it into the prompt per call:

```python
context = {
    "system": SYSTEM_PROMPT,
    "user_profile": user_profile_for(uid),       # ~200 tokens, stable
    "recent_activity": last_n_events(uid, n=5),  # ~500 tokens, refreshed each call
    "active_task": task_state,                   # ~300 tokens, mutates
    "scratchpad": notes_md.read(),               # bounded, agent-curated
}
prompt = compile_prompt(context)
```

This is Drew Breunig's structural recommendation. Pruning becomes mechanical: drop the field, the rest stays coherent. You can also vary which fields go to which call. A sub-agent's coordinator may not need `recent_activity`.

### 5.2 Provence-style trained pruning

For one-shot RAG calls where the retrieved doc is large, a trained pruner can cut **~95%** of the doc while keeping the answer intact (Provence, arXiv:2501.16214). The full code from Drew Breunig's article:

```python
from transformers import AutoModel

provence = AutoModel.from_pretrained(
    "naver/provence-reranker-debertav3-v1",
    trust_remote_code=True,
)

with open("alameda_wiki.md", "r", encoding="utf-8") as f:
    alameda_wiki = f.read()

question = "What are my options for leaving Alameda?"
provence_output = provence.process(question, alameda_wiki)
```

When you don't have a trained pruner, the cheaper fallback is **section-level chunking** + an initial LLM call that returns the section indices that contain the answer.

### 5.3 Meta-tool reveal pattern (dodging the >30-tool cliff)

When you genuinely need 50+ tools, don't surface them all. Surface one **meta-tool**:

```
Tool: list_tool_categories()
  Returns: ["crm", "calendar", "billing", "reporting", "admin"]

Tool: load_tools_for(category: str)
  Side effect: registers the tools for `category` for the next turn
  Returns: short description of what's now available
```

Step 1: model calls `list_tool_categories()`, picks one. Step 2: tools for that category are now in the setup, model picks the right one. Per-step tool count stays under ~10 even though the universe is 50+.

This is one of the canonical fixes for confusion. The `pydantic-ai-toolsets` skill documents the framework primitives that implement it.

### 5.4 Sub-agent brief template

When delegating to a sub-agent, the brief itself is context engineering. A good brief is:

```
You are <specialist role>.

Goal: <one sentence — what the coordinator needs back>

Inputs:
  - <identifier 1, e.g. file path>
  - <identifier 2, e.g. ticket ID>

Constraints:
  - <budget, format, scope>

Return: <exact shape the coordinator will parse>
```

A sub-agent that returns a 5K-token essay when the coordinator asked for a 200-token bullet list is a context-engineering failure in the brief, not in the sub-agent.

### 5.5 The "is this earning its keep?" reflex

Drew Breunig's closing question, applicable on every iteration of a long-running session:

> *"Is everything in this context earning its keep?"*

If not: cut, compact, or move it. The default direction is *out*, not *in*.

---

## Sources

See the SKILL.md sources block, same set of primary references.
