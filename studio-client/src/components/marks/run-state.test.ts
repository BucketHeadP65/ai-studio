import { describe, expect, it } from "vitest";

import { RUN_STATE_PILL } from "./run-state";

describe("RUN_STATE_PILL", () => {
  it("gives every run state a word and a tone", () => {
    for (const entry of Object.values(RUN_STATE_PILL)) {
      expect(entry.word.length).toBeGreaterThan(0);
      expect(entry.tone.length).toBeGreaterThan(0);
    }
  });

  it("says the words the rest of the app agrees on", () => {
    expect(RUN_STATE_PILL.waiting.word).toBe("Waiting on you");
    expect(RUN_STATE_PILL.completed.word).toBe("Completed");
    expect(RUN_STATE_PILL.stopped).toEqual({ tone: "queued", word: "Stopped" });
  });
});
