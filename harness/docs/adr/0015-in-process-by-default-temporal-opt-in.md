---
id: harness-adr-0015-in-process-by-default-temporal-opt-in
aliases: ["harness-adr-0015-in-process-by-default-temporal-opt-in"]
type: adr
app: harness
status: accepted
tags: []
title: "0015 — In-process by default, Temporal the opt-in"
---

# 0015 — In-process by default, Temporal the opt-in

**Status:** Accepted (2026-09-02). Amends [0010](0010-temporal-by-default-and-a-harness-owned-local-server.md): the
default engine flips, the harness-owned local server stays, for the deployment that opts in. Its consequence that a run does not survive the app dying is amended by [0021](0021-every-session-run-relaunches-at-boot.md): every session's run log is relaunched at boot.

## Context

0010 made Temporal the default engine so a run survives the app dying. Every boot since then exported
`APP_RUN__RUN_ENGINE=in_process`, because the default arm did not boot (TD-10) and nothing exercised it (TD-10a). A default
nobody walks is a promise the code does not keep, and a defect on that arm survives until someone boots it on purpose.

## Decision

`RunSettings.run_engine` defaults to `in_process`. A deployment chooses Temporal explicitly with `APP_RUN__RUN_ENGINE=temporal`
and keeps everything 0010 built for it: the harness-owned local server when the address names this machine and answers
nothing, the refusal of a remote address nobody serves, and the `engine unreachable` stamp on a scheduled fire against
it. The stack script exports no engine at all.

## Consequences

A bare `harness serve` boots with no environment beyond the knowledge base it mounts. A parked gate survives a restart through
the run journal on disk, not through server state. A run does not survive the app dying unless the deployment opted
into Temporal. TD-10 now meets only the opt-in, and TD-10a is paid.

## Options considered

Keep Temporal the default and fix the boot (TD-10). Lost, because the fix removes the crash and not the fact that no
deployment in this repo wants a server by default.

Keep Temporal the default and add an integration test on that arm. The test exists
(`tests/integration/test_temporal_durability.py`) and stays, but a test does not make an unused default honest.
