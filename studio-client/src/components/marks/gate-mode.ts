import type { GateMode } from "@/api/studio-types";

// The words for the one control an owner uses to decide who judges a gate this run reaches, so
// every screen that names a mode -- a launch's own choice, a live run's radio, a schedule's own
// tag, the composer's hint -- says the same thing.

export interface GateModeWords {
  label: string;
  // The word a segment shows where three must fit on one row. Contained in `label`, so the name
  // assistive tech reads still holds the text a sighted person sees.
  short: string;
  description: string;
  tag: string;
  hint: string;
}

export const GATE_MODE_WORDS: Record<GateMode, GateModeWords> = {
  wait: {
    label: "Wait for me",
    short: "Wait for me",
    description: "The run stops at a gate until you approve or reject it.",
    tag: "gates wait",
    hint: "gates wait for you",
  },
  orchestrate: {
    label: "The orchestrator decides",
    short: "Orchestrator",
    description: "The orchestrator agent decides each gate, no person approves it.",
    tag: "the orchestrator decides gates",
    hint: "the orchestrator decides gates",
  },
  auto: {
    label: "Pass automatically",
    short: "Automatically",
    description:
      "Every gate passes on its own, no person approves it. It is recorded as automatic.",
    tag: "gates pass automatically",
    hint: "gates pass automatically",
  },
};
