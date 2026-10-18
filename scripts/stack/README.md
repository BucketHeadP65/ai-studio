# The always-on stack

Four launchd LaunchAgents keep the engram stack running on this machine: they start at login,
restart a service that exits, and are restarted on demand when code lands.

| Service         | What                                   | Port | Wrapper            |
|-----------------|----------------------------------------|------|--------------------|
| `db`            | Postgres via `engram/docker-compose.yml` | 5434 | `db.sh`            |
| `harness`       | `uv run harness serve` (agent backend) | 8770 | `harness.sh`       |
| `studio-api`    | `uv run studio serve-http` (knowledge base API) | 8100 | `studio-api.sh`    |
| `studio-client` | `npm run dev` (AI Factory Studio, hot reload)  | 3031 | `studio-client.sh` |

`stack.sh install` renders the four plists into `~/Library/LaunchAgents/com.engram.<service>.plist`
with this checkout's absolute paths and bootstraps them. Logs land in `~/Library/Logs/engram/<service>.log`.

```bash
scripts/stack/stack.sh status                      # launchd state + a port probe per service
scripts/stack/stack.sh restart harness studio-api  # after backend code lands
scripts/stack/stack.sh logs harness                # tail one service's log
scripts/stack/stack.sh stop|start all
scripts/stack/stack.sh uninstall
```

## One-time prerequisites

1. **Docker Desktop starts at login** (Docker Desktop settings, General). The `db` agent only runs
   `docker compose up`. It cannot start Docker itself.
2. **Grant `/bin/bash` Full Disk Access when the checkout sits in a folder macOS protects**, such as
   `~/Desktop` or `~/Documents`. A process launchd spawns gets "Operation not permitted" there until the shell that runs
   the wrappers is allowed in. `/bin` is hidden in the file picker, so open both sides from a
   terminal, `open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"` and
   `open /bin`, drag `bash` from the Finder window onto the Full Disk Access list, enable it, then
   `scripts/stack/stack.sh restart all`. Symptom without it: `stack.sh status` shows
   `last exit code = 126` and the log says the wrapper is not permitted.

## Working with it

- Frontend changes are live: AI Factory Studio is the dev server, saving a file hot-reloads.
- Backend changes (harness, studio, engram, core, connectors, the vendored fork, migrations) need
  `stack.sh restart harness studio-api`. The running app keeps the old code until then.
- `harness.sh` exports no environment. Every knob has a committed default in
  `harness/config/harness_settings.json`, and an export overrides one field of one section
  (`APP_<SECTION>__<FIELD>`). The agent reads every registered knowledge base and needs nothing
  named for it: each read tool (`search`, `fetch`, `graph_expand`, `find_paths`) names the one base
  it reads, and `APP_LIBRARY_SEARCH__CROSS_KNOWLEDGE_BASE_SEARCH=true` lets one call read every
  base at once instead.
- **The harness needs `studio-api` up to boot.** The registry of knowledge bases belongs to the
  studio, so the agent reads it over the studio API (`GET /api/knowledge-bases` for the names, folders and
  whether each is private or shared, `GET /api/knowledge-bases/{name}/schema` for each one's schema) rather than out of
  `engram/config/application_settings.json`. A studio that does not answer is a refusal naming its
  base URL: the harness exits and `KeepAlive` starts it again every 10 seconds, so it comes up by
  itself once `studio-api` answers. A harness that keeps restarting means the studio is down, and
  its log says which base URL went unanswered.
- `db` adopts an already-running container (`--no-recreate`). Stopping the agent stops the container,
  the volume persists.
- Tests never bind the stable ports, so a test run and the live stack coexist.
- `stack.sh install` takes over a port held by one of our own processes (a hand-started
  `harness serve`, `next dev` from this checkout) and leaves a stranger's listener alone, skipping
  that service and saying so.
