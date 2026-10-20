# engram: Markdown knowledge-base indexer (the library)

A **domain-general** Obsidian-style Markdown knowledge-base indexer (Python 3.13), driven by each
knowledge base's own **`KnowledgeBaseSchema`** (`src/engram/kb/domain/schema.py`) so it indexes any knowledge base, cooking, legal, personal,
company docs (engram ADR-0014). There is no default schema: a knowledge base with no `knowledge-base-schema.json` is
refused, naming `studio init --template <slug>` and the templates in `../templates/` (root ADR-0018). Every
knowledge base's typed frontmatter fields and narrowable facets are **declared**, in that same `KnowledgeBaseSchema`
(`FrontmatterFieldSpec`, `FacetSpec`), the storage backbone (`pages.frontmatter`, `pages.facets` jsonb)
carries no knowledge-base-named column, and there is no fixed set of research facet slots a generic knowledge base is
locked out of.

## One package, one boundary

`src/` holds exactly **`engram`**, the library (`kb`, `db`, `config`, `indexing`, `retrieval`, `authoring`,
`evals`, `sources`, `init`, `export`, `testing`). The application over it, the `studio` CLI,
`settings`, `container`, `wiring/`, `http/`, `ask/`, and the coding agent, is the `studio`
package in `../studio` (see `../studio/CLAUDE.md`).

**`engram.testing`** (engram ADR-0040) is the official test toolkit every consumer imports,
fakes, factories, knowledge-base builders, golden fixtures as package data, and the containerized-DB
pytest plugin (`pytest_plugins = ["engram.testing.infrastructure.knowledge_base_db"]`), behind the `engram[testing]`
extra. Never hand-copy a fake into an app's test tree. Extend the toolkit. The runtime library
never imports `engram.testing` (boundary-gated both ways).

**`engram` never imports `studio`**, `tests/unit/engram/test_package_boundaries.py` fails if it
does. Resource shapes live in `engram.config` (the studio's settings re-export them). Wireup
registration lives in `studio/knowledge_base/wiring/<slice>.py`, never in a library slice. New module?
If it composes (wireup, settings, HTTP routing, agent assembly) it belongs to the studio.
Otherwise it is `engram`. Outside consumers import `engram` only, through the lazy imports in
`engram/__init__.py` and `engram/init/contracts.py` (engram ADR-0039).

## Common Commands *(run from `engram/`)*

```bash
# Bring up the local Postgres — the DEFAULT local dev DB on host port 5434 (pg_textsearch BM25 +
# Apache AGE, matching the managed Postgres target, engram ADR-0017). ParadeDB is the opt-in bed:
# `docker compose --profile paradedb up paradedb` (5433) + APP_RETRIEVAL__DIALECT=pg_search / graph_backend=cte.
# This AGE-backed default reaches `alembic upgrade head` (the AGE graph projection carries the
# collection tag and scopes a partitioned traversal per hop; engram ADR-0043).
docker compose up -d

# Apply the schema (tables, indexes, api views/functions, role grants). This is the repo's ONE
# alembic chain — it also migrates the workflow-store DB (`engram_workflows`) by URL parameter.
uv run alembic upgrade head

# Tests: unit suite is pure and runs without Docker; the library integration tier uses
# testcontainers and auto-skips when Docker is unavailable. The default local Postgres (AGE)
# testcontainer (engram ADR-0017) reaches head. KNOWLEDGE_BASE_TEST_DIALECT=pg_search opts into the
# ParadeDB bed (relational `cte` graph backend) instead. Run both beds after a change to api.search or the walks. App-tier e2e
# lives in ../studio/tests.
uv run pytest
```

Knowledge base operations (`index`, `sync`, `note`, `evals`, `init`, `export`, `search`, `fetch`,
`mcp`, `serve-http`) are the studio CLI: run `uv run studio <command>` from `../studio`.

Changes in `core/` ripple into its consumers, run those suites plus engram's when you touch it.

**After changing the package layout** (adding/moving/removing a top-level package under `src/`), run
`uv sync --reinstall-package engram` in **both** `engram/` and `studio/`, hatchling's editable
install records the layout at install time, so a stale `.pth` keeps resolving the old tree. Seeing the
`.pth` line twice is inert: hatchling emits one entry per configured root.

## HTTP Boundary Modeling

The codebase's convention for adapters that talk to external HTTP APIs. The generic guarded fetcher lives in core (`core.net`, which refuses a URL resolving to a private address), and the adapters that follow this convention live in the `connectors` project (`../connectors/src/engram_connectors/conversion/infrastructure/azure_converter.py` for Azure Document Intelligence, `../connectors/src/engram_connectors/conversion/infrastructure/arxiv_html/` for the ar5iv/arXiv HTML version):

- **Response shapes** become Pydantic `BaseModel(frozen=True, extra="ignore")`. Validate inbound at the boundary. API drift becomes a clean `ValidationError` rather than malformed data leaking downstream. Field names mirror the upstream API exactly (e.g. a vendor's camelCase) with `# noqa: N815`. Cache list validators at module level via `TypeAdapter(list[Model])`.
- **Request payloads we construct** become `TypedDict`. The static type check already verified the shape we built. Runtime re-validation would just be re-checking our own work.

## Strategy Registry Pattern

When code dispatches on a closed set of types (connector, adapter, DB dialect, etc.), the project shape is a **frozen dataclass
wrapping `Mapping[Enum, Adapter]`**, registered as a wireup singleton. Wireup doesn't bind raw `Mapping` types directly, so the
wrapper is required. engram's live dispatch surfaces are the **dual-dialect BM25 seam** (`SearchDialects` over
`Mapping[DbDialect, SearchDialect]`, engram ADR 0006) and the **dual graph backend** (the relational `cte` walk vs Apache AGE, engram ADR
0007), both genuine enum-keyed registries. The **connector** and **document converter** registries are not enum-keyed and
not engram's own: a knowledge base names its connectors as plain strings in `spec.sources.connectors`, and
`engram.sources.registry` resolves each name against the entry-point groups `engram.sources.connectors`,
`engram.sources.converters` and `engram.sources.scanners` that the `connectors` project registers (root ADR-0018). It loads
only the one entry point a name asks for, so a knowledge base declaring `youtube` alone never imports another connector's dependencies,
and a name nothing installs raises `ConnectorNotInstalledError` naming it. Every lookup takes an optional `source` (a mapping or a
zero-arg callable building one), so
a test drives them with a dict of fakes instead of patching `importlib.metadata`. The same seam reaches the write side:
`AuthoringTools(..., connectors=...)` and `source_type_of(..., source=...)` thread it through every tool that classifies a URL or
folds an item type, and left at its `None` default both resolve against the real installed entry points.
`studio.knowledge_base.wiring.{sources,conversion}` only
wrap the resolved bag for wireup and hold no name-to-factory dict of their own. `MultiConnectorSync`
(`engram.sources.service.sync`) then runs one handler per declared connector and merges their reports, so a knowledge base naming more
than one connector syncs all of them from a single `SyncMirrors` dispatch. Adding a connector is publishing a distribution that
registers the entry point, never an edit here or at the composition root.

Not every fallback chain is this pattern. The **conversion pipeline** (arXiv-HTML-first with an Azure Document Intelligence fallback, see engram ADR 0003) lives in the `connectors` project and is a **Composite / Chain-of-Responsibility**, *not* a strategy registry: `HybridConverter` holds an `_html` and a `_di` converter behind engram's one `DocumentConverter` port (`src/engram/sources/internal_contracts.py`) and selects by *exception fallback* (`try` HTML, fall back to Azure DI on `ConversionError`), there is no `Mapping[Enum, Adapter]` and no enum dispatch, so adding a third converter is a `HybridConverter` rewrite, not a one-entry diff.

## Sources: watch lists and the source queue

engram owns the ports and the mechanics here, never a host. `engram.sources` is one slice: outside callers
import `engram.sources.contracts`. `sources/internal_contracts.py` holds the ports (`Connector`,
`DocumentConverter`, `MirrorStore`, `Scanner`), `sources/registry.py` resolves a knowledge base's declared
connector names against the entry points, `core.net` is the one SSRF-guarded fetcher, `sources/domain/` holds the
items, rows and pure sync decisions, `sources/service/` the sync and the watch-list scan, and
`sources/infrastructure/` the mirror store, the source queue file, the watch-list file and the generic `xml` reader. The connector and scanner
implementations are in `../connectors/` (root ADR-0018).

The source queue, `<knowledge_base>/_meta/source-queue.yaml` (`engram.sources.infrastructure.source_queue`), holds one row per
URL somebody wants in the knowledge base: the URL, a source type (`video`, `paper`, `post` or `article`, decided
from the URL alone with no network call) and a status (`requested`, `resolved`, `failed`, `skipped`
or `seen`, the last held back by the knowledge base's ingestion policy rather than fetched). The four source types
are engram's own sources grammar: a source type names what a connector hands over, never the connector,
and each schema maps a source type onto its own page types through `sources.digest_types`.
`classify_url` (`engram.sources.domain.source_queue`) decides a row's source type and key by asking the knowledge base's registered connectors in
declared order (`claims`, then `key_for`), so no host name appears in engram. A URL nothing claims is
an `article` keyed by URL hash. The connector registered as `intake`
(`engram_connectors.source_queue`) then resolves a `requested` row through the source type's resolver (a YouTube
transcript, an arXiv or an article conversion) and caches its rendered markdown under
`<knowledge_base>/_meta/source-queue-cache/`. A `resolved` row is served straight from that cache on every later
sync, and a `failed` row moves back to `requested` on retry. A web page that answers a bot check (by its title, or a
short body asking the reader to pass a check) is never stored or served: the fetch fails the row, and a cached one is
deleted, its row failed, or fetched again when the row was retried.

The watch list (`engram.sources.service.watch_list`) watches for new URLs to add to that source queue. Each row of
`<knowledge_base>/_meta/watch-list.yaml` names the scanner that reads it (`scanner`), what that scanner reads
(`ref`) and a `max_items` cap on items pulled per scan. A `scanner` is a scanner's registered name,
never a host: engram ships `xml` alone (`XmlScanner`, any Atom or RSS document fetched at the
entry's own URL, so a YouTube channel is watched by its own Atom URL), and every other name resolves
through the `engram.sources.scanners` entry-point group, whose implementations live in
`../connectors/` (the `x` scanner is a Grok agent carrying `XSearch`, which renders each post
straight into the source-queue cache rather than queuing it for a later resolve). A scanner class carries
one `build(resources: SourceResources)` classmethod, refusing there when a resource it needs is
absent. `scan_watch_list` resolves every watch-list row's `scanner` against the scanners the host built, drops what
is new into the source queue, and stamps the row's last scan. A row whose scanner nothing serves,
or whose scan fails, is reported against that one row and never fails the run.

## Authoring tools

`engram.authoring.tools.AuthoringTools` is the write side of a knowledge base: twenty tools a
host mounts on `embedded_read_tools` via `authoring=`, alongside the standing `search`/`fetch` pair
(`None`, the default, keeps a knowledge base read-only). `write_note(knowledge_base, payload, path="")` validates an
authoring payload against the knowledge base's own grammar (render, parse, validate), writes it under the page
type's folder, and indexes it again through the host's `Reindex` port. An id already on disk is
replaced, unless that page is the digest of a different source, which is refused. A grammar or
validation refusal becomes a `ModelRetry` naming every finding. A claim must read back from the rendered page as written, each part read the way the parser reads it (`kb.claims_not_read_back`: edge whitespace and comments dropped, a quote unwrapped). A claim whose text holds a blank line, or whose field value holds a line break, is refused before validation runs, and the refusal names every such claim and the part that differs. When the schema sets `sources.quote_verify` and a page names its mirror through `sources.key_field`, or else through `sources.digest_link_field`, the write checks every quote against that mirror (`Sources/*.md`, as the index reads it), swaps a quote that is only a locator with its loc when the loc is the mirror's words, and refuses any other missing quote naming the claim. `digest_skeleton` numbers a digest's id
(`-2`, `-3`) when another page already sits where it would land.
`check_note(knowledge_base, payload, path="")` runs the same checks with nothing written and answers the
findings as its reply, so a drafting step fixes them before any write. An explicit
`path` (relative to the knowledge base root, ending in `.md`) places the page there instead of under the page type's
own folder, so long as it resolves under one of that page type's declared roots (its `folder` plus every
entry of its `folders`, or `Wiki` when it declares neither). `read_notes(knowledge_base, path)` answers one
file's text together with its open owner note threads, replies included, and answers `exists: False`
rather than raising when the file is absent. `resolve_note(knowledge_base, path, note_id, reply="")` marks
one thread resolved, appending `reply` as the agent first when it is not empty.
`list_pages(knowledge_base, page_type, status="", limit=500, lacking_link="")` lists every page of one page type by id with its title
and status, at one status when given, only the pages with no outgoing link of the relation `lacking_link` names when
given, an unknown page type refused naming the page types. `read_page(knowledge_base, page_id)` answers one indexed
page's knowledge-base-relative `path` (the one the index last read it from) and its file parsed by `kb.parse` into a
ready `write_note` `payload` (`kb.payload_of`), which written back with that path keeps every frontmatter value and
the body byte for byte. In the payload each heading is a section of its own while that renders the page back. When it
does not, a section the page type does not declare comes inside the body of the section before it, heading included
and as written, but only when the renderer would move it (it sits before a declared section) or change it (such as a
heading with no blank line under it). Every other section stays a section of its own. It refuses an id no indexed
page carries and a file a payload cannot carry whole, such as text before the first heading, a claim the grammar does
not read back, or a section that would move or change with nothing before it to carry it (it sits before the first
declared section, or right after a claims section that holds claims).
`request_source(knowledge_base, url, found_via="", reason="")` queues a URL onto the knowledge base's source
queue (`_meta/source-queue.yaml`) under its ingestion policy, idempotently: a URL already on the source queue
answers with its own status, not a duplicate row. A request with no `found_via` is the owner's own.
A reference (`found_via` set) needs a one-sentence `reason`, is refused when its link class is on
the policy's `never` list or its source type is not in `page_types`, is held back as `seen` rather than fetched
past the policy's `hop_depth` or once its source is at `per_source_cap`, and otherwise lands as
`requested`. `source_queue_policy`, `write_policy`, `source_queue_rows`, and `source_queue_decide` read and change
that policy and source queue on the owner's word. `list_undigested(knowledge_base, limit=40)` lists source
mirrors with no digest page linking them, newest synced first, read over the admin SQL gateway,
since that join needs the `kb` schema the read-only role has no grant on. Every source field
name in these tools comes from the knowledge base's own schema, never from a literal: `sources.key_field` is
the mirror key a digest joins on, `sources.digest_link_field` is the link a digest carries back to
its source, and `sources.hash_field` is the source hash (the research template declares
`zotero_key`, `digest_of` and `source_hash`, another knowledge base declares its own three). `list_recent_digests(knowledge_base, since_hours=26, limit=40)` lists the digest
pages indexed within that window, `compounding_work(knowledge_base, name, page_ids=None)` lists the pending inputs for
one entry of the knowledge base's `compounding` block (the derivation map: undigested mirrors, pages of
some page types within a window or at a status, files with open note threads under a folder) together
with the page types and the opening status to write, an unknown name refused naming the entries, and
`digest_skeleton(knowledge_base, source_id)` fills in a ready `write_note` payload for one undigested
mirror. `compounding_work`'s pages rows also carry `source_id`, `path` and `findings` (the page's stored validation errors by rule name), and `page_ids` reads exactly the named pages of a pages entry, whatever their age or links. `read_source(knowledge_base, source_id)` answers one
mirror whole over the same gateway: its identity plus its `raw_chunks` in reading order, each with
the `loc` a claim's quote cites, since `fetch` addresses pages and sections, never a mirror. The
answer carries `chunk_count` and `section_count` (the mirror's heading sections, a long one spanning several
chunks that share its heading), and when the knowledge base's schema sets `sources.digest_sections_over`, a
`split_recommended` flag (`section_count` over it) alongside the threshold. `trace_source(knowledge_base, source)` (`source` by
`source_id`, exact title, or URL) is the removal closure for one mirror: `only_x` names what it
alone accounts for (its digest pages, its indexed mirror file, its source-queue cache files, the
decision-type pages naming it, its source-queue status), `expected_paths` is every knowledge-base-relative file a
removal would delete, and `candidates` is every other page reachable by following inbound links
from the digest pages, so a human can look at what a removal would leave dangling.
`remove_source_artifacts(knowledge_base, source_id, expected_paths, reason="")` deletes exactly the paths a
fresh `trace_source` recomputes, all or nothing, refusing on any disagreement with the
`expected_paths` passed in, then blocks the source's source-queue row and indexes again. `decide_page(knowledge_base,
page_id, status, links, reason="")` moves one page to one of its page type's declared statuses, writes
the link fields an outcome asks for, an optional reason noted on the page, and indexes again.
`resolve_entities(knowledge_base, entities)` is entity resolution: each name answers the id of its one entity
page, matching a page's title or alias once case, punctuation and a leading "the" are set aside. A name that
matches none resolves through its first alias that does, joining that page as an alias. Otherwise it gets a new
page of the schema's `entities.page_type`, unless an existing page already carries the id it would make, which
it then joins. A page only ever takes names no other page holds: a matched page takes the new ones as aliases,
and so does a page another writer created first. Every name is checked before anything is written, the knowledge
base indexes again once when anything was written, and every alias merge holds the page's hidden `.lock` file.
Calls on one knowledge base through one `AuthoringTools` instance on one event loop resolve one at a time, from
the scan to the last write, under an `asyncio.Lock`, so calls at once never make two pages for one entity.

Beyond that twenty-tool set (`AuthoringTools.tools()` stays exactly those twenty), the class
carries grounding and memory tools a host mounts deliberately per step rather than surface-wide:
`knowledge_base_profile(knowledge_base)` (the base's `_meta/purpose.md` text, its facet/relation/claim-type
vocabulary, every page type with its authoring guidance and canonical sections, each declared
decision type with its open status and outcomes, and every page of one, so a step can look back
over past decisions), `skeleton(knowledge_base, page_type)` (a ready `write_note` payload for one page type, each canonical
section seeded with the guidance the schema declares, an unknown page type refused naming the valid
ones), and `recall_beliefs(knowledge_base)` /
`remember_belief(knowledge_base, text)` over `engram.agent_memory`, live only when the host passes a
`memory_for` store factory (collection named by `collection_of(target)`). Without one they
answer a plain sentence, never an exception.

`skip_digest(knowledge_base, page_id, reason)` sets skipped on every page naming the same source through
`sources.digest_link_field` as `page_id`, the reason noted on each, refusing a page that names no source or a page type
declaring no skipped status, then indexes once. A host grants it to one step at a time and never to chat.

Guidance itself is authored in `_meta/documentation-policy.md` (an H2 per page type, an H3 per
canonical section) and `_meta/compounding-policy.md` (an H2 per compounding entry).
`KnowledgeBaseSchema.from_path` (and so `load_knowledge_base_schema`) merges the page prose beside the schema file into
the schema through `engram.kb.infrastructure.guidance_pages`, and a page type's or section's JSON `guidance` string is
the fallback only when the page carries none.

## The read path

Every agent read runs through `ReadOnlySqlGateway` as the read-only `kb_reader` role, against the `api` schema only, and
`verify_reader_role` refuses a reader engine whose role can do more, naming `infrastructure.database.reader_url`. A private
knowledge base is visible only to a session logged in as its own role (`kb_reader_` and a hash of the collection), which the
indexer creates and lists in `kb.reader_scopes` with a password derived from `infrastructure.database.reader_secret`. With no
secret a private knowledge base reads as empty (engram ADR-0046). `run_sql` passes `core.ai.retrieval.statement.single_statement`
and runs inside `SELECT * FROM (...) AS q LIMIT cap + 1` (engram ADR-0047). A tool call wraps its reads in `read_snapshot()`, one
read-only `REPEATABLE READ` transaction, released before any model call. `api.search` reads its numbers from
`kb.retrieval_tuning` (engram ADR-0048, amended by migration 0071): a default row for the database and at most one row
per knowledge base, which sets only `max_hits_per_document` and `folded_snippet_chars` for its own hits. It keeps the
best hit of each document (the source a unit was made from, else its page) up to `max_hits_per_document`, 1 by default
and 0 for every hit, and lists the document's next matching units under that hit as `folded_units`, each with its
snippet cut to `folded_snippet_chars` (160 by default) when longer, which the search tools give the refs that fetch them.
A `search` description states the cap a process read once per knowledge base through `api.search_hits_per_document(collection)`,
which answers the database's cap for a knowledge base the session cannot see, with the exceptions `docs/reference/serve.md` lists (a failed read, bases that disagree, a tuned override). The graph walks keep a reached-node set on both backends, and
`api.graph_reach` says how far a walk can go (engram ADR-0049).

## Chunking source mirrors

The index cuts each source mirror into chunks (`chunk_mirror`, `engram.indexing.domain.mirror_chunks`) by one chain, the mirror's
own shape picking the step: a heading section within the maximum chunk size is one chunk, so a mirror with no heading that fits
stays whole, and longer text of several paragraphs within the planner ceiling is cut where the chunk planner says its topic
changes. Any other longer text, or text the chunk planner gave no accepted answer for, is filled paragraph by paragraph to the
chunk sizes. The chunk planner is a port the host provides (`ChunkPlanner`): it is shown the text as numbered paragraphs and
answers paragraph numbers only, never text, so every chunk is the mirror's own text, and the domain refuses an answer that is not
strictly increasing paragraph numbers. `CutPlanStage` stores each accepted answer as a cut plan in `kb.cut_plans`, by knowledge
base, text hash and cut plan identity (the planner's model, prompt version and thinking level, with the paragraph numbering version), and reuses it with no model call. A failed or late planner
call fills its text and never fails an index. The studio wires `PydanticAiChunkPlanner` over the Azure OpenAI deployment
`ingestion.chunk_planner_model` names. That setting is empty by default, and with no deployment or no endpoint the index has no chunk planner. The stored indexer
version names the chunk sizes, the chunk planner and its ceiling, so changing any of them makes the next index a full one.

## Search re-ranking

`engram.retrieval.rerank` wraps a host's search handler so each search's hits come back in the order a decision model
judges them likely to help answer the query. It drops no hit, each hit keeps its fused `score` and gains `rerank_score`, its
probability, and a search over several knowledge bases merges by that probability when every hit has one, else rank by rank. The shared
settings file's `retrieval.rerank` block (`RerankSettings`) turns it on and picks the model, the text judged (`snippet` or
`chunk`) and the request shape (`per_hit` or `all_hits`). `reranking(settings, openrouter)` answers `None` when it is off
or OpenRouter has no key, and a failed or late re-rank returns the search's own result. The model is reached through
core's `decision_model` over `infrastructure.openrouter`.

## Type Hints

- When ruff says `TC001`/`TC002`/`TC003` (move import into a type-checking block), do exactly that: `from typing import TYPE_CHECKING` and put the import under `if TYPE_CHECKING:`. The only valid exception is when a runtime DI / serialization framework (e.g. wireup, pydantic at runtime, or **SQLAlchemy, `Mapped[T]` annotations are resolved at mapper-configuration time**) actually needs the symbol at module load, and that exception must be obvious from context, not assumed. Encode that exception in `[tool.ruff.lint.flake8-type-checking]` via `runtime-evaluated-base-classes` / `runtime-evaluated-decorators` (engram lists `pydantic.BaseModel`, `pydantic_settings.BaseSettings`, `engram.db.contracts.KbBase`, and the decorator `wireup.injectable`) so ruff stops flagging it project-wide, **not** a per-import `# noqa`. So a `TC00X` on a `Mapped[...]` import in an ORM file (e.g. `engram/indexing/infrastructure/orm.py`) means that base class is missing from `runtime-evaluated-base-classes`, not that the import should move.

## Dependency Injection

DI conventions are binding per ADR-0038 (docs/adr/0038-dependency-injection-conventions.md). The
structural gates live in `../studio/tests/unit/knowledge_base/test_di_gates.py` (the composition root
moved to the studio with the app).

## Code standard

Docstrings, slice layout, CQRS, names and what belongs in `core` follow the root `CLAUDE.md` section of the same name.
