import { describe, expect, it } from "vitest";

import { GATE_MODE_WORDS } from "./gate-mode";

describe("GATE_MODE_WORDS", () => {
  it("gives every mode a label, a description, a tag, and a hint", () => {
    for (const words of Object.values(GATE_MODE_WORDS)) {
      expect(words.label.length).toBeGreaterThan(0);
      expect(words.description.length).toBeGreaterThan(0);
      expect(words.tag.length).toBeGreaterThan(0);
      expect(words.hint.length).toBeGreaterThan(0);
    }
  });

  it("gives every mode a short word a segment can show, contained in its label", () => {
    for (const words of Object.values(GATE_MODE_WORDS)) {
      expect(words.short.length).toBeGreaterThan(0);
      expect(words.label.toLowerCase()).toContain(words.short.toLowerCase());
    }
  });

  it("hints say who the gates wait for, and otherwise read as the tag", () => {
    expect(GATE_MODE_WORDS.wait.hint).toBe("gates wait for you");
    expect(GATE_MODE_WORDS.orchestrate.hint).toBe(GATE_MODE_WORDS.orchestrate.tag);
    expect(GATE_MODE_WORDS.auto.hint).toBe(GATE_MODE_WORDS.auto.tag);
  });

  it("says the orchestrator, not the agent, wherever a mode is named", () => {
    expect(GATE_MODE_WORDS.orchestrate.label).toBe("The orchestrator decides");
    expect(GATE_MODE_WORDS.orchestrate.tag).toBe("the orchestrator decides gates");
  });

  it("uses only the house's allowed punctuation in every word", () => {
    const ALLOWED_PUNCTUATION = /^[\w\s,.?()]*$/;
    for (const words of Object.values(GATE_MODE_WORDS)) {
      expect(words.label).toMatch(ALLOWED_PUNCTUATION);
      expect(words.description).toMatch(ALLOWED_PUNCTUATION);
      expect(words.tag).toMatch(ALLOWED_PUNCTUATION);
    }
  });
});
