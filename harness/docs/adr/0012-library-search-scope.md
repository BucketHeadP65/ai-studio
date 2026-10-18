---
id: harness-adr-0012-library-search-scope
aliases: ["harness-adr-0012-library-search-scope"]
type: adr
app: harness
status: accepted
tags: []
title: "0012 — The library searches the session's own base by default, and a registry row can stay out of it"
---

# 0012 — The library searches the session's own base by default, and a registry row can stay out of it

**Status:** Accepted (2026-08-29). Amended by [0016](0016-every-knowledge-base-by-default.md) on 2026-09-02: the
session-knowledge_base default below is withdrawn and a `knowledge_base`-less search reads every visible knowledge base, the collection
stamp on every hit and both privacy declarations stay.

## Context

The studio chat agent's `search` tool is the library: `studio_library()` (`composition.py`) builds engram's
`embedded_library` over one `KnowledgeBaseConnection` per row of the knowledge base registry (`engram/config/application_settings.json`,
`knowledge_bases`) plus this checkout's own `repo-docs`. When the model named no `knowledge_base`, `RetrievalKnowledgeBase.run_many` fanned out
to every surface and merged by score. A Logfire trace of the agent answering "do you know the documentation ceremony of
this repo?" showed three searches whose top hit was a page from a private vault. That vault is registered so the
studio plugin can index and serve it inside the vault, and the registration made it searchable from any studio
session. The model had no way to tell: a `SearchHit` carried no collection, and the tool's description never said what an
omitted `knowledge_base` meant.

## Decision

- **The library takes a host-owned default.** engram's `RetrievalKnowledgeBase` gains `default_knowledge_base_source:
  Callable[[], str | None] | None`, read on every call in the same way as `k_source` (engram ADR-0041). With `knowledge base`
  omitted, a name the source returns that is one of the surfaces scopes the search to that surface; no source, `None`,
  or a name that is not a surface keeps the full fan-out. The literal `knowledge_base="all"` always fans out. An unknown name is
  refused with the list of bases and `"all"`. `embedded_library` threads the source. The `search` description states the
  default scope and how to widen it. engram stays domain-general: it knows "the host prefers this base right now" and
  nothing about sessions or projects.
- **Hits say where they came from.** core's `SearchHit` gains `collection: str | None`; the library stamps every hit
  with its surface's name, whether the search ran on one surface or on all of them.
- **The harness names the session's base.** `harness.session` carries `ACTIVE_WORKSPACE_ROOT`, a context var bound by
  `bind_workspace_root(root)` beside `ACTIVE_RUN`. The plain chat door binds it around `agent.run`; the streamed door
  binds it around the draw of its chunk generator (`_workspace_scoped_chunks`), because the run happens in the task that
  draws the stream, not the one that built it. `composition._session_default_knowledge_base()` reads the var (the monorepo
  workspace, `settings.WORKSPACE`, when a session has no workspace of its own), maps it through
  `adapters.knowledge_base.default_knowledge_base_for(root, roots)`, the registered knowledge base whose path sits inside that root, the deepest
  when several do, with `repo-docs` (`docs/`) as the one inside the monorepo workspace, and answers `None` when no
  registered knowledge base sits inside it. The roots are keyed by each knowledge base's spec slug, the name `search` takes as `knowledge_base`.
- **A registry row can stay out of the library, and so can a knowledge base itself.** `registered_knowledge_bases` skips a row carrying
  `"library": false`, logged at debug. A knowledge base may also declare `retrieval.library: false` in its own
  `knowledge-base-schema.json` (engram's `RetrievalSpec`); either declaration keeps it out, checked and logged separately so a
  reader can tell which side said so. A knowledge base that declares itself private in its own
  `knowledge-base-schema.json` makes the gitignored `engram/config/application_settings.json` row a per-machine backstop
  rather than the only place the privacy is recorded. A fresh checkout that has not yet re-registered the knowledge base
  still keeps it out of the library once the vault itself is checked out. Inside the harness either flag removes the knowledge base from
  everything `studio_library` mounts, the read doors and, with them, the authoring doors and the agent-memory store
  for that knowledge base. The studio's own registry, the plugin's MCP server, and the knowledge-bases station (including the
  terminal's registered-root check) are unaffected.

## Consequences

- (+) A session's default search is its own project's base, and the answer to a question about this repo comes from
  `repo-docs`. Widening is one explicit word, `knowledge_base="all"`.
- (+) Every hit names its base, so the model can see and say where an answer came from, and a mixed result set is
  legible rather than silently merged.
- (+) A private vault stays out of every studio session's library while it keeps serving its own plugin.
- (−) A session whose workspace holds no registered knowledge base gets the full fan-out over the library's bases, not
  `repo-docs`. Registering that project's knowledge base is the fix, not a special case here.
- (−) The registry's own flag is per machine and gitignored; the knowledge base's declaration is the one that travels, so a
  knowledge base that wants to stay private should say so in its own `knowledge-base-schema.json` rather than rely on every registry.
- (−) The default is decided by path containment: a knowledge base at the root itself, else the deepest one inside it. A session
  rooted above several registered knowledge bases (a home directory) resolves to the deepest of them rather than to none, which
  is a wrong guess only for a session nobody opens today.
