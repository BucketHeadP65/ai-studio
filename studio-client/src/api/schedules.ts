import { createApiClient } from "client-core/api/client";

import { z } from "zod";

import { STUDIO_API_URL } from "./studio-config";
import { type GateMode, GateModeSchema, type JsonValue, JsonValueSchema } from "./studio-types";

// The schedules surface over the studio agent backend (`studio.agent`, :8770): CRUD over a standing,
// cadence-driven intent to run one workflow again and again, the ticker's own tick log
// (last_run_at/last_outcome/next_run_at), firing one by hand, and a cadence preview.
// Same shape as `studioApi`: client-core supplies request/parse/error, this module injects the
// base URL and builds the endpoint object.

const { request } = createApiClient({ baseUrl: STUDIO_API_URL });

// The backend stores `arguments` as `dict[str, JsonValue]` and admits each one against its
// parameter's own JSON Schema. Values stay exactly as stored: an array is an array, a boolean a
// boolean, and nothing here is turned into the text of a value on the way in or out.
const ScheduleArgumentsSchema = z.record(z.string(), JsonValueSchema);

export const ScheduleSchema = z.object({
  id: z.string(),
  workflow_id: z.string(),
  arguments: ScheduleArgumentsSchema,
  cadence: z.string(),
  enabled: z.boolean(),
  gate_policy: z.string(),
  // Who decides a gate this schedule's own run reaches. Defaulted, so a row read from a backend
  // that predates this field still parses as the wait every such backend only ever meant.
  gate_mode: GateModeSchema.default("wait"),
  auto_approve_gates: z.boolean(),
  // A shared concurrency budget (`capacity_key` paired with `capacity_limit`), this schedule's
  // own daily quiet hours (`blackout`), and whether a held fire catches up the moment it clears
  // (`catch_up`) rather than skipping to the cadence's next legitimate occurrence. All four are
  // absent on a row read from a backend that predates them, so each defaults rather than
  // requiring the key — the same wire-compatibility discipline `ScheduleArgumentsSchema` follows.
  capacity_key: z.string().nullable().default(null),
  capacity_limit: z.number().nullable().default(null),
  blackout: z.string().nullable().default(null),
  catch_up: z.boolean().default(false),
  session_id: z.string(),
  created_at: z.string(),
  last_run_at: z.string().nullable(),
  last_outcome: z.string(),
  next_run_at: z.string().nullable(),
  // Where the schedule's own runner session stands right now, read off the live pool: "running",
  // "waiting", "paused", or `null` when nothing is live.
  live: z.string().nullable(),
  // When a person last flipped this schedule off, `null` while it is on or when it predates this
  // field. Cleared the moment it flips back on.
  paused_at: z.string().nullable().default(null),
  // What this schedule's own runs have cost today, and over the past seven days. Absent or
  // null before any run of it has been priced.
  spend_today_usd: z.string().nullish(),
  spend_week_usd: z.string().nullish(),
  // Responses in each figure that no source could price. Above zero, the figure beside it is a floor.
  spend_today_unpriced: z.number().nullish(),
  spend_week_unpriced: z.number().nullish(),
});
export type Schedule = z.infer<typeof ScheduleSchema>;

const SchedulesListSchema = z.object({ schedules: z.array(ScheduleSchema) });
const ScheduleEnvelopeSchema = z.object({ schedule: ScheduleSchema });
const DeletedSchema = z.object({ ok: z.literal(true) });
const RunNowSchema = z.object({ ok: z.literal(true), outcome: z.string() });
const PreviewSchema = z.object({ next: z.array(z.string()) });

export interface ScheduleCreateInput {
  workflow_id: string;
  arguments: Record<string, JsonValue>;
  cadence: string;
  gate_policy: string;
  gate_mode?: GateMode;
  // The legacy switch `gate_mode` grew from -- kept here for compatibility with any caller still
  // sending it; the schedule form itself sends `gate_mode` only.
  auto_approve_gates?: boolean;
  capacity_key?: string | null;
  capacity_limit?: number | null;
  blackout?: string | null;
  catch_up?: boolean;
}

// Every field optional — an absent one leaves that column alone, mirroring the backend's
// `SchedulePatch`.
export interface ScheduleUpdateInput {
  workflow_id?: string;
  arguments?: Record<string, JsonValue>;
  cadence?: string;
  enabled?: boolean;
  gate_policy?: string;
  gate_mode?: GateMode;
  // The legacy switch `gate_mode` grew from -- kept here for compatibility with any caller still
  // sending it; the schedule form itself sends `gate_mode` only.
  auto_approve_gates?: boolean;
  capacity_key?: string | null;
  capacity_limit?: number | null;
  blackout?: string | null;
  catch_up?: boolean;
}

export const schedulesApi = {
  list: (): Promise<Schedule[]> =>
    request<z.infer<typeof SchedulesListSchema>>("/api/schedules", SchedulesListSchema).then(
      (result) => result.schedules,
    ),

  create: (input: ScheduleCreateInput): Promise<Schedule> =>
    request<z.infer<typeof ScheduleEnvelopeSchema>>("/api/schedules", ScheduleEnvelopeSchema, {
      method: "POST",
      body: JSON.stringify(input),
    }).then((result) => result.schedule),

  update: (id: string, patch: ScheduleUpdateInput): Promise<Schedule> =>
    request<z.infer<typeof ScheduleEnvelopeSchema>>(
      `/api/schedules/${encodeURIComponent(id)}`,
      ScheduleEnvelopeSchema,
      { method: "PATCH", body: JSON.stringify(patch) },
    ).then((result) => result.schedule),

  remove: (id: string): Promise<{ ok: true }> =>
    request(`/api/schedules/${encodeURIComponent(id)}`, DeletedSchema, { method: "DELETE" }),

  runNow: (id: string): Promise<{ ok: true; outcome: string }> =>
    request(`/api/schedules/${encodeURIComponent(id)}/run-now`, RunNowSchema, { method: "POST" }),

  // The next three fires a cadence would produce, walked forward from now. Refused with a 400
  // naming what a cadence looks like — `studioErrorMessage` unwraps that sentence.
  preview: (cadence: string): Promise<string[]> =>
    request<z.infer<typeof PreviewSchema>>("/api/schedules/preview", PreviewSchema, {
      method: "POST",
      body: JSON.stringify({ cadence }),
    }).then((result) => result.next),
};
