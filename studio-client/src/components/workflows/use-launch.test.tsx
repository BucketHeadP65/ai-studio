import { screen, waitFor } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import type { RunIntent } from "@/api/studio-types";
import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

import { useDeliberateLaunch } from "./use-launch";

// The launch rule itself, without a screen around it: one request per decision to start, and a
// start whose answer never came back left in doubt rather than sent again.

const BASE = "http://studio.test";

const INTENT: RunIntent = {
  workflowId: "wf.lit-check.v5",
  runTitle: "Check against the literature",
  arguments: { context: "a decision" },
  gateMode: "wait",
};

function sessionRow() {
  return {
    id: "sess-new",
    project_id: "",
    title: "",
    started_at: "2026-09-16T09:00:00Z",
    last_activity_at: "2026-09-16T09:00:00Z",
    archived: false,
    answering: false,
    turn_count: 0,
    usage: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0 },
    kind: "runner",
    cost_usd: "0",
    context: null,
  };
}

/** Counts the start requests that actually reach the backend, answering however a case needs. */
function mountLaunch(answer: () => Response): { starts: number } {
  const counted = { starts: 0 };
  server.use(
    http.post(`${BASE}/api/sessions`, () => HttpResponse.json({ session: sessionRow() })),
    http.patch(`${BASE}/api/sessions/sess-new`, () => HttpResponse.json({ session: sessionRow() })),
    http.post(`${BASE}/api/sessions/sess-new/run`, () => {
      counted.starts += 1;
      return answer();
    }),
  );
  return counted;
}

interface ProbeProps {
  /** How many times one decision to start calls `start`. Two is the double press. */
  presses: number;
  started: string[];
}

function Probe({ presses, started }: ProbeProps): ReactElement {
  const launch = useDeliberateLaunch({
    sessionTitle: "Check against the literature",
    onStarted: (sessionId) => started.push(sessionId),
  });
  return (
    <div>
      <button
        type="button"
        onClick={() => {
          for (let press = 0; press < presses; press += 1) launch.start(INTENT);
        }}
      >
        Start
      </button>
      <button type="button" onClick={launch.reconcile}>
        Settle
      </button>
      <p>{launch.unknownReason ?? "nothing in doubt"}</p>
      <p>{launch.settledAsNothing ? "nothing started" : "not settled"}</p>
    </div>
  );
}

describe("useDeliberateLaunch", () => {
  it("sends one start however many times one decision asks for it", async () => {
    const counted = mountLaunch(() => HttpResponse.json({ ok: true, running: INTENT.workflowId }));
    const started: string[] = [];
    render(<Probe presses={3} started={started} />);

    await userEvent.click(screen.getByRole("button", { name: "Start" }));

    await waitFor(() => expect(started).toEqual(["sess-new"]));
    expect(counted.starts).toBe(1);
  });

  it("leaves a start that got no answer in doubt, and sends nothing again on its own", async () => {
    const counted = mountLaunch(() => HttpResponse.error());
    const started: string[] = [];
    render(<Probe presses={1} started={started} />);

    await userEvent.click(screen.getByRole("button", { name: "Start" }));

    expect(await screen.findByText(/The start was sent and nothing answered/)).toBeInTheDocument();
    expect(started).toEqual([]);
    expect(counted.starts).toBe(1);
  });

  it("settles that doubt by reading the runs, which finds nothing when nothing started", async () => {
    const counted = mountLaunch(() => HttpResponse.error());
    server.use(
      http.get(`${BASE}/api/runs`, () => HttpResponse.json({ runs: [] })),
      http.get(`${BASE}/api/runs/recent`, () => HttpResponse.json({ runs: [] })),
    );
    render(<Probe presses={1} started={[]} />);

    await userEvent.click(screen.getByRole("button", { name: "Start" }));
    await screen.findByText(/The start was sent and nothing answered/);
    await userEvent.click(screen.getByRole("button", { name: "Settle" }));

    expect(await screen.findByText("nothing started")).toBeInTheDocument();
    expect(counted.starts).toBe(1);
  });

  it("starts again only when the person asks a second time", async () => {
    const counted = mountLaunch(() => HttpResponse.error());
    render(<Probe presses={1} started={[]} />);

    await userEvent.click(screen.getByRole("button", { name: "Start" }));
    await screen.findByText(/The start was sent and nothing answered/);
    await userEvent.click(screen.getByRole("button", { name: "Start" }));

    await waitFor(() => expect(counted.starts).toBe(2));
  });
});
