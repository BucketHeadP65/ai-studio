"use client";

import { useStoredFlag } from "./use-stored";

// The chat's per-browser preferences, each one stored key. Whether a settled turn's earlier
// steps fold away under "N earlier steps" or stay in view; they always stay in view while the
// turn is live.
export function useFoldSettledSteps(): [boolean, (next: boolean) => void] {
  return useStoredFlag("chat.fold-settled-steps", false);
}
