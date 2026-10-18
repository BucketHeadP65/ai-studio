import { describe, expect, it } from "vitest";

import { PAGE_CONTEXT_CLOSE, PAGE_CONTEXT_OPEN, stripPageContext } from "./page-context";

describe("stripPageContext", () => {
  it("removes a leading fence and keeps the person's words", () => {
    const text = `${PAGE_CONTEXT_OPEN}\nThe person is on chat at /chat.\n${PAGE_CONTEXT_CLOSE}\n\nwhat failed?`;
    expect(stripPageContext(text)).toBe("what failed?");
  });
  it("leaves plain text alone", () => {
    expect(stripPageContext("plain words")).toBe("plain words");
  });
  it("leaves an unclosed fence alone", () => {
    expect(stripPageContext(`${PAGE_CONTEXT_OPEN} never closed`)).toBe(
      `${PAGE_CONTEXT_OPEN} never closed`,
    );
  });
});
