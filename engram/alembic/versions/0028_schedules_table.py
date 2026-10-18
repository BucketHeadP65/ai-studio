"""The `public.schedules` table: a standing, cadence-driven intent to run one workflow again and again.

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-24 00:00:00.000000

One row is a workflow that fires on its own, spaced by `cadence` ("every 30m", a 5-field cron
expression, ...) rather than run once and forgotten. `arguments` is the jsonb payload every fire
of this schedule passes to `workflow_id`, unchanged tick to tick. `enabled` is the owner's pause
switch: a scheduler service selects only enabled rows whose `next_run_at` has arrived, so a
disabled schedule sits inert without losing its history. `gate_policy` and `auto_approve_gates`
carry the same run-time gate choice an interactive run makes once, so a fire nobody is watching
answers its own gates the same way every time; `session_id` remembers the runner session this
schedule's own agent thread lives in, so every tick reuses one identity rather than opening a
fresh session per fire.

`last_run_at`, `last_outcome`, and `next_run_at` are the tick log a scheduler stamps after every
fire (`stamp`, in the store adapter) -- `next_run_at` starts null (nothing has scheduled a first
fire yet) and both `last_run_at`/`next_run_at` stay nullable so "never run" and "no fire pending"
are representable states, not sentinel values.

This table is disposable dev-chain DDL, exactly like its siblings in this file's neighborhood:
downgrade drops it outright, re-running upgrade is the intended way back.
"""

from __future__ import annotations

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Create `public.schedules` and the partial index its due-scan (`enabled AND next_run_at <= now()`) uses."""
    op.execute(
        """
        CREATE TABLE public.schedules (
          id                 uuid PRIMARY KEY,
          workflow_id        text NOT NULL,
          arguments          jsonb NOT NULL DEFAULT '{}',
          cadence            text NOT NULL,
          enabled            boolean NOT NULL DEFAULT true,
          gate_policy        text NOT NULL DEFAULT 'wait',
          auto_approve_gates boolean NOT NULL DEFAULT false,
          session_id         text NOT NULL DEFAULT '',
          created_at         timestamptz NOT NULL DEFAULT now(),
          last_run_at        timestamptz,
          last_outcome       text NOT NULL DEFAULT '',
          next_run_at        timestamptz
        )
        """
    )
    op.execute("CREATE INDEX ix_schedules_due ON public.schedules (next_run_at) WHERE enabled")
    op.execute(
        "COMMENT ON TABLE public.schedules IS "
        "'A cadence-driven standing intent to run workflow_id again and again. One row per "
        "schedule; enabled + next_run_at is the due-scan a scheduler polls.'"
    )


def downgrade() -> None:
    """Drop `public.schedules` (the partial index goes with it)."""
    op.execute("DROP TABLE IF EXISTS public.schedules")
