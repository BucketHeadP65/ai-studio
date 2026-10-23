import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { Job, JobState } from "@/api/types";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

import { RefreshCard } from "./refresh";

afterEach(cleanup);

function job(state: JobState): Job {
  return {
    id: 7,
    kind: "kb.refresh",
    key: "tube",
    payload: { knowledge_base: "tube", trigger: "manual" },
    state,
    attempts: 1,
    max_attempts: 3,
    available_at: "2026-08-26T09:00:00+00:00",
    locked_at: null,
    locked_by: null,
    lease_seconds: 1800,
    last_error: null,
    result: state === "done" ? "indexed 3 pages, 3 embedded" : null,
    created_at: "2026-08-26T09:00:00+00:00",
    started_at: "2026-08-26T09:00:01+00:00",
    finished_at: state === "done" ? "2026-08-26T09:00:20+00:00" : null,
  };
}

// The card queues a job, then follows it: the refresh-history read and the job read are what it lives on.
// `states` is drained one entry per job read, so a test scripts the run's progress.
function serveRefresh(states: JobState[]) {
  server.use(
    http.get("http://api.test/api/knowledge-bases/:name/refresh-runs", () => HttpResponse.json({ refresh_runs: [] })),
    http.post("http://api.test/api/knowledge-bases/:name/refresh", () =>
      HttpResponse.json({ job_id: 7 }, { status: 202 }),
    ),
    http.get("http://api.test/api/jobs/7", () =>
      HttpResponse.json(
        job(states.length > 1 ? (states.shift() ?? "running") : (states[0] ?? "running")),
      ),
    ),
  );
}

beforeEach(() => serveRefresh(["running"]));

describe("RefreshCard", () => {
  it("names the job it queued while the worker is still running it", async () => {
    render(<RefreshCard name="tube" registered={true} lastRun={null} schedule={null} />);

    await userEvent.click(screen.getByRole("button", { name: "Refresh now" }));

    expect(await screen.findByText(/Queued as job 7/)).toBeInTheDocument();
  });

  it("stops naming the job once the run is over", async () => {
    serveRefresh(["running", "done"]);
    const { queryClient } = render(
      <RefreshCard name="tube" registered={true} lastRun={null} schedule={null} />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Refresh now" }));
    await screen.findByText(/Queued as job 7/);

    // Nothing polls the job query: the push channel invalidates it on a "jobs" topic, which this
    // stands in for.
    await queryClient.invalidateQueries({ queryKey: ["jobs", 7] });

    await waitFor(() => expect(screen.queryByText(/Queued as job 7/)).not.toBeInTheDocument(), {
      timeout: 8000,
    });
  }, 15000);

  it("stops naming a run that was already underway once it is over", async () => {
    const states: JobState[] = ["running", "abandoned"];
    server.use(
      http.get("http://api.test/api/knowledge-bases/:name/refresh-runs", () => HttpResponse.json({ refresh_runs: [] })),
      http.post("http://api.test/api/knowledge-bases/:name/refresh", () =>
        HttpResponse.json(
          { error: "refresh_in_progress", detail: "already underway.", job_id: 7 },
          { status: 409 },
        ),
      ),
      http.get("http://api.test/api/jobs/7", () =>
        HttpResponse.json(
          job(states.length > 1 ? (states.shift() ?? "running") : (states[0] ?? "running")),
        ),
      ),
    );
    const { queryClient } = render(
      <RefreshCard name="tube" registered={true} lastRun={null} schedule={null} />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Refresh now" }));
    await screen.findByText(/already underway/);

    // Nothing polls the job query: the push channel invalidates it on a "jobs" topic, which this
    // stands in for.
    await queryClient.invalidateQueries({ queryKey: ["jobs", 7] });

    await waitFor(() => expect(screen.queryByText(/already underway/)).not.toBeInTheDocument(), {
      timeout: 8000,
    });
  }, 15000);
});
