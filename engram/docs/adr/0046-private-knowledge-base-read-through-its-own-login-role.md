---
id: engram-adr-0046-private-knowledge-base-read-through-its-own-login-role
aliases: ["engram-adr-0046-private-knowledge-base-read-through-its-own-login-role"]
type: adr
app: engram
status: accepted
tags: []
title: "0046: A private knowledge base is read only through its own login role, and the reader connection is checked at startup"
---

# 0046: A private knowledge base is read only through its own login role, and the reader connection is checked at startup

**Status:** Accepted (2026-10-11)

**Context.** Migration 0037 hid a private knowledge base behind `api.visible_collection`, which trusted
`current_setting('app.collection')`. Any reader can write that setting with `set_config`, and `run_sql` sent the agent's
statement unchecked, so one statement could claim a private knowledge base and then read it through `api.search` or
`api.cards`. The privacy rested on a value the reader controls. Separately, nothing checked which role the reader connection
logged in as. A local settings file pointing `infrastructure.database.reader_url` at the owner role ran every agent
statement as the owner, with nothing to say so.

**Options considered.**

- **Keep the setting and refuse `set_config` in the statement guard.** Rejected: the guard parses text a model wrote. A form
  it misreads, or a function it does not list, would reopen the leak. The guard is defence in depth
  ([ADR-0047](0047-the-agents-sql-is-checked-before-it-runs.md)), never the boundary.
- **One table of allowed collections per reader, keyed by a setting the owner writes.** Rejected: any value a session can
  set is a value the reader can set.
- **A login role per private knowledge base, with visibility keyed by `session_user`** (chosen). A non-superuser cannot
  change `session_user`, so the boundary is the login itself.

**Decision.**

1. `api.visible_collection(p_collection)` keeps a shared knowledge base, and one with no fingerprint, visible to every
   session. A private one is visible only when `session_user` has a row for it in `kb.reader_scopes(role_name,
   collection)`, or when the session can read the `kb` tables directly and so gains nothing from hidden `api` rows. No
   reader is granted `kb.reader_scopes` (migration 0055). `app.collection` stays the routing default of `api.search`'s
   collection argument and grants nothing.
2. A private knowledge base's role is `kb_reader_` followed by 20 hex digits of the collection's SHA-256. Its password is
   the HMAC-SHA-256 of the role name under `infrastructure.database.reader_secret`. The indexer that creates the role and
   the reader that logs in as it agree without storing a password (`engram.retrieval.reader_roles`).
3. The indexer's switch stage provisions the role when a knowledge base declares itself private: it creates the login role,
   makes it a member of `kb_reader` so it holds every `api` grant, copies the reader's settings onto it and writes its
   `kb.reader_scopes` row (`engram.retrieval.infrastructure.reader_roles.SqlReaderScopes`).
4. The read gateway opens a private knowledge base through that role (`PrivateReaders`, `private_reader_url`) and reads
   shared ones as `kb_reader`.
5. `verify_reader_role` runs once where the reader engine is built. It refuses a role that does not keep
   `default_transaction_read_only` on, holds a role attribute, belongs to any role but `kb_reader`, uses or creates in a
   schema beyond `api` and `public`, or reaches a relation outside `api`. Privileges every role holds through PUBLIC are
   ignored, since an extension may grant PUBLIC its own functions. The refusal, `ReaderRoleOverprivilegedError`, names the
   role, each problem and the setting to change.

**Consequences.**

- (+) The privacy boundary is the database login, which no statement can change.
- (+) A misconfigured reader stops the application at startup with a message naming the setting, instead of running every
  agent statement with the owner's privileges.
- (+) A shared knowledge base reads exactly as before.
- (−) A deployment with a private knowledge base must set `infrastructure.database.reader_secret`, and the indexer must be
  able to create roles.
- (−) Roles belong to the whole server, so a downgrade of 0055 leaves the created roles in place. They read nothing private
  once `kb.reader_scopes` is gone.
- **Reversible?** Yes. Migration 0055's downgrade restores 0047's predicate and drops `kb.reader_scopes`.

**Related.** harness [ADR-0012](../../../harness/docs/adr/0012-library-search-scope.md) (the two declarations that make a
knowledge base private, unchanged), [ADR-0047](0047-the-agents-sql-is-checked-before-it-runs.md) (the statement guard),
[ADR-0042](0042-corpus-qualified-identity.md) (the `(collection, id)` key). Code: `engram/alembic/versions/0055_private_reader_scopes.py`,
`engram/src/engram/retrieval/reader_roles.py`,
`engram/src/engram/retrieval/infrastructure/reader_roles.py`, `engram/src/engram/retrieval/infrastructure/gateway.py`
(`verify_reader_role`), `core/src/core/settings.py` (`DatabaseSettings.reader_secret`).
