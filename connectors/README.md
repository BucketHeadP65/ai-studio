# engram-connectors

The connectors, watch-list scanners and document converters that bring sources into a knowledge base. They implement
the `Connector`, `Scanner` and `DocumentConverter` ports of [`engram`](../engram/README.md), imported from
`engram.sources.contracts`. The distribution is `engram-connectors` and the package is `engram_connectors`.

`engram` never imports this package and indexes with none of it installed. A host finds what is installed through three
entry-point groups, `engram.sources.connectors`, `engram.sources.converters` and `engram.sources.scanners`, and loads
only the one a knowledge base names in its schema. `studio` and `harness` both install it.

## What is inside

| Subpackage | Registered as | What it does | Extra |
| --- | --- | --- | --- |
| `zotero` | `zotero` | Reads a read-only copy of a Zotero library's `zotero.sqlite`. | `zotero` |
| `youtube` | `youtube` | Fetches a YouTube video's transcript from its public watch page. | `youtube` |
| `source_queue` | `intake` | Resolves the URLs on a knowledge base's source queue: a video, an arXiv paper, a post or an article. | `source-queue` |
| `x` | `x` (scanner) | Lists an account's recent posts on X with a Grok model that has X search, for a watch-list entry. | `x` |
| `beir` | `beir` | Serves a BEIR benchmark dataset's documents and writes its relevance judgments. | none |
| `multihop_rag` | `multihop_rag` | Serves the MultiHop-RAG benchmark's news articles and writes its questions as a judgments file. | none |
| `conversion` | one converter per connector | Turns a PDF or a web page into markdown: trafilatura for articles, the arXiv HTML version of a paper, and Azure Document Intelligence for other PDFs. | `conversion` |
| `testing` | none | Fakes and factories that other projects' tests import. | `testing` |

Each extra carries only that connector's own dependencies, so a knowledge base that names `youtube` alone never
imports another connector's libraries.

## Adding a connector

A connector, converter or scanner registers itself in a package's `pyproject.toml` under the name a knowledge base
uses:

```toml
[project.entry-points."engram.sources.connectors"]
zotero = "engram_connectors.zotero.contracts:SqliteZoteroConnector"

[project.entry-points."engram.sources.converters"]
zotero = "engram_connectors.conversion.contracts:zotero_converter_factory"

[project.entry-points."engram.sources.scanners"]
x = "engram_connectors.x.contracts:XScanner"
```

A connector class answers which URLs it claims and how it keys them without any settings, and builds a live instance
from `SourceResources` with its `build` classmethod. A third party adds a connector by publishing a package with the
same entry points, with no change to `engram` or to this package. `CLAUDE.md` describes the contract in full.

## Develop

```bash
uv sync
uv run ruff check && uv run pyright && uv run pytest
```

The tests are unit tests only, one folder per connector under `tests/unit/`. This package depends on `engram` and
`core` from their folders and on nothing app-specific. The wiring that builds connectors for a knowledge base lives in
`studio`.
