# harness, the coding agent and workflow runtime

The agent half of AI Factory Studio (package `harness`, console script `harness`),
serving the Studio on :8770: chat-to-draft authoring, environment composition over engram's
plugins, the Monty sandbox, run/pause/gate machinery, ACP, and the **self-authoring
harness** (`harness/features/selfauthoring/`: the agent authors capabilities, steps,
declarative agents, and profiles, each landing pending in the settings-declared,
git-tracked `custom/{capabilities,steps,agents,profiles}/` root.
`harness list|activate|disable|delete|plugins|profile|promote|demote` is the owner's
review-and-activate surface, see `docs/reference/self-authoring.md`), **delegation**
(`features/authoring/workers.py`: one generic worker on a three-tier model menu behind the
`delegate` and `agent-scripts` plugins, doctrine in `skills/delegating-work/`), plus the
**terminal** feature (`/api/terminal/ws`, a pty bridge behind the Knowledge-bases station's
"design it with AI" button, off by default, `APP_TERMINAL__ENABLED=true` to enable. Four
gates, see `studio/docs/reference/knowledge-base-management.md`). The workflow engine itself is
`core.ai.workflows`. The knowledge base app is the sibling `studio/` project (:8100).

**Read `CONTEXT.md`** for the authoritative domain glossary.

## Layering (enforced by `tests/unit/test_layering.py`)

Dependencies point inward, and every sanctioned exception is pinned in the test. The list can
only shrink. The tiers, inward first:

- `domain/`: ports (`KnowledgeBaseSurface`, `ToolCaller`, `EmbeddingContract`) and the step-surface
  vocabulary. Imports nothing of ours.
- `settings.py`: checkout geometry and deployment knobs. Imports domain only.
- the kernel (`session`, `plugins`, `knowledge_base`, `rendering`, `sandbox`, `tracing`): the shared
  surface every slice may hold.
- `adapters/` (third-party construction): `store` (the settings file, both DSN derivations, the
  pools, the record's own store over its own pool onto the same database, in its own schema), `knowledge_base` (the registered knowledge bases, terminal roots, engram's read/authoring plugins), `embedding`,
  `models` (the gateway model catalog, the model factory, pricing), `model_tiers` (the owner's tier store,
  environment stamps), `model_list` (the deployment's reachable model list), `environment` (sandbox, shell, spill,
  step guards), `session_log` (the append-only session-event file and its boot sweep, see
  `docs/reference/session-journal.md`), `record` (the recall source over the conversation record, narrowed to one
  conversation). Code outside it imports `adapters.contracts`, and the composition roots also `adapters.wiring`.
- `features/<slice>/`: authoring, browsing, projects, running, scheduling, selfauthoring, sessions,
  terminal, plus evals and followme, which serve no routes, and inspection, which serves its own eight. A slice
  reaches inward and into itself, never into another slice. A slice with routes carries a `controller.py` (its
  routes, one class, its own router and prefix) and a `wiring.py` (`REGISTRATIONS`, what the slice puts in the
  container).
- `composition.py`, the one module that knows every slice: `harness_session`, `build_environment`.
- `container.py`, the composition map: the sections, the adapters' leaves and every slice's
  registrations, in one `create_container(settings)`.
- `migrations/`: one-time rewrites of this deployment's own state, reaching no further than `adapters/`.
- entries: `main` (`create_app(settings)`, the HTTP composition root), `acp`, `cli`. Each builds the
  same container. Nothing imports them. The loop also serves the inspection's three job kinds
  (`inspect_run`, `attribute_lead`, `scan_run`) from the `coordination` schema of the sessions database
  and runs its dispatch and sweep. `harness inspect` issues commands by hand.

`vendor/` is verbatim third-party (ruff-excluded). `harness/skills/` (the app root, beside `workflows/`) is
the registry of skills: every skill a model tier here may name and the files this repository's `.claude/skills`
and `.agents/skills` symlinks point at, data not code. Runtime state (`.state/`, `custom/`) lives under this checkout, provider keys live in the shared settings file's `infrastructure` section, and
every session's conversation lives in the record in `aifactory_sessions`.

## Wiring (enforced by `tests/unit/test_wiring.py` and `tests/unit/test_routes.py`)

One settings root, one container, three composition roots.

- **Settings.** `HarnessSettings` in `settings.py` composes eleven mandatory sections, read from
  `config/harness_settings.json` (committed, every section present) then `APP_`-prefixed environment
  variables with `__` between a section and its field: `APP_RUN__RUN_ENGINE`,
  `APP_STORE__HYBRID_SEARCH`, `APP_MODELS__DEEPSEEK_GATEWAY_ROUTE`,
  `APP_SELF_AUTHORING__CUSTOM_ROOT`, `APP_TERMINAL__ENABLED`, `APP_WEB_SEARCH__TAVILY_API_KEY`,
  `APP_STUDIO_API__BASE_URL`, `APP_KNOWLEDGE_BASE_GUIDANCE__GUIDANCE`,
  `APP_LIBRARY_SEARCH__CROSS_KNOWLEDGE_BASE_SEARCH`, `APP_STATE__HOME`,
  `APP_INSPECTION__DISPATCH_SECONDS`. Init kwargs beat the environment, which beats the file. This
  app boots with no environment at all. Every durable path this deployment writes hangs off
  `state.home`, so a test points that one field at its own directory.
- **Container.** `create_container(settings)` in `container.py` registers every section under its own
  model type, the I/O leaves from `adapters/wiring.py`, and each slice's `REGISTRATIONS`. Lifetimes
  are singleton, always. Wireup is imported only in the three entries, `container.py`, the wiring
  modules and the controllers.
- **Controllers.** One `<Slice>Controller` per slice with routes, its router carrying its own prefix, mounted
  through `wireup.integration.fastapi.setup(..., class_based_handlers=CONTROLLERS)`. A router that serves HTTP
  sets `route_class=WireupRoute` (the terminal's one websocket needs none). A controller takes what it always
  needs through its constructor and anything reaching a network as `Injected[Port]` on the method.
- **Gates.** `test_routes.py` pins the served `(method, path)` set to `route_baseline.py`, refuses a
  pair declared twice, and sends one request through each controller's cheapest route against the real
  registrations. `test_wiring.py` refuses a container type outside a composition root, a `lifetime=`
  anywhere, a `wireup` import outside its seams, a settings reader cached for the whole process, and a
  field a controller stores that no route reads. `test_create_app.py` refuses a container the module
  keeps, so two applications never share one. `test_environment_names.py` reads `harness/` and
  `scripts/stack/` whole, decision records included, and refuses a retired environment name wherever
  it appears.

## No knowledge base vocabulary here (binding)

`workflows/` holds only workflow and step files **generic over the knowledge base's schema**: every literal a step
uses reaches it through the schema of the knowledge base it addresses. A workflow written for one knowledge base
lives in that knowledge base's template (`../templates/<slug>/workflows/{steps,workflows}/`), and `harness import <dir>`
imports it from there. A step that more than one template's workflow uses stays here. Nothing in `src/` names a
knowledge base: the chat knowledge base plugin is `knowledge-base`, it reads every registered knowledge base, and the domain
text is every one's own `api.about`. Nothing here reads the registry of knowledge bases either: it belongs to the
studio, and this app reads it over the studio API (`GET /api/knowledge-bases` plus `GET /api/knowledge-bases/{name}/schema`, in
`adapters/knowledge_base.py`), so the studio must answer for this one to boot. That rule is the root `CLAUDE.md` ownership
clause and root ADR-0018.

## Upstream first (binding)

Before designing any agent-side mechanism, inventory what already exists in `pydantic_ai`
(capabilities, toolsets, `ui`, `durable_exec`, native tools) and
`pydantic_ai_harness`. Then mount, wrap, or extend it. A parallel build is legitimate only when
its design doc names the upstream gap it fills. The installed version is the surface to check
(the pyproject floor undersells it).

## Commands *(run from `harness/`)*

```bash
uv sync                                                # dev+test groups install by default
uv run ruff check && uv run pyright && uv run pytest   # the standard gate
uv run harness serve                                   # the app on 127.0.0.1:8770
uv run harness --help                                  # the owner's review-and-activate CLI
uv run harness sessions list                           # the session log: spans, tail, search
uv run harness sessions rebuild                        # reproject every workspace into aifactory_sessions
uv run harness sessions import                         # copy the pre-record transcripts into the record, once
uv run harness inspect --since 24                       # issue InspectRun by hand, every watch over the last N hours
uv run harness evals inspection --repetitions 2          # compare inspector evidence access on versioned evaluation cases
uv run uvicorn harness.main:create_app --factory --port 8770   # the same app, uvicorn spelled out
uv run harness import ../templates/research/workflows   # load a folder of workflow and step files into the library
uv run python -m harness.acp                           # the agent served over ACP (Zed et al.)
```

Depends on `engram`, `engram-connectors` (with the `conversion,source-queue,x,youtube,zotero` extras, because
`request_source` classifies a pasted URL in this process and needs the real connector classes
installed) and `core[workflows-temporal]` (path sources), and `pydantic-ai-harness` from PyPI (`>=0.56.0`, versioned with pydantic-ai, 0.56 with 2.56). Every agent that holds
a shell, file tools or skills runs in a `LocalWorkspace` (harness ADR 0023). A change in
any of them must keep this app green. `docker-compose.yml` here brings up the Temporal dev server only.

**After changing the package layout** (adding/moving/removing a top-level package under `src/`),
run `uv sync --reinstall-package harness` here. Hatchling's editable install records the layout
at install time, so a stale `.pth` keeps resolving the old tree. The same applies in reverse:
after engram or core change THEIR layouts, re-sync here with `--reinstall-package <pkg>`.

## Code standard

Docstrings, slice layout, CQRS, names and what belongs in `core` follow the root `CLAUDE.md` section of the same name.
