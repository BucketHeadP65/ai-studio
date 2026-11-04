import { describe, expect, it } from "vitest";

import { recordedValue } from "./recorded-value";

const values = {
  write_stances: '{\n  "written": ["stance.x"],\n  "failed": []\n}',
  report: '{"written": 12, "skipped": 0, "review": {"decision": "keep"}}',
  "letters.draft_answer": '"Dear reader"',
  group_pages: "[PageGroup(pages=[])]",
};

describe("recordedValue", () => {
  it("reads a node's whole value under values.<node>", () => {
    expect(recordedValue(values, "values.write_stances")).toBe(values.write_stances);
  });

  it("walks a field inside a node's value", () => {
    expect(recordedValue(values, "values.report.written")).toBe("12");
  });

  it("walks a nested field and shows an object it reaches as JSON", () => {
    expect(recordedValue(values, "values.report.review")).toBe('{\n  "decision": "keep"\n}');
    expect(recordedValue(values, "values.report.review.decision")).toBe("keep");
  });

  it("reads a turn's own value, whose key holds a dot", () => {
    expect(recordedValue(values, "values.letters.draft_answer")).toBe('"Dear reader"');
  });

  it("shows a zero it reaches rather than calling it not recorded", () => {
    expect(recordedValue(values, "values.report.skipped")).toBe("0");
  });

  it("says not recorded for a node the run never reached", () => {
    expect(recordedValue(values, "values.write_flags")).toBe("not recorded");
  });

  it("says not recorded for a field the value does not hold", () => {
    expect(recordedValue(values, "values.report.missing")).toBe("not recorded");
  });

  it("says not recorded for a field inside a value that is not JSON", () => {
    expect(recordedValue(values, "values.group_pages.pages")).toBe("not recorded");
  });

  it("says not recorded with no values at all", () => {
    expect(recordedValue(null, "values.write_stances")).toBe("not recorded");
  });
});
