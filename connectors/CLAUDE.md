# connectors: source connectors, scanners and document converters

Implements engram's `Connector`, `DocumentConverter` and `Scanner` ports (all three imported from
`engram.sources.contracts`): one subpackage
per connector or scanner, each an optional extra carrying only its own fetcher dependencies.
`engram` itself never imports this package and indexes with none of it installed. A host
discovers what is here through three entry-point groups engram's `engram.sources.registry` walks
lazily at wiring time.

## Module map

- `zotero/`: the `Connector` adapter over a read-only copy of `zotero.sqlite`. Extra:
  `zotero` (`aiosqlite`, `sqlalchemy[asyncio]`).
- `youtube/`: the `Connector` adapter over YouTube's public watch page, its player API (asked as
  the iOS client, the one route whose caption URLs still serve a transcript) and the caption endpoint
  (hand-rolled over `httpx`, no dedicated transcript library). Extra: `youtube`.
- `source_queue/`: the `Connector` adapter over a knowledge base's general source queue
  (`engram.sources.contracts.SourceQueue`), one resolver per `SourceType`
  (`VideoResolver`, `ArxivResolver`, `ArticleResolver`, `PostResolver`). A web page that is a bot check (`challenge_reason`)
  fails its row at fetch and is never served from the cache. Extra: `source-queue`, registered name `intake`.
- `x/`: the `Scanner` for `x` watch-list entries. A Grok model with native X search lists an
  account's posts one day of the scan's window at a time, oldest day first, each day asked for up to the
  entry's `max_items`, for at most fourteen days or until the day that brings it to `max_items`, and answers
  how far it covered. The next scan continues from there. A scan of the whole window that found nothing
  covers up to a day before now, so only the last day stays open. One scan costs at most fourteen agent
  runs, each held to eight model responses (`UsageLimits(request_limit=8)`). One X search answers about 20
  posts at most, so a day that answers 18 or more gets a note (not a failure) that X search may have held
  more. A fault of a day's content (`UnexpectedModelBehavior`, which covers a content filter,
  `ModelContextWindowExceededError`, `UsageLimitExceeded`) skips that day and reports it. Any other fault
  (a connection error, HTTP 401, 403, 429) stops the walk before that day, and raises `ScanError` when it
  hits the first day. Each post is rendered straight into the source queue cache, so the refresh that
  follows never re-fetches it. Extra: `x` (`pydantic-ai`, `pydantic-ai-slim[xai]`).
- `conversion/`: the `DocumentConverter` adapters a resolver or the sync slice hands a PDF or
  URL to: `ArticleConverter` (trafilatura), `ArxivHtmlConverter` (the ar5iv/arXiv HTML route),
  `AzureDocumentConverter` (Azure Document Intelligence, the PDF fallback), `HybridConverter`
  (composes the two arXiv routes), `PassthroughConverter` (reads back markdown a connector already
  rendered, serving both `youtube` and `intake`). Extra: `conversion`.
- `testing/`: fakes and polyfactory fixtures other projects' tests import (`studio`'s wiring and
  integration tests, this project's own suite), mirroring `engram.testing`'s shape. Extra:
  `testing` (polyfactory only).

## Registering a connector, converter or scanner

A connector registers its own class directly, under the knowledge base's declared name, in
`pyproject.toml`:

```toml
[project.entry-points."engram.sources.connectors"]
zotero = "engram_connectors.zotero.contracts:SqliteZoteroConnector"

[project.entry-points."engram.sources.converters"]
zotero = "engram_connectors.conversion.contracts:zotero_converter_factory"

[project.entry-points."engram.sources.scanners"]
x = "engram_connectors.x.contracts:XScanner"
```

The registered class implements engram's `ConnectorClass` port (`engram.sources.internal_contracts`): `claims`/
`key_for`/`source_type_of` as `@staticmethod`s (stateless, so `engram.sources.contracts.claimants_for`
uses the bare class with no settings in hand) plus a `build(resources: SourceResources)`
classmethod. `SourceResources` (`engram.config`) is the one container every connector's live
construction might need (`knowledge_base`, `document_intelligence`, `connectors`, `settings`, and only `knowledge_base` is
required), so a future connector's new need is a block in `settings` under a name it documents,
never a signature change to `build` itself. `settings` is raw JSON keyed by that name: each
connector owns the model it validates its own block with (`engram_connectors.zotero.domain` for
`zotero`, `engram_connectors.xai` for `xai`), and decides there whether an absent or empty block is
a default or a refusal. A
converter instead registers a plain factory function (`DocumentIntelligenceSettings -> DocumentConverter`),
since nothing classifies through it. A scanner registers a class implementing
engram's `ScannerClass` port (`engram.sources.internal_contracts`): the same `build(resources: SourceResources)`
classmethod and nothing else, since a scanner recognizes no URL of its own. The name it registers
under is what a watch-list entry carries as its `kind` in `<knowledge base>/_meta/watch-list.yaml`. engram's own
generic `xml` reader (any Atom or RSS document at the watch entry's own URL) needs no entry point, so
this group holds only the scanners a knowledge base has to install. None of the three takes anything
studio-specific, so this package depends on `engram` and `core` only (never `studio`), and a
third party adds a connector or a scanner by publishing a package with the same entry-point shape,
never by editing `engram` or this one.

## Tests

Pure unit tier only, one directory per connector or scanner plus `conversion/` (`tests/unit/{zotero,youtube,source_queue,x,conversion}/`),
mirroring `src/engram_connectors/`. Fakes and polyfactory fixtures live in the `testing/` subpackage (public
scriptable attributes, no leading underscores), never hand-copied into a test file.

## Common Commands *(run from `connectors/`)*

```bash
uv sync
uv run ruff check && uv run pyright && uv run pytest
```

## Boundary

This package depends on `engram` and `core` (path sources) and nothing app-tier: no `studio`, no
`wireup`, no HTTP/CLI framework. Composition (which connectors a knowledge base gets, which scanners
its watch list names, DI registration) lives in `studio/knowledge_base/wiring/{sources,conversion,watch_list}.py`,
never here.

## Code standard

Docstrings, slice layout, CQRS, names and what belongs in `core` follow the root `CLAUDE.md` section of the same name.
