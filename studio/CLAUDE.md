# studio: the knowledge base application

The knowledge base half of AI Factory Studio (package `studio`, console script `studio`): the
knowledge base CLI
(`studio index/ingest/sync/source-queue/watch-list/note/evals/init/vocab/export/status/search/fetch/decisions/decide/mcp`)
and the knowledge base
HTTP API on :8100 (`studio serve-http`: `GET /api/graph` for the Graph station, plus the
knowledge-base registry surface (`GET`/`POST /api/knowledge-bases`, `GET /api/knowledge-bases/{name}`,
`GET /api/knowledge-bases/{name}/schema`, `GET /api/knowledge-bases/{name}/decisions`,
`PATCH /api/knowledge-bases/{name}/pages/{page_id}`, `POST /api/knowledge-bases/{name}/source-queue`,
`GET /api/templates`, `PUT`/`DELETE /api/knowledge-bases/{name}/registration`) backing the Knowledge-bases
station (see `docs/reference/knowledge-base-management.md`)).

Nothing here names a knowledge base's page types, statuses, folders or fields. The decision surface
(`studio decisions`, `studio decide`, the two routes) takes them from the knowledge base's own
schema or from the request, and `studio init --template <slug>` starts a knowledge base from
`../templates/<slug>/`. That rule is the root `CLAUDE.md` ownership clause and root ADR-0018.

Composition style: **wireup DI**: resource shapes come from `engram.config`, every settings section is
declared in `engram/config/application_settings.json` (copy the committed
`application_settings.example.json`, which carries every section), the container derives its sections
from the settings tree, each slice lists what it registers in
`studio/knowledge_base/wiring/<slice>.py` as `REGISTRATIONS`, and every route group is a controller
class in `studio/knowledge_base/http/controllers/`. The knowledge base a process acts on is an
argument of `create_container`, never a settings field. Structural gates:
`tests/unit/knowledge_base/test_di_gates.py` (registrations) and
`tests/unit/knowledge_base/http/test_routes.py` (the served `(method, path)` baseline). See
`docs/adr/0003-studio-wiring-and-controllers.md`.

The coding agent and workflow runtime live in the sibling **`harness/`** project (:8770). The two
apps share `engram`, `engram-connectors` and `core`, nothing else. The harness's `knowledge-base` plugin
reaches this knowledge base by spawning `uv run studio mcp` from this checkout.

## Databases and the one alembic chain

There is exactly ONE alembic chain, engram's, at `engram/alembic` (checkout-relative). It
migrates BOTH databases by URL parameter: the knowledge base DB (`kb` schema, engram's
docker-compose on host port 5434) and the workflow-library DB (`engram_workflows`, same shape).
Neither app owns a chain. Each points engram's at the URL it needs.

## Commands *(run from `studio/`)*

```bash
uv sync                                                # dev+test groups install by default
uv run ruff check && uv run pyright && uv run pytest   # the standard gate
uv run studio --help                                   # the knowledge base CLI
uv run studio serve-http                               # the knowledge base API on 127.0.0.1:8100
```

Depends on `engram`, `engram-connectors` (the connectors and their converters, with the
`conversion,source-queue,x,youtube,zotero` extras) and `core`, all path sources. A change in any of them
must keep this app green.

**After changing the package layout** (adding/moving/removing a top-level package under `src/`),
run `uv sync --reinstall-package studio` here. Hatchling's editable install records the layout
at install time, so a stale `.pth` keeps resolving the old tree. The same applies in reverse:
after engram or core change THEIR layouts, re-sync here with `--reinstall-package <pkg>`.

## Code standard

Docstrings, slice layout, CQRS, names and what belongs in `core` follow the root `CLAUDE.md` section of the same name.
