---
id: engram-adr-0039-library-app-package-split
aliases: ["engram-adr-0039-library-app-package-split"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0039 — Library/app package split: `engram` + `engram_app`"
---

# ADR-0039 — Library/app package split: `engram` + `engram_app`

**Status:** Accepted (2026-08-10).

**Context.** engram packaged its `src/` tree flat, so installing it — which workflows does, as a
path dependency — dropped top-level modules named `features`, `memory`, `db`, `settings`,
`container`, `resolver`, and `cli` into every consumer's environment. None of those names are ours
to own. A consumer's own `settings.py` was one import away from colliding with engram's, and the
only way in was through `features/` internals: workflows imported `from features.serve.embedded
import …`, a path that says nothing about what engram offers and everything about how engram
happens to be laid out.

The leak shaped consumer code for the worse. Every consumer-side workaround — a hand-assembled
door built from internals, an `uv run engram ingest` subprocess, environment shims reconstructing a
DSN — existed because engram had no library surface to call. The `embedded_*` doors were the first
correction: the library asserting that a host should compose a corpus in-process, not shell out to
a CLI. But doors on a flat tree still made a consumer import `features.serve`.

A slice-level import measurement showed the tree was already nearly two packages. `memory` and `db`
imported nothing app-side. `compile`, `serve` (minus `http.py`), and the `evals` machinery each had
one or two violations, all of the same shape: a resource model reached from app `settings`, or a
`deps.py` wireup provider sitting inside an otherwise framework-pure slice. Only `cli*`, `settings`,
`container`, `resolver`, `serve/http.py`, `jobs.py`, `prefs.py`, and `ask/` were genuinely
application code — the composition roots and the dashboard's own features.

**Options considered.**

- **Keep one flat package, rename the leaked modules** (`engram_features`, `engram_settings`, …).
  Rejected: it removes the collision risk without giving consumers a surface. They would still
  import internals, just internals with longer names, and every subsequent refactor would still be
  a consumer-visible break.
- **Split the repository** — engram-lib and engram-app as separate distributions. Rejected as
  premature: two lockfiles, two release cadences, and a cross-repo change for every seam adjustment,
  bought before we know the seam is stable. Packages inside the monorepo give the same boundary with
  none of the coordination cost, and after the split a repository split is a `git mv` should we ever
  want one.
- **A namespace package** (`engram.features.*` etc. with no top-level re-exports). Rejected: it
  fixes the collision but leaves the public surface undefined — a consumer still has to know which
  submodule holds the thing it wants, so nothing tells us which moves are breaking.

**Decision.** `src/` holds exactly two packages, and the boundary between them is a rule, not a
convention.

### 1. `engram` — the library

`memory` (note grammar, `CorpusSpec`, `AuthoringPayload`), `db` (`MemoryBase` + engine glue),
`config` (the resource shapes library factories take), `compile`, `retrieval`, `evals` (the
machinery), `sources` (sync, zotero, conversion, mirror), `init`, and `export`. This is what
`import engram` means, anywhere.

**`engram.*` never imports `engram_app.*`.** The three measured violations are closed structurally,
not by discipline: the resource models (`OpenAiResource`, `IngestionSettings`, `DatabaseResource`,
`CorpusSettings`) now live in `engram.config`; the wireup providers moved to `engram_app/wiring/`;
and the MCP server factory takes its advisor settings as a parameter of a library-defined type.
A boundary test enforces the rule against both absolute and relative imports.

### 2. `engram_app` — the application

The `cli*` modules, `settings`, `container`, `resolver`, `wiring/`, `http/`, and `ask/` — the
composition roots and the application built on the library. The entry point is
`engram = "engram_app.cli:app"`.

`engram_app.settings` re-exports the resource shapes from `engram.config`, so the app's settings
tree stays one import for its callers while the models themselves belong to the library that
consumes them. The per-slice `deps.py` files became `engram_app/wiring/<slice>.py`: wireup
registration is composition, so it belongs to whoever composes. No library slice imports wireup.

### 3. The doors are the surface, and they are lazy

`engram/__init__.py` exposes a deliberately small set — the grammar (`CorpusSpec`,
`AuthoringPayload`, `default_research_spec`, `load_schema_spec`), the doors (`CorpusDoor`,
`embedded_corpus`, `embedded_ingest`, `embedded_library`, `embedded_retrieval`), the capabilities
(`RetrievalCorpus`, `SqlCorpus`), and the supporting factories (`document_embedder`,
`policy_instructions`). `engram.init` exposes its onboarding surface the same way.

Both resolve through `__getattr__` rather than eager imports, so `import engram` does not drag in
SQLAlchemy, pydantic-ai, and the retrieval stack for a host that only wants the note grammar.

### 4. The wheel packages exactly two packages

Packaging names them explicitly — `only-include = ["src/engram", "src/engram_app"]` with `sources =
["src"]` — so nothing else under `src/` reaches the wheel and both import at top level in the
installed environment. The top-level `features` / `memory` / `db` / `settings` / `container` leak is
over.

**Consequences.**

- (+) **Consumers import `engram` and nothing else.** workflows now reaches engram through the
  top-level doors plus four `engram.*` modules; not one of its imports is a generic name, and none
  of them is a top-level module engram had no right to.
- (+) **The app reaches the library like any consumer would** — through public modules, with the
  depth it genuinely needs (wireup providers building handlers) quarantined in `engram_app/wiring/`
  and marked as composition. When the app's import of the library gets awkward, that is signal
  about the library's surface, not noise.
- (+) **The boundary is testable, so it holds.** `tests/unit/engram/test_package_boundaries.py`
  fails if any `engram` module imports the app spine — reading relative and absolute import forms
  alike — and `tests/unit/engram/test_doors.py` fails if an advertised door has no table entry or
  does not resolve. Drift fails a test that names the rule rather than surfacing in review.
- (+) A repository split, if it is ever wanted, is now a `git mv` — the seam is already load-bearing.
- (−) **Two names to keep straight.** Deciding where a new module goes is now a real decision every
  time. The rule that settles it: if it composes (wireup, settings, HTTP routing, agent assembly) it
  is `engram_app`; otherwise it is `engram`.
- (−) **Alembic still resolves its environment relative to the checkout.** `alembic/env.py` reads
  engram's own migration chain from the repo, so an embedding host that installs the wheel gets the
  library and the ORM but must bring its own migration environment to create the schema. Moving the
  chain inside the package is a separate decision.
- (−) The library layer now spans eight subpackages plus `config` under one name, so a reader
  looking for "the serve slice" must know it is `engram.retrieval` — named for what it is to a
  consumer rather than for the process that serves it.

**Consequences — follow-ups and retained couplings.** The split closed the import direction; it did not close everything the
boundary review surfaced. Each item below is a known, accepted state with its own trigger for revisiting.

- **Credential fields are plain `str`, and closing that is breaking.** `DatabaseResource.url` / `.reader_url`,
  `LogfireResource.token`, and `GatewayResource.api_key` carry secrets in `str` fields, so a stray `repr`, a validation error,
  or a settings dump can print them. `SecretStr` is the fix. It changes the read site of every consumer
  (`.get_secret_value()`), which costs nothing while engram is a path dependency inside this monorepo and becomes a breaking
  change to a published API the moment the wheel ships. **Close it before the first publish**, not after.
- **Nothing asserts at runtime that the reader engine is actually read-only.** The `wiki_reader` role is granted read-only by
  migration, and the retrieval path's safety argument rests on that grant holding. A misconfigured `reader_url` pointing at
  the owner role would be caught by no test and no assertion — it would simply work, with write privileges. An assertion at
  reader-engine open (probe the session's privileges, refuse a writable one) turns a silent misconfiguration into a startup
  failure.
- **`retrieval` and `evals` depend on each other, and only one direction has a seam.** `evals` reaches retrieval exclusively
  through `engram.evals.shared.serve_bridge`, the anti-corruption funnel a boundary test keeps one-directional. The reverse
  edge has no such funnel: `engram.retrieval.mcp_server` (and the embedded doors behind it) import
  `engram.evals.advisor.mcp_advisor`, `engram.evals.calibration.mcp_calibration`, and the dataset builders **at module
  scope**, to register the opt-in authoring/calibration/advisor tool families. The cost is paid on import, not on use: a host
  that wires none of those deps still drags the whole evals tree in behind `import engram.retrieval.mcp_server`. Deferring
  those imports to the registration branches, or moving the registrations behind a plug-in seam the host opts into, would
  break the cycle; neither is done.
- **`evals` imports `compile`'s ORM directly, and that is accepted.** Seven `engram.evals` modules reach
  `engram.compile`'s ORM models to read corpus state. The direction is correct (evals is downstream of compile) and the
  coupling is to the schema evals genuinely measures, so no anti-corruption seam is warranted. Recorded so the edge is not
  mistaken for an oversight.
- **Two paths still resolve relative to the checkout, not the installed package.** `alembic/env.py` (above) is one;
  `engram_app/settings.py`'s `_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"` is its twin — it walks out of the
  installed package to find `config/application_settings.json`. Both work from a source checkout and neither is meaningful
  from a wheel in site-packages. A host embedding engram must supply the DB URL and the migration environment itself.
- **The dependency set is not split, so a library-only install pulls the app's dependencies.** One `pyproject.toml` declares
  one dependency list, so installing `engram` for the note grammar alone still installs typer, FastAPI, uvicorn, and the
  dashboard's stack. Extras (`engram[app]`, `engram[retrieval]`, …) would let a consumer take only what it imports. Packaging
  work, deliberately deferred until the seam has been exercised by a second consumer.

**Related.** [ADR-0038](0038-dependency-injection-conventions.md) (the DI conventions the
`wiring/` move preserves — factories only, one wiring module per slice, no library slice importing
wireup), [ADR-0021](0021-explicit-eager-composition-root.md) (the composition root that stayed
app-side), [ADR-0014](0014-domain-general-vault-spec-and-plugin.md) (the `CorpusSpec` the library
surface hands out). Structure: [architecture §2](../architecture/architecture.md). The design that
set this shape is *Engram library/app split — design*.
