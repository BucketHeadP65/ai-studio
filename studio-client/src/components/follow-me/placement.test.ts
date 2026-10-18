import { describe, expect, it } from "vitest";

import {
  HEIGHT,
  PLACEMENTS,
  WIDTH,
  WINDOW_H,
  WINDOW_W,
  clamp,
  clampWindowRect,
  parseWindowRect,
  percentText,
  tokensText,
} from "./placement";

// The companion's pure vocabulary: the four placements, the size ranges every stored value is
// read back through, and the two readings the vitals row prints.

describe("placements", () => {
  it("names the four slots in switcher order", () => {
    expect(PLACEMENTS).toEqual(["left", "right", "bottom", "floating"]);
  });
});

describe("clamp", () => {
  it("holds a value inside its range from either side", () => {
    expect(clamp(500, WIDTH.min, WIDTH.max)).toBe(500);
    expect(clamp(120, WIDTH.min, WIDTH.max)).toBe(320);
    expect(clamp(4000, WIDTH.min, WIDTH.max)).toBe(640);
    expect(clamp(60, HEIGHT.min, HEIGHT.max)).toBe(240);
    expect(clamp(9000, HEIGHT.min, HEIGHT.max)).toBe(600);
  });
});

describe("parseWindowRect", () => {
  it("reads a stored rect and clamps its size into range", () => {
    expect(parseWindowRect({ x: 40, y: 60, w: 500, h: 620 })).toEqual({
      x: 40,
      y: 60,
      w: 500,
      h: 620,
    });
    expect(parseWindowRect({ x: -10, y: -4, w: 10, h: 4000 })).toEqual({
      x: 0,
      y: 0,
      w: WINDOW_W.min,
      h: WINDOW_H.max,
    });
  });

  it("rejects anything that is not a full rect of numbers", () => {
    expect(parseWindowRect(null)).toBeUndefined();
    expect(parseWindowRect("left")).toBeUndefined();
    expect(parseWindowRect({ x: 1, y: 2, w: 3 })).toBeUndefined();
    expect(parseWindowRect({ x: 1, y: 2, w: "3", h: 4 })).toBeUndefined();
  });
});

describe("clampWindowRect", () => {
  it("keeps the size inside the range and the corner off the negative side", () => {
    expect(clampWindowRect({ x: -30, y: 12, w: 900, h: 100 })).toEqual({
      x: 0,
      y: 12,
      w: WINDOW_W.max,
      h: WINDOW_H.min,
    });
  });
});

describe("tokensText", () => {
  it("prints a used and window pair at the scale each one deserves", () => {
    expect(tokensText(3200, 1_000_000)).toBe("3.2k of 1M");
    expect(tokensText(842, 200_000)).toBe("842 of 200k");
    expect(tokensText(12_000, 1_000_000)).toBe("12k of 1M");
    expect(tokensText(8000, 128_000)).toBe("8k of 128k");
    expect(tokensText(0, 128_000)).toBe("0 of 128k");
    expect(tokensText(1_500_000, 12_000_000)).toBe("1.5M of 12M");
  });
});

describe("percentText", () => {
  it("gains a decimal only under one percent", () => {
    expect(percentText(0.003, true)).toBe("0.3%");
    expect(percentText(0.0625, true)).toBe("6%");
    expect(percentText(0.874, true)).toBe("87%");
  });

  it("marks an unresolved reading as an estimate", () => {
    expect(percentText(0.0625, false)).toBe("~6%");
    expect(percentText(0.003, false)).toBe("~0.3%");
  });
});
