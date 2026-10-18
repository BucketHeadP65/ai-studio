import { fireEvent, screen, waitFor, within } from "@testing-library/dom";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { sessionKeys } from "@/api/query-keys";
import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

import { ClearControl, VitalsReadout, formatTurnElapsed, tokensReadoutText } from "./vitals";

describe("tokensReadoutText", () => {
  it("reads under a thousand as-is", () => {
    expect(tokensReadoutText(999, 0)).toBe("↑999 ↓0");
  });

  it("reads a round thousand with the decimal dropped", () => {
    expect(tokensReadoutText(1000, 0)).toBe("↑1k ↓0");
  });

  it("reads thousands with one decimal", () => {
    expect(tokensReadoutText(12345, 1100)).toBe("↑12.3k ↓1.1k");
  });

  it("reads millions with one decimal", () => {
    expect(tokensReadoutText(1_500_000, 0)).toBe("↑1.5M ↓0");
  });
});

describe("formatTurnElapsed", () => {
  it("pads seconds under a minute", () => {
    expect(formatTurnElapsed(42_000)).toBe("0:42");
  });

  it("reads minutes and padded seconds past a minute", () => {
    expect(formatTurnElapsed(725_000)).toBe("12:05");
  });

  it("clamps a negative input to zero rather than going blank", () => {
    expect(formatTurnElapsed(-1)).toBe("0:00");
  });

  it("reads empty for a non-finite input", () => {
    expect(formatTurnElapsed(Number.NaN)).toBe("");
  });
});

function baseProps() {
  return {
    costUsd: "0",
    unpriced: 0,
    context: null,
    tokensIn: 0,
    tokensOut: 0,
    turnStartedAt: null as string | null,
    now: 0,
  };
}

describe("VitalsReadout", () => {
  it("shows the session's token totals, compactly, with the full count as the accessible name", () => {
    render(<VitalsReadout {...baseProps()} tokensIn={12345} tokensOut={1100} />);

    expect(screen.getByRole("img", { name: "12,345 tokens in, 1,100 out" })).toHaveTextContent(
      "↑12.3k ↓1.1k",
    );
  });

  it("hides the elapsed reading while no turn is in flight", () => {
    render(<VitalsReadout {...baseProps()} turnStartedAt={null} />);

    expect(screen.queryByText(/^\d+:\d{2}$/)).not.toBeInTheDocument();
  });

  it("shows the turn's elapsed time, off the caller's own clock", () => {
    render(
      <VitalsReadout
        {...baseProps()}
        turnStartedAt="2026-08-24T00:00:00.000Z"
        now={new Date("2026-08-24T00:00:42.000Z").getTime()}
      />,
    );

    expect(screen.getByText("0:42")).toBeInTheDocument();
  });
});

describe("ClearControl", () => {
  it("invalidates the transcript and detail queries the chat rail reads", async () => {
    server.use(
      http.delete("http://studio.test/api/sessions/s1/conversation", () =>
        HttpResponse.json({ ok: true }),
      ),
    );
    let cleared = false;
    const { queryClient } = render(
      <ClearControl
        masterId="s1"
        onCleared={() => {
          cleared = true;
        }}
      />,
    );
    queryClient.setQueryData(sessionKeys.transcript("s1"), { messages: [] });
    queryClient.setQueryData(sessionKeys.detail("s1"), { id: "s1" });

    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Clear" }));

    await waitFor(() => expect(cleared).toBe(true));
    expect(queryClient.getQueryState(sessionKeys.transcript("s1"))?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(sessionKeys.detail("s1"))?.isInvalidated).toBe(true);
  });
});
