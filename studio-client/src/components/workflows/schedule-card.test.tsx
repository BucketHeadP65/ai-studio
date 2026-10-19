import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Schedule } from "@/api/schedules";
import { render } from "@/test-support/render";

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: routerPush }),
}));

// Imported after the mock so ScheduleCard sees the mocked module.
import { ScheduleCard, cadenceWords, formatNextRun } from "./schedule-card";

// One card's own acts (switch, run now, open the run, delete) and the two pure formatters it
// leans on: cadence text in words, and a next-run instant in words (formatRelative's
// future-facing sibling, since that helper only ever reads a past instant correctly). The whole
// card opens its workflow; a click on one of the card's own acts never navigates.

afterEach(() => {
  routerPush.mockClear();
});

function scheduleFixture(overrides: Partial<Schedule> = {}): Schedule {
  return {
    id: "sched-1",
    workflow_id: "wf-sales",
    arguments: { quarter: "Q1" },
    cadence: "every 30m",
    enabled: true,
    gate_policy: "wait",
    gate_mode: "wait",
    capacity_key: null,
    capacity_limit: null,
    blackout: null,
    catch_up: false,
    session_id: "",
    created_at: "2026-08-23T10:00:00+00:00",
    last_run_at: null,
    last_outcome: "",
    next_run_at: "2026-08-23T10:30:00+00:00",
    live: null,
    paused_at: null,
    spend_today_usd: "0",
    spend_week_usd: "0",
    ...overrides,
  };
}

function renderCard(overrides: Partial<Schedule> = {}, now?: number) {
  const onToggleEnabled = vi.fn();
  const onRunNow = vi.fn();
  const onDelete = vi.fn();
  render(
    <ul>
      <ScheduleCard
        schedule={scheduleFixture(overrides)}
        name="Sales"
        togglingEnabled={false}
        runningNow={false}
        onToggleEnabled={onToggleEnabled}
        onRunNow={onRunNow}
        onDelete={onDelete}
        now={now}
      />
    </ul>,
  );
  return { onToggleEnabled, onRunNow, onDelete };
}

describe("cadenceWords", () => {
  it("renders a singular interval as one unit", () => {
    expect(cadenceWords("every 1h")).toBe("every 1 hour");
  });

  it("renders a plural interval with the unit pluralized", () => {
    expect(cadenceWords("every 30m")).toBe("every 30 minutes");
  });

  it("names a cron expression as its own schedule, not pretending to translate it", () => {
    expect(cadenceWords("0 9 * * 1")).toBe("on cron schedule 0 9 * * 1");
  });
});

describe("formatNextRun", () => {
  const now = new Date("2026-08-23T10:00:00Z").getTime();

  it("reads a future instant as in words", () => {
    expect(formatNextRun("2026-08-23T10:05:00Z", now)).toBe("in 5 minutes");
  });

  it("reads a past-due instant as due now", () => {
    expect(formatNextRun("2026-08-23T09:55:00Z", now)).toBe("due now");
  });

  it("reads a null next run as not scheduled", () => {
    expect(formatNextRun(null, now)).toBe("not scheduled");
  });
});

describe("ScheduleCard, an active schedule", () => {
  it("shows the resolved name, the workflow id tag, the cadence tag, and the next run sentence", () => {
    renderCard();

    expect(screen.getByRole("link", { name: "Sales" })).toHaveAttribute(
      "href",
      "/workflows/wf-sales",
    );
    expect(screen.getByText("wf-sales")).toBeInTheDocument();
    expect(screen.getByText("every 30m")).toBeInTheDocument();
    expect(screen.getByText(/Next run/)).toBeInTheDocument();
  });

  it("opens the workflow when the card body is clicked, not only its name", async () => {
    renderCard();

    await userEvent.click(screen.getByText("every 30m"));
    expect(routerPush).toHaveBeenCalledWith("/workflows/wf-sales");
  });

  it("shows what this schedule has spent today and this week, after the next run line", () => {
    renderCard({ spend_today_usd: "0.0042", spend_week_usd: "1.50" });

    expect(screen.getByText("Spent $0.0042 today, $1.50 this week.")).toBeInTheDocument();
  });

  it("reads nothing spent this week when both readings are zero", () => {
    renderCard({ spend_today_usd: "0", spend_week_usd: "0" });

    expect(screen.getByText("Nothing spent this week.")).toBeInTheDocument();
  });

  it("says unknown, never nothing spent, for a schedule whose runs no source could price", () => {
    // Twenty fires on a model with no published price. Both readings are zero because zero is the
    // floor of what is known, and "Nothing spent this week." would be a plain lie about the cost.
    renderCard({
      spend_today_usd: "0",
      spend_week_usd: "0",
      spend_today_unpriced: 4,
      spend_week_unpriced: 20,
    });

    expect(screen.queryByText("Nothing spent this week.")).not.toBeInTheDocument();
    expect(screen.getByText("Spent unknown today, unknown this week.")).toBeInTheDocument();
  });

  it("says what is known as a floor when some of a schedule's runs priced and others did not", () => {
    renderCard({ spend_today_usd: "0.0042", spend_week_usd: "1.50", spend_week_unpriced: 3 });

    expect(screen.getByText("Spent $0.0042 today, at least $1.50 this week.")).toBeInTheDocument();
  });

  it.each([
    [{ live: "running" }, "Running now"],
    [{ live: "waiting" }, "Waiting on you"],
    [{ live: "paused" }, "Paused"],
    [{ live: null, last_outcome: "failed, boom" }, "Last run failed"],
    [{ live: null, last_outcome: "", last_run_at: null }, "Never fired"],
    [{ live: null, last_outcome: "launched", last_run_at: "2026-08-23T09:00:00Z" }, "Caught up"],
  ] as const)("renders the %s pill as %s", (overrides, word) => {
    renderCard(overrides);
    expect(screen.getByText(word)).toBeInTheDocument();
  });

  it("draws the pool tag with its limit once both are set", () => {
    renderCard({ capacity_key: "gpu-pool", capacity_limit: 3 });
    expect(screen.getByText("pool gpu-pool limit 3")).toBeInTheDocument();
  });

  it("draws the pool tag without a limit when only the key is set", () => {
    renderCard({ capacity_key: "gpu-pool", capacity_limit: null });
    expect(screen.getByText("pool gpu-pool")).toBeInTheDocument();
  });

  it("omits the pool tag when no capacity key is set", () => {
    renderCard();
    expect(screen.queryByText(/^pool/)).toBeNull();
  });

  it("draws the quiet tag only when a blackout window is set", () => {
    renderCard({ blackout: "22:00-06:00" });
    expect(screen.getByText("quiet 22:00-06:00")).toBeInTheDocument();
  });

  it("always draws the catch up and gate mode tags", () => {
    renderCard({ catch_up: true, gate_mode: "orchestrate" });
    expect(screen.getByText("catches up")).toBeInTheDocument();
    expect(screen.getByText("the orchestrator decides gates")).toBeInTheDocument();
  });

  it("names skipping a missed fire when catch up is off", () => {
    renderCard({ catch_up: false });
    expect(screen.getByText("skips missed")).toBeInTheDocument();
  });

  it.each([
    ["wait", "gates wait"],
    ["auto", "gates pass automatically"],
  ] as const)("names the %s gate mode as %s", (gateMode, word) => {
    renderCard({ gate_mode: gateMode });
    expect(screen.getByText(word)).toBeInTheDocument();
  });

  it("shows a Run now button and fires it without navigating when nothing is live", async () => {
    const { onRunNow } = renderCard({ live: null });

    const runNow = screen.getByRole("button", { name: "Run now" });
    await userEvent.click(runNow);
    expect(onRunNow).toHaveBeenCalledTimes(1);
    expect(routerPush).not.toHaveBeenCalled();
  });

  it("shows Open the run instead of Run now while a fire is live, linking straight to the workflow", () => {
    renderCard({ live: "running" });

    expect(screen.queryByRole("button", { name: "Run now" })).toBeNull();
    expect(screen.getByRole("link", { name: "Open the run" })).toHaveAttribute(
      "href",
      "/workflows/wf-sales",
    );
  });

  it.each([
    [{ last_run_at: null }, "Never run."],
    [
      { last_run_at: "2026-08-23T09:00:00Z", last_outcome: "failed, boom" },
      "Last run failed 1 hour ago.",
    ],
    [
      { last_run_at: "2026-08-23T09:00:00Z", last_outcome: "launched" },
      "Last run completed 1 hour ago.",
    ],
  ] as const)("shows the last run sentence %s", (overrides, expected) => {
    renderCard(overrides, new Date("2026-08-23T10:00:00Z").getTime());
    expect(screen.getByText(expected)).toBeInTheDocument();
  });

  it("flips its switch and names it by its current state, without navigating", async () => {
    const { onToggleEnabled } = renderCard({ enabled: true });

    const switchButton = screen.getByRole("switch", { name: "Schedule on" });
    expect(switchButton).toHaveAttribute("aria-checked", "true");
    await userEvent.click(switchButton);
    expect(onToggleEnabled).toHaveBeenCalledTimes(1);
    expect(routerPush).not.toHaveBeenCalled();
  });

  it("asks to delete without navigating away", async () => {
    const { onDelete } = renderCard();

    const deleteButton = screen.getByRole("button", { name: "Delete schedule" });
    await userEvent.click(deleteButton);
    expect(onDelete).toHaveBeenCalledTimes(1);
    expect(routerPush).not.toHaveBeenCalled();
  });
});

describe("ScheduleCard, a paused schedule", () => {
  it("renders the switch off and the paused pill", () => {
    renderCard({ enabled: false });

    const switchButton = screen.getByRole("switch", { name: "Schedule off" });
    expect(switchButton).toHaveAttribute("aria-checked", "false");
    expect(screen.getByText("Paused")).toBeInTheDocument();
  });

  it("names the sentence with the cadence and the relative pause time when paused_at is set", () => {
    renderCard(
      { enabled: false, paused_at: "2026-08-23T09:55:00Z" },
      new Date("2026-08-23T10:00:00Z").getTime(),
    );

    expect(
      screen.getByText(
        "Every 30 minutes, paused by a person, 5 minutes ago. It will not fire while it is paused.",
      ),
    ).toBeInTheDocument();
  });

  it("drops the relative clause when paused_at is null", () => {
    renderCard({ enabled: false, paused_at: null });

    expect(
      screen.getByText(
        "Every 30 minutes, paused by a person. It will not fire while it is paused.",
      ),
    ).toBeInTheDocument();
  });

  it("shows no Run now button and no Open the run link", () => {
    renderCard({ enabled: false });

    expect(screen.queryByRole("button", { name: "Run now" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Open the run" })).toBeNull();
  });

  it("still flips its switch and deletes without navigating", async () => {
    const { onToggleEnabled, onDelete } = renderCard({ enabled: false });

    await userEvent.click(screen.getByRole("switch", { name: "Schedule off" }));
    expect(onToggleEnabled).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByRole("button", { name: "Delete schedule" }));
    expect(onDelete).toHaveBeenCalledTimes(1);
    expect(routerPush).not.toHaveBeenCalled();
  });

  it("opens the workflow when the card body is clicked", async () => {
    renderCard({ enabled: false });

    await userEvent.click(screen.getByText(/paused by a person/));
    expect(routerPush).toHaveBeenCalledWith("/workflows/wf-sales");
  });
});
