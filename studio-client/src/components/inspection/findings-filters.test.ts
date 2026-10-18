import { describe, expect, it } from "vitest";

import type { Finding } from "@/api/inspection";
import { countLine, filterFindings } from "./findings-filters";

function finding(overrides: Partial<Finding>): Finding {
  return {
    finding_id: 1,
    lead_id: 1,
    question_id: 1,
    source: "record:harness-author-d01d1796",
    symptom: "warning_ignored",
    symptom_text: "a warning",
    explanation: "",
    found_by: "inspector",
    model: "m",
    decided_at: "2026-09-10T12:17:00+00:00",
    blames: [],
    lead_found_by: "inspector",
    ...overrides,
  };
}

describe("filterFindings", () => {
  const list = [
    finding({ finding_id: 1 }),
    finding({ finding_id: 2, symptom: "duplicate_call", found_by: "rule", question_id: null, source: "record:eval-scribe-b3c2d91e" }),
  ];

  it("narrows by symptom, run, detector and time, and All keeps everything", () => {
    const all = { symptom: "", run: "", foundBy: "", days: 0 };
    expect(filterFindings(list, all)).toHaveLength(2);
    expect(filterFindings(list, { ...all, symptom: "duplicate_call" }).map((row) => row.finding_id)).toEqual([2]);
    expect(filterFindings(list, { ...all, run: "harness-author-d01d1796" }).map((row) => row.finding_id)).toEqual([1]);
    expect(filterFindings(list, { ...all, foundBy: "rule" }).map((row) => row.finding_id)).toEqual([2]);
    expect(filterFindings(list, { ...all, days: 1 }, new Date("2026-09-12T12:00:00+00:00"))).toEqual([]);
    expect(filterFindings(list, { ...all, days: 7 }, new Date("2026-09-12T12:00:00+00:00"))).toHaveLength(2);
  });
});

describe("countLine", () => {
  it("counts the findings, the ones of the last day and the runs", () => {
    expect(countLine([finding({}), finding({ finding_id: 2, source: "record:other-1a2b3c4d" })], new Date("2026-09-10T20:00:00+00:00"))).toBe(
      "2 findings, 2 in the last day, 2 runs",
    );
    expect(countLine([finding({})], new Date("2026-09-20T20:00:00+00:00"))).toBe("1 finding, 0 in the last day, 1 run");
    expect(countLine([], new Date())).toBe("0 findings, 0 in the last day, 0 runs");
  });
});
