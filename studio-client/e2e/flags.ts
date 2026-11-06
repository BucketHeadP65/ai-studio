import type { MaintenanceFlag } from "@/api/studio-types";

// One maintenance flag as the harness's flag routes answer it: an open diverged copy of one step
// in one workflow, with its evidence and no proposed change. A caller names the fields its page
// shows.
export function maintenanceFlag(overrides: Partial<MaintenanceFlag> = {}): MaintenanceFlag {
  return {
    id: "mf.diverged.weekly-sweep.compose",
    kind: "diverged_copy",
    status: "open",
    step: "compose",
    workflow: "wf.weekly-sweep.v2",
    title: "Diverged copy of compose in weekly-sweep",
    summary:
      "Decide whether the copy of compose in wf.weekly-sweep.v2 stays as it is, or takes compose v3.",
    raised_at: "2026-11-05T11:00:00Z",
    evidence:
      "wf.weekly-sweep.v2 holds a copy of compose that differs from st.compose.v3.\n\n```diff\n-Write three lines.\n+Write five lines.\n```",
    proposed_change: "",
    ...overrides,
  };
}
