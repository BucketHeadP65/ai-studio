import type { PillTone } from "./state-pill";

// The one place a run's state becomes a pill: the tone it wears and the word
// it says. Every surface that shows a run (fleet rows, the history rail,
// OutcomeStrip, chat run cards, timeline bars) reads from here, so the same
// state never gets two words.

export type RunStateKey =
  | "waiting"
  | "running"
  | "paused"
  | "completed"
  | "failed"
  | "stopped"
  | "queued"
  | "held";

export interface RunStatePill {
  tone: PillTone;
  word: string;
}

export const RUN_STATE_PILL: Record<RunStateKey, RunStatePill> = {
  waiting: { tone: "waiting", word: "Waiting on you" },
  running: { tone: "running", word: "Running" },
  paused: { tone: "queued", word: "Paused" },
  completed: { tone: "completed", word: "Completed" },
  failed: { tone: "failed", word: "Failed" },
  stopped: { tone: "queued", word: "Stopped" },
  queued: { tone: "queued", word: "Queued" },
  held: { tone: "queued", word: "Held" },
};
