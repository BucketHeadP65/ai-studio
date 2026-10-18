import { screen } from "@testing-library/dom";
import { act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PendingGate, StepEvent } from "@/api/studio-types";
import { render } from "@/test-support/render";

import { GateTurn } from "./gate-turn";

const PENDING: PendingGate = {
  gate_id: "gate-1",
  workflow: "wf.lit-check.v5",
  reviewing: '{"title": "a draft"}',
  rejection_reruns: "compose",
};

const STARTED_AT = "2026-08-24T00:00:00.000Z";

function eventAt(seconds: number, finished = false): StepEvent {
  return { seq: 1, node: "gate-1", at: seconds, finished, failed: false };
}

describe("GateTurn", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("names the gate that is waiting", () => {
    vi.setSystemTime(new Date(STARTED_AT));
    render(
      <GateTurn
        pending={PENDING}
        startedAt={STARTED_AT}
        events={[]}
        onDecide={vi.fn()}
        deciding={false}
      />,
    );

    expect(
      screen.getByText(
        (_, element) =>
          element?.tagName.toLowerCase() === "p" &&
          element.textContent === "gate-1 is waiting for you.",
      ),
    ).toBeInTheDocument();
  });

  it("reads the elapsed wait off the gate's own unfinished event, not the run's start", () => {
    vi.setSystemTime(new Date("2026-08-24T00:01:00.000Z"));
    render(
      <GateTurn
        pending={PENDING}
        startedAt={STARTED_AT}
        events={[eventAt(30)]}
        onDecide={vi.fn()}
        deciding={false}
      />,
    );

    // The run started at 00:00:00, this gate's own event started at +30s (00:00:30), and
    // the clock reads 00:01:00 -- thirty seconds waiting at the gate, not sixty.
    expect(screen.getByText("30s")).toBeInTheDocument();
  });

  it("shows no elapsed time until the run state carries the gate's own event", () => {
    vi.setSystemTime(new Date("2026-08-24T00:01:00.000Z"));
    render(
      <GateTurn
        pending={PENDING}
        startedAt={STARTED_AT}
        events={[]}
        onDecide={vi.fn()}
        deciding={false}
      />,
    );

    expect(screen.queryByText(/^\d+[hms]/)).not.toBeInTheDocument();
  });

  it("ignores a finished event at the same node", () => {
    vi.setSystemTime(new Date("2026-08-24T00:01:00.000Z"));
    render(
      <GateTurn
        pending={PENDING}
        startedAt={STARTED_AT}
        events={[eventAt(10, true)]}
        onDecide={vi.fn()}
        deciding={false}
      />,
    );

    expect(screen.queryByText(/^\d+[hms]/)).not.toBeInTheDocument();
  });

  it("ticks the elapsed wait forward", () => {
    vi.setSystemTime(new Date("2026-08-24T00:01:00.000Z"));
    render(
      <GateTurn
        pending={PENDING}
        startedAt={STARTED_AT}
        events={[eventAt(30)]}
        onDecide={vi.fn()}
        deciding={false}
      />,
    );
    expect(screen.getByText("30s")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(5000);
    });

    expect(screen.getByText("35s")).toBeInTheDocument();
  });

  it("renders the existing GatePanel as its body, wired to the same onDecide", async () => {
    vi.setSystemTime(new Date(STARTED_AT));
    const onDecide = vi.fn();
    render(
      <GateTurn
        pending={PENDING}
        startedAt={STARTED_AT}
        events={[]}
        onDecide={onDecide}
        deciding={false}
      />,
    );

    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByText(PENDING.reviewing)).toBeInTheDocument();
  });

  it("disables the panel's own decide buttons while deciding", () => {
    vi.setSystemTime(new Date(STARTED_AT));
    render(
      <GateTurn
        pending={PENDING}
        startedAt={STARTED_AT}
        events={[]}
        onDecide={vi.fn()}
        deciding={true}
      />,
    );

    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
  });

  it("renders as one card, with no second header or pill from the panel underneath", () => {
    vi.setSystemTime(new Date(STARTED_AT));
    render(
      <GateTurn
        pending={PENDING}
        startedAt={STARTED_AT}
        events={[]}
        onDecide={vi.fn()}
        deciding={false}
      />,
    );

    expect(screen.queryByText("Waiting on you")).not.toBeInTheDocument();
    expect(screen.queryByText(/is waiting for you at/)).not.toBeInTheDocument();
  });

  it("keeps the rejection-reruns fact visible as a caption under the panel's buttons", () => {
    vi.setSystemTime(new Date(STARTED_AT));
    render(
      <GateTurn
        pending={PENDING}
        startedAt={STARTED_AT}
        events={[]}
        onDecide={vi.fn()}
        deciding={false}
      />,
    );

    expect(
      screen.getByText(
        (_, element) =>
          element?.tagName.toLowerCase() === "p" &&
          element.textContent === "(a rejection reruns from compose)",
      ),
    ).toBeInTheDocument();
  });
});
