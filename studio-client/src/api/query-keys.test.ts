import { describe, expect, it } from "vitest";

import { keyOf } from "@/app/live-invalidation";

import { knowledgeBaseKeys, runKeys, scheduleKeys, sessionKeys, spendKeys, stepKeys } from "./query-keys";

// Every key a topic must reach begins with that topic's root, because react-query matches by
// prefix. These assertions are the contract between the backends' topic names and the SPA's keys.
describe("query keys sit under their topic root", () => {
  it("run keys sit under run", () => {
    for (const key of [
      runKeys.live,
      runKeys.recent,
      runKeys.recentAtMost(6),
      runKeys.history("wf.report.v1"),
      runKeys.state("wf.report.v1", "s-1"),
      runKeys.timeline("wf.report.v1", "s-1"),
      runKeys.values("wf.report.v1", "s-1"),
    ]) {
      expect(key[0]).toBe("run");
    }
  });

  it("a run topic reaches every key for that workflow", () => {
    const [root, rest] = keyOf("run.wf.report.v1");
    expect(runKeys.state("wf.report.v1", "s-1").slice(0, 2)).toEqual([root, rest]);
    expect(runKeys.history("wf.report.v1").slice(0, 2)).toEqual([root, rest]);
  });

  it("a sized recent key still sits under the plain recent one, so invalidating it reaches both", () => {
    expect(runKeys.recentAtMost(6).slice(0, 2)).toEqual([...runKeys.recent]);
  });

  it("session collection keys sit under sessions and per-session keys under session", () => {
    expect(sessionKeys.list(false, "").slice(0, 2)).toEqual(["sessions", "list"]);
    expect(sessionKeys.projects[0]).toBe("sessions");
    expect(sessionKeys.detail("s-1")[0]).toBe("session");
    expect(sessionKeys.transcript("s-1")[0]).toBe("session");
    expect(sessionKeys.conversation("s-1")[0]).toBe("session");
  });

  it("a session topic reaches only that session", () => {
    expect(sessionKeys.detail("s-1").slice(0, 2)).toEqual(keyOf("session.s-1"));
    expect(sessionKeys.detail("s-2").slice(0, 2)).not.toEqual(keyOf("session.s-1"));
  });

  it("schedule and knowledge base keys sit under their own roots", () => {
    expect(scheduleKeys.all).toEqual(keyOf("schedules"));
    expect(knowledgeBaseKeys.all[0]).toBe("knowledge-base");
    expect(knowledgeBaseKeys.detail("research").slice(0, 2)).toEqual(keyOf("knowledge-base.research"));
    expect(knowledgeBaseKeys.schema("research")[0]).toBe("knowledge-base");
    expect(knowledgeBaseKeys.refreshRuns("research").slice(0, 2)).toEqual(keyOf("knowledge-base.research"));
  });

  it("the spend key sits under its own root, outside run, so no run frame ever refetches it", () => {
    expect(spendKeys.run("run-1")[0]).toBe("spend");
    expect(spendKeys.run("run-1")[0]).not.toBe("run");
  });

  it("step lines sit under run, so any run topic's root invalidation reaches them", () => {
    expect(stepKeys.lines("s-1")[0]).toBe("run");
    const [root] = keyOf("run.wf.report.v1");
    expect(stepKeys.lines("s-1")[0]).toBe(root);
  });
});
