import { screen, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { PendingGate } from "@/api/studio-types";
import { render } from "@/test-support/render";

import { ControlStrip, type ControlStripProps } from "./control-strip";

// The one sticky block every act on the live run funnels through. Every test
// states which run phase it exercises; the component derives which buttons
// show from the plain booleans a phase implies, never from a phase name of
// its own.

const GATE: PendingGate = {
  gate_id: "review",
  workflow: "wf.lit-check.v5",
  reviewing: '{"risk": "low"}',
  rejection_reruns: "draft",
};

function baseProps(overrides: Partial<ControlStripProps> = {}): ControlStripProps {
  return {
    gate: null,
    onDecide: vi.fn(),
    deciding: false,
    running: false,
    paused: false,
    onRun: vi.fn(),
    runDisabled: false,
    starting: false,
    onPause: vi.fn(),
    pausing: false,
    onResume: vi.fn(),
    resuming: false,
    onStop: vi.fn(),
    stopping: false,
    automode: false,
    onAutomode: vi.fn(),
    settingAutomode: false,
    gateMode: "wait",
    ...overrides,
  };
}

describe("ControlStrip", () => {
  it("idle: offers Run only", () => {
    render(<ControlStrip {...baseProps()} />);

    expect(screen.getByRole("button", { name: "Run" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pause" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resume" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
  });

  it("running: offers Pause and Stop, not Run or Resume", () => {
    render(<ControlStrip {...baseProps({ running: true })} />);

    expect(screen.queryByRole("button", { name: "Run" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resume" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
  });

  it("paused: offers Resume and Stop, not Run or Pause", () => {
    render(<ControlStrip {...baseProps({ paused: true })} />);

    expect(screen.queryByRole("button", { name: "Run" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pause" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
  });

  it("waiting on a gate: shows Approve/Reject and Stop, hides Run/Pause/Resume", () => {
    render(<ControlStrip {...baseProps({ gate: GATE, running: true })} />);

    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pause" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
  });

  it("Stop asks first, then only fires once confirmed", async () => {
    const onStop = vi.fn();
    render(<ControlStrip {...baseProps({ running: true, onStop })} />);

    await userEvent.click(screen.getByRole("button", { name: "Stop" }));

    expect(onStop).not.toHaveBeenCalled();
    expect(screen.getByText("Stop for good?")).toBeInTheDocument();
    expect(screen.getByText("Nothing will be resumable.")).toBeInTheDocument();

    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Stop" }));

    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("cancelling the confirm never calls onStop", async () => {
    const onStop = vi.fn();
    render(<ControlStrip {...baseProps({ running: true, onStop })} />);

    await userEvent.click(screen.getByRole("button", { name: "Stop" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onStop).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("offers no Schedule it button when no handler is given", () => {
    render(<ControlStrip {...baseProps()} />);

    expect(screen.queryByRole("button", { name: "Schedule it" })).not.toBeInTheDocument();
  });

  it("offers Schedule it once a handler is given, and fires it", async () => {
    const onSchedule = vi.fn();
    render(<ControlStrip {...baseProps({ onSchedule })} />);

    await userEvent.click(screen.getByRole("button", { name: "Schedule it" }));

    expect(onSchedule).toHaveBeenCalledTimes(1);
  });

  it("hides the automode switch while idle", () => {
    render(<ControlStrip {...baseProps()} />);

    expect(screen.queryByRole("checkbox", { name: /Auto approve gates/ })).not.toBeInTheDocument();
  });

  it("hides the automode switch while paused", () => {
    render(<ControlStrip {...baseProps({ paused: true })} />);

    expect(screen.queryByRole("checkbox", { name: /Auto approve gates/ })).not.toBeInTheDocument();
  });

  it("shows the automode switch while running, unchecked by default", () => {
    render(<ControlStrip {...baseProps({ running: true })} />);

    const switchInput = screen.getByRole("checkbox", { name: /Auto approve gates/ });
    expect(switchInput).not.toBeChecked();
  });

  it("shows the automode switch while waiting at a gate", () => {
    render(<ControlStrip {...baseProps({ gate: GATE, running: true })} />);

    expect(screen.getByRole("checkbox", { name: /Auto approve gates/ })).toBeInTheDocument();
  });

  it("renders the switch checked once automode is on", () => {
    render(<ControlStrip {...baseProps({ running: true, automode: true })} />);

    expect(screen.getByRole("checkbox", { name: /Auto approve gates/ })).toBeChecked();
  });

  it("toggling the automode switch calls onAutomode with the opposite of its current state", async () => {
    const onAutomode = vi.fn();
    render(<ControlStrip {...baseProps({ running: true, automode: false, onAutomode })} />);

    await userEvent.click(screen.getByRole("checkbox", { name: /Auto approve gates/ }));

    expect(onAutomode).toHaveBeenCalledWith(true);
  });

  it("disables the automode switch while it is being set", () => {
    render(<ControlStrip {...baseProps({ running: true, settingAutomode: true })} />);

    expect(screen.getByRole("checkbox", { name: /Auto approve gates/ })).toBeDisabled();
  });

  it("shows no gate-mode line while waiting is this run's mode", () => {
    render(<ControlStrip {...baseProps({ running: true, gateMode: "wait" })} />);

    expect(screen.queryByText(/orchestrator/)).not.toBeInTheDocument();
    expect(screen.queryByText(/automatically/)).not.toBeInTheDocument();
  });

  it("states the run's mode when the orchestrator decides", () => {
    render(<ControlStrip {...baseProps({ running: true, gateMode: "orchestrate" })} />);

    expect(screen.getByText("gates decided by the orchestrator")).toBeInTheDocument();
  });

  it("states the run's mode when gates pass automatically", () => {
    render(<ControlStrip {...baseProps({ running: true, gateMode: "auto" })} />);

    expect(screen.getByText("gates pass automatically")).toBeInTheDocument();
  });

  it("hides the gate-mode line while idle, even when the mode is not wait", () => {
    render(<ControlStrip {...baseProps({ running: false, gateMode: "orchestrate" })} />);

    expect(screen.queryByText("gates decided by the orchestrator")).not.toBeInTheDocument();
  });

  it("uses only the house's allowed punctuation in the gate-mode line", () => {
    const ALLOWED_PUNCTUATION = /^[\w\s,.?()]*$/;
    render(<ControlStrip {...baseProps({ running: true, gateMode: "orchestrate" })} />);

    expect(screen.getByText("gates decided by the orchestrator").textContent ?? "").toMatch(
      ALLOWED_PUNCTUATION,
    );
  });
});
