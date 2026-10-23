---
id: deferred-decisions
aliases: [deferred-decisions]
type: explanation
app: root
status: current
tags: [decisions, record]
title: "Deferred decisions, what we chose not to adopt and what would make us look again"
---

# Deferred decisions

The record of things this repo considered and deliberately did not adopt. Not debt: nothing here is wrong today. Each entry is a
choice that was right under the conditions we had, and each names the condition that would make it wrong. When that condition
arrives, the entry is the argument to reopen rather than a decision to defend.

A decision that shaped the architecture gets an ADR. A choice not to adopt something gets an entry here, and an entry may point at
the ADR or spec that carries the full reasoning. Code that is worse than it should be goes in [technical-debt.md](technical-debt.md).

Entry shape: what was not adopted, what we do instead, why not, what would make us look again, where the reasoning lives.

## DD-1. No message broker

- **Not adopted:** a message bus or broker for coordination between processes, runs and sessions.
- **Instead:** durable run state, the run engine defaulting to Temporal with an in-process engine for local boots, a job table in
  Postgres, and direct in-process addressing for anything session to session.
- **Why not:** one machine, one owner, one Postgres, three long-lived processes, and one real event source. A broker adds a failure
  domain without solving a constraint we have. `SessionPool` already holds every live runtime in process, keyed by session id, so
  reaching another session is a lookup and a queue.
- **Look again when:** the deployment is more than one machine, or more than one process family needs to address the same session,
  or a relay has to survive a restart (see DD-8).
- **Where:** `spec-2026-08-26-durable-coordination-design`, root ADR-0002, and `bs.follow-me-s3-orchestrator-comms`.

## DD-2. The chat plugin answers in prose, not a typed envelope

- **Not adopted:** `PromptedOutput(Reply)`, the five-field JSON object the SPA chat agent has answered with.
- **Instead:** text output, with a draft reaching the screen through `check_draft`, which gains the card fields.
- **Why not:** the envelope's own payload is already distrusted (`settled_draft` re-serialises from the checked object), it forces
  every turn into DeepSeek's `json_object` mode whose guide admits it "may occasionally return empty content", and the resulting
  validator retry reaches the model in the owner's voice.
- **Look again when:** a plugin's whole answer is data rather than prose for a person. `build_agent` keeps `output_type` as a
  parameter, so a future agent asks for a typed output without reopening this.
- **Where:** `spec-2026-08-26-chat-reply-contract-design`, section 1.1 for the provider evidence.

## DD-3. Structured output stays available, just not on the chat reply

- **Not adopted:** removing typed outputs from the codebase.
- **Instead:** workflow steps keep their `output_type`, the validator type-checks the chain against it, and judges read a bool
  field off the step's own model.
- **Why not:** structured output earns its place when a whole answer is data. That is what a step is and what a chat reply is not.
- **Look again when:** never as a whole. Per agent, when its answer is data.
- **Where:** `core/src/core/ai/workflows/_validator.py`, and DD-2.

## DD-4. The chat plugin stays on DeepSeek rather than moving to a model whose structured output holds

- **Not adopted:** switching the SPA chat plugin's default to `gpt-5.4`, whose native `json_schema` output would have made the
  envelope work.
- **Instead:** the contract changes rather than the model, so any catalog model behaves.
- **Why not:** a contract that only works on some models is a contract that breaks whenever the owner switches, which they do at
  runtime.
- **Look again when:** the chat plugin's failure profile turns out to be model-specific in some other way that the contract cannot
  absorb.
- **Where:** DD-2.

## DD-5. The DeepSeek JSON-mode mitigations were not built

- **Not adopted:** a custom `PromptedOutput` template carrying an example instance (their guide asks for one), widening
  `EmptyAnswerRetry` to fire on empty content whatever the parts are, and setting `max_tokens` so the JSON cannot truncate.
- **Instead:** DD-2 removes the JSON mode entirely, which makes all three unnecessary.
- **Why not:** three patches to keep a contract we decided to drop.
- **Look again when:** the envelope comes back for any plugin, or another agent here uses prompted output against DeepSeek.
- **Where:** `spec-2026-08-26-chat-reply-contract-design`, section 1.1.

## DD-6. The deferred marker is gated on the follow-me plugin, not always on

- **Not adopted:** `output_type=[str, DeferredToolRequests]` on every construction.
- **Instead:** the marker is included when `build_agent` receives a `follow_me` capability, and the output is plain `str` otherwise.
- **Why not:** nothing else in this app ends a run deferred. `NamedToolDeferral` is lazy loading, a different mechanism, and the only
  other deferring thing is follow-me's own approval toolset behind the same plugin. ACP keeps a narrow `Agent[Runtime, str]` with
  nothing to narrow at its call sites.
- **Look again when:** a second capability outside the follow-me plugin needs a run to end deferred.
- **Where:** `spec-2026-08-26-chat-reply-contract-design`, D1.

## DD-7. The files plugin stays open

- **Not adopted:** closing the files plugin because it duplicates the shell.
- **Instead:** each plugin's copy now says what it is for beside its sibling, and the duplication is carried as TD-2.
- **Why not:** NoteOverlay rides `read_file`'s return, so the owner notes would stop reaching the agent. And the files plugin is
  the guarded surface, the shell is the unguarded one (TD-1), so closing it removes the guard and keeps the hole.
- **Look again when:** a live session with the plugin closed shows the agent working comfortably through the shell alone, or TD-1 is
  paid off so closing it no longer widens what is readable.
- **Where:** TD-1, TD-2.

## DD-8. A cross-session relay would be live only, with no durability

- **Not adopted:** persisting relayed messages so they survive a restart, in the first slice of orchestrator comms.
- **Instead:** a live-only channel, where a message to a session that is gone fails visibly.
- **Why not:** the working implementation we are copying is a unix socket per session with an in-memory queue at the receiver, and
  it has carried real multi-agent coordination. Durability is the expensive half and nothing has yet needed it.
- **Look again when:** a relay is worth retrying after a restart, or mid-run injection lands, where an injected instruction has to
  survive whatever the run's own steps survive.
- **Where:** `bs.follow-me-s3-orchestrator-comms`.

## DD-9. Plugin answers keep the "label: detail" shape

- **Not adopted:** rewriting every plugin answer to drop the colon, which the punctuation rule for owner-facing copy would otherwise
  forbid.
- **Instead:** the existing shape stands (`already on the ledger: arxiv-2401.01234 [requested]`), and the rule is read as targeting
  em dashes, semicolons and ellipses.
- **Why not:** every engram plugin already answers this way, and the tests pin the strings, so a sweep touches every plugin at once.
- **Look again when:** the plugin answers are being revised for another reason, so the sweep rides along.
- **Where:** raised in the Task 5 review, 2026-08-26.

## DD-10. No users, no logins

- **Not adopted:** identity for the harness. Actor names are free text, `"owner"` by convention.
- **Instead:** one owner, one deployment, and a machine actor stamp where a message is not from a person.
- **Why not:** a single-owner deployment. Identity is a subsystem, not a field.
- **Look again when:** a second person uses the studio, or a relayed message needs to prove which agent sent it.
- **Where:** deferred 2026-08-25.

## DD-11. The agent authors schedules, under the owner's approval

- **Not adopted:** the restriction that only the owner's own plugins may create a schedule.
- **Instead:** `harness/src/harness/features/authoring/process.py:39` mounts `schedule_workflow`, `update_schedule`,
  `delete_schedule` and `run_schedule_now` on the chat agent. The three authoring verbs wait for the owner's approval
  in every session, not only a master one (`needs_approval` in `harness/src/harness/features/followme/acts.py`, over
  `SCHEDULE_AUTHORING_ACTS` in `harness/src/harness/features/followme/domain.py`). `run_schedule_now` does not wait,
  because it fires a schedule the owner already approved rather than standing one up or changing one.
- **Why not:** the owner drives the studio through the chat, and a schedule the owner asked for in conversation is
  the owner's schedule. Requiring them to leave the chat for the HTTP API to get it would be the worse surface. The
  approval card is the governance the ADR said was owed: the owner sees every standing trigger before it exists.
- **Look again when:** the owner approves schedule requests without reading them, or standing schedules pile up past
  what one approval at a time keeps in view. That is the point a cap or a schedules listing in the chat earns its place.
- **Where:** `harness/docs/adr/0006-schedules-and-the-ticker.md`, the superseding note of 2026-08-28. Found 2026-08-27
  in the record sweep.


## DD-12. No uv workspace, shared Python code ships as path sources

- **Not adopted:** a uv workspace over the Python projects.
- **Instead:** every project keeps its own in-project venv and lockfile and names the shared code as a path source
  (`studio/pyproject.toml:51-53`, `engram/pyproject.toml:63`, `harness/pyproject.toml:79-81`). The frontend is one
  npm package, `studio-client`, whose shared code lives in `src/shared/` (root ADR 0021), so no npm workspace is needed.
- **Why not:** a workspace puts every project on one shared lockfile, and each project here keeps its own.
- **Look again when:** keeping the separate lockfiles in step costs more than the independence they buy.
- **Where:** root `CLAUDE.md` (Package Management).

## DD-13. One database with logical collection partitions, not a schema or a database per knowledge base

- **Not adopted:** a Postgres schema per knowledge base, or a database per knowledge base.
- **Instead:** one `kb` schema where every unit-level table keys on `(collection, id)` (`engram/src/engram/indexing/infrastructure/orm.py:144-145`,
  and the same pair at the four other levels) and every `api` function takes the collection as its first argument
  (`engram/alembic/versions/0025_corpus_qualified_identity.py`).
- **Why not:** one owner, one database, and a retrieval surface that answers for any knowledge base without a second connection or a second
  migration chain.
- **Look again when:** two knowledge bases on one database cost each other rows, or a knowledge base needs credentials of its own. TD-21 is that first
  cost, and a defaulted collection name is all it takes to pay it.
- **Where:** `engram/src/engram/indexing/infrastructure/orm.py`, revision `0025_corpus_qualified_identity`.

## DD-14. One process holds every live session, no process per session and no supervisor

- **Not adopted:** a process per session with a supervisor above them.
- **Instead:** `SessionPool` holds every open session's runtime in one process, over one shared agent, registry of steps, library and
  custom-capability store (`harness/src/harness/session.py:918-929`).
- **Why not:** a second agent per session means a second of everything underneath it, and the shared objects carry no per-session
  state, so the isolation would buy nothing but memory.
- **Look again when:** one crash taking every live session with it actually costs a session's work, or a second machine appears. DD-1
  names the same second trigger for the broker question, which this is not.
- **Where:** `harness/src/harness/session.py:918-929`.

## DD-15. A schedule fire that is held is dropped, never queued

- **Not adopted:** a queue of held fires, replayed once the block clears.
- **Instead:** a fire held by its own overlap or by a blackout window advances `next_run_at` to the cadence's next legitimate
  occurrence and the held one is gone. `catch_up` (default `False`) is the per-schedule opt-in that holds `next_run_at` in place so the
  first tick after the block fires it (`harness/src/harness/features/scheduling/domain.py:196-223`,
  `harness/src/harness/features/scheduling/service.py:201-239`).
- **Why not:** a backlog of catch-up fires on an hourly schedule is worse than a missed hour, and a full capacity pool already retries
  on the next tick whatever `catch_up` says.
- **Look again when:** a schedule must not miss its window (a daily refresh whose source keeps only one day), or the owner wants to see
  what was dropped, which nothing records today.
- **Where:** `harness/src/harness/features/scheduling/service.py:201-239`.

## DD-16. Re-saving the current version of a workflow is allowed, only a superseded version is refused

- **Not adopted:** immutable versions, where a second save at the same version is refused.
- **Instead:** `LibraryCollection.put` refuses only a version something has already superseded, and says why. The current version of
  anything re-saves freely as an upsert (`core/src/core/ai/workflows/adapters/library_collection.py:550-590`).
- **Why not:** an agent iterating on a draft would otherwise burn a version number per attempt, and re-minting a superseded page would
  erase a `superseded_by` link nothing could restore.
- **Look again when:** a saved version has to stand as evidence rather than as the current draft. The fingerprint and lineage a save
  records are what a re-save replaces, and the folder library claims the opposite in its own docstring (TD-30).
- **Where:** `core/src/core/ai/workflows/adapters/library_collection.py:550-590`.

## DD-17. A source mirror is disposable, a hand edit to one is not preserved

- **Not adopted:** treating a mirror as editable, with a sync that merges rather than overwrites.
- **Instead:** the connector is the truth and a mirror is a projection of it. `write` overwrites unconditionally, so a re-sync that
  decides RECONVERT or REFRESH for that item rewrites the note and renames it on a title change. The record that is never overwritten
  is the `memory.sources_raw` row (`engram/src/engram/sources/mirror/store.py:13-15`,
  `engram/src/engram/sources/mirror/domain.py:27-30`, `engram/src/engram/sources/sync/handlers.py:5`).
- **Why not:** an editable mirror is a second source of truth to reconcile on every sync, and the note that cites a mirror is where an
  owner's own words belong.
- **Look again when:** a mirror is the only place a correction can live (a fixed OCR line in a converted PDF has nowhere else to go),
  and nothing warns that the next sync will take it.
- **Where:** `engram/src/engram/sources/mirror/store.py`, `engram/src/engram/sources/sync/handlers.py`.

## DD-18. The read tools name a knowledge base by its collection, never by its knowledge base name or path

- **Not adopted:** an alias or the knowledge base's folder name on `api.about`, so an agent could map the owner's name for a knowledge base
  ("the Knowledge base") onto the collection the read tools take.
- **Instead:** the collection name is the one handle (`research`, `repo-docs`), and `api.about` lists the ones reachable here. The
  registered knowledge base on the studio API (`GET /api/knowledge-bases`) is where the path lives, and only the owner's own tooling
  reads it.
- **Why not:** a knowledge base's path is a host filesystem detail, the read tools are read-only over the indexed copy, and the owner wants an agent
  to know what a knowledge base covers, not where its files sit.
- **Look again when:** an agent has to resolve a human's name for a knowledge base onto a collection more than once per session, or
  a second machine mounts the same knowledge base under another path.
- **Where:** `api.about`, `studio/src/studio/knowledge_base/http/api.py` (`GET /api/knowledge-bases`).

## DD-19. A template is a directory tree, not an installable distribution

- **Not adopted:** publishing each knowledge base template as its own package (or its own repository), versioned and installed like
  `engram` or `engram-providers`.
- **Instead:** `templates/<slug>/` is a folder in this checkout holding the knowledge base's `knowledge-base-schema.json`, its `_meta/` guidance pages,
  its `workflows/{steps,workflows}/` and a README. `studio init --template <slug>` resolves it by repo path or by the
  `ENGRAM_TEMPLATES` environment variable, and `harness import templates/<slug>/workflows` imports its workflows from where they sit.
- **Why not:** there is one maintainer and one checkout, so a version number, a release step and a resolver would buy nothing that
  a path does not already give. A template is read at two moments only (initializing a knowledge base, importing its workflows), and neither is
  a runtime dependency that a lockfile has to pin.
- **Look again when:** a template has a consumer outside this repo, or two knowledge bases need to sit on different versions of the same
  template, or someone has to install one without cloning the monorepo.
- **Where:** root ADR-0018, `templates/`, `engram/src/engram/init/infrastructure/templates.py`.

## DD-20. No drift report between a knowledge base and the template it came from

- **Not adopted:** a command that diffs a knowledge base's `knowledge-base-schema.json` and `_meta/` guidance pages against the template they were
  copied from, and reports what moved apart.
- **Instead:** the copy is made once at `studio init --template` and nothing compares them again. A knowledge base's schema is its own truth
  from that moment, and indexing validates it against the grammar rather than against its origin.
- **Why not:** divergence is the expected outcome, not a fault. The knowledge base is where its page types and vocabulary get worked out
  in use, so a report would flag intended edits as findings, and there is no owner action a finding would lead to while the template
  cannot push anything back (DD-21).
- **Look again when:** a template gains a fix that several knowledge bases should pick up, and no one can tell which of them still matches it,
  or a template is published for others (DD-19) and its consumers need to know what they changed.
- **Where:** root ADR-0018 (D7), `studio init --template`.

## DD-21. A template never pushes an update into a knowledge base that copied it

- **Not adopted:** an update channel, a merge, or any upgrade path from a template into the knowledge bases initialized from it.
- **Instead:** the template is read once. The knowledge base owns its copy of the schema and the guidance pages outright, and a
  later template change reaches an existing knowledge base only if someone copies it by hand.
- **Why not:** a push means a merge, a merge means conflicts on a file the owner edits by hand as the knowledge base evolves, and the
  guidance pages are exactly the ones a knowledge base is expected to rewrite in its own words. One-way copy has no failure mode.
- **Look again when:** a template change is a correction rather than a template edit (a page type whose validator rule was wrong), and
  hand-copying it into every knowledge base is more error-prone than a reviewed merge would be. DD-20's report is the prerequisite: nothing
  can be pushed before something can say what differs.
- **Where:** root ADR-0018 (D7), `templates/research/README.md`.

## DD-22. The semantic leg of `api.search` never reads the HNSW index

- **Not adopted:** an approximate nearest-neighbour path through the HNSW index for the semantic leg, with iterative scan above a
  pool size and the result sorted again by exact distance.
- **Instead:** each semantic leg orders its pool by exact distance (`(embedding <=> query_vec) + 0`, which the planner cannot serve
  from the index) and stops at its budget.
- **Why not:** the largest tier holds about 9,000 embedded rows. An exact scan returns the true nearest rows with no recall loss and
  no planner settings to keep in a separate function, and a whole hybrid search over 8,783 sections took 254 ms.
- **Look again when:** a tier's pool grows into the tens of thousands of rows, or the semantic leg becomes the largest part of a
  search's time.
- **Where:** engram ADR-0048, `core/src/core/ai/retrieval/search_builder.py` (`_budgeted_vector_leg`).

## DD-23. No keyword search dialect computed in plain SQL

- **Not adopted:** a third search dialect that computes BM25 in plain SQL from `tsvector` positions and statistics tables, for a
  managed Postgres that offers neither `pg_textsearch` nor ParadeDB.
- **Instead:** the two dialects of engram ADR-0006 and ADR-0017, `pg_textsearch` by default and ParadeDB's `pg_search` as the opt-in.
- **Why not:** the managed Postgres target ships `pg_textsearch`, so a third dialect would carry a second ranking implementation,
  its statistics tables and its parity tests for no target that needs it.
- **Look again when:** a deployment target cannot install `pg_textsearch` or ParadeDB.
- **Where:** engram ADR-0006, engram ADR-0017.

## DD-24. Search hits keep four levels, and a surface keeps one text and one vector column with text keys

- **Not adopted:** named fields per surface that each declare the ways they support, an item scored by its best text, and a
  namespace and key type per search schema.
- **Instead:** `SearchSchema` keeps one text column and one embedding column per surface, text ids, and four hit levels (card,
  section, claim, raw), each returned as its own row with its page or source.
- **Why not:** the owner ruled on 2026-10-11 to keep the multi-level hit model. Every knowledge base indexed today has text ids, and
  a hit at its own level is what `fetch` reads next.
- **Look again when:** a caller needs a key type other than text, or a knowledge base needs one item scored by the best of several
  texts.
- **Where:** engram ADR-0035, `core/src/core/ai/retrieval/schema.py` (`SearchSchema`, `SearchSurface`).
