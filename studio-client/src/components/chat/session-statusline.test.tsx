import { screen, waitFor, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PluginsListing } from "@/api/harness-types";
import type { RunView, SessionSummary } from "@/api/sessions-types";
import type { GateMode, RunState } from "@/api/studio-types";
import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

import { SessionStatusline } from "./session-statusline";

const BASE = "http://studio.test";

afterEach(() => {
  vi.restoreAllMocks();
});

function usage() {
  return { input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, cache_write_tokens: 0 };
}

function sessionFixture(overrides: Partial<SessionSummary> = {}): SessionSummary {
  return {
    id: "s1",
    project_id: "proj-1",
    title: "Literature check",
    started_at: "2026-08-23T10:00:00Z",
    last_activity_at: "2026-08-23T10:05:00Z",
    archived: false,
    answering: true,
    turn_count: 2,
    usage: usage(),
    kind: "chat",
    cost_usd: "0",
    unpriced: 0,
    context: null,
    ...overrides,
  };
}

function runFixture(overrides: Partial<RunView> = {}): RunView {
  return {
    session_id: "s1",
    workflow_id: "wf.lit-check.v5",
    run_title: "",
    state: "running",
    active: ["draft"],
    done: ["intake"],
    failed: [],
    waiting_at: null,
    gate: null,
    started_at: "2026-08-24T00:00:00.000Z",
    ...overrides,
  };
}

function runStateFixture(overrides: Partial<RunState> = {}): RunState {
  return {
    running: true,
    workflow: "wf.lit-check.v5",
    result: null,
    active: ["draft"],
    done: ["intake"],
    failed: [],
    waiting_at: null,
    events: [],
    seq: 1,
    auto_approve: false,
    gate_mode: "wait",
    ...overrides,
  };
}

function pluginsFixture(): PluginsListing {
  return {
    plugins: [
      {
        name: "run_workflow",
        description: "It can run a checked workflow you already own.",
        enabled: true,
        fixed: true,
      },
      {
        name: "delete_knowledge_base",
        description: "It could delete a knowledge base.",
        enabled: false,
        fixed: false,
      },
    ],
  };
}

function mountApi(
  overrides: {
    session?: SessionSummary;
    runs?: RunView[];
    runState?: RunState;
    plugins?: PluginsListing;
  } = {},
) {
  server.use(
    http.get(`${BASE}/api/sessions/:id`, () =>
      HttpResponse.json({ session: overrides.session ?? sessionFixture(), turns: [] }),
    ),
    http.get(`${BASE}/api/runs`, () => HttpResponse.json({ runs: overrides.runs ?? [] })),
    http.get(`${BASE}/api/sessions/:id/run/state`, () =>
      HttpResponse.json(overrides.runState ?? runStateFixture()),
    ),
    http.get(`${BASE}/api/harness/plugins`, () =>
      HttpResponse.json(overrides.plugins ?? pluginsFixture()),
    ),
  );
}

function renderLine(gateMode: GateMode = "wait", onGateModeChange = vi.fn(), onCleared = vi.fn()) {
  return render(
    <SessionStatusline
      sessionId="s1"
      gateMode={gateMode}
      onGateModeChange={onGateModeChange}
      onCleared={onCleared}
    />,
  );
}

describe("SessionStatusline", () => {
  it("says nothing about an absent run, and shows no run actions", async () => {
    mountApi({ runs: [] });
    renderLine();

    await screen.findByRole("link", { name: "1 plugin" });
    expect(screen.queryByText("No run in this session.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pause" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
  });

  it("holds the station's own gate mode choice with no run live", async () => {
    const onGateModeChange = vi.fn();
    mountApi({ runs: [] });
    renderLine("wait", onGateModeChange);
    await screen.findByRole("link", { name: "1 plugin" });

    await userEvent.click(screen.getByRole("radio", { name: "Pass automatically" }));

    expect(onGateModeChange).toHaveBeenCalledWith("auto");
  });

  it("reflects the live run's own gate mode, and setAutomode when changed", async () => {
    mountApi({ runs: [runFixture()], runState: runStateFixture({ gate_mode: "wait" }) });
    let automodeBody: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions/s1/run/automode`, async ({ request }) => {
        automodeBody = await request.json();
        return HttpResponse.json({ ok: true, auto_approve: true });
      }),
    );
    renderLine();

    expect(await screen.findByRole("radio", { name: "Wait for me" })).toBeChecked();
    await userEvent.click(screen.getByRole("radio", { name: "Pass automatically" }));

    expect(automodeBody).toEqual({ on: true });
  });

  it("disables the orchestrator option while live, since this control cannot switch into it", async () => {
    mountApi({ runs: [runFixture()] });
    renderLine();
    await screen.findByRole("button", { name: "Pause" });

    expect(screen.getByRole("radio", { name: "The orchestrator decides" })).toBeDisabled();
  });

  it("attaches the given ref to the first gate radio, for Change the gates to focus", async () => {
    const ref = createRef<HTMLInputElement>();
    mountApi({ runs: [] });
    render(
      <SessionStatusline
        sessionId="s1"
        gateMode="wait"
        onGateModeChange={vi.fn()}
        onCleared={vi.fn()}
        firstGateRadioRef={ref}
      />,
    );
    await screen.findByRole("link", { name: "1 plugin" });

    expect(ref.current).toBe(screen.getByRole("radio", { name: "Wait for me" }));
  });

  it("counts the enabled plugins alone, linking to the harness station", async () => {
    mountApi({ runs: [] });
    renderLine();

    const link = await screen.findByRole("link", { name: "1 plugin" });
    expect(link).toHaveAttribute("href", "/harness");
    expect(link).toHaveAttribute("title", "What the agent may do here. Open Harness.");
  });

  it("names plural plugin counts correctly", async () => {
    mountApi({
      runs: [],
      plugins: {
        plugins: [
          {
            name: "run_workflow",
            description: "It can run a checked workflow you already own.",
            enabled: true,
            fixed: true,
          },
          {
            name: "read_knowledge_base",
            description: "It can read a knowledge base you already own.",
            enabled: true,
            fixed: false,
          },
        ],
      },
    });
    renderLine();

    expect(await screen.findByRole("link", { name: "2 plugins" })).toBeInTheDocument();
  });

  it("shows Pause for a running run and calls pauseRun", async () => {
    mountApi({ runs: [runFixture()] });
    let paused = false;
    server.use(
      http.post(`${BASE}/api/sessions/s1/run/pause`, () => {
        paused = true;
        return HttpResponse.json({ ok: true, paused: "wf.lit-check.v5" });
      }),
    );
    renderLine();

    await userEvent.click(await screen.findByRole("button", { name: "Pause" }));

    expect(paused).toBe(true);
  });

  it("shows Resume for a paused run and calls resumeRun", async () => {
    mountApi({ runs: [runFixture({ state: "paused" })] });
    let resumed = false;
    server.use(
      http.post(`${BASE}/api/sessions/s1/run/resume`, () => {
        resumed = true;
        return HttpResponse.json({ ok: true, running: "wf.lit-check.v5" });
      }),
    );
    renderLine();

    await userEvent.click(await screen.findByRole("button", { name: "Resume" }));

    expect(resumed).toBe(true);
  });

  it("stops the run only after the confirm dialog is accepted", async () => {
    mountApi({ runs: [runFixture()] });
    let stopped = false;
    server.use(
      http.post(`${BASE}/api/sessions/s1/run/stop`, () => {
        stopped = true;
        return HttpResponse.json({ ok: true, stopped: "wf.lit-check.v5" });
      }),
    );
    renderLine();

    await userEvent.click(await screen.findByRole("button", { name: "Stop" }));
    expect(stopped).toBe(false);
    const dialog = screen.getByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Stop" }));

    expect(stopped).toBe(true);
  });

  it("shows the run's refusal message when resume answers ok false, without touching the run", async () => {
    mountApi({ runs: [runFixture({ state: "paused" })] });
    server.use(
      http.post(`${BASE}/api/sessions/s1/run/resume`, () =>
        HttpResponse.json({
          ok: false,
          message: "send a graph to resume with, or settle a draft first",
        }),
      ),
    );
    renderLine();

    await userEvent.click(await screen.findByRole("button", { name: "Resume" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "send a graph to resume with, or settle a draft first",
    );
  });

  it("clears a refusal message once a later action succeeds", async () => {
    mountApi({ runs: [runFixture({ state: "paused" })] });
    let resumeCalls = 0;
    server.use(
      http.post(`${BASE}/api/sessions/s1/run/resume`, () => {
        resumeCalls += 1;
        return resumeCalls === 1
          ? HttpResponse.json({
              ok: false,
              message: "send a graph to resume with, or settle a draft first",
            })
          : HttpResponse.json({ ok: true, running: "wf.lit-check.v5" });
      }),
    );
    renderLine();

    await userEvent.click(await screen.findByRole("button", { name: "Resume" }));
    await screen.findByRole("alert");

    await userEvent.click(screen.getByRole("button", { name: "Resume" }));

    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });

  it("renders the cost under a dollar with four decimals, and the used/window/percent reading", async () => {
    mountApi({
      runs: [],
      session: sessionFixture({
        cost_usd: "0.0123",
        unpriced: 0,
        context: { used_tokens: 8000, window_tokens: 128000, fraction: 0.0625, resolved: true },
      }),
    });
    renderLine();

    expect(await screen.findByText("$0.0123")).toBeInTheDocument();
    expect(screen.getByText("8k of 128k")).toBeInTheDocument();
    expect(screen.getByText("6%")).toBeInTheDocument();
  });

  it("says unknown, not $0, for a conversation whose responses no source could price", async () => {
    mountApi({ runs: [], session: sessionFixture({ cost_usd: "0", unpriced: 5, context: null }) });
    renderLine();

    expect(await screen.findByText("unknown")).toBeInTheDocument();
    expect(screen.queryByText("$0")).not.toBeInTheDocument();
  });

  it("passes the session's usage totals through to the vitals readout", async () => {
    mountApi({
      runs: [],
      session: sessionFixture({
        usage: {
          input_tokens: 12345,
          output_tokens: 1100,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
        },
      }),
    });
    renderLine();

    expect(await screen.findByText("↑12.3k ↓1.1k")).toBeInTheDocument();
  });

  it("shows the turn's own elapsed time while one is in flight, off its own clock", async () => {
    vi.spyOn(Date, "now").mockReturnValue(new Date("2026-08-24T00:00:42.000Z").getTime());
    mountApi({ runs: [] });
    render(
      <SessionStatusline
        sessionId="s1"
        gateMode="wait"
        onGateModeChange={vi.fn()}
        onCleared={vi.fn()}
        turnStartedAt="2026-08-24T00:00:00.000Z"
      />,
    );

    expect(await screen.findByText("0:42")).toBeInTheDocument();
  });

  it("renders no readout and no Clear with no session to read, keeping the gate choice", async () => {
    mountApi({ runs: [] });
    render(
      <SessionStatusline
        sessionId={null}
        gateMode="wait"
        onGateModeChange={vi.fn()}
        onCleared={vi.fn()}
      />,
    );

    await screen.findByRole("link", { name: "1 plugin" });
    expect(screen.queryByText(/^\$/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear" })).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Wait for me" })).toBeInTheDocument();
  });

  it("renders no readout while the session's detail has not arrived, keeping Clear and the gate choice", async () => {
    mountApi({ runs: [] });
    server.use(http.get(`${BASE}/api/sessions/:id`, () => new Promise(() => {})));
    renderLine();

    await screen.findByRole("link", { name: "1 plugin" });
    expect(screen.queryByText(/^\$/)).not.toBeInTheDocument();
    expect(screen.queryByText("no reading")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Wait for me" })).toBeInTheDocument();
  });

  it("opens the confirm with the station's own title, clears through the same control, and calls onCleared", async () => {
    mountApi({ runs: [] });
    let cleared = false;
    server.use(
      http.delete(`${BASE}/api/sessions/s1/conversation`, () => {
        cleared = true;
        return HttpResponse.json({ ok: true });
      }),
    );
    const onCleared = vi.fn();
    render(
      <SessionStatusline
        sessionId="s1"
        gateMode="wait"
        onGateModeChange={vi.fn()}
        onCleared={onCleared}
      />,
    );
    await screen.findByRole("link", { name: "1 plugin" });

    await userEvent.click(screen.getByRole("button", { name: "Clear" }));
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByRole("heading", { name: "Start this session over?" }),
    ).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Clear" }));

    await waitFor(() => expect(cleared).toBe(true));
    await waitFor(() => expect(onCleared).toHaveBeenCalledTimes(1));
  });
});
