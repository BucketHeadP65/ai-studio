import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { ZodError } from "zod";

import { server } from "@/shared/test-support/msw/server";

import { ApiError } from "./knowledge-base";
import { harnessApi } from "./harness";
import { studioErrorMessage } from "./studio";

// The harness rides the agent backend (:8770), faked here as http://studio.test
// (vitest.config env). Each test pins one endpoint's zod boundary with a payload
// mirroring the FastAPI routes' actual JSON, so a backend contract drift fails HERE,
// named, rather than as an undefined deep in a view.

const BASE = "http://studio.test";

const CAPABILITY_ROW = {
  name: "marker",
  status: "disabled",
  descriptor: "Marker",
  last_error: null,
  promoted: false,
  next_step: "activate",
};

const MODEL_ENTRY = {
  id: "openrouter/glm-5.3-flash",
  name: "GLM 5.3 Flash",
  route: "openrouter",
  context_length: 1_048_576,
  price_in_per_m: "0.075",
  price_out_per_m: null,
  reasoning: "effort",
  pinned: true,
};

const TIER_VIEW = {
  tier: "orchestrator",
  role: "design judgment, multi-file changes, hard debugging",
  model: MODEL_ENTRY,
  thinking: "high",
  default: true,
};

describe("harnessApi", () => {
  it("parses the live composition", async () => {
    server.use(
      http.get(`${BASE}/api/harness/composition`, () =>
        HttpResponse.json({
          model: "gpt-5.4",
          run_engine: "temporal",
          steps_registered: ["double"],
          promoted: ["Marker"],
          active: { capability: 1, step: 0, agent: 0, profile: 0 },
        }),
      ),
    );

    const composition = await harnessApi.composition();

    expect(composition.run_engine).toBe("temporal");
    expect(composition.steps_registered).toEqual(["double"]);
    expect(composition.active.capability).toBe(1);
  });

  it("parses a kind listing", async () => {
    server.use(
      http.get(`${BASE}/api/harness/capability`, () =>
        HttpResponse.json({ kind: "capability", artifacts: [CAPABILITY_ROW] }),
      ),
    );

    const listing = await harnessApi.list("capability");

    expect(listing.kind).toBe("capability");
    expect(listing.artifacts[0].next_step).toBe("activate");
    expect(listing.artifacts[0].promoted).toBe(false);
  });

  it("parses an activate action", async () => {
    server.use(
      http.post(`${BASE}/api/harness/step/lookup/activate`, () =>
        HttpResponse.json({ kind: "step", name: "lookup", status: "active" }),
      ),
    );

    const action = await harnessApi.activate("step", "lookup");

    expect(action.status).toBe("active");
  });

  it("parses a spend report", async () => {
    server.use(
      http.get(`${BASE}/api/spend`, () =>
        HttpResponse.json({
          available: true,
          scope: "step",
          subject: "run-1",
          since_hours: 24,
          lines: [
            {
              key: "run-1",
              calls: 3,
              cost_usd: "0.5",
              input_tokens: 10,
              output_tokens: 2,
              cache_read_tokens: 0,
              cache_write_tokens: 0,
            },
          ],
          total: {
            key: "total",
            calls: 3,
            cost_usd: "0.5",
            input_tokens: 10,
            output_tokens: 2,
            cache_read_tokens: 0,
            cache_write_tokens: 0,
          },
          source: "telemetry",
        }),
      ),
    );

    expect((await harnessApi.spend("step", "run-1")).available).toBe(true);
  });

  it("parses a promotion action", async () => {
    server.use(
      http.post(`${BASE}/api/harness/capability/marker/promote`, () =>
        HttpResponse.json({ name: "marker", promoted: true }),
      ),
    );

    const action = await harnessApi.promote("marker");

    expect(action.promoted).toBe(true);
  });

  it("surfaces the backend's refusal sentence through studioErrorMessage", async () => {
    server.use(
      http.post(`${BASE}/api/harness/capability/marker/promote`, () =>
        HttpResponse.json(
          { detail: "Capability 'marker' is not active; activate it first." },
          { status: 400 },
        ),
      ),
    );

    const failure = await harnessApi.promote("marker").catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect(studioErrorMessage(failure)).toBe(
      "Capability 'marker' is not active; activate it first.",
    );
  });

  it("parses the tiers listing, prices as decimal strings", async () => {
    server.use(
      http.get(`${BASE}/api/harness/tiers`, () => HttpResponse.json({ tiers: [TIER_VIEW] })),
    );

    const listing = await harnessApi.tiers();

    expect(listing.tiers[0].tier).toBe("orchestrator");
    expect(listing.tiers[0].model.price_in_per_m).toBe("0.075");
    expect(listing.tiers[0].model.price_out_per_m).toBeNull();
    expect(listing.tiers[0].default).toBe(true);
  });

  it("PUTs a tier's model and thinking level", async () => {
    let sentBody: unknown;
    server.use(
      http.put(`${BASE}/api/harness/tiers/worker`, async ({ request }) => {
        sentBody = await request.json();
        return HttpResponse.json({ tiers: [{ ...TIER_VIEW, tier: "worker", default: false }] });
      }),
    );

    const listing = await harnessApi.setTier("worker", {
      model: "gateway/deepseek-v4",
      thinking: "medium",
    });

    expect(sentBody).toEqual({ model: "gateway/deepseek-v4", thinking: "medium" });
    expect(listing.tiers[0].tier).toBe("worker");
  });

  it("DELETEs a tier to reset it", async () => {
    server.use(
      http.delete(`${BASE}/api/harness/tiers/helper`, () =>
        HttpResponse.json({ tiers: [{ ...TIER_VIEW, tier: "helper" }] }),
      ),
    );

    const listing = await harnessApi.resetTier("helper");

    expect(listing.tiers[0].tier).toBe("helper");
  });

  it("400s a tier PUT with an unlisted model id", async () => {
    server.use(
      http.put(`${BASE}/api/harness/tiers/worker`, () =>
        HttpResponse.json({ detail: "Unknown model 'not-a-real-model'." }, { status: 400 }),
      ),
    );

    const failure = await harnessApi
      .setTier("worker", { model: "not-a-real-model", thinking: "medium" })
      .catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect(studioErrorMessage(failure)).toBe("Unknown model 'not-a-real-model'.");
  });

  it("searches the model list, pinned first as the server sends them", async () => {
    server.use(
      http.get(`${BASE}/api/harness/models`, ({ request }) => {
        expect(new URL(request.url).searchParams.get("q")).toBe("glm");
        return HttpResponse.json({ models: [MODEL_ENTRY] });
      }),
    );

    const listing = await harnessApi.models("glm");

    expect(listing.models[0].id).toBe("openrouter/glm-5.3-flash");
    expect(listing.models[0].pinned).toBe(true);
  });

  it("rejects a drifted listing at the boundary", async () => {
    server.use(
      http.get(`${BASE}/api/harness/agent`, () =>
        HttpResponse.json({ kind: "agent", artifacts: [{ name: "scout" }] }),
      ),
    );

    await expect(harnessApi.list("agent")).rejects.toBeInstanceOf(ZodError);
  });
});
