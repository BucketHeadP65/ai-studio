<!-- GENERATED from architecture.md by scripts/docs/split_doc.py --dir docs/architecture — do not edit here; edit the master and re-run the script. -->

## 6. Quality, risks, tech debt
> **TL;DR:** Clean codebase (zero inline debt markers, debt is ledgered in prose). The live risks are engram's hand-written SQL api surface, the sync lazy-route invariant, Docker-gated coverage, the workflow runner's shell-out posture, and ADR-0009's recorded manifest/contract follow-ups.
<!-- load-when: risks, tech debt, known issues, caveats, coverage, drift -->

- **engram `api` surface is hand-written SQL** inside the migration chain,
  no type-checker coverage against `compile/orm.py`. Drift is a silent
  maintenance seam.
- **Sync lazy-route invariant.** `SyncMirrors` is bus-registered as a side
  effect of resolving its handler. Every dispatch site must go through
  `resolve_sync_bus` or the route is missing. Funnelled by design, breakable
  by refactor.
- **Coverage shape.** The integration/e2e tiers auto-skip without Docker.
  CI without Docker exercises only the unit tier. `core/` and `engram/` changes
  require running every consumer suite manually (no enforced gate).
- **CQRS onboarding bar.** The bus/UoW shape has no analogue in the agent half.
  ADR 0002 accepts this cost. Subscriber failures are logged, not surfaced.
  A future outbox subscriber needs its own monitoring.
- **Note grammar and knowledge base schema drift.** The kernel encodes the knowledge base's
  `_meta/schema.md` in code. The early-warning net is one golden fixture
  (`lightrag-2025.md`).
- **Workflow steps can shell out by design.** A workflow's shell capability is
  a runner, not a sandbox. Running a workflow authored elsewhere would execute
  its steps locally. The gate decided by a person and the validator's checks guard this,
  not a technical jail.
- **Consolidation follow-ups.**
  [ADR 0009](../../adr/0009-ai-factory-studio-consolidation.md) records what the
  restructure deliberately did not do: engram's and studio's manifests still
  need a hygiene pass, and `/api/health` returns a `corpus` probe the client still reads
  as `vault`.

  Debt is ledgered in `CLAUDE.md`'s "Known caveats" and ADR Consequences sections. There are deliberately no inline TODO/FIXME markers.
there are deliberately no inline TODO/FIXME markers.
