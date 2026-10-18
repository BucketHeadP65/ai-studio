---
id: harness-adr-0010-temporal-by-default-and-a-harness-owned-local-server
aliases: ["harness-adr-0010-temporal-by-default-and-a-harness-owned-local-server"]
type: adr
app: harness
status: accepted
tags: []
title: "0010 — Temporal by default, a harness-owned local server"
---

# 0010 — Temporal by default, a harness-owned local server

**Status:** Accepted (2026-08-26). Amended by [0015](0015-in-process-by-default-temporal-opt-in.md) on 2026-09-02: the
default engine is `in_process` again and Temporal is the opt-in, the local server below serves that opt-in. Supersedes the in-process default that [0006](0006-schedules-and-the-ticker.md) matched
its ticker to, and reuses [0005](0005-runs-are-first-class-monitored-artifacts.md)'s run surfaces without change.

## Context

An in-process run is a coroutine. When the app stops, every run in flight stops with it, and only a gate parked at the
moment of death is recoverable, from the run journal on disk. Everything a run had already done is gone. Temporal has been
the alternative since the adapter was written, and it was not the default for one reason: it needed a server, and the
server needed Docker. That made durability an opt-in that nobody opted into, and left the app's most valuable long-running
work on the engine that cannot keep it.

The Temporal Python SDK ships the server. `WorkflowEnvironment.start_local` runs Temporal's own dev-server binary against a
SQLite file, using an installed `temporal` binary when the machine has one and downloading one once when it does not. It is
the same binary the durability tier has been testing against all along.

## Decision

**`APP_RUN__RUN_ENGINE` defaults to `temporal`.** `in_process` stays a supported, explicit choice, and it is the engine the whole
unit tier runs on (a `tests/unit/conftest.py` fixture exports it, so no unit test needs a server).

**The app owns a local server when nobody else does.** `harness/src/harness/adapters/temporal_server.py` is entered on the
composition stack before the durable worker is served, and has exactly three answers:

1. **Something answers `temporal_address` within two seconds.** Do nothing. The probe is a plain TCP connect, because the
   only question it decides is whether a server can bind that port, and `Client.connect` carries its own retries and takes
   no connect timeout of its own.
2. **Nothing answers and the host is this machine** (`localhost`, `127.0.0.1`, `::1`). Start the dev server on that port,
   web UI on 8233, history in `.state/temporal.db`, and shut it down when the app stops. The history is what survives: a
   run parked at a gate is still parked after the app and the server have both been restarted.
3. **Nothing answers and the address is somebody else's.** Serve no durable runs, log it, and refuse. A launch raises
   `RunEngineUnavailableError`, and a scheduled fire is stamped `engine unreachable` and comes due again on its own cadence.

**There is no fall back to the in-process engine.** A deployment that asked for durable runs and silently got fragile ones
would look identical from the browser until the process died.

**Automode works on Temporal.** `RunRequest.auto_approve_gates` travels with the run, the workflow answers its own gates
without parking, and the gate ids land in `RunEvidence.gates_auto_approved`, mapped onto `Attestation.gates_auto`, which
trust derivation ignores. A gate nobody was asked about therefore cannot read as a person's approval on either engine.

**Rejected.**
- Docker as the default path. The compose file stays, as the opt-in Postgres-backed profile for a deployment that wants
  history in Postgres. Requiring it for an ordinary run is what kept durability unused.
- Falling back to in-process when the server is unreachable (see above).
- Starting a server for a remote address that does not answer. A local server would answer the probe next time and hide a
  broken deployment behind a laptop.
- An orchestrated gate on Temporal. A gate the run's own orchestrator answers is an agent turn, which workflow code cannot
  take. It is still refused in words, and `wait` and `auto` both work.

## Consequences

- A workflow run survives the app restarting, with no Docker, no compose file, and no setup step, on any machine that can
  either run `temporal` or fetch it once.
- The app owns a server process and a SQLite file it did not own before, both under `.state/`, which is gitignored. The
  process goes away with the app. The file stays, and is what a restart finds.
- Port 7233 is now contested by default: an already-running Temporal (compose, or another checkout) is detected and reused,
  which is right, but two harness checkouts pointed at one address share one server's history. Give the second one its own
  `APP_RUN__TEMPORAL_ADDRESS`.
- The unit tier is pinned to `in_process` by a conftest fixture. A test that means to exercise the durable dispatch sets
  `APP_RUN__RUN_ENGINE` itself.
- `engine unreachable` joins the tick log's vocabulary beside `missed (previous run still live)` and `caught up`.
