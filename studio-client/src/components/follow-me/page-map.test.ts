import { describe, expect, it } from "vitest";

import { idsOf, pageOf } from "./page-map";

describe("pageOf", () => {
  it.each([
    ["/", "chat"],
    ["/chat", "chat"],
    ["/workflows", "fleet"],
    ["/workflows/draft", "draft"],
    ["/workflows/library", "library"],
    ["/workflows/library/wf.digest.v3", "workflow-about"],
    ["/workflows/schedules", "schedules"],
    ["/workflows/run-123", "run-inspector"],
    ["/projects", "projects"],
    ["/projects/p-1", "project"],
    ["/knowledge-bases", "knowledge-bases"],
    ["/knowledge-bases/research", "knowledge-base-detail"],
    ["/harness", "harness"],
    ["/settings", "settings"],
    ["/nowhere", "unknown"],
  ])("%s -> %s", (path, page) => {
    expect(pageOf(path)).toBe(page);
  });
});

describe("idsOf", () => {
  it("names the run and session from the inspector url", () => {
    expect(idsOf("/workflows/run-123", new URLSearchParams("run=r-9"))).toEqual({
      workflow: "run-123",
      run: "r-9",
    });
  });
  it("names the knowledge base, the workflow, the project, and the viewed session", () => {
    expect(idsOf("/knowledge-bases/research", new URLSearchParams())).toEqual({
      knowledge_base: "research",
    });
    expect(idsOf("/workflows/library/wf.digest.v3", new URLSearchParams())).toEqual({
      workflow: "wf.digest.v3",
    });
    expect(idsOf("/projects/p-1", new URLSearchParams())).toEqual({ project: "p-1" });
    expect(idsOf("/chat", new URLSearchParams("session=s-2"))).toEqual({ session: "s-2" });
  });
});
