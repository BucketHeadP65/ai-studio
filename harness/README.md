# harness, the agent and workflow runtime

`harness` is the backend AI Factory Studio talks to on port 8770. It runs the chat agent, keeps the workflow library,
runs workflows behind their gates, schedules them, records every conversation and inspects finished runs. It is a
Python 3.13 project with the package `harness` and the console script `harness`. The workflow engine itself lives in
[`core.ai.workflows`](../core/README.md), and the knowledge base app is the sibling [`studio/`](../studio/README.md)
project on port 8100.

## Run it

```bash
uv sync
uv run harness serve                    # the app on 127.0.0.1:8770
uv run python -m harness.acp            # the same agent over ACP, for an editor such as Zed
```

The harness needs the knowledge base API to answer before it starts, because it reads the registered knowledge bases
from it (`GET /api/knowledge-bases` and each one's schema). It also needs the shared settings file,
`../engram/config/application_settings.json`, with a database and an embedding connection. A Logfire read token is
optional: without one, spend reports say Logfire is off. The root [README](../README.md#2-configure) lists the keys.

Its own settings are in `config/harness_settings.json`, committed with every section present. An environment variable
overrides one field: `APP_<SECTION>__<FIELD>`, such as `APP_RUN__RUN_ENGINE`, `APP_TERMINAL__ENABLED` or
`APP_STUDIO_API__BASE_URL`. The app keeps its run-time state under `.state/` in this folder.

## Commands

| Command | What it does |
| --- | --- |
| `serve` | Serves the app (`--host`, `--port`). |
| `import <folder>` | Imports a folder of workflow and step files into the library, steps first, then the workflows that use them. |
| `plugins` | Lists the agent's built-in plugins, or turns one on or off with `--on` or `--off`. |
| `list`, `activate`, `disable`, `delete` | Reviews the custom capabilities, steps, agents and profiles the agent wrote, and decides which load. |
| `promote`, `demote` | Adds a custom capability to the registry a custom agent can name, or takes it out. |
| `profile` | Selects the profile that decides which plugins the agent gets. |
| `sessions` | The session log: `list`, `tail`, `search`, `friction`, and `rebuild` or `import` for the conversation record. |
| `inspect` | Queues inspection by hand for one run (`--run`) or every run of the last hours (`--since`). |
| `evals` | Runs a step's own dataset through the same executor a workflow run uses, and compares inspector evidence. |

## What it serves

- **Chat.** A session with the agent, which works with plugins: the knowledge bases' reads, SQL over them, a shell and
  the workspace's files, skills, recall over past conversations, web search, delegation to worker agents, and the
  workflow tools. `harness plugins` lists them all. The agent and every workflow step run on three model tiers,
  `orchestrator`, `worker` and `helper`, which you set on the Studio's Harness page.
- **The workflow library.** Workflows and reusable steps, stored as an indexed collection in the `engram_workflows`
  database. The steps the templates share are in [`workflows/`](workflows/), and each template's own workflows
  are in [`../templates/`](../templates/).
- **Runs.** A run's live state, its gates, its values, its artifacts and its spend, plus schedules and the run archive.
- **Inspection.** Rules read each finished run's recorded conversation and raise leads, and an inspector model turns a
  lead into a finding or dismisses it. The jobs run in the background of `harness serve`.
- **Self-authoring.** The agent can write custom capabilities, steps, agents and profiles. Each lands pending under
  `custom/` in this folder until you activate it.
- **Projects and the terminal.** A project's files, notes and skills, and a terminal inside a knowledge base. The
  terminal is off until `APP_TERMINAL__ENABLED=true`.

## Workflows as data

A workflow is a markdown file whose frontmatter holds a typed workflow: parameters, nodes, wires and an output. It is
validated as a whole before it runs: ids resolve, steps exist, types line up, the wires have no cycle and each gate
rewinds only into its own history. Then it is built into a `pydantic_graph` graph and run.

There are five kinds of node:

| Node | What it does |
| --- | --- |
| `run` | Runs one step, an agent or a plain function. |
| `map` | Runs one or more steps once per item of a list, with a bound on how many run at once. |
| `gate` | Waits for a person to approve or reject a value. |
| `judge` | Lets a step approve or reject a value, or hand it to a person. |
| `orchestrator-gate` | Lets the session agent decide, from a written brief, so a run can go unattended. |

A gate passes the value it approved through, so a node that must not run unapproved wires its input from the gate, not
from the gate's source. A rejection reruns the node the gate names. This excerpt from
[`workflows/workflows/source-remove.md`](workflows/workflows/source-remove.md) holds every change until the owner has
read the traced plan:

```yaml
- kind: run
  id: trace
  work: trace_removal
  inputs: {collection: {parameter: collection}, source: {parameter: source}}
- kind: gate
  id: owner_approves
  reviews: {node: trace}
  rejected_to: trace
- kind: run
  id: plan_items
  work: plan_items
  inputs: {plan: {node: owner_approves}}
```

A workflow may carry steps of its own under `inline`, for a step the app does not register: a briefing (instructions
for an agent, with typed inputs and output), a program (Python for deterministic glue, run in the Monty sandbox, never
`exec`), or a whole declared agent. A workflow may invent a computation, but never a database, a credential or a tool:
an inline step can use only the tools the host offers.

Trust is derived, never written. Every run records evidence of what happened, and a workflow's trust level
(`unverified`, `machine-confirmed` or `approved`) is folded from that evidence. Saving a workflow stamps the
environment it was built against, and every fetch compares the stamp again, so a changed step contract shows as drift.
A new version is the old one plus a typed diff, so evidence about the nodes the diff did not touch carries over, and
the old version stays as it was.

Runs execute in this process by default, with a run log on disk that keeps a waiting gate across a restart.
`APP_RUN__RUN_ENGINE=temporal` runs them on Temporal instead. The app then starts Temporal's own dev server when
nothing answers at `localhost:7233`, with its history in `.state/temporal.db` and its web UI on port 8233.
[`docker-compose.yml`](docker-compose.yml) starts a Temporal server that outlives the app, and its `persistent` profile
one backed by Postgres.

## A workflow from a file, with no database and no key

```bash
uv run python examples/run_from_file.py --yes
uv run python examples/run_from_file.py --reject-first --yes
```

It loads [`examples/verified_answer.md`](examples/verified_answer.md), validates it, builds the graph and runs it
behind a console gate, with an offline model standing in for the drafting agent. `--reject-first` rejects the draft
once to show the gate sending it back. The approved answer lands in `.state/examples/answer.md`, which git ignores.

## Drive the agent from Zed

The agent speaks [ACP](https://agentclientprotocol.com/). Add it to Zed's `settings.json` with absolute paths, because
an app started from the dock inherits neither your shell's `PATH` nor its environment:

```json
{
  "agent_servers": {
    "AI Studio": {
      "type": "custom",
      "command": "/absolute/path/to/uv",
      "args": ["run", "--directory", "/absolute/path/to/ai-studio/harness", "python", "-m", "harness.acp"],
      "env": { "PATH": "/absolute/path/to/uv/dir:/usr/bin:/bin:/usr/sbin:/sbin" }
    }
  }
}
```

The browser and the editor share the same conversation state, so use one at a time.

## Develop

```bash
uv run ruff check && uv run pyright && uv run pytest
```

`harness` depends on `engram`, `engram-connectors` (with the `conversion`, `source-queue`, `x`, `youtube` and `zotero`
extras, because it classifies pasted URLs itself), `core[workflows-temporal]` and `pydantic-ai-harness` from PyPI.
`tests/unit/test_layering.py`, `test_wiring.py` and `test_routes.py` hold the package's structure and its served routes.

## Docs

- [`CONTEXT.md`](CONTEXT.md), the glossary of the workflow and session domains.
- [`docs/reference/`](docs/reference/): self-authoring, model tiers, sessions and the session log, workflow monitoring,
  inspection, spend, agent memory and the store protocol.
- [`docs/adr/`](docs/adr/), this app's decisions.
