import { fireEvent, screen, waitFor, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { type ReactElement, useRef } from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type { SessionSummary } from "@/api/sessions-types";
import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

import { Companion } from "./companion";
import type { Placement } from "./placement";
import { FollowMeProvider } from "./provider";

// The companion's own network surface: the master session id, its detail (the vitals row's cost
// and context reading), the transcript and lists SessionChat polls once it mounts, and the
// conversation route Clear calls through. Every request the component can fire is mocked here.
//
// jsdom builds PointerEvent but never implements pointer capture, so the two capture calls are
// stubbed; every drag below is a real press, move, release sequence against the grips.

// The pathname is read through a store so a push moves the page the way a router does: the
// companion's navigate verb only answers once the page it asked for is the page on screen.
const listeners = new Set<() => void>();
let pathnameValue = "/projects";
let searchParamsValue = new URLSearchParams();

function goTo(pathname: string): void {
  pathnameValue = pathname;
  for (const listener of [...listeners]) listener();
}

const routerPush = vi.fn((url: string) => {
  goTo(url.split("?")[0] ?? "");
});

vi.mock("next/navigation", async () => {
  const { useSyncExternalStore } = await import("react");
  const subscribe = (listener: () => void) => {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  };
  const read = () => pathnameValue;
  return {
    usePathname: () => useSyncExternalStore(subscribe, read, read),
    useSearchParams: () => searchParamsValue,
    useRouter: () => ({ push: routerPush }),
  };
});

const BASE = "http://studio.test";

beforeAll(() => {
  Element.prototype.setPointerCapture = vi.fn();
  Element.prototype.releasePointerCapture = vi.fn();
});

function usage() {
  return { input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, cache_write_tokens: 0 };
}

function masterSessionFixture(overrides: Partial<SessionSummary> = {}): SessionSummary {
  return {
    id: "m-1",
    project_id: "proj-1",
    title: "Companion",
    started_at: "2026-08-25T10:00:00Z",
    last_activity_at: "2026-08-25T10:00:00Z",
    archived: false,
    answering: false,
    turn_count: 0,
    usage: usage(),
    kind: "master",
    cost_usd: "0.0421",
    unpriced: 0,
    context: { used_tokens: 8000, window_tokens: 128000, fraction: 0.0625, resolved: true },
    ...overrides,
  };
}

function mountApi(overrides: { masterStatus?: number; session?: SessionSummary } = {}) {
  server.use(
    // Every send knocks on the steer endpoint first; an idle session answers 409 and the message
    // goes out as an ordinary turn.
    http.post(`${BASE}/api/sessions/:id/steer`, () =>
      HttpResponse.json({ detail: "nothing is in flight in this session" }, { status: 409 }),
    ),
    http.get(`${BASE}/api/master`, () => {
      if (overrides.masterStatus !== undefined) {
        return HttpResponse.json(
          { detail: "master is not resolved" },
          { status: overrides.masterStatus },
        );
      }
      return HttpResponse.json({ session_id: "m-1" });
    }),
    http.get(`${BASE}/api/sessions/m-1`, () =>
      HttpResponse.json({ session: overrides.session ?? masterSessionFixture(), turns: [] }),
    ),
    http.get(`${BASE}/api/sessions/m-1/transcript`, () => HttpResponse.json({ messages: [] })),
    http.get(`${BASE}/api/sessions`, () => HttpResponse.json({ sessions: [] })),
    http.get(`${BASE}/api/runs`, () => HttpResponse.json({ runs: [] })),
  );
}

function Host(): ReactElement {
  const columnRef = useRef<HTMLDivElement>(null);
  return (
    <FollowMeProvider>
      <div ref={columnRef} className="relative">
        <main>page content</main>
        <Companion columnRef={columnRef} />
      </div>
    </FollowMeProvider>
  );
}

function renderCompanion() {
  return render(<Host />);
}

function stored(key: string): string | null {
  return window.localStorage.getItem(`studio:${key}`);
}

function preset(entries: Record<string, string>): void {
  for (const [key, value] of Object.entries(entries)) {
    window.localStorage.setItem(`studio:${key}`, value);
  }
}

function pinnedAt(placement: Placement): void {
  preset({ "followme:pinned": "1", "followme:placement": placement });
}

function separator(): HTMLElement {
  return screen.getByRole("separator", { name: "Resize" });
}

function drag(target: HTMLElement, to: { x: number; y: number }): void {
  fireEvent.pointerDown(target, { pointerId: 1, clientX: 0, clientY: 0 });
  fireEvent.pointerMove(target, { pointerId: 1, clientX: to.x, clientY: to.y });
}

beforeEach(() => {
  window.localStorage.clear();
  pathnameValue = "/projects";
  searchParamsValue = new URLSearchParams();
  routerPush.mockClear();
});

afterEach(() => {
  window.localStorage.clear();
});

describe("Companion, collapsed and pinned", () => {
  it("collapses to exactly one labelled button until pinned", async () => {
    mountApi();
    renderCompanion();

    expect(await screen.findByRole("button", { name: "Companion" })).toBeInTheDocument();
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("makes no master request while unpinned", async () => {
    let calls = 0;
    server.use(
      http.get(`${BASE}/api/master`, () => {
        calls += 1;
        return HttpResponse.json({ session_id: "m-1" });
      }),
    );
    renderCompanion();

    await screen.findByRole("button", { name: "Companion" });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(calls).toBe(0);
  });

  it("pinning loads the master conversation, shows its vitals, and collapses the nav", async () => {
    mountApi();
    renderCompanion();

    await userEvent.click(await screen.findByRole("button", { name: "Companion" }));

    expect(await screen.findByRole("textbox", { name: "Message" })).toBeInTheDocument();
    expect(await screen.findByText("$0.0421")).toBeInTheDocument();
    expect(screen.getByText("8k of 128k")).toBeInTheDocument();
    expect(screen.getByText("6%")).toBeInTheDocument();
    expect(stored("sidebar:collapsed")).toBe("1");
  });

  it("opens beside the page rather than over it, and collapses from its own button", async () => {
    mountApi();
    renderCompanion();

    await userEvent.click(await screen.findByRole("button", { name: "Companion" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(await screen.findByText("Companion", { exact: true })).toBeInTheDocument();
    expect(screen.getByText("page content")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Collapse" }));
    expect(await screen.findByRole("button", { name: "Companion" })).toBeInTheDocument();
    expect(screen.getByText("page content")).toBeInTheDocument();
  });

  it("the pinned choice persists across a remount, same as a page change", async () => {
    mountApi();
    const { unmount } = renderCompanion();
    await userEvent.click(await screen.findByRole("button", { name: "Companion" }));
    expect(await screen.findByRole("button", { name: "Collapse" })).toBeInTheDocument();
    unmount();

    renderCompanion();
    expect(await screen.findByRole("button", { name: "Collapse" })).toBeInTheDocument();
  });

  it("pinning into a failed master lookup shows the notice and the backend detail on separate lines", async () => {
    mountApi({ masterStatus: 500 });
    renderCompanion();

    await userEvent.click(await screen.findByRole("button", { name: "Companion" }));

    expect(await screen.findByText("Could not load the companion.")).toBeInTheDocument();
    expect(screen.getByText("master is not resolved")).toBeInTheDocument();
  });

  it("Clear asks for confirmation, then forgets the conversation and empties the thread", async () => {
    mountApi();
    const deleteSpy = vi.fn();
    let cleared = false;
    server.use(
      http.get(`${BASE}/api/sessions/m-1/transcript`, () =>
        HttpResponse.json({
          messages: cleared
            ? []
            : [
                {
                  run_id: "run-1",
                  idx: 0,
                  kind: "request",
                  payload: {
                    kind: "request",
                    parts: [{ part_kind: "user-prompt", content: "remember this" }],
                  },
                  text: "remember this",
                },
              ],
        }),
      ),
      http.delete(`${BASE}/api/sessions/m-1/conversation`, () => {
        deleteSpy();
        cleared = true;
        return HttpResponse.json({ ok: true });
      }),
    );
    renderCompanion();
    await userEvent.click(await screen.findByRole("button", { name: "Companion" }));
    await screen.findByText("$0.0421");
    expect(await screen.findByText("remember this")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Clear" }));
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByRole("heading", { name: "Start the companion over?" }),
    ).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Clear" }));

    await waitFor(() => expect(deleteSpy).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByText("remember this")).not.toBeInTheDocument());
  });

  it("says so plainly when the backend has no context reading yet", async () => {
    mountApi({ session: masterSessionFixture({ context: null }) });
    pinnedAt("left");
    renderCompanion();

    expect(await screen.findByText("no reading")).toBeInTheDocument();
    expect(screen.queryByText(/of 128k/)).not.toBeInTheDocument();
  });
});

describe("Companion, the nav it shares an edge with", () => {
  it("unpin restores the nav to whatever it was before this pin, not a hardcoded value", async () => {
    preset({ "sidebar:collapsed": "1" });
    mountApi();
    renderCompanion();

    await userEvent.click(await screen.findByRole("button", { name: "Companion" }));
    await screen.findByRole("button", { name: "Collapse" });

    await userEvent.click(screen.getByRole("button", { name: "Collapse" }));

    await waitFor(() => expect(stored("sidebar:collapsed")).toBe("1"));
  });

  it("mounting already pinned restores the nav to expanded on collapse, since no pin here captured a prior value", async () => {
    preset({ "followme:pinned": "1", "sidebar:collapsed": "1" });
    mountApi();
    renderCompanion();

    await userEvent.click(await screen.findByRole("button", { name: "Collapse" }));

    await waitFor(() => expect(stored("sidebar:collapsed")).toBe("0"));
  });

  it("leaves the nav alone when it is pinned anywhere but the left edge", async () => {
    mountApi();
    pinnedAt("bottom");
    renderCompanion();

    await userEvent.click(await screen.findByRole("button", { name: "Collapse" }));
    expect(stored("sidebar:collapsed")).toBeNull();

    await userEvent.click(await screen.findByRole("button", { name: "Companion" }));
    await screen.findByRole("button", { name: "Collapse" });
    expect(stored("sidebar:collapsed")).toBeNull();
  });

  it("moving off the left edge restores the nav, and moving back collapses it again", async () => {
    mountApi();
    pinnedAt("left");
    renderCompanion();

    await waitFor(() => expect(stored("followme:placement")).toBe("left"));
    await userEvent.click(await screen.findByRole("button", { name: "Bottom panel" }));

    await waitFor(() => expect(stored("sidebar:collapsed")).toBe("0"));

    await userEvent.click(screen.getByRole("button", { name: "Left rail" }));

    await waitFor(() => expect(stored("sidebar:collapsed")).toBe("1"));
  });
});

describe("Companion, the placement switcher", () => {
  it("offers the four slots and marks the one it is in", async () => {
    mountApi();
    pinnedAt("right");
    renderCompanion();

    const labels = ["Left rail", "Right rail", "Bottom panel", "Floating window"];
    for (const label of labels) {
      expect(await screen.findByRole("button", { name: label })).toBeInTheDocument();
    }
    expect(screen.getByRole("button", { name: "Right rail" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Left rail" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("remembers the slot a viewer chose", async () => {
    mountApi();
    pinnedAt("left");
    renderCompanion();

    await userEvent.click(await screen.findByRole("button", { name: "Floating window" }));

    await waitFor(() => expect(stored("followme:placement")).toBe("floating"));
    expect(screen.getByRole("button", { name: "Floating window" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("names its collapsed control Companion in every slot", async () => {
    for (const placement of ["left", "right", "bottom", "floating"] as const) {
      window.localStorage.clear();
      preset({ "followme:placement": placement });
      const { unmount } = renderCompanion();
      expect(await screen.findByRole("button", { name: "Companion" })).toBeInTheDocument();
      expect(screen.getAllByRole("button")).toHaveLength(1);
      unmount();
    }
  });
});

describe("Companion, resizing", () => {
  it("drags a left rail wider, shows the size while it moves, and keeps it on release", async () => {
    mountApi();
    pinnedAt("left");
    renderCompanion();

    const grip = separator();
    expect(grip).toHaveAttribute("aria-orientation", "vertical");
    expect(grip).toHaveAttribute("aria-valuemin", "320");
    expect(grip).toHaveAttribute("aria-valuemax", "640");
    expect(grip).toHaveAttribute("aria-valuenow", "400");

    drag(grip, { x: 60, y: 0 });

    expect(grip).toHaveAttribute("aria-valuenow", "460");
    expect(screen.getByText("460")).toBeInTheDocument();
    expect(stored("followme:width")).toBeNull();

    fireEvent.pointerUp(grip, { pointerId: 1, clientX: 60, clientY: 0 });

    expect(stored("followme:width")).toBe("460");
    expect(screen.queryByText("460")).not.toBeInTheDocument();
  });

  it("never drags a rail past its range", async () => {
    mountApi();
    pinnedAt("left");
    renderCompanion();

    const grip = separator();
    drag(grip, { x: 900, y: 0 });
    expect(grip).toHaveAttribute("aria-valuenow", "640");

    fireEvent.pointerMove(grip, { pointerId: 1, clientX: -900, clientY: 0 });
    expect(grip).toHaveAttribute("aria-valuenow", "320");
  });

  it("reads a stored width back inside the range", async () => {
    mountApi();
    pinnedAt("left");
    preset({ "followme:width": "5000" });
    renderCompanion();

    await waitFor(() => expect(separator()).toHaveAttribute("aria-valuenow", "640"));
  });

  it("grows a right rail when the grip travels toward the page", async () => {
    mountApi();
    pinnedAt("right");
    renderCompanion();

    drag(separator(), { x: -60, y: 0 });

    expect(separator()).toHaveAttribute("aria-valuenow", "460");
  });

  it("drags the bottom panel taller from its top edge", async () => {
    mountApi();
    pinnedAt("bottom");
    renderCompanion();

    const grip = separator();
    expect(grip).toHaveAttribute("aria-orientation", "horizontal");
    expect(grip).toHaveAttribute("aria-valuemin", "240");
    expect(grip).toHaveAttribute("aria-valuemax", "600");
    expect(grip).toHaveAttribute("aria-valuenow", "360");

    drag(grip, { x: 0, y: -60 });

    expect(grip).toHaveAttribute("aria-valuenow", "420");

    fireEvent.pointerUp(grip, { pointerId: 1, clientX: 0, clientY: -60 });
    expect(stored("followme:height")).toBe("420");
  });

  it("resizes from the keyboard, one step per press", async () => {
    mountApi();
    pinnedAt("left");
    renderCompanion();

    const grip = separator();
    fireEvent.keyDown(grip, { key: "ArrowRight" });
    expect(grip).toHaveAttribute("aria-valuenow", "416");
    fireEvent.keyDown(grip, { key: "ArrowLeft" });
    fireEvent.keyDown(grip, { key: "ArrowLeft" });
    expect(grip).toHaveAttribute("aria-valuenow", "384");
    expect(stored("followme:width")).toBe("384");
  });
});

describe("Companion, the floating window", () => {
  it("moves by its handle and keeps where it landed", async () => {
    mountApi();
    pinnedAt("floating");
    preset({ "followme:window": JSON.stringify({ x: 100, y: 80, w: 420, h: 580 }) });
    renderCompanion();

    const handle = await screen.findByTestId("companion-handle");
    drag(handle, { x: 40, y: 25 });
    fireEvent.pointerUp(handle, { pointerId: 1, clientX: 40, clientY: 25 });

    expect(stored("followme:window")).toBe(JSON.stringify({ x: 140, y: 105, w: 420, h: 580 }));
  });

  it("never moves the window off the top or the left of the page", async () => {
    mountApi();
    pinnedAt("floating");
    preset({ "followme:window": JSON.stringify({ x: 20, y: 20, w: 420, h: 580 }) });
    renderCompanion();

    const handle = await screen.findByTestId("companion-handle");
    drag(handle, { x: -300, y: -300 });
    fireEvent.pointerUp(handle, { pointerId: 1, clientX: -300, clientY: -300 });

    expect(stored("followme:window")).toBe(JSON.stringify({ x: 0, y: 0, w: 420, h: 580 }));
  });

  it("resizes from its corner, clamped to the window's own range", async () => {
    mountApi();
    pinnedAt("floating");
    preset({ "followme:window": JSON.stringify({ x: 10, y: 10, w: 420, h: 580 }) });
    renderCompanion();

    const corner = await screen.findByRole("separator", { name: "Resize" });
    drag(corner, { x: 60, y: -100 });
    fireEvent.pointerUp(corner, { pointerId: 1, clientX: 60, clientY: -100 });

    expect(stored("followme:window")).toBe(JSON.stringify({ x: 10, y: 10, w: 480, h: 480 }));

    drag(corner, { x: -900, y: -900 });
    fireEvent.pointerUp(corner, { pointerId: 1, clientX: -900, clientY: -900 });

    expect(stored("followme:window")).toBe(JSON.stringify({ x: 10, y: 10, w: 360, h: 400 }));
  });

  it("clicking a control on the handle row never drags the window", async () => {
    mountApi();
    pinnedAt("floating");
    preset({ "followme:window": JSON.stringify({ x: 10, y: 10, w: 420, h: 580 }) });
    renderCompanion();

    await userEvent.click(await screen.findByRole("button", { name: "Left rail" }));

    await waitFor(() => expect(stored("followme:placement")).toBe("left"));
    expect(stored("followme:window")).toBe(JSON.stringify({ x: 10, y: 10, w: 420, h: 580 }));
  });
});

// The chat stream protocol (Vercel AI data-stream chunks over SSE): a deferred turn ends
// with a companion tool call still unanswered, a settled one ends in plain text.

type StreamChunk = Record<string, unknown>;

function sseBody(chunks: StreamChunk[]): string {
  const lines = chunks.map((chunk) => `data: ${JSON.stringify(chunk)}\n\n`).join("");
  return `${lines}data: [DONE]\n\n`;
}

function streamResponse(chunks: StreamChunk[]) {
  return new HttpResponse(sseBody(chunks), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

const DEFERRED: StreamChunk[] = [
  { type: "start" },
  { type: "start-step" },
  { type: "tool-input-start", toolCallId: "c-1", toolName: "navigate" },
  {
    type: "tool-input-available",
    toolCallId: "c-1",
    toolName: "navigate",
    input: { url: "/chat" },
  },
  { type: "finish-step" },
  { type: "finish" },
];

const SETTLED: StreamChunk[] = [
  { type: "start" },
  { type: "start-step" },
  { type: "text-start", id: "t1" },
  { type: "text-delta", id: "t1", delta: "done" },
  { type: "text-end", id: "t1" },
  { type: "finish-step" },
  { type: "finish" },
];

describe("Companion, performing a verb", () => {
  it("runs a navigate call the stream defers, moves the router, and reports the outcome back", async () => {
    mountApi();
    const bodies: unknown[] = [];
    let calls = 0;
    server.use(
      http.post(`${BASE}/api/sessions/m-1/messages`, async ({ request }) => {
        bodies.push(await request.json());
        calls += 1;
        return streamResponse(calls === 1 ? DEFERRED : SETTLED);
      }),
    );
    renderCompanion();

    await userEvent.click(await screen.findByRole("button", { name: "Companion" }));
    const box = await screen.findByRole("textbox", { name: "Message" });
    await userEvent.type(box, "take me to chat{Enter}");

    await waitFor(() => expect(routerPush).toHaveBeenCalledWith("/chat"));
    await waitFor(() => expect(bodies).toHaveLength(2));
    expect(bodies[1]).toMatchObject({
      tool_results: [{ tool_call_id: "c-1", tool_name: "navigate", output: "navigated to /chat" }],
    });
  });
});
