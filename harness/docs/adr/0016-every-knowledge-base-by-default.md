---
id: harness-adr-0016-every-knowledge-base-by-default
aliases: ["harness-adr-0016-every-knowledge-base-by-default"]
type: adr
app: harness
status: accepted
tags: []
title: "0016 — Every knowledge base by default, and no knowledge base of the harness's own"
---

# 0016 — Every knowledge base by default, and no knowledge base of the harness's own

**Status:** Accepted (2026-09-02). Amends [0012](0012-library-search-scope.md): the session-knowledge-base default is
withdrawn, the collection stamp on every hit and the two privacy declarations stay.

**Amended (2026-09-02), naming the knowledge base is mandatory.** On the owner's ruling the same day, engram now enforces
that a read names the base it reads. Over several knowledge bases, `search` and `fetch` take a required `knowledge_base` argument
whose description lists the names, and a call naming none of them, naming one that does not exist, or naming `"all"` is
refused with that list. The graph tools (`graph_expand`, `find_paths`) follow the same rule: an omitted `knowledge_base` routes
to the host default only while cross-search is on, else to the sole base that has earned a graph, else it is refused.
Reading every base at once is no longer what an unnamed call means: it is a host setting, `RetrievalKnowledgeBase.cross_search`,
threaded by `embedded_library(cross_search=...)`. The setting itself defaults to false, and a deployment turns it on by
exporting `APP_LIBRARY_SEARCH__CROSS_KNOWLEDGE_BASE_SEARCH=true`. `default_knowledge_base_source` is consulted only while that setting is on,
since an unnamed read is a refusal otherwise. What the decision below still holds is the scope: every
registered knowledge base is reachable, and the harness names none of them as its own. What changes is who says which one
a given call reads, and it is the agent, on every call.

**Amended (2026-09-03), the registry of knowledge bases is the studio's.** On the owner's ruling that a boundary is a
contract and never a shared file, the harness no longer reads which knowledge bases exist from engram's settings file, and
no longer opens a knowledge base's schema file from its folder. It asks the studio's HTTP API: `GET /api/knowledge-bases` for the
names, folders, privacy and collection of every knowledge base, and `GET /api/knowledge-bases/{name}/spec` for each schema, through
the one studio client the harness already holds. A studio that does not answer at boot is a refusal naming the studio,
never an empty registry. The two reads of engram's settings file that remain in the harness are the database URLs and the
embedding contract, both deployment resources the studio does not serve.

## Context

0012 gave `search` a host-owned default so a `knowledge_base`-less search read the knowledge base inside the turn's workspace,
after a trace showed a private vault's pages answering a question about this repo. Two things it built are what actually
fixed that: every hit now carries its `collection`, and a knowledge base can declare itself out of the library from either side.
The default was the third, and it narrows the model to one base at the moment it has said least about what it wants.

Everything else in the harness had already gone multi-knowledge_base. `studio_library()` opens one door per registry row plus
`repo-docs`, the write doors take an explicit `knowledge_base`, and `run_sql` reaches every collection the privacy predicate
`api.visible_collection` allows. Two leftovers still pinned the process to one base and refused to boot without it:
`AgentKnowledgeBaseSettings.name`, read from a required `APP_NAME`, and the session default itself.

## Decision

- **A `search` with no `knowledge_base` argument reads every visible knowledge base.** `harness_session` passes no
  `default_knowledge_base_source`, and `session_knowledge_base_source`, `_session_default_knowledge_base` and `default_knowledge_base_for` are deleted.
  `studio_library` keeps the parameter, which is engram's contract, and passes none. `ACTIVE_WORKSPACE_ROOT` still binds
  a turn to its session's project, and no longer chooses a knowledge base.
- **The SQL door speaks for every visible knowledge base.** `knowledge_base_capability()` builds one `SqlKnowledgeBase` from
  `registered_knowledge_bases()` plus the repo-docs target. Its `domain` is one block per base in registry order, each base's own
  indexed `api.about` body under its collection name, the blocks separated by a rule because a indexed body is itself
  several markdown paragraphs. `describe_schema` is grounded in every target's spec:
  engram's `DescribeSchemaHandler` now takes a sequence, renders the migration-baked text unchanged when it holds more
  than one, and follows it with one block per base naming that base's slug, page kinds, two example ids in its own
  shape, and its decision kinds. `embedded_knowledge_base` splits its old `spec` in two: `spec` is the knowledge base a door IS,
  deciding the collection it reads as and whether it is `Earned`, and `grounding` is what `describe_schema` names beside
  it. This door passes `spec=None`, so it reads anonymously, seeing exactly what the privacy predicate allows, and is
  never `Earned` however many bases the registry resolves to or whichever retrieval mode they declare.
- **The SQL door's guidance is the deployment's authored floor alone** (`knowledge_base_guidance(None)`). A base's applied
  retrieval policy rides on its own search door inside `studio_library`, where it speaks for one knowledge base rather than all
  of them. `engram_knowledge_base_path()` is deleted with the single-knowledge_base checkout it resolved.
- **The harness mounts no knowledge base of its own.** `AgentKnowledgeBaseSettings`, `knowledge_base_settings()`, `_active_knowledge_base_spec()` and
  `APP_NAME` are deleted, with their tests. `scripts/stack/harness.sh` exports nothing.

## Consequences

- (+) A question reaches every knowledge base the deployment can see, and each hit still names the base it came from, so
  the model narrows with `knowledge_base=<slug>` when it wants one rather than being narrowed before it asks.
- (+) `harness serve` boots with no environment at all: with 0015's engine default, nothing is required to start it.
- (+) A deployment restored from a database dump, with no checkout on disk, opens the SQL door on whatever collections it
  carries, instead of refusing because no registry entry names one.
- (−) A `knowledge_base`-less search costs more than a scoped one: it runs on every door and merges. Scoping is one argument, and
  the tool description says so.
- (−) The SQL door's `describe_schema` no longer grounds the baked COMMENT text in any knowledge base's page kinds, because no
  one knowledge base may speak for the rest. The per-base grounding blocks carry that information instead, which is longer to
  read than a grounded render was.
- (−) A privacy mistake is now wider: a registry row that should have declared itself out of the library is reachable
  from every session rather than from sessions rooted near it. Both declarations from 0012 stay the fix.

## Options considered

Keep the session default and add every base behind an explicit `knowledge_base="all"`. Lost: the owner wants every knowledge base
by default, and `"all"` is a word the model has to know to say before it has learned the library exists.

Keep `APP_NAME` for the SQL door alone and fan out only `search`. Lost: nothing else reads the name once the door speaks
for every base, so keeping it would leave a required environment variable that decides nothing.
