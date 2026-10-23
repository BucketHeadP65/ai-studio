import { describe, expect, it } from "vitest";

import { cn } from "./cn";

describe("cn", () => {
  it("merges conflicting tailwind classes with last-wins", () => {
    expect(cn("p-2", "p-4")).toBe("p-4");
  });

  it("filters falsy values", () => {
    expect(cn("text-sm", false && "hidden", null, undefined, "font-bold")).toBe(
      "text-sm font-bold",
    );
  });

  it("treats the app's type-scale steps as sizes, so a text color survives beside them", () => {
    expect(cn("text-surface", "text-caption")).toBe("text-surface text-caption");
    expect(cn("bg-label-1 text-surface", "text-micro")).toBe("bg-label-1 text-surface text-micro");
  });

  it("still lets a type-scale step replace another size", () => {
    expect(cn("text-sm", "text-caption")).toBe("text-caption");
    expect(cn("text-caption", "text-body")).toBe("text-body");
  });
});
