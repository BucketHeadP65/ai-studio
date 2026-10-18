import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

import { RunChatDrawer } from "./run-chat-drawer";

// The drawer's own network surface is entirely SessionChat's: the transcript
// for the run's session, plus the session list and runs list SessionChat
// reads for `answering` and a takeover. Mocked here end to end.

const BASE = "http://studio.test";

function mountApi() {
  server.use(
    http.get(`${BASE}/api/sessions/run-s1/transcript`, () =>
      HttpResponse.json({
        messages: [
          {
            run_id: "r1",
            idx: 0,
            kind: "request",
            payload: {
              kind: "request",
              parts: [{ part_kind: "user-prompt", content: "start it" }],
            },
            text: "start it",
          },
          {
            run_id: "r1",
            idx: 1,
            kind: "response",
            payload: { kind: "response", parts: [{ part_kind: "text", content: "on it" }] },
            text: "on it",
          },
        ],
      }),
    ),
    http.get(`${BASE}/api/sessions`, () => HttpResponse.json({ sessions: [] })),
    http.get(`${BASE}/api/runs`, () => HttpResponse.json({ runs: [] })),
  );
}

describe("RunChatDrawer", () => {
  it("renders nothing when closed", () => {
    mountApi();
    render(<RunChatDrawer sessionId="run-s1" open={false} onClose={vi.fn()} />);

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("hosts the run's session conversation, full height, when open", async () => {
    mountApi();
    render(<RunChatDrawer sessionId="run-s1" open onClose={vi.fn()} />);

    expect(await screen.findByText("on it")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Message" })).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Run chat" })).toBeInTheDocument();
  });

  it("closes on the close button", async () => {
    mountApi();
    const onClose = vi.fn();
    render(<RunChatDrawer sessionId="run-s1" open onClose={onClose} />);
    await screen.findByText("on it");

    await userEvent.click(screen.getByRole("button", { name: "Close drawer" }));

    expect(onClose).toHaveBeenCalledOnce();
  });
});
