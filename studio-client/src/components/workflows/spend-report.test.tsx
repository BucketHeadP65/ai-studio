import { screen } from "@testing-library/dom";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

import { SpendReport } from "./spend-report";

// The run page's own spend cell, read straight from the telemetry: a total sentence and one
// row per key when the report has lines, a plain sentence when it has none, and the reason
// alongside where a narrower answer still lives when the telemetry could not answer at all.

const BASE = "http://studio.test";

const LINE = {
  key: "run-1",
  calls: 3,
  cost_usd: "0.5",
  input_tokens: 10,
  output_tokens: 2,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
};

describe("SpendReport", () => {
  it("shows the total sentence and one row per line", async () => {
    server.use(
      http.get(`${BASE}/api/spend`, () =>
        HttpResponse.json({
          available: true,
          scope: "step",
          subject: "run-1",
          since_hours: 720,
          lines: [LINE],
          total: LINE,
          source: "telemetry",
        }),
      ),
    );

    render(<SpendReport runId="run-1" />);

    expect(screen.getByText("Spend (telemetry)")).toBeInTheDocument();
    expect(await screen.findByText("$0.5000 across 3 calls, last 30 days")).toBeInTheDocument();
    expect(screen.getByText("run-1")).toBeInTheDocument();
    expect(screen.getByText("3 calls")).toBeInTheDocument();
  });

  it("names no priced calls when the report has no lines", async () => {
    server.use(
      http.get(`${BASE}/api/spend`, () =>
        HttpResponse.json({
          available: true,
          scope: "step",
          subject: "run-2",
          since_hours: 720,
          lines: [],
          total: { ...LINE, key: "total", calls: 0, cost_usd: "0" },
          source: "telemetry",
        }),
      ),
    );

    render(<SpendReport runId="run-2" />);

    expect(
      await screen.findByText("No priced model calls for this run in the last 30 days."),
    ).toBeInTheDocument();
  });

  it("names the reason and the fallback when the telemetry is unreachable", async () => {
    server.use(
      http.get(`${BASE}/api/spend`, () =>
        HttpResponse.json({
          available: false,
          scope: "step",
          subject: "run-3",
          since_hours: 720,
          reason: "telemetry unreachable (ConnectError)",
          fallback:
            "The run ledger on disk still answers per run, GET /api/runs/history/{run_id} carries its spend.",
        }),
      ),
    );

    render(<SpendReport runId="run-3" />);

    expect(
      await screen.findByText(
        "telemetry unreachable (ConnectError). The run ledger on disk still answers per run, GET /api/runs/history/{run_id} carries its spend.",
      ),
    ).toBeInTheDocument();
  });

  it("shows the transport failure when the spend route itself answers with a server error", async () => {
    server.use(
      http.get(`${BASE}/api/spend`, () =>
        HttpResponse.json({ detail: "spend query timed out" }, { status: 500 }),
      ),
    );

    render(<SpendReport runId="run-4" />);

    expect(screen.getByText("Spend (telemetry)")).toBeInTheDocument();
    expect(await screen.findByText("spend query timed out")).toBeInTheDocument();
  });
});
