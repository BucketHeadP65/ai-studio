import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { ZodError } from "zod";

import { server } from "@/shared/test-support/msw/server";

import { schedulesApi } from "./schedules";
import { studioErrorMessage } from "./studio";

// The schedules surface rides the same studio agent backend (:8770) as `studioApi`, faked here as
// http://studio.test (vitest.config env). Each test pins one endpoint's zod boundary with a
// payload mirroring `harness/features/scheduling/routes.py`'s actual JSON.

const BASE = "http://studio.test";

interface RawSchedule {
  id: string;
  workflow_id: string;
  arguments: Record<string, unknown>;
  cadence: string;
  enabled: boolean;
  gate_policy: string;
  gate_mode: string;
  capacity_key: string | null;
  capacity_limit: number | null;
  blackout: string | null;
  catch_up: boolean;
  session_id: string;
  created_at: string;
  last_run_at: string | null;
  last_outcome: string;
  next_run_at: string | null;
  live: string | null;
  paused_at: string | null;
  spend_today_usd: string;
  spend_week_usd: string;
  spend_today_unpriced: number;
  spend_week_unpriced: number;
}

function scheduleFixture(overrides: Partial<RawSchedule> = {}): RawSchedule {
  return {
    id: "sched-1",
    workflow_id: "wf-sales",
    arguments: { quarter: "Q1" },
    cadence: "every 30m",
    enabled: true,
    gate_policy: "wait",
    gate_mode: "wait",
    capacity_key: null,
    capacity_limit: null,
    blackout: null,
    catch_up: false,
    session_id: "",
    created_at: "2026-08-23T10:00:00+00:00",
    last_run_at: null,
    last_outcome: "",
    next_run_at: "2026-08-23T10:30:00+00:00",
    live: null,
    paused_at: null,
    spend_today_usd: "0",
    spend_week_usd: "0",
    spend_today_unpriced: 0,
    spend_week_unpriced: 0,
    ...overrides,
  };
}

describe("schedulesApi", () => {
  it("lists schedules", async () => {
    server.use(
      http.get(`${BASE}/api/schedules`, () =>
        HttpResponse.json({ schedules: [scheduleFixture()] }),
      ),
    );

    const schedules = await schedulesApi.list();

    expect(schedules).toHaveLength(1);
    expect(schedules[0]?.workflow_id).toBe("wf-sales");
    expect(schedules[0]?.next_run_at).toBe("2026-08-23T10:30:00+00:00");
  });

  it("refuses a row missing gate_mode, which every schedule row carries", async () => {
    const { gate_mode: _omitted, ...row } = scheduleFixture();
    server.use(http.get(`${BASE}/api/schedules`, () => HttpResponse.json({ schedules: [row] })));

    await expect(schedulesApi.list()).rejects.toBeInstanceOf(ZodError);
  });

  it("reads gate_mode when a row carries it", async () => {
    server.use(
      http.get(`${BASE}/api/schedules`, () =>
        HttpResponse.json({ schedules: [scheduleFixture({ gate_mode: "orchestrate" })] }),
      ),
    );

    const [schedule] = await schedulesApi.list();

    expect(schedule?.gate_mode).toBe("orchestrate");
  });

  it("reads a null paused_at for a schedule that is on", async () => {
    server.use(
      http.get(`${BASE}/api/schedules`, () => HttpResponse.json({ schedules: [scheduleFixture()] })),
    );

    const [schedule] = await schedulesApi.list();

    expect(schedule?.paused_at).toBeNull();
  });

  it("reads paused_at when a row carries it", async () => {
    server.use(
      http.get(`${BASE}/api/schedules`, () =>
        HttpResponse.json({
          schedules: [scheduleFixture({ paused_at: "2026-08-23T09:00:00+00:00" })],
        }),
      ),
    );

    const [schedule] = await schedulesApi.list();

    expect(schedule?.paused_at).toBe("2026-08-23T09:00:00+00:00");
  });

  it("parses capacity, blackout, and catch up when a row carries them", async () => {
    server.use(
      http.get(`${BASE}/api/schedules`, () =>
        HttpResponse.json({
          schedules: [
            scheduleFixture({
              capacity_key: "gpu-pool",
              capacity_limit: 3,
              blackout: "22:00-06:00, 12:00-13:00",
              catch_up: true,
            }),
          ],
        }),
      ),
    );

    const [schedule] = await schedulesApi.list();

    expect(schedule?.capacity_key).toBe("gpu-pool");
    expect(schedule?.capacity_limit).toBe(3);
    expect(schedule?.blackout).toBe("22:00-06:00, 12:00-13:00");
    expect(schedule?.catch_up).toBe(true);
  });

  it("reads a stored argument back as the JSON value it was stored as", async () => {
    server.use(
      http.get(`${BASE}/api/schedules`, () =>
        HttpResponse.json({
          schedules: [
            scheduleFixture({
              arguments: {
                quarter: "Q1",
                limit: 5,
                urgent: true,
                knowledge_bases: ["research", "repo-docs"],
                budget: { cap: 4 },
              },
            }),
          ],
        }),
      ),
    );

    const [schedule] = await schedulesApi.list();

    expect(schedule?.arguments).toEqual({
      quarter: "Q1",
      limit: 5,
      urgent: true,
      knowledge_bases: ["research", "repo-docs"],
      budget: { cap: 4 },
    });
  });

  it("creates a schedule and reads back the row", async () => {
    server.use(
      http.post(`${BASE}/api/schedules`, async ({ request }) => {
        const body = await request.json();
        expect(body).toEqual({
          workflow_id: "wf-sales",
          arguments: { quarter: "Q1" },
          cadence: "every 30m",
          gate_policy: "wait",
        });
        return HttpResponse.json({ schedule: scheduleFixture() }, { status: 201 });
      }),
    );

    const schedule = await schedulesApi.create({
      workflow_id: "wf-sales",
      arguments: { quarter: "Q1" },
      cadence: "every 30m",
      gate_policy: "wait",
    });

    expect(schedule.id).toBe("sched-1");
  });

  it("posts capacity, blackout, and catch up when the caller sets them", async () => {
    server.use(
      http.post(`${BASE}/api/schedules`, async ({ request }) => {
        const body = await request.json();
        expect(body).toEqual({
          workflow_id: "wf-sales",
          arguments: { quarter: "Q1" },
          cadence: "every 30m",
          gate_policy: "wait",
          capacity_key: "gpu-pool",
          capacity_limit: 3,
          blackout: "22:00-06:00",
          catch_up: true,
        });
        return HttpResponse.json(
          {
            schedule: scheduleFixture({
              capacity_key: "gpu-pool",
              capacity_limit: 3,
              blackout: "22:00-06:00",
              catch_up: true,
            }),
          },
          { status: 201 },
        );
      }),
    );

    const schedule = await schedulesApi.create({
      workflow_id: "wf-sales",
      arguments: { quarter: "Q1" },
      cadence: "every 30m",
      gate_policy: "wait",
      capacity_key: "gpu-pool",
      capacity_limit: 3,
      blackout: "22:00-06:00",
      catch_up: true,
    });

    expect(schedule.capacity_key).toBe("gpu-pool");
    expect(schedule.blackout).toBe("22:00-06:00");
  });

  it("omits capacity, blackout, and catch up from the wire when the caller never sets them", async () => {
    server.use(
      http.post(`${BASE}/api/schedules`, async ({ request }) => {
        const body = await request.json();
        expect(body).toEqual({
          workflow_id: "wf-sales",
          arguments: { quarter: "Q1" },
          cadence: "every 30m",
          gate_policy: "wait",
        });
        return HttpResponse.json({ schedule: scheduleFixture() }, { status: 201 });
      }),
    );

    await schedulesApi.create({
      workflow_id: "wf-sales",
      arguments: { quarter: "Q1" },
      cadence: "every 30m",
      gate_policy: "wait",
    });
  });

  it("surfaces the create refusal's sentence when the cadence is not one", async () => {
    server.use(
      http.post(`${BASE}/api/schedules`, () =>
        HttpResponse.json(
          { ok: false, message: "'whenever' is not a cadence -- a cadence looks like ..." },
          { status: 400 },
        ),
      ),
    );

    const failure = await schedulesApi
      .create({ workflow_id: "wf-sales", arguments: {}, cadence: "whenever", gate_policy: "wait" })
      .catch((error: unknown) => studioErrorMessage(error));

    expect(failure).toBe("'whenever' is not a cadence -- a cadence looks like ...");
  });

  it("patches only the given fields", async () => {
    server.use(
      http.patch(`${BASE}/api/schedules/sched-1`, async ({ request }) => {
        const body = await request.json();
        expect(body).toEqual({ enabled: false });
        return HttpResponse.json({ schedule: scheduleFixture({ enabled: false }) });
      }),
    );

    const schedule = await schedulesApi.update("sched-1", { enabled: false });

    expect(schedule.enabled).toBe(false);
  });

  it("deletes a schedule", async () => {
    server.use(http.delete(`${BASE}/api/schedules/sched-1`, () => HttpResponse.json({ ok: true })));

    await expect(schedulesApi.remove("sched-1")).resolves.toEqual({ ok: true });
  });

  it("fires a schedule now", async () => {
    server.use(
      http.post(`${BASE}/api/schedules/sched-1/run-now`, () =>
        HttpResponse.json({ ok: true, outcome: "launched" }),
      ),
    );

    await expect(schedulesApi.runNow("sched-1")).resolves.toEqual({
      ok: true,
      outcome: "launched",
    });
  });

  it("previews the next three occurrences", async () => {
    const next = [
      "2026-08-23T11:00:00+00:00",
      "2026-08-23T11:30:00+00:00",
      "2026-08-23T12:00:00+00:00",
    ];
    server.use(
      http.post(`${BASE}/api/schedules/preview`, async ({ request }) => {
        const body = await request.json();
        expect(body).toEqual({ cadence: "every 30m" });
        return HttpResponse.json({ next });
      }),
    );

    await expect(schedulesApi.preview("every 30m")).resolves.toEqual(next);
  });

  it("surfaces the preview refusal's sentence, naming the cadence shape", async () => {
    server.use(
      http.post(`${BASE}/api/schedules/preview`, () =>
        HttpResponse.json(
          { ok: false, message: "'whenever' is not a cadence -- a cadence looks like ..." },
          { status: 400 },
        ),
      ),
    );

    const failure = await schedulesApi
      .preview("whenever")
      .catch((error: unknown) => studioErrorMessage(error));

    expect(failure).toBe("'whenever' is not a cadence -- a cadence looks like ...");
  });

  it("rejects a schedule row with a wrong-typed enabled field", async () => {
    server.use(
      http.get(`${BASE}/api/schedules`, () =>
        HttpResponse.json({ schedules: [{ ...scheduleFixture(), enabled: "yes" }] }),
      ),
    );

    await expect(schedulesApi.list()).rejects.toThrow(ZodError);
  });
});
