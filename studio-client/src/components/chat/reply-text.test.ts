import { describe, expect, it } from "vitest";

import { readableToolText } from "./reply-text";

describe("readableToolText", () => {
  it("decodes embedded JSON string escapes for display", () => {
    const spilled =
      '[Tool output too large.] {"instructions":"# Skill\\n\\nUse this after \\"review\\"."}';
    expect(readableToolText(spilled)).toBe(
      '[Tool output too large.] {"instructions":"# Skill\n\nUse this after "review"."}',
    );
  });

  it("leaves text without escape pairs untouched", () => {
    expect(readableToolText("plain text, real\nnewline")).toBe("plain text, real\nnewline");
  });

  it("keeps unknown escapes as written instead of guessing", () => {
    expect(readableToolText("a \\q b \\n c")).toBe("a \\q b \n c");
  });
});
