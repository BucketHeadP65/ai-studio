import { Ban, Terminal } from "lucide-react";
import { describe, expect, it } from "vitest";

import { partIcon } from "./icons";

describe("partIcon", () => {
  it("marks a retried return apart from a tool's return", () => {
    expect(partIcon("retried-return")).toBe(Ban);
    expect(partIcon("tool-return")).toBe(Terminal);
  });
});
