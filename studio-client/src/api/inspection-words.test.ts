import { describe, expect, it } from "vitest";

import {
  citesOf,
  duration,
  keyLine,
  longDate,
  partWords,
  plural,
  runLocator,
  runName,
  sentence,
  shortDate,
  spaced,
  spanStep,
  surfaceLabel,
} from "./inspection-words";

describe("inspection words", () => {
  it("reads a source's locator and splits a run id into its agent and short id", () => {
    expect(runLocator("record:harness-author-d01d1796")).toBe("harness-author-d01d1796");
    expect(runName("harness-author-d01d1796")).toEqual({ agent: "harness-author", short: "d01d1796" });
    expect(runName("inspector-a1b2")).toEqual({ agent: "inspector-a1b2", short: "" });
    expect(runName("plain")).toEqual({ agent: "plain", short: "" });
  });

  it("reads a run id the transcript derives, its agent and run id encoded after `sp-`", () => {
    const derived = (agent: string, run: string): string =>
      `sp-${Buffer.from(`${Buffer.byteLength(agent)}:${agent}${run}`).toString("base64url")}`;
    const run = "01a0ed63-79ef-72fb-ab66-676f410e1fd1";
    expect(runName(derived("harness-author", run))).toEqual({
      agent: "harness-author",
      short: "410e1fd1",
    });
    expect(runName(derived("réviseur", run))).toEqual({ agent: "réviseur", short: "410e1fd1" });
    expect(runName("sp-aGVsbG8")).toEqual({ agent: "sp-aGVsbG8", short: "" });
    expect(runName("sp-not*base64")).toEqual({ agent: "sp-not*base64", short: "" });
  });

  it("spaces a taxonomy name and starts a sentence", () => {
    expect(spaced("warning_ignored")).toBe("warning ignored");
    expect(sentence("warning ignored")).toBe("Warning ignored");
    expect(surfaceLabel("system_prompt")).toBe("system prompt");
    expect(surfaceLabel("model_tier")).toBe("model tier");
  });

  it("formats a date short and long, in the reader's own time zone", () => {
    expect(shortDate("2026-09-10T12:17:00+00:00")).toMatch(/^\d{1,2} [A-Z][a-z]{2} \d{2}:\d{2}$/);
    expect(longDate("2026-09-10T12:17:00+00:00")).toMatch(/^\d{1,2} [A-Z][a-z]{2} 2026 \d{2}:\d{2}$/);
  });

  it("says how long a run took in the largest two units", () => {
    expect(duration("2026-09-10T12:02:00+00:00", "2026-09-10T12:06:12+00:00")).toBe("4 min 12 s");
    expect(duration("2026-09-10T12:02:00+00:00", "2026-09-10T12:02:48+00:00")).toBe("48 s");
    expect(duration("2026-09-10T12:02:00+00:00", "2026-09-10T13:05:00+00:00")).toBe("1 h 3 min");
    expect(duration("2026-09-10T12:02:00+00:00", null)).toBe("");
  });

  it("reads the spans a why cites, and every span when it names none", () => {
    const spans = ["5.8.0", "5.9.0", "5.9.2"];
    expect(citesOf("fetch_workflow_spec at 5.8.0 returned fit, but the next thinking (5.9.0) quotes half", spans)).toEqual(["5.8.0", "5.9.0"]);
    expect(citesOf("the fit field is one blob", spans)).toEqual(spans);
    expect(citesOf("it cites 9.9.9 which is not here", spans)).toEqual(spans);
  });

  it("reads the words after step N, in a part's own label", () => {
    expect(partWords("step 5, the return of fetch_workflow_spec")).toBe("the return of fetch_workflow_spec");
  });

  it("reads a span's own step, 0 rather than NaN when it has no leading number", () => {
    expect(spanStep("5.8.0")).toBe(5);
    expect(spanStep("not-a-span")).toBe(0);
  });

  it("counts singular at exactly one, plural otherwise", () => {
    expect(plural(1, "message", "messages")).toBe("1 message");
    expect(plural(12, "message", "messages")).toBe("12 messages");
    expect(plural(0, "message", "messages")).toBe("0 messages");
  });

  it("picks the line that quotes a blame's phrase, else the first line with text", () => {
    const text = "output:\n  summary: looked fine\n  fit: re-check before trusting it\n  fits_here: true";
    expect(keyLine(text, ["re-check before trusting it"])).toBe("  fit: re-check before trusting it");
    expect(keyLine("\n\nfirst words\nmore", [])).toBe("first words");
  });
});
