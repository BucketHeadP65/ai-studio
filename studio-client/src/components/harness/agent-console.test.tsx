import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";

import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

import { AgentConsole } from "./agent-console";

// The control room's drawer. Its turns go to the session the conversation read names, so the
// composer and the one-click starters stay shut until that read lands: the starters sit inside
// the empty state, which is exactly what shows while the read is still in flight.

afterEach(cleanup);

const BASE = "http://studio.test";

describe("<AgentConsole />", () => {
  it("holds Send and the starters shut until the conversation read names a session", async () => {
    let release = (): void => undefined;
    const held = new Promise<void>((resolve) => {
      release = () => resolve();
    });
    server.use(
      http.get(`${BASE}/api/conversation`, async () => {
        await held;
        return HttpResponse.json({ turns: [], session_id: "s-1" });
      }),
    );

    render(<AgentConsole />);

    const starter = await screen.findByRole("button", { name: "Craft a capability" });
    await userEvent.type(screen.getByRole("textbox"), "craft me something");

    expect(starter).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();

    release();

    await waitFor(() => expect(starter).toBeEnabled());
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
  });

  it("enables Send and the starters once the read resolves with no session started yet", async () => {
    server.use(
      http.get(`${BASE}/api/conversation`, () =>
        HttpResponse.json({ turns: [], session_id: null }),
      ),
    );

    render(<AgentConsole />);

    const starter = await screen.findByRole("button", { name: "Craft a capability" });
    await userEvent.type(screen.getByRole("textbox"), "craft me something");

    await waitFor(() => expect(starter).toBeEnabled());
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
  });
});
