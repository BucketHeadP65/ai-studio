---
id: studio-knowledge-base-management
aliases: ["studio-knowledge-base-management"]
type: reference
app: studio
status: current
tags: [knowledge-bases, registry, terminal, vocabulary, refresh]
title: "knowledge base management: the Knowledge-bases station and its two write checks"
summary: 'How Studio registers, creates, validates, refreshes and serves knowledge bases while keeping their vocabulary and source roots explicit.'
---

# knowledge base management: the Knowledge-bases station and its two write checks

## Purpose

AI Factory Studio knows every knowledge base its owner has, lists their vital signs, creates new ones, and puts a
real terminal (and therefore the `engram-init` interview) one click away inside any of them. Vocabulary evolution (a facet value an
author's page uses that the vocabulary doesn't yet recognise) is a propose-then-approve loop, not silent drift. `studio ingest`
(section 6) checks the vocabulary before it writes. `embedded_authoring`, the write path the workflow library uses to save into a knowledge
base, runs the same check.

Six pieces, spanning both halves of the app: the registry of knowledge bases lives in settings and is read by both `studio.knowledge_base`
and the `harness` app. The Studio's API endpoints, the write validation, and the scheduled-refresh ticker are all `studio.knowledge_base`
(:8100). The terminal is the `harness` app (:8770). The Knowledge-bases station in `studio-client` is the one screen that ties the
registry, the terminal, and creation together.
This document is written from the landed code. Every claim below names the module a reader can check it in.

---

## 1. The registry of knowledge bases

`ApplicationSettings.knowledge_bases: dict[str, KnowledgeBaseEntry]` (`studio/src/studio/knowledge_base/settings.py`) is the whole registry of knowledge
bases: name maps to `{path, description}`. `KnowledgeBaseEntry` (`engram/src/engram/config.py`) carries the same shape validation
`KnowledgeBaseSettings.path` does: `path` must be an existing directory holding a `knowledge-base-schema.json`.

`ApplicationSettings.knowledge_base: KnowledgeBaseSettings | None` is never read from the settings JSON or the environment. It exists only as
an init-kwarg one process supplies for its own single knowledge base (a CLI command's `--knowledge-base`, or the per-job container
`RefreshKnowledgeBaseJob` builds for one refresh run), never a standing "active knowledge base" the registry itself declares. `studio
serve-http` and `studio worker` build their `ApplicationSettings` with no knowledge base path at all
(`fetch_application_settings`), so both hold `knowledge_base: None` for their whole run. They serve the registry and hold no knowledge
base of their own (section 7's identity lines reflect this: neither process has a single knowledge base to report on before a request
or a job names one). A non-empty registry is validated (`ApplicationSettings._check_knowledge_bases`): every name is non-empty and every
path is unique. Nothing checks `knowledge_base.path` against the registry, since a registry-serving process has no `knowledge_base.path` to
check. `active_knowledge_base_name()` still serves a process that does carry one (a CLI command's own scoped run): it returns the
registry name whose path matches `knowledge_base.path`, `None` when there is no active knowledge base or no match.

**No auto-enroll.** `POST /api/knowledge-bases` and `PUT .../registration` write only the entry the request names. Creation and
registration protect nothing "active," because no process holds an active knowledge base for a fresh write to leave
unrepresented. Both run the same collision-only refusal chain (section 2's POST/PUT refusal order) and the same `registry_write_refused`
belt. Neither one enrolls, or otherwise touches, any row besides the one requested.

**Two parsers, kept in parity.** The typed reader is `ApplicationSettings` itself (`studio.knowledge_base`). The agent server reads the same
file as a raw dict, and names no knowledge base of its own: `registered_knowledge_bases` (`harness/src/harness/adapters/knowledge_base.py`) turns
every registered knowledge base with a loadable `knowledge-base-schema.json` into one it reads, and `registered_knowledge_base_roots` resolves every
legal terminal `cwd` root from the same file. Both read the `application_settings.json` the studio's own registry lives in,
independently of the typed reader. `registered_knowledge_base_roots` reads that file fresh (`_read_engram_settings`, uncached) on every
call rather than the process-lifetime `_engram_settings` cache the rest of the agent server shares, for the reason section 4's terminal
checks give. A malformed registered knowledge base (no `path`, or not even an object) is skipped by `registered_knowledge_base_roots`
rather than raising, logging a debug line naming the row: this is a security check, so a bad row degrades to "one fewer legal
root," never an unhandled exception.

**The schedule declaration.** A registered knowledge base optionally carries `schedule: {every_seconds: int (>=300)} |
{daily_at: "HH:MM"} | None`. `Schedule` (`studio/src/studio/knowledge_base/settings.py`) is studio-only, deliberately not part of
`engram.config.KnowledgeBaseEntry`, since when to refresh is an operator's choice over an already-declared knowledge base, never part
of the knowledge base's own schema. So studio declares its own `KnowledgeBaseEntry` subclass carrying the extra field rather than
editing engram's. The two shapes are closed and mutually exclusive (a validator rejects both set, or neither): deliberately not
cron, so there is nothing to parse and no timezone ambiguity. `daily_at` is a bare 24-hour UTC clock reading. A declared
schedule is carried forward, not silently dropped, whenever its row is written again: `PUT /api/knowledge-bases/{name}/registration`
re-registering an already-registered name at its same path threads the row's existing `schedule` into the replacement
(`_register_knowledge_base`, `_append_registry_entries`). Only a genuinely fresh name starts scheduleless. section 2 below covers the routes
that set and clear one, and the ticker that now executes it.

**The private flag.** A registered knowledge base optionally carries `private: true` (`KnowledgeBaseEntry.private`, studio-only beside
`schedule`). It changes nothing about the row itself: it is registered, served, refreshed, scheduled, and listed exactly as any
other, and its root stays a legal terminal `cwd`. What it decides is one field of the listing, `KnowledgeBaseListEntry.private` (section 3),
and the reader of that field is the `harness` app's search surface, the chat agent's `search` over every registered knowledge
base (`harness.adapters.knowledge_base.registered_knowledge_bases`, harness ADR-0012): a private row is not mounted there, so a private knowledge
base registered for its own plugin never answers a studio session's search. A knowledge base may declare the same thing about
itself, `retrieval.private: true` in its own `knowledge-base-schema.json` (engram's `RetrievalSpec`), and either declaration alone makes
the row private. The knowledge-base-declared route survives a fresh checkout of the registry, where the row's own flag does
not. The flag is carried forward whenever the row is written again, the same way `schedule` is. Only the
knowledge-base-declared flag reaches the database: `studio index` gives that knowledge base its own login reader role and a row in
`kb.reader_scopes`, and its `api` rows are visible only to sessions logged in as that role. `studio serve-http` and `studio mcp`
open the role through `PrivateReaders` (`studio/src/studio/knowledge_base/wiring/serve.py`) with the password derived from
`infrastructure.database.reader_secret`, so the knowledge base's own surfaces read it as before. With that secret empty it reads as
empty (engram ADR-0046, [serve.md](../../../engram/docs/reference/serve.md)).

---

## 2. The Studio's API endpoints

Eighteen routes on the knowledge base's `/api` (`studio/src/studio/knowledge_base/http/api.py`, served by `studio serve-http` on :8100),
alongside `GET /api/graph`, which answers only for a named registered knowledge base: `?knowledge_base=<name>` resolves that knowledge
base's declared collection, omitting it is a 400 (`knowledge_base_required`), an unregistered or unreadable name the same 404 the schema
route answers. Every refusal is
the uniform `{error, detail}` JSON body the whole `/api` contract uses. `TrustedHostMiddleware`
binds the app to `localhost`/`127.0.0.1` `Host` headers only, closing the DNS-rebinding gap CORS's `Origin` check alone leaves open
for a request the browser never blocks from being sent, only from being read back.

| Route | Body | Answers | Refusals |
|---|---|---|---|
| `GET /api/knowledge-bases` | none | `KnowledgeBaseListResult`: the registry ∪ indexed DB, one `KnowledgeBaseListEntry` per name, each carrying its `private` flag and its `collection` | none |
| `POST /api/knowledge-bases` | `{name, path, spec, description?, force?}` | `InitReport` (the scaffold result) | 400 `hidden_directory`, 400 `name_collection_mismatch`, 409 `name_collision`, 409 `path_collision`, 400 `invalid_path`, 409 `directory_not_empty` (unless `force`), 500 `registry_write_refused` (belt), 422 `invalid_request` (an invalid `KnowledgeBaseSchema`) |
| `GET /api/knowledge-bases/{name}/schema` | none | the registered knowledge base's parsed `KnowledgeBaseSchema` | 404 `unknown_knowledge_base`, 404 `spec_not_found` |
| `GET /api/knowledge-bases/{name}` | none | `KnowledgeBaseDetail`: the list row's own fields plus the declared connectors, standing validation, the source queue, the watch list, the last refresh run, and the schedule | 404 `unknown_knowledge_base` |
| `POST /api/knowledge-bases/{name}/source-queue` | `{url}` | `SourceQueueEntry` (the row created) | 404 `unknown_knowledge_base`, 400 `sources_without_queue`, 409 `source_queue_unreadable` |
| `PUT /api/knowledge-bases/{name}/registration` | `{path, description?}` | `RegistrationReport` | 400 `hidden_directory`, 409 `name_collision`, 409 `path_collision`, 400 `invalid_path`, 500 `registry_write_refused` (belt) |
| `DELETE /api/knowledge-bases/{name}/registration` | none | `RegistrationReport` | 404 `unknown_knowledge_base` |
| `POST /api/knowledge-bases/{name}/source-queue/{key}/retry` | none | `SourceQueueEntry` (the row now `requested`) | 404 `unknown_knowledge_base`, 409 `nothing_to_retry`, 409 `source_queue_unreadable` |
| `POST /api/knowledge-bases/{name}/watch-list` | `{scanner, ref, max_items?}` | `WatchEntry` (added, or the existing one for the same `scanner`+`ref`) | 400 `unknown_scanner`, 404 `unknown_knowledge_base`, 409 `watch_list_unreadable` |
| `DELETE /api/knowledge-bases/{name}/watch-list` | none (`scanner`, `ref` query params) | `{removed: bool}` | 404 `unknown_knowledge_base`, 409 `watch_list_unreadable` |
| `GET /api/knowledge-bases/{name}/decisions` | none | `{rows, skipped, outcomes_by_kind}`: every page at its type's open status (`DecidedPage` without its host filesystem `path`), the filenames that failed to parse, and each declared decision type's allowed outcomes | 404 `unknown_knowledge_base`, 404 `spec_not_found` |
| `PATCH /api/knowledge-bases/{name}/pages/{page_id}` | `{status, links?, reason?}` | the page as it now reads (`DecidedPage` without its host filesystem `path`) plus `job_id` and `index` (`queued` or `in_progress`), the refresh run that publishes the change | 404 `unknown_knowledge_base`, 404 `spec_not_found`, 404 `unknown_page`, 409 `invalid_decision` |
| `POST /api/knowledge-bases/{name}/refresh` | none | `202 {job_id}` with a `Location` of `/api/jobs/{id}` | 404 `unknown_knowledge_base`, 409 `broken_knowledge_base`, 409 `refresh_in_progress` |
| `GET /api/knowledge-bases/{name}/refresh-runs` | none | `{refresh_runs: RefreshRun[]}`: the last 20, newest first | 404 `unknown_knowledge_base` |
| `PUT /api/knowledge-bases/{name}/schedule` | `{every_seconds}` \| `{daily_at}` | `{name, schedule}` | 404 `unknown_knowledge_base`, 422 `invalid_request` (both/neither field set) |
| `DELETE /api/knowledge-bases/{name}/schedule` | none | `{name, schedule: null}` | 404 `unknown_knowledge_base` |
| `POST /api/knowledge-bases/{name}/notes/preview` | `{origin, edited_draft?}` | `NotePreview`: the normalized draft, the Markdown, the target path, the read ref, and the schema and preview digests. Nothing is written | 404 `unknown_knowledge_base`, 409 `unreadable_schema`, 409 `origin_unavailable`, 422 `not_a_note_draft`, 400 `unknown_page_type`, 422 `draft_refused` |
| `POST /api/knowledge-bases/{name}/notes` | `{idempotency_key, origin, draft, schema_digest, preview_digest}` | `RetainedNote`: the outcome (`saved`, `saved_not_indexed`, `already_indexed`), the note id, its path and its read ref | every preview refusal, plus 409 `schema_changed`, 409 `preview_changed`, 409 `key_reused`, 409 `unreadable_receipt`, 409 `target_changed`, 500 `target_unwritable` |

**The list is a reconciliation, not a lookup.** `KnowledgeBaseListEntry.registered` and `.indexed` are independent booleans, so all four
states are explicit in the response shape rather than inferred: registered-only (declared, never indexed), indexed-only (a
collection exists in Postgres under a name nothing registered), both, or (by simply not appearing) neither. The indexed half comes
from `KnowledgeBasesHandler` (`engram/src/engram/retrieval/queries.py`): `SELECT collection, count(*), array_agg(DISTINCT page_type ORDER BY
page_type), max(indexed_at) … GROUP BY collection` over `kb.pages` left-joined to `index_fingerprint`, read through the **admin**
gateway. `kb_reader` (the plain, read-only role) has no grant on the `kb` schema, only on `api`. A fifth state a name can be
in: `broken`, a registered knowledge base whose `KnowledgeBaseEntry` validation fails (the directory it names moved or was deleted).
`_registry_rows` validates every row individually rather than the whole `knowledge_bases` map at once, so one broken row degrades to that
one row, never a 500 on the whole list. The broken row still appears (`broken: true`, its raw unvalidated `path`, no validation
error text in the response) rather than silently vanishing.

**Two fields come from the knowledge base, not the database.** `private` is true when the row declares `private: true` or the
knowledge base's own schema declares `retrieval.private: true` (section 1's private flag), and `collection` is that schema's
`retrieval.collection`, `None` for a name whose schema cannot be read (`_row_spec` reads it best-effort, so a missing or
malformed schema costs those two fields and never the route). Together with `path` they are what the `harness` app reads this
listing for: the registry of knowledge bases belongs here, so the agent app reads it over this route plus
`GET /api/knowledge-bases/{name}/schema` for each name's schema, and opens no registry file of its own
(`harness.adapters.knowledge_base.registered_knowledge_bases`). A studio that does not answer refuses that app's boot by name rather than leaving
it with an empty registry.

**`POST` refusal order:** a hidden (dot-prefixed) directory component anywhere in the resolved target path, then a `name` that
doesn't equal `spec.retrieval.collection` (registry names are collection namespaces), then name collision, then path collision (a
new name pointed at an already-registered path, caught before `ApplicationSettings`'s own unique-paths invariant would), then a
non-empty target directory without `force`. Only past all five does the scaffold write happen, and a refusal
past that point (the belt, below) can leave an orphaned scaffold directory on disk: `scaffold`/`write_scaffold` run before the
registry write is validated, so a belt refusal means the knowledge base's files exist but nothing points at them. Nothing deletes
them automatically. `KnowledgeBaseSchema.slug` and `PageTypeSpec.kind` (`engram/src/engram/memory/spec.py`) are independently constrained
to a lowercase-hyphen identifier pattern at the pydantic layer (`^[a-z0-9]+(-[a-z0-9]+)*$`), the same shape neither field could
carry a path separator or markdown heading syntax into `write_scaffold`'s generated files with, an ordinary `422 invalid_request`
refusal before any of the above ever runs. `write_scaffold` (`engram/src/engram/init/infrastructure/writer.py`) carries its own belt beneath
that: `core.files.guarded_child` refuses any scaffold relpath that would resolve outside the knowledge base root, for any future
field that reaches a file path without going through the slug/kind pattern.

The 500 `registry_write_refused` belt is the last resort: `_append_registry_entries` merges the write into the settings JSON, then
`_refuse_unloadable_merge` validates a *throwaway* copy (pre-existing broken rows filtered out, exactly as `_registry_rows`
already tolerates them) as a full `ApplicationSettings`, so a stale row already known and forgiven elsewhere in the registry can
never block an unrelated write. Only a problem with the write itself (a duplicate path, an empty name, the active knowledge base
left unrepresented) refuses. Only once that check passes does the real file get written, atomically. Single-writer posture:
concurrent writers last-replace-wins, no locking. This deployment is localhost single-owner. `PUT /api/knowledge-bases/{name}/registration`
shares this exact belt (below).

**`GET /api/knowledge-bases/{name}`** assembles one knowledge base's whole Studio-visible state in a single call: the list row's own
fields, its declared connectors (`sources_provider` the first one, for a caller that still reads a single name,
`sources_providers` the full tuple, both empty when the schema declares no source layer or can't be read), the index's standing
validation (`KnowledgeBaseValidationHandler`, empty when never indexed), its source queue (`source_queue`, each row `requested`,
`resolved`, `failed`, `skipped` or `seen`, populated only when `intake` is among the declared connectors), its watch list
(`watch_list`, from `_meta/watch-list.yaml`), its most recent refresh (`last_refresh_run`, `None` before the first
`POST .../refresh`), its declared
`schedule` (`None` when unset), and its `identity` (which database, at what revision, last indexed when, and the indexer version that index stored, `None` for a name
neither registered nor indexed, see section 7). `KnowledgeBaseDetail` is declared as a `KnowledgeBaseListEntry` subclass carrying exactly these eight
additions, so the two shapes can never drift apart field-by-field. The knowledge base's open decisions are their own route,
`GET .../decisions` (below), never a `KnowledgeBaseDetail` field.

**`POST /api/knowledge-bases/{name}/source-queue`** puts any URL (YouTube, arXiv, X/Twitter, or a plain article/PDF link) on an
`intake`-connector knowledge base's source queue (`_meta/source-queue.yaml`, the same file `studio sync` resolves on its next run). The
URL is classified by shape alone, with no network call, into one of four source types (`video`, `paper`, `post`, `article`) and a
stable row key. A URL already on the source queue, in any status, is reported back unchanged rather than duplicated.
`sources_not_intake` names the exact `spec.sources` JSON to paste into the knowledge base's `knowledge-base-schema.json` to accept URLs.
`409 source_queue_unreadable` answers when `_meta/source-queue.yaml` exists but fails to parse, rather than a 500.

**`POST /api/knowledge-bases/{name}/source-queue/{key}/retry`** sets one `failed` queued row back to `requested`. `409 nothing_to_retry`
names a row that is absent, or was never `failed`. `409 ledger_unreadable` names an unparseable source-queue file. **`POST`/`DELETE
/api/knowledge-bases/{name}/watch-list`** add or drop a watch-list entry (`{scanner, ref, max_items?}`) on the knowledge base's `_meta/watch-list.yaml`.
A `scanner` is the name of the scanner that reads the entry, `xml` (any Atom or RSS document at `ref`) plus whatever the installed
connectors package registers under `engram.sources.scanners` (`x` today, an account handle as its `ref`). `POST` refuses a
name nothing answers to with `400 unknown_scanner` naming the ones that exist, while `DELETE` takes any name so an entry whose
scanner is gone can still be dropped. `POST` is idempotent on `(scanner, ref)` and returns the existing entry unchanged when it is
already watched, `DELETE` (query params `kind`, `ref`) answers `{removed: bool}` (`false` for an entry that was never watched).
Both refuse `409 watch_list_unreadable` when `_meta/watch-list.yaml` exists but fails to parse. **`GET /api/knowledge-bases/{name}/decisions`** lists
every page currently at its type's open status, across every one of the knowledge base's declared decision types
(`KnowledgeBaseSchema.decision_page_types`), newest file first, alongside `outcomes_by_kind`: for each declared type, the statuses an owner
may move a page of that type to and the link field (if any) each one asks for, the decisions card's source for its per-row
outcome buttons. A page that fails to parse is named in `skipped` rather than blanking the listing. **`PATCH
/api/knowledge-bases/{name}/pages/{page_id}`** moves one page to `{status}`, rewriting its frontmatter `status:` line in place, and
writes any given `links` (a link-list field name to the page id(s), bare or `[[id]]`, it should now hold, replacing that
field's whole entry). The field an outcome's `link` names is filled in when moving to that status. It then enqueues the
knowledge base's `kb.refresh` job, the same one `POST .../refresh` creates under the same one-live-pass guard, so the indexed
copy searches read follows the page. The response is the page as it now reads, minus the host filesystem `path`, plus `job_id`
and `index` (`queued`, or `in_progress` when a run for the knowledge base was already queued or underway). `404 unknown_page`
names an id no page under the knowledge base carries, `404 spec_not_found` a knowledge base whose schema can't load, `409
invalid_decision` a status or link field outside the page's own type, naming the type's allowed values.

**The CLI mirrors these three surfaces**, filesystem-only, no DB or container: `studio watch-list add|list|remove`
(`studio/src/studio/knowledge_base/cli_watch_list.py`) operates directly on `_meta/watch-list.yaml`. `studio source-queue add|list|retry`
(`cli_source_queue.py`) operates directly on `_meta/source-queue.yaml`, the same idempotent add and failed-to-requested retry the routes
above give the Studio. `add` reads the knowledge base's `knowledge-base-schema.json` first and refuses with `sources_without_queue` when
`intake` is not among the declared `sources.connectors`, the same refusal `POST /api/knowledge-bases/{name}/source-queue` gives
(section 2). A knowledge base that declares only the `youtube` connector keeps syncing on its own schedule, but neither this CLI, the
API nor the Studio can add a URL to it, until its
`knowledge-base-schema.json` also declares `intake`. `studio decisions [--count]` and `studio decide <page-id> --status <s> [--link
field=id]... [--reason ...]` (`cli_decisions.py`, over `engram.retrieval.decisions`) list and move the knowledge base's open-decision
pages, skipping (and warning on stderr about) a page that fails to parse rather than failing the whole listing. `decide` writes
the page only and says so on stderr: the indexed knowledge base reads the new status after the next index (`studio index --knowledge-base
<path>`, or the next refresh run). `--knowledge-base` defaults to `.` on all four, matching `studio vocab`'s own convention (section 5).

**`PUT`/`DELETE /api/knowledge-bases/{name}/registration`** enroll or remove a registered knowledge base without touching anything on disk.
Registration is a settings-JSON write only, never a scaffold. `PUT` (body: `{path, description?}`) runs the same
hidden-directory/name-collision/path-collision refusal chain the `POST /api/knowledge-bases` order above uses, then the same
`KnowledgeBaseEntry` shape validation (`invalid_path`) and the same `registry_write_refused` belt. Unlike `POST`, it never scaffolds
files. `path` must already be a knowledge base directory (one holding a `knowledge-base-schema.json`). One
carve-out from that collision chain: a name already registered at its **same** `path` is not a collision. It is a
re-registration (refreshing `description`, or fixing a since-repaired broken row), so that case falls through to the write
instead of refusing. A name registered at a **different** path still refuses `name_collision` (re-pointing a name elsewhere is
`DELETE` then `PUT`, never a silent redirect). A re-registration carries the existing `schedule` forward unchanged (section 1).
Only a genuinely fresh name starts scheduleless. `DELETE` refuses `unknown_knowledge_base` when the name isn't registered. Any
registered knowledge base can be unregistered, active or not, since this process holds no active knowledge base of its own to protect.

**`POST /api/knowledge-bases/{name}/refresh`** is the manual-refresh route. It enqueues one `kb.refresh` job for the named knowledge base and answers `202`
with `{job_id}` plus a `Location` header of `/api/jobs/{job_id}`. Nothing about the run happens in this process. The job is keyed by
the knowledge base's registry name and the jobs table indexes one live job per key, so a second call while a run is queued or underway
answers `409 ingest_in_progress` carrying the id of the run already going. That index is the overlap guard, so it holds across
processes (this API, a second API instance, the ticker) rather than only within one. `404 unknown_knowledge_base` is a name absent from the
registry entirely, `409 broken_knowledge_base` a registered knowledge base present but unusable (`KnowledgeBaseEntry` validation failed, a moved or deleted
directory), naming the fix (re-register at the current path, or remove the stale row).

**`studio worker` runs the pipeline** (`studio/src/studio/knowledge_base/refresh/service.py`, `RefreshKnowledgeBaseJob`). It claims the job under a lease,
re-reads the registry (so a knowledge base registered or moved since the worker started is seen), builds a short-lived container scoped to
that knowledge base's `path`, and runs the same steps the CLI runs. A schema preflight first, then a watch-list scan (only when the schema's
`sources.enabled` and the knowledge base has a `_meta/watch-list.yaml` to scan, and a failed scan never fails the pass, its counts ride into the
recorded detail as a fact, and whatever it queues onto the source queue is what the sync that follows picks up), then `sync` (only
when the schema's `sources.enabled`, dispatching every declared connector through `MultiProviderSync` from one call), then the
librarian placement pass when the schema declares `sources.librarian`, then `index` with embeddings unconditionally on (there is no
flag to turn them off, matching the standing embed-always rule everywhere else in this app). The preflight
(`engram.init.contracts.assert_schema_at_head`, the same one `studio index` runs before touching a row) checks the database is at the
code's migration head before sync or index ever dispatch, so a database behind head fails with that check's own actionable
sentence (naming both revisions and the fix) rather than an opaque mid-indexing SQL error surfacing later. It runs off the event loop
(`asyncio.to_thread`), since the check's own synchronous `asyncio.run` cannot nest inside the loop already driving the job. Sync and
index dispatch through `MessageBus` exactly the way `studio sync` and `studio index` do. Every registered knowledge base keeps its own
`knowledge-base-schema.json`, `_meta/vocabulary` and dashboard on disk even though every one indexes into the same database (partitioned by
`collection`), which is why a run builds a container of its own rather than reusing the API's.

A pipeline failure (the preflight, a sync item error, a validation error, an unexpected report shape) raises out of the handler and
the worker decides what happens next. `INGEST_POLICY` allows 3 attempts, backing off 30 s and doubling to a 10 min cap, under a
30 min lease the worker's heartbeat renews while a long index runs, so the lease expires only for a worker that actually died. A
pass that exhausts its attempts is abandoned and stays visible with its `last_error`. The retries cover a transient connector or
embedding failure, since rows already written keep their vectors and the next attempt embeds only what is still missing.

**`GET /api/jobs/{job_id}`** reads one job whole (the `Job` shape in `core/src/core/cqrs/ports.py`), which is how a client follows the
run it just started. `404 unknown_job` names an id no job carries.

**The refresh history** is a projection of those jobs, not a file of its own. The old `_meta/runs.jsonl` history is no longer
read, the Studio shows the refresh history only. `GET /api/knowledge-bases/{name}/runs` reads the newest 60
`kb.refresh` jobs for that key off the jobs table and projects the finished ones onto `RefreshRun`
(`studio/src/studio/knowledge_base/runs.py`), answering the last 20 as `{runs}`, newest first. A record carries `started_at`,
`finished_at`, `outcome`, `detail` and `trigger`, timestamps RFC 3339 UTC. A `done` job reads as `outcome` `ok` carrying the
handler's one-sentence detail (what sync did, then pages indexed and embedded), a `abandoned` job as `outcome` `failed` carrying its
last error verbatim, and a job still queued, running or waiting to retry has no record at all, so a run appears on the refresh history once it
is over. `trigger` is `manual` for a run this route enqueued and `schedule` for one the ticker enqueued. `GET /api/knowledge-bases/{name}`
surfaces only the newest record, as `last_run`, so the detail page can show it without a second fetch.

**`PUT`/`DELETE /api/knowledge-bases/{name}/schedule`** set or clear one registered knowledge base's `schedule` sub-key directly on the raw JSON,
not through a validated `KnowledgeBaseEntry`, so every other key on that row, every other row, and the rest of the settings file stay
byte-faithful (the same read-mutate-belt-write shape `DELETE .../registration` uses, applied to one sub-key). A schedule can be
set even on a currently-broken row. It simply waits until the row is fixed to mean anything. `PUT`'s body is a bare `Schedule`
(section 1): `{every_seconds: N}` or `{daily_at: "HH:MM"}`. An invalid shape (both fields, or neither) is an ordinary `422
invalid_request` before the write is attempted. Both routes refuse `unknown_knowledge_base` (404) for a name absent from the registry
entirely.

**The ticker executes a schedule.** `studio.knowledge_base.refresh.contracts.Ticker` runs as a periodic on the `studio worker` process
(`worker.every(TICK_INTERVAL_SECONDS, ticker.tick)`, every 60 s), never in the API process, so an index never competes with request
latency. Each tick reads the registry fresh, never cached, so a schedule set from the Studio takes effect on the very next tick
with no restart, and for every row that declares a `schedule` it calls `is_due` against that knowledge base's newest `kb.refresh` job
(that job's `started_at`, or its `created_at` when it was never claimed). A knowledge base that has never run is due immediately.
`every_seconds` is a plain elapsed-time floor off the last run's start. `daily_at` fires once the most recent `HH:MM` UTC boundary
has passed and the last run started before it, and that check is deliberately the single most recent boundary, never every boundary
missed since the last run, so a period of downtime collapses to at most one catch-up run per knowledge base on the next tick rather than a
backlog. A due knowledge base is enqueued exactly the way the manual route enqueues it, with `trigger` `schedule`, and the one-live-job index
collapses an overlap, so a knowledge base already queued or running is skipped with no check-then-act race. The first tick runs on the
worker's first pass rather than after the first interval. A registry read failure, or one knowledge base's enqueue failure, is logged and
never stops the rest of that tick or any later one.

---

## 3. The Knowledge-bases station

`studio-client`'s fourth station, `/knowledge-bases` (nav label "Knowledge bases", `src/app/knowledge-bases/page.tsx`). Four pieces:

- **The list** (`src/components/knowledge-bases/list.tsx`) renders the registry∪DB union from `GET /api/knowledge-bases`. `registered`/`indexed`
  render as their own text, never collapsed into one derived state.
- **The create dialog** (`src/components/knowledge-bases/create-dialog.tsx`): name, path, and a starting point. Options are any template listed from
  `GET /api/templates` (each one's own `knowledge-base-schema.json` and `_meta/*.md` guidance copied into the new knowledge base via `template` in
  the `POST /api/knowledge-bases` body, the same starting point `studio init --template` gives a knowledge base from the CLI), *minimal* (one `note`
  page type, built client-side), or *paste a schema JSON* verbatim. For minimal, `slug` and `retrieval.collection` are
  stamped from the typed `name` at submit time, never hand-entered separately. For a template, its own `knowledge-base-schema.json` already
  declares its collection, so the typed `name` must equal it instead. Registry names are collection namespaces either way, and
  letting them diverge would recreate the mismatch the backend's own invariants exist to prevent. Paste mode is the one escape
  hatch: it sends the pasted schema exactly as typed.
- **The schema viewer** (`src/components/knowledge-bases/schema-viewer.tsx`): on-demand read-only view of `GET /api/knowledge-bases/{name}/schema`.
- **The detail page** (`/knowledge-bases/{name}`, `src/app/knowledge-bases/[name]/page.tsx` over `src/components/knowledge-bases/detail.tsx`): one knowledge base's
  whole Studio-visible state from `GET /api/knowledge-bases/{name}`: vitals, the registration switch (`PUT`/`DELETE
  .../registration`), the source queue with its add-URL form (`POST .../source-queue`) for a knowledge base declaring the `intake`
  connector, and the standing validation list.

**"Design it with AI" creates the knowledge base first, then opens the terminal there.** It builds the same payload the "Create" button
would from the dialog's current fields and submits it exactly the same way. Only on that request's success does it open the
terminal pane (section 4) at the now-existing, now-registered path, closing the create dialog first. A refusal shows in the dialog exactly
like an ordinary "Create" refusal. The pane never opens. This ordering isn't a UI preference: the terminal's own gate 3 (section 4) only
accepts a `cwd` that already exists and already resolves inside a registered root, so a path that hasn't been created yet could
never pass it. The button is disabled only while the path field is empty. The terminal's own gates (section 4) decide whether the created
path is actually reachable.

---

## 4. The terminal

`/api/terminal/ws?cwd=<path>`, a websocket on the agent app (:8770, `harness/src/harness/features/terminal/controller.py` and `infrastructure/pty_session.py`).
It spawns `claude` (falling back to `$SHELL`) behind a real pty in the requested directory and bridges bytes both ways, the "design
it with AI" route and, more generally, a shell inside the Studio. This is machine access, so four gates run in order, cheapest and
most consequential first, and **every refusal closes before a pty is ever opened**:

| # | Gate | Enforced in | Refuses when |
|---|---|---|---|
| 1 | The feature is on | `TerminalSettings.enabled` (`harness/src/harness/settings.py`), injected into the terminal controller | `APP_TERMINAL__ENABLED` is unset, **default false** |
| 2 | Exact origin | `AI_FACTORY_STUDIO_ORIGINS` in `harness/src/harness/settings.py` (`http://localhost:3031`, `http://127.0.0.1:3031`), the one list that `main.py`'s CORS middleware and the terminal's gate both read. The terminal checks it by hand, since a websocket handshake is never CORS-preflighted | the `Origin` header isn't one of those two exact strings |
| 3 | `cwd` inside a registered root | `_resolve_cwd`, checked against `registered_knowledge_base_roots()` (section 1) via `Path.is_relative_to` after `Path.resolve(strict=True)` (symlinks followed) | `cwd` doesn't exist, isn't a directory, or resolves outside every registered knowledge base root and the app's own checkout |
| 4 | (none, implicit) | the same `_resolve_cwd` | (no fourth network-facing gate. `cwd` must additionally be a directory, not a file, or `subprocess.Popen` would raise after `accept()`) |

Every refusal closes the socket with code `1008` (`POLICY_VIOLATION`, RFC 6455 "policy violation") and a reason that never names more
about the filesystem than "not a registered root." `agent.terminal.enabled` in the original design's settings-JSON language became
`TerminalSettings.enabled`, one section of the agent server's settings root (`APP_TERMINAL__ENABLED`) beside `RunSettings`/`StoreSettings` in
`harness/settings.py`, the agent server's own idiom for a deployment toggle, not a key on the shared `application_settings.json`
(that file is the *knowledge base's* settings. The terminal has no knowledge base-domain meaning). `registered_knowledge_base_roots()` reads that settings
file fresh on every connection attempt rather than the `@cache`d reader the agent server's other settings lookups (the DB reader
URL, the embedding contract, the active knowledge base path) share. Those are boot-time bindings, but a knowledge base registered through the
Studio after this process started must pass gate 3 on the very next connection, not only after a restart.

**Gate 3 is a starting-point check, not a containment boundary.** It constrains where the child process is spawned, nothing more.
Once the pty is open, the child is an ordinary, unconfined process free to `cd` anywhere its own filesystem permissions allow (`cd /`
included). Nothing in this gate, or anywhere else in the terminal feature, restricts where a command run inside the pane can read,
write, or execute. The child also inherits this server's complete process environment. Any database DSNs, tokens, or model API
keys the agent server process holds are visible to whatever runs in the pane. An operator enabling `APP_TERMINAL__ENABLED` should
read both of those as: this is genuine shell access to the machine the Studio runs on, scoped only by *where it starts*.

**Wire framing** (documented authoritatively in `controller.py`'s module docstring, mirrored in `studio-client/src/components/terminal/
protocol.ts`'s header comment): binary frames carry raw pty bytes both ways. A text frame is JSON control, one shape today:
`{"type": "resize", "cols": <int>, "rows": <int>}`, both fields bounded `1..65535` (`struct.pack`'s own unsigned-short ceiling,
`PtySession.resize`'s `TIOCSWINSZ` call). Anything else sent as text (malformed JSON, an unrecognised `type`, an out-of-range
dimension) is dropped silently, never fatal to the pump.

**Session lifecycle** (`infrastructure/pty_session.py`): `pty.openpty()` + `subprocess.Popen`, with `preexec_fn` claiming the controlling terminal
(`setsid()` + `ioctl(TIOCSCTTY)`) since `Popen`'s `dup2`-based fd handoff never triggers the usual open()-acquires-tty behavior. Without
it, job control (`Ctrl-C`/`Ctrl-Z`) inside the pane would target the wrong process group. `close()` kills and reaps the child
(`SIGTERM`, then `SIGKILL` after a grace period) **before** closing the pty master fd, deliberately: the output pump's blocking
`os.read()` on an idle child can hang `os.close()` itself if the master fd closes out from under it, and killing the child first is
what actually unblocks that read. `close()` is idempotent and runs from every exit path (EOF, socket disconnect, or the route
handler's own `finally`), so nothing outlives a session.

---

## 5. Vocab governance

New vocabulary encountered while authoring (a facet value the registry doesn't yet recognise) is *proposed*, not written straight
into the registry. `engram.indexing.infrastructure.vocab_governance.VocabGovernance` (`engram/src/engram/indexing/infrastructure/vocab_governance.py`) owns one
knowledge base's `_meta/vocabulary/pending.yaml`: a list of `VocabProposal{facet, value, page_id, proposed_at}` rows, keyed by `(facet,
value)`. `propose` is idempotent on that key (a second sighting returns the existing row unchanged, not re-stamped). `approve` and
`reject` each address exactly one pending row.

`studio vocab propose|list|approve|reject` (`studio/src/studio/knowledge_base/cli_vocab.py`) is pure filesystem CLI plumbing over
`VocabGovernance`, no database, no container, the same composition style as `studio init scaffold`. `--knowledge-base` defaults to `.`.
`approve` and `reject` exit 1 with a clear message on an unaddressed `(facet, value)` (`ProposalUnknownError`), never a traceback.

**`approve` composes through the existing writer, never a second one.** It calls `engram.init.contracts.write_scaffold` (the same
union-merge `studio init scaffold` uses) to land the value into `_meta/vocabulary/facets.md`. A value that turns out to already be
registered is a clean no-op on the registry file, and either way the pending row clears. `write_scaffold`'s merge always re-renders
the *whole* `facets.md`, not just the section gaining a row: it reads the file's own frontmatter/title as a header template, then
regenerates every facet section as a table, values preserved. Two things worth knowing before running `approve` on a curated knowledge base:
a facet section written in the prose `Seed: a, b, ...` form is reformatted to table form on the *next* approval of *any* facet, and any
curated free-text gloss on an untouched facet (an intro sentence, a per-value provenance note) is not carried into the regenerated
file. This is pre-existing `write_scaffold` behavior, not something vocab governance changes. It just makes `approve` the everyday
path that triggers it.

**No locking between `approve` and `ingest`.** `approve` writes `_meta/vocabulary/facets.md` on disk. `ingest` (section 6) reads the registry
into a `VocabularyIndex` at the start of its own run. Nothing coordinates the two. An `ingest` already in flight when an `approve`
lands keeps validating against the registry it read at start, so a page whose only problem was the value `approve` just added can
still refuse. This is the same single-owner, no-locking posture the settings-registry write (section 2) carries: fine for one person
operating one knowledge base at a time, not a guarantee under concurrent writers.

**Facets are the only governed vocabulary today.** Entity types, relations, and claim types have no proposal path. The write validation
(section 6) still refuses them, it just has no CLI to point at yet.

---

## 6. Write validation

`IngestPageHandler` behind `studio ingest` checks a page's vocabulary against the knowledge base's indexed registry **before** writing
anything, not just before the first upsert: the check runs before `self._uow_factory()` is even called, so a refusal touches no
database, not even a read. This is the check `studio ingest` runs. `embedded_authoring`, the write path a saved workflow uses to write
pages through the workflow knowledge base's own registry of steps, runs it too, against the `_meta/vocabulary/` pages beside the schema file it is
given. A schema passed as an object has no vocabulary pages, so its pages are checked against an empty vocabulary.

`IngestPageHandler(..., registry: VocabularyIndex | None = None)` (`engram/src/engram/index/ingest.py`) runs
`validator.validation_vocabulary(note, registry, spec=spec)` (`engram/src/engram/index/validator.py`) when a registry is configured,
the page-local half of the indexer's Vocabulary rules: `unknown-entity-type`, `unknown-relation`, `unknown-facet-value`,
`unknown-kind`. Every other `validate` rule (duplicate ids, schema/vocabulary drift, quote checks, section length, code paths)
needs the whole knowledge base and stays index-time only. Any finding raises `IngestValidationError(findings)`
(`engram/src/engram/index/domain.py`). `registry=None` (no `_meta/vocabulary/` directory on the knowledge base) skips the check entirely,
the same degrade-to-empty `studio index` validation already gives an ungoverned knowledge base.

`studio.knowledge_base.wiring.index.vocabulary_provider` builds the `VocabularyIndex` from `<knowledge_base>/_meta/vocabulary/*.md` via the same
`parse_vocabulary` every other registry consumer uses. `studio ingest` (`studio/src/studio/knowledge_base/cli.py`) catches
`IngestValidationError`, prints every finding (`SEVERITY page_id [anchor] — rule: message`), and exits 1. When any finding is
`unknown-facet-value` it additionally prints the fix path: `studio vocab propose --facet <key> --value <value> --page <page id>`,
since that's the only rule with a governance CLI behind it (section 5). The other three name the unregistered value in their own message
with no propose hint.

---

## 6b. Keeping a run's value as a note

A workflow run produces values. `POST .../notes/preview` and `POST .../notes`
(`studio/src/studio/knowledge_base/http/controllers/notes.py`, over
`studio/src/studio/knowledge_base/retention/`) are how one of those values becomes a page in a
knowledge base the owner chooses, without ever writing over anybody's work.

**The value is named, never addressed.** A request carries an `ArtifactRef`
(`core/src/core/ai/artifacts.py`): which run, which node, and the digest of the value. This app
resolves it at the one harness address its own settings declare
(`infrastructure.harness.base_url`, overridable with
`APP_INFRASTRUCTURE__HARNESS__BASE_URL`), through `HarnessArtifactResolver`
(`studio/src/studio/knowledge_base/retention/infrastructure/artifact_resolver.py`). The client cannot supply that address,
this app imports no harness module, and the answer is validated back against the reference that
was asked about before anything reads it.

**A preview writes nothing.** The resolved value must itself be a `NoteDraft`
(`engram/src/engram/authoring/domain.py`): a page type, a title, a summary, the frontmatter the
schema declares, and the sections. A draft may not carry `id`, `type`, `title` or `summary` in its
frontmatter, because the destination assigns all four. The note's id is the page type's
`id_prefix` plus 24 hex characters of a digest over the origin's identity alone (its run, node,
turn, artifact id and value digest, never its label), so neither editing the words nor renaming
the artifact moves the file. The preview returns the draft normalized (with `created` stamped, so
its bytes do not change at midnight), the Markdown, the path, and two digests.

**Every request resolves the value, including the one that writes.** `NoteRetention._prepare`
resolves the origin and reads it as a proposed note before it looks at anything the caller
supplied, on both routes. An edited draft replaces that proposal, it never excuses it: a request
whose reference names no run, no artifact, or bytes that disagree with its digest is refused
whatever draft it carries, and a keep against a harness that will not answer writes nothing. So a
kept note always came from a value the host answered for.

**Keeping repeats every other check too.** `POST .../notes` re-validates against the current
schema and recomputes the preview: a schema that moved on, a draft that is not the one previewed,
or a request key already used for other content is refused before anything is written. The note is
then created with `core.files.atomic_create`, which publishes into a free name and refuses a taken
one outright, so an ordinary `write` after an exists check never happens here.

**The receipt crosses publication.** Before creating, the request's own record lands under
`<knowledge base>/.engram/retention/` (no page type's folder reaches it, so nothing indexes it),
carrying the target, the origin, and the digests of the bytes agreed. A crash between the file
landing and that record updating recovers by comparing the bytes on disk against the bytes the
record expected. Indexing is recorded separately: `saved_not_indexed` means the page is on disk
and the index did not run, and the identical request again indexes it and writes nothing.

**An edit always wins, and so does a removal.** A file at the target holding anything other than
these exact bytes is left exactly as it is and the answer is `changed` at 409, whoever wrote it
and whenever. A request whose record says it already published, meeting a target that is no longer
there, answers `changed` too rather than writing the note back: removing a page is the owner's own
decision about their own knowledge base. Retention never restores an older preview over either.

**Keeping it is the person's own act, and three suites prove different halves of it.**
`studio-client/e2e/automation-journey.spec.ts` drives the browser against both hosts mocked, so
what it proves is what the Studio does: pressing Keep as a note opens the preview and posts no
keep, a knowledge base nothing registers is never offered as a destination, changing a word in the
note takes the Keep button away until the destination has been asked about the new one, indexing
again after `saved_not_indexed` carries the same request key as the keep before it, and a value the
run no longer holds refuses at the press with the host's own sentence, leaving nothing previewed
and nothing kept. What this app then does with that key is its own, and
`studio/tests/unit/knowledge_base/http/test_note_retention.py` and
`studio/tests/integration/knowledge_base/test_note_retention_index.py` are where it is proven,
over the real routes and a real index. The harness half is
`harness/tests/integration/test_automation_journey.py::test_the_note_a_run_proposed_is_readable_and_offered_for_a_person_to_keep`:
the run fixes the proposed note as an addressable value, resolving it answers those exact bytes,
and the card offers it as an action a person presses rather than as a page the run wrote.

---

## 7. Knowledge base identity

Every command that touches a knowledge base opens by printing which knowledge base it is about to act on, and, when it also opens a database,
which database, at what revision, how fresh the index is, and how far the knowledge base has moved since. Two lines, printed
before the command does any other work.

```
knowledge base repo-docs at /Users/x/engram/docs
db localhost:5432/engram at 0034 (code expects 0034), last indexed 2026-08-26 23:50 UTC, ok, 2 files changed since
```

The first line is `knowledge_base <slug> at <root>`, the knowledge base's own `knowledge-base-schema.json` slug and the resolved path `--knowledge-base` (or the
env / `application_settings.json` default) landed on. A command that touches no database (`studio note *`, `studio vocab *`,
`studio source-queue *`, `studio watch-list *`, `studio decisions`, `studio decide`, `studio init scaffold`) prints only this one line. `studio init db` and
`studio init spec` print it too, once the path they act on is known. `studio worker` and `studio serve-http` print only the
knowledge base line as well, at startup. Both are long-lived processes serving requests as they arrive rather than acting on one
named collection up front, so there is no single "at what revision, how fresh" to report before the first request lands.

The second line, `db <host:port/dbname> at <revision> (code expects <revision>), last indexed <when>, <outcome>, <changed
tail>`, is `engram.identity.KnowledgeBaseIdentity` rendered (`engram.identity.read_identity`, called through the admin gateway,
since the index manifest and fingerprint live in the `kb` schema the read-only reader role cannot see). `<host:port/dbname>` is the database URL with its credentials stripped (`core.db.redact_db`, the one function every
printed or served database identifier goes through, so a username or password never reaches a terminal, a log, or an HTTP
response). `<when>` is local time with its zone abbreviation (`last_indexed_at.astimezone()`), never the raw UTC value the
database stores, so an afternoon local indexing run reads as an afternoon time, not as whatever hour UTC happened to land on. A
database with no `alembic_version` table yet reports `at no revision`. A knowledge base never indexed reports `last indexed never`
with no outcome slot, there is nothing to report an outcome for. An indexing run whose fingerprint is stale against a failed
refresh run reports the outcome `failed`, with the run's own detail appended in parentheses. `code expects <revision>` is
`engram.init.contracts.code_head_revision()`, the alembic head the installed code declares, independent of what the database is
actually at. `<when>` is `kb.index_fingerprint.indexed_at`, re-stamped by every indexing run that writes rows, full or
incremental, so it names the last time the collection actually took a write, not the last global-invalidator change.

The `<changed tail>` names how many files on disk differ from the stored index manifest, the same raw diff an incremental
index would start from, computed by walking the knowledge base with a `KnowledgeBaseReader` and comparing content hashes.
`2 files changed since` and `1 file changed since` name an indexed knowledge base with pending edits, `nothing changed since` names
one that is fully caught up. A knowledge base never indexed instead reads `193 files awaiting index` (`1 file awaiting index`
for a single file), since there is no prior manifest to diff against and every file on disk is the whole gap.

The tail also says when the next index rebuilds everything. The identity carries the indexer version the last index stored
(`KnowledgeBaseIdentity.indexer_version`, read from `kb.index_fingerprint` with the index time), and the line compares it
with the one the code and the settings produce now (the code's `INDEXER_VERSION`, the chunk sizes and the chunk planner), through
`studio.knowledge_base.wiring.indexing.indexer_version_changed`, the same comparison `studio status` makes for its
`Manifest stale` hint. When they differ, `nothing changed since` reads `the indexer or its chunk settings changed since, the
next index rebuilds everything`, and `2 files changed since` reads `2 files and the indexer or its chunk settings changed
since, the next index rebuilds everything`.

**An unreachable database is one line, never a traceback.** A dead port, an unresolvable host, a wrong password, or a wrong
database name each end the identity read the same way, a single line naming the redacted target and the reason.

```
db localhost:5434/knowledge_bases unreachable, connection refused.
```

The reason is one of `connection refused`, `host not found`, `timed out`, `authentication failed`, `database does not
exist`, or the exception's own class name for a case not yet mapped. Every command that opens a database exits 1 right
after printing this line, with no traceback and no `search failed` (or `index failed`, or any other) prefix. `studio mcp`
is the one exception. A long-lived server has nothing to exit into, so it prints `serving anyway, every call fails until
the database is reachable.` as a third line and still builds and serves its tool surface, every tool call failing cleanly
once it actually reads the database, rather than the whole server dying at startup on a database that has not come up yet.
Its reader role is then checked on each new connection, so no read runs until the database answers with a role that may
only read. Since it could not check its role at launch, it builds no authoring and no documentation-authoring tool, and
prints one stderr line saying so. Once the database answers, `search` and `fetch` answer with no restart. Each answer's
stamp still reads freshness, and once Postgres refuses that read, as it does when the reader role is in both database
URLs, the stamp names the knowledge base alone and reads nothing more. A wrong password and a database name the server does not hold count as unreachable
here too, though SQLAlchemy reports them wrapped in its own error.

`studio mcp` also serves when its role may not read the index state, as when the knowledge base reader role is in both
database URLs (the harness launches it that way, naming the knowledge base with `--knowledge-base`). Postgres refuses
the identity read there, so the server prints `database: this role may not read the index state, so it is not shown.
Serving read-only.` in place of the database line, and every answer's stamp names its knowledge base with no freshness
read. A read-only server also builds no authoring and no documentation-authoring tool, whatever opts in, and prints one
stderr line naming what it left out. Every line `studio mcp` prints before it serves, a refusal such as `No knowledge base named` included, goes to
stderr, because an MCP client reads the server's stdout as JSON-RPC. A missing knowledge base folder, a folder with no
`knowledge-base-schema.json` and a schema that does not load are each refused in one line, with no traceback.

**A connection that fails later names its service.** A command that reports its failure as one line (`<command> failed:
<error text>`, such as `evals score failed` or `search failed`) names the service when the failure came from a
connection: the embedding endpoint or a model endpoint with its host, or the database with its redacted URL, before the
error's own text.

```
evals score failed: embedding endpoint example.openai.azure.com: Connection error.
evals run failed: database localhost:5434/knowledge_bases: [Errno 61] Connect call failed ('127.0.0.1', 5434)
```

`studio index`, `studio ingest` and `studio sync` print the same line for a failed connection (`index failed: ...`,
`ingest failed: ...`, `sync failed: ...`) and exit 1 with no traceback, so the last line of `studio index`, the one the
plugin's session-start hook shows, names the service. Any other error those three commands meet still raises as before.

```
index failed: embedding endpoint example.openai.azure.com: Connection error.
```

The text comes from `core.connection_failures.connection_failure_message`, the same rule an evaluation run uses for
a failed case. Any other failure keeps its own text unchanged.

**A collection belongs to the knowledge base root that indexed it.** Each indexing run stamps its knowledge base root onto
`kb.index_fingerprint.knowledge_base_root`. A later indexing run from a different root, with `--collection` omitted, refuses
before writing a single row and exits 2, because the indexing run's prune would delete everything the owning knowledge base put there.
Two knowledge base roots whose schemas name the same collection meet this refusal:

```
collection research was indexed from /Users/x/research-vault, this knowledge base is at /tmp/scratch. Pass --collection research
to take the collection over, or index the knowledge base that owns it.
```

Passing `--collection <name>` explicitly is the deliberate takeover (`resolve_partition`'s standing contract): the index
runs as a full index and re-stamps the root. A `--dry-run` never refuses, its preview of the would-be prune is the
warning. A row whose root is empty, such as one a retrieval switch write created before any indexing run, reads as
unclaimed and is claimed by its next indexing run.

**`status`, `search`, and `fetch` no longer default `--collection` to the literal `"research"`.** Each now resolves an
omitted `--collection` from the knowledge base's own declared `retrieval.collection` (`resolve_partition`, the same partition
`index` and `ingest` already used), so a `--knowledge-base` pointed at a differently-named collection never silently reads or
reports on `research` rows that knowledge base never wrote. `--collection` stays a deliberate override when given explicitly.

**`GET /api/knowledge-bases/{name}` carries the same identity** on its `identity: KnowledgeBaseIdentity | None` field (section 2's tenth addition),
read the same way, through the request's own admin gateway and its own `FilesystemKnowledgeBaseReader` over that knowledge base's root. It
is populated only for a *registered* name, since `read_identity` needs the knowledge base's own root and slug, which only a
registered knowledge base carries. An indexed-only name (rows in Postgres under a name nothing registered, section 2's four-state
reconciliation) answers `identity: null` alongside its otherwise-populated row.

**The plugin's session-start hook (`engram/plugin/hooks/nudge.sh`) prints these same two lines.** When `./knowledge-base-schema.json`
exists, the hook runs `studio status --knowledge-base .` and shows its first two lines before its own pending-decisions check, so a
session opens already knowing the knowledge base, the database, and how many files are waiting on the next index, with no query
of its own to run first. A failing or unreachable database still shows its one clean line here rather than derailing the
hook, which never fails the session regardless of what the identity read finds.

`submit_feedback` appends every piece of agent feedback to a knowledge base's `_meta/mcp-feedback.jsonl`. `studio feedback` lists it
newest first with each entry's category, named tool and age, and `--since YYYY-MM-DD` narrows the listing to entries filed on
or after that date. A line that will not parse is named by its line number rather than dropped in silence. A `index` whose
knowledge base holds entries filed after its last index ends its report with a line naming how many, read with `studio feedback`.
