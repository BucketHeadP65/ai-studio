---
id: engram-adr-0047-the-agents-sql-is-checked-before-it-runs
aliases: ["engram-adr-0047-the-agents-sql-is-checked-before-it-runs"]
type: adr
app: engram
status: accepted
tags: []
title: "0047: The agent's SQL is checked before it runs, and its meaning texts are found by tokens"
---

# 0047: The agent's SQL is checked before it runs, and its meaning texts are found by tokens

**Status:** Accepted (2026-10-11)

**Context.** `run_sql` sent the agent's statement unchecked. Only asyncpg's one-statement rule and the reader role stood in
the way. The row cap was a `fetchmany` on the client, so the server ran the whole query until the statement timeout. A
statement could call `set_config`, `pg_sleep`, `query_to_xml`, or take an advisory lock another session waits on. The
`vector_query` rewrite was a regular expression over the raw text. It rewrote inside comments and string literals, missed
dollar-quoted and `E'...'` text, embedded one text per request with no bound on count, length or time, and the vector it
wrote carried no model identity. Every generated `SECURITY DEFINER` function pinned `<schemas>, public, pg_catalog` and
left `pg_temp` unlisted, so Postgres searched it first.

**Decision.**

1. `core.ai.retrieval.statement.single_statement` checks every statement before it is sent. It refuses more than one
   statement, more than 20,000 characters, brackets nested deeper than 100, `WITH RECURSIVE` (the graph walks are `api`
   functions), any form `sqlparse` reads differently from Postgres (`core.ai.retrieval.unreadable_forms`), and a call to a
   denied function. Names are matched after the schema prefix and quotes are removed and case is folded. The deny-list
   holds the setting, lock, sleep, SQL-in-a-string, session-control, server-control and file-reading functions, and every
   `lo_` function. Each refusal says what to write instead.
2. `run_sql` sends the checked statement as `SELECT * FROM (<statement>) AS q LIMIT cap + 1`, so the database refuses
   anything that is not a query and stops at the row cap.
3. `VectorQueryRewrite` finds each `vector_query => '<text>'` by the statement's tokens, so a word inside a string or a
   comment is never touched. It accepts at most four texts of at most 2,000 characters, refuses any other quoting with the
   form to use, and refuses a statement that passes `query_vec` or `query_model` itself. Each distinct text is embedded
   once, all of them under one 10-second deadline. A failed or late embedding degrades the call to keyword search with a
   note.
4. The rewrite writes the embedder's model as `query_model`, and `api.search`'s semantic leg leaves out a knowledge base
   whose fingerprint records another embedding model (migration 0057).
5. Every generated and hand-written function that pins a `search_path` reads `pg_catalog` first and `pg_temp` last
   (migration 0054, `core.ai.retrieval.search_builder.pinned_search_path`).
6. Every reader role waits at most five seconds for a lock (migration 0056).

The guard is defence in depth. The privacy boundary is the login role of
[ADR-0046](0046-private-knowledge-base-read-through-its-own-login-role.md), and the reader's read-only transactions stay
the boundary against writes.

**Consequences.**

- (+) A statement that sleeps, locks, changes a session setting or runs SQL from a string is refused before it reaches the
  database, with the reason.
- (+) The server stops at the row cap instead of running the query to its timeout.
- (+) A semantic rank always compares vectors of one model.
- (−) The guard can refuse a valid query that `sqlparse` misreads. The refusal names the form, so the agent can rewrite it.
- (−) The deny-list must grow when Postgres or an extension adds a function of the same kinds.
- **Reversible?** Yes. Each migration's downgrade restores the previous function, and the guard is one call in `run_sql`.

**Related.** [ADR-0046](0046-private-knowledge-base-read-through-its-own-login-role.md) (the privacy boundary),
[ADR-0035](0035-retrieval-sql-from-descriptors.md) (the generated search). Code: `core/src/core/ai/retrieval/statement.py`,
`core/src/core/ai/retrieval/sql_tokens.py`, `core/src/core/ai/retrieval/unreadable_forms.py`,
`core/src/core/ai/retrieval/vector_query_rewrite.py`, `engram/src/engram/retrieval/sql_tools.py`,
`engram/alembic/versions/0054_pinned_search_path.py`, `engram/alembic/versions/0056_reader_lock_timeout.py`,
`engram/alembic/versions/0057_search_query_model.py`.
