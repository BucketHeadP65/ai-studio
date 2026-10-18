import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { formatElapsedMs, useNowTick } from "./elapsed";

describe("formatElapsedMs", () => {
  it("reads seconds alone under a minute", () => {
    expect(formatElapsedMs(45_000)).toBe("45s");
  });

  it("reads minutes and seconds under an hour", () => {
    expect(formatElapsedMs(125_000)).toBe("2m 5s");
  });

  it("reads hours and minutes past an hour", () => {
    expect(formatElapsedMs(3_725_000)).toBe("1h 2m");
  });

  it("clamps a negative input to zero rather than going blank", () => {
    expect(formatElapsedMs(-1)).toBe("0s");
  });

  it("reads empty for a non-finite input", () => {
    expect(formatElapsedMs(Number.NaN)).toBe("");
  });
});

describe("useNowTick", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("advances once per interval", () => {
    const { result } = renderHook(() => useNowTick(1000));
    const first = result.current;

    act(() => {
      vi.advanceTimersByTime(1000);
    });

    expect(result.current).toBeGreaterThan(first);
  });

  it("never advances while inactive", () => {
    const { result } = renderHook(() => useNowTick(1000, false));
    const first = result.current;

    act(() => {
      vi.advanceTimersByTime(5000);
    });

    expect(result.current).toBe(first);
  });
});
