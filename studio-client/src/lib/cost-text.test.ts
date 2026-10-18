import { describe, expect, it } from "vitest";

import { costText, spendText } from "./cost-text";

// The one reading every screen that shows a priced cost shares: four decimals under a dollar,
// so a cheap call still reads as more than nothing, two decimals at or above one, and a bare
// "$0" for a cost that is exactly zero rather than four trailing zeros. Rounding works on the
// wire string's own digits, never on a binary float, so a decimal that lands exactly on a tie
// still rounds the way the digits themselves say it should.

describe("costText", () => {
  it("reads a cost under a dollar with four decimals", () => {
    expect(costText("0.004235")).toBe("$0.0042");
  });

  it("reads a cost at or above a dollar with two decimals", () => {
    expect(costText("1.5")).toBe("$1.50");
  });

  it("reads a zero cost as a bare dollar sign, not four trailing zeros", () => {
    expect(costText("0")).toBe("$0");
  });

  it("reads an empty or unparseable reading the same as no cost at all", () => {
    expect(costText("")).toBe("$0");
    expect(costText("N/A")).toBe("$0");
  });

  it("rounds a tie up to the cent even where a binary float would round it down", () => {
    expect(costText("1.005")).toBe("$1.01");
  });

  it("carries a rounded-up tie into the whole part at two decimals", () => {
    expect(costText("9.995")).toBe("$10.00");
  });

  it("carries a rounded-up tie into the whole part at four decimals", () => {
    expect(costText("0.99999")).toBe("$1.0000");
  });

  it("reads a price the backend printed in scientific notation, which a real small sum is", () => {
    // Python's Decimal prints "7.5E-8" for a few hundred tokens on a cheap model. It is a price,
    // and rendering it as $0 says the call was free.
    expect(costText("7.5E-8")).toBe("$0.0000");
    expect(costText("1.50E-7")).toBe("$0.0000");
    expect(costText("2.5E-3")).toBe("$0.0025");
    expect(costText("1E+2")).toBe("$100.00");
  });

  it("still reads an unparseable figure as no cost at all", () => {
    expect(costText("1E")).toBe("$0");
    expect(costText("E-7")).toBe("$0");
  });
});

describe("spendText", () => {
  it("reads as an ordinary cost when every response had a price", () => {
    expect(spendText("0.0042", 0)).toBe("$0.0042");
    expect(spendText("0", 0)).toBe("$0");
  });

  it("says unknown, never $0, when nothing in the reading could be priced", () => {
    // A model with no published price and no billed figure is UNKNOWN. Showing $0 here says the
    // work was free, which is the one thing it certainly was not.
    expect(spendText("0", 3)).toBe("unknown");
  });

  it("says what is known as a floor when some responses priced and others did not", () => {
    expect(spendText("0.0042", 1)).toBe("at least $0.0042");
  });
});
