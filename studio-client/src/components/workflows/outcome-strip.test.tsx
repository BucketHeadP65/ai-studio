import { screen } from "@testing-library/dom";
import { act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Outcome, RunEnvelope } from "@/api/studio-types";
import { render } from "@/test-support/render";

import { OutcomeStrip, RESULT_STATUS, ResultSummary } from "./outcome-strip";

// The strip's one job: read a run's own record honestly, in words a reader does not have to
// decode the backend's own field names to trust -- and keep a live run's Duration counting on
// its own clock, since the poll that carries the envelope runs far coarser than a second.

function envelope(overrides: Partial<RunEnvelope> = {}): RunEnvelope {
  return {
    result: { status: "unknown", summary: "", attempted: 0, succeeded: 0, skipped: 0, failed: 0, retry_attempts: 0, primary_input: null, generation: null, checks: [], artifacts: [] },
    run_id: "run-1",
    workflow_id: "wf.lit-check.v5",
    run_title: "",
    outcome: "completed",
    failure: "",
    started_at: "2026-08-20T10:00:00Z",
    ended_at: "2026-08-20T10:05:00Z",
    runner: "app:chat",
    session_id: "sess-1",
    params: {},
    gates_approved: [],
    gates_rejected: {},
    gates_auto: [],
    gates_orchestrated: [],
    pinned: false,
    spend: null,
    ...overrides,
  };
}

describe("OutcomeStrip", () => {
  it("shows the archived outcome and its duration, the duration set in mono", () => {
    render(<OutcomeStrip envelope={envelope({ outcome: "failed" })} liveState={null} />);

    expect(screen.getByText("Failed")).toBeInTheDocument();
    const duration = screen.getByText("5m 0s");
    expect(duration).toBeInTheDocument();
    expect(duration.className).toContain("font-mono");
  });

  it("shows a live run's finer state, not the bare word live", () => {
    render(
      <OutcomeStrip envelope={envelope({ outcome: "live", ended_at: "" })} liveState="waiting" />,
    );

    expect(screen.getByText("Waiting on you")).toBeInTheDocument();
  });

  it("shows a paused live run as paused", () => {
    render(
      <OutcomeStrip envelope={envelope({ outcome: "live", ended_at: "" })} liveState="paused" />,
    );

    expect(screen.getByText("Paused")).toBeInTheDocument();
  });

  it("reads who started it in words: chat, a person at the controls, or a named schedule", () => {
    const { rerender } = render(
      <OutcomeStrip envelope={envelope({ runner: "app:harness" })} liveState={null} />,
    );
    expect(screen.getByText("a person at the controls")).toBeInTheDocument();

    rerender(<OutcomeStrip envelope={envelope({ runner: "app:chat" })} liveState={null} />);
    expect(screen.getByText("chat")).toBeInTheDocument();

    rerender(
      <OutcomeStrip envelope={envelope({ runner: "app:schedule:sched-1" })} liveState={null} />,
    );
    expect(screen.getByText("schedule sched-1")).toBeInTheDocument();
  });

  it("says unknown, not $0, for a run whose every response had no price anyone could name", () => {
    render(
      <OutcomeStrip
        envelope={envelope({
          spend: {
            cost_usd: "0",
            unpriced: 4,
            usage: {
              input_tokens: 100,
              output_tokens: 50,
              cache_read_tokens: 0,
              cache_write_tokens: 0,
            },
            steps: {},
          },
        })}
        liveState={null}
      />,
    );

    // The run has a ledger and it ran four responses. What it cost is unknown, and $0 would say
    // the run was free.
    expect(screen.getByText("unknown")).toBeInTheDocument();
    expect(screen.queryByText("$0")).not.toBeInTheDocument();
  });

  it("shows the run's own priced cost in a fifth cell labeled Cost", () => {
    render(
      <OutcomeStrip
        envelope={envelope({
          spend: {
            cost_usd: "0.0421",
            unpriced: 0,
            usage: {
              input_tokens: 100,
              output_tokens: 50,
              cache_read_tokens: 0,
              cache_write_tokens: 0,
            },
            steps: {},
          },
        })}
        liveState={null}
      />,
    );

    expect(screen.getByText("Cost")).toBeInTheDocument();
    expect(screen.getByText("$0.0421")).toBeInTheDocument();
  });

  it("reads the cost cell as unknown when no ledger exists for the run", () => {
    render(<OutcomeStrip envelope={envelope({ spend: null })} liveState={null} />);

    expect(screen.getByText("unknown")).toBeInTheDocument();
  });

  it("names the absolute start time on the relative one, for a hover to read", () => {
    render(<OutcomeStrip envelope={envelope()} liveState={null} />);

    const started = screen.getByTitle(new Date("2026-08-20T10:00:00Z").toLocaleString());
    expect(started).toBeInTheDocument();
  });

  describe("a live run's duration", () => {
    beforeEach(() => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date("2026-08-20T10:00:10Z"));
    });
    afterEach(() => {
      vi.useRealTimers();
    });

    it("ticks on its own clock without waiting on a new envelope", () => {
      render(
        <OutcomeStrip envelope={envelope({ outcome: "live", ended_at: "" })} liveState="running" />,
      );

      expect(screen.getByText("10s")).toBeInTheDocument();

      act(() => {
        vi.advanceTimersByTime(5000);
      });

      expect(screen.getByText("15s")).toBeInTheDocument();
    });

    it("never ticks once the run has ended", () => {
      render(<OutcomeStrip envelope={envelope({ outcome: "completed" })} liveState={null} />);

      expect(screen.getByText("5m 0s")).toBeInTheDocument();

      act(() => {
        vi.advanceTimersByTime(5000);
      });

      expect(screen.getByText("5m 0s")).toBeInTheDocument();
    });
  });
});

// The status table: one word and one reading per checked status, and nothing that reads a
// produced value as evidence of anything. Recorded outputs sit beside the status, never inside it.

function checked(overrides: Partial<Outcome> = {}): Outcome {
  return {
    status: "succeeded",
    summary: "",
    attempted: 3,
    succeeded: 3,
    skipped: 0,
    failed: 0,
    retry_attempts: 0,
    primary_input: 3,
    generation: 0,
    checks: [],
    artifacts: [],
    ...overrides,
  };
}

describe("ResultSummary status table", () => {
  it("says a succeeded run's result was checked", () => {
    render(<ResultSummary result={checked()} />);

    expect(screen.getByText("Succeeded")).toBeInTheDocument();
    expect(
      screen.getByText(/3 of 3 succeeded, checked against the run's own facts\./),
    ).toBeInTheDocument();
  });

  it("says no work was needed for a no-op", () => {
    render(
      <ResultSummary
        result={checked({ status: "noop", attempted: 0, succeeded: 0, primary_input: 0 })}
      />,
    );

    expect(screen.getByText("No work needed")).toBeInTheDocument();
    expect(screen.getByText(/no work was needed/)).toBeInTheDocument();
  });

  it("names the successes and the skipped and failed counts for a partial run", () => {
    render(
      <ResultSummary
        result={checked({ status: "partial", attempted: 3, succeeded: 2, skipped: 1, failed: 0 })}
      />,
    );

    expect(screen.getByText("Partly done")).toBeInTheDocument();
    expect(screen.getByText("2 of 3 succeeded, 1 skipped, 0 failed.")).toBeInTheDocument();
  });

  it("names the failure for a failed run", () => {
    render(
      <ResultSummary
        result={checked({ status: "failed", attempted: 3, succeeded: 0, skipped: 0, failed: 3 })}
      />,
    );

    expect(screen.getByText("Work failed")).toBeInTheDocument();
    expect(screen.getByText("3 of 3 failed, 0 skipped, 0 succeeded.")).toBeInTheDocument();
  });

  it("reads a failed run whose work did settle as exactly the counts it carries", () => {
    // A gate that spent its last reach fails the run with the primary work already succeeded, so a
    // reading that claims nothing succeeded would be false.
    render(
      <ResultSummary
        result={checked({
          status: "failed",
          attempted: 1,
          succeeded: 1,
          skipped: 0,
          failed: 0,
          primary_input: 1,
        })}
      />,
    );

    expect(screen.getByText("Work failed")).toBeInTheDocument();
    expect(screen.getByText("0 of 1 failed, 0 skipped, 1 succeeded.")).toBeInTheDocument();
  });

  it("says the evidence is unavailable for an unknown result", () => {
    render(
      <ResultSummary
        result={checked({ status: "unknown", attempted: 0, succeeded: 0, primary_input: null })}
      />,
    );

    expect(screen.getByText("Unchecked")).toBeInTheDocument();
    expect(screen.getByText(/Result evidence is unavailable/)).toBeInTheDocument();
  });

  it("reads a record that carries no result at all the same way it reads unknown", () => {
    render(<ResultSummary result={null} />);

    expect(screen.getByText("Unchecked")).toBeInTheDocument();
    expect(screen.getByText(/Result evidence is unavailable/)).toBeInTheDocument();
  });

  it("gives every status its own mark, so no two read alike without their words", () => {
    const tones = Object.values(RESULT_STATUS).map((status) => status.tone);

    // Nothing needed doing and nothing could be established are the pair a reader conflates, and
    // the words alone would be carrying the difference if the marks were the same.
    expect(new Set(tones).size).toBe(tones.length);
    expect(RESULT_STATUS.noop.tone).not.toBe(RESULT_STATUS.unknown.tone);
  });

  it("never calls any status useful", () => {
    for (const status of ["succeeded", "noop", "partial", "failed", "unknown"] as const) {
      const { unmount } = render(<ResultSummary result={checked({ status })} />);
      expect(screen.queryByText(/useful/i)).not.toBeInTheDocument();
      unmount();
    }
  });

  it("labels a recorded business counter as recorded output, leaving the status word alone", () => {
    render(
      <ResultSummary
        result={checked({ status: "noop", attempted: 0, succeeded: 0, primary_input: 0 })}
        recorded={[{ label: "Notes written", value: "12" }]}
      />,
    );

    expect(screen.getByText("Recorded output")).toBeInTheDocument();
    expect(screen.getByText("Notes written")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("No work needed")).toBeInTheDocument();
  });

  it("carries the run's own summary sentence under the reading", () => {
    render(<ResultSummary result={checked({ summary: "The primary work was handed nothing." })} />);

    expect(screen.getByText("The primary work was handed nothing.")).toBeInTheDocument();
  });
});
