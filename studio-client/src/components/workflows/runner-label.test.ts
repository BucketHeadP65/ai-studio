import { describe, expect, it } from "vitest";

import { runnerActor, runnerLabel, startedBySentence } from "./runner-label";

describe("runnerLabel", () => {
  it("names the route a run came through", () => {
    expect(runnerLabel("app:schedule:ingest-daily")).toBe("schedule ingest-daily");
    expect(runnerLabel("app:chat")).toBe("chat");
    expect(runnerLabel("app:harness")).toBe("a person at the controls");
  });
});

describe("runnerActor", () => {
  it("reads a schedule runner as schedule-started, and any other runner as person-started", () => {
    expect(runnerActor("app:schedule:ingest-daily")).toBe("schedule-started");
    expect(runnerActor("app:chat")).toBe("person-started");
    expect(runnerActor("app:harness")).toBe("person-started");
  });
});

describe("startedBySentence", () => {
  it("says who started the run as a sentence", () => {
    expect(startedBySentence("app:schedule:ingest-daily")).toBe(
      "Started by a schedule, ingest-daily.",
    );
    expect(startedBySentence("app:chat")).toBe("Started from chat.");
    expect(startedBySentence("app:harness")).toBe("Started by a person.");
  });
});
