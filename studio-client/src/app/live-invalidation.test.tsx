import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { invalidateTopic, keyOf, topicOf, useLiveInvalidation } from "./live-invalidation";

// A hand-written EventSource. The hook takes its constructor as an argument, so nothing global is
// replaced: the test hands this in and drives it directly.
class FakeEventSource {
  static opened: FakeEventSource[] = [];
  readonly url: string;
  closed = false;
  private listeners = new Map<string, ((event: Event) => void)[]>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.opened.push(this);
  }

  addEventListener(type: string, listener: (event: Event) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close(): void {
    this.closed = true;
  }

  emit(type: string, event: Event): void {
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }

  change(topic: string): void {
    this.emit(
      "change",
      new MessageEvent("change", { data: JSON.stringify({ topic, at: "2026-08-26T12:00:00Z" }) }),
    );
  }
}

function wrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

function mount(bases?: readonly string[]) {
  FakeEventSource.opened = [];
  const client = new QueryClient();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const view = renderHook(
    () => useLiveInvalidation(bases, (url) => new FakeEventSource(url) as unknown as EventSource),
    {
      wrapper: wrapper(client),
    },
  );
  return { client, invalidate, view };
}

describe("keyOf", () => {
  it("splits a dotted topic into its root and the rest", () => {
    expect(keyOf("run.wf.report")).toEqual(["run", "wf.report"]);
    expect(keyOf("knowledge-base.research")).toEqual(["knowledge-base", "research"]);
  });

  it("leaves a bare topic as one element", () => {
    expect(keyOf("jobs")).toEqual(["jobs"]);
    expect(keyOf("sessions")).toEqual(["sessions"]);
    expect(keyOf("schedules")).toEqual(["schedules"]);
  });
});

describe("invalidateTopic", () => {
  it("invalidates the exact key and its root, so a collection under that root refetches too", () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    invalidateTopic(client, "run.wf.report");
    expect(invalidate.mock.calls.map((call) => call[0])).toEqual([
      { queryKey: ["run", "wf.report"] },
      { queryKey: ["run"] },
    ]);
  });

  it("invalidates a bare topic once", () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    invalidateTopic(client, "sessions");
    expect(invalidate.mock.calls.map((call) => call[0])).toEqual([{ queryKey: ["sessions"] }]);
  });
});

describe("useLiveInvalidation", () => {
  it("opens one stream per base, exactly the URL it was given", () => {
    mount(["http://a.test/api/events", "http://b.test/api/events"]);
    expect(FakeEventSource.opened.map((source) => source.url)).toEqual([
      "http://a.test/api/events",
      "http://b.test/api/events",
    ]);
  });

  it("filters each default backend to its own topics, so neither doubles the other's", () => {
    mount();
    expect(FakeEventSource.opened.map((source) => source.url)).toEqual([
      "http://api.test/api/events?topics=jobs,knowledge-base",
      "http://studio.test/api/events?topics=run,session,sessions,schedules",
    ]);
  });

  it("turns a change frame into an invalidation", () => {
    const { invalidate } = mount(["http://a.test"]);
    invalidate.mockClear();
    FakeEventSource.opened[0].change("knowledge-base.research");
    expect(invalidate.mock.calls.map((call) => call[0])).toEqual([
      { queryKey: ["knowledge-base", "research"] },
      { queryKey: ["knowledge-base"] },
    ]);
  });

  it("ignores a frame that is not a change event", () => {
    const { invalidate } = mount(["http://a.test"]);
    invalidate.mockClear();
    FakeEventSource.opened[0].emit("change", new MessageEvent("change", { data: "{}" }));
    expect(invalidate).not.toHaveBeenCalled();
  });

  it("survives a frame that is not JSON at all", () => {
    const { invalidate } = mount(["http://a.test"]);
    invalidate.mockClear();
    FakeEventSource.opened[0].emit("change", new MessageEvent("change", { data: "not json" }));
    expect(invalidate).not.toHaveBeenCalled();
    expect(topicOf("not json")).toBeNull();
    expect(topicOf('{"topic":"jobs","at":"2026-08-26T12:00:00Z"}')).toBe("jobs");
  });

  it("invalidates everything once when a stream reopens after a drop", () => {
    const { invalidate } = mount(["http://a.test"]);
    const source = FakeEventSource.opened[0];
    source.emit("open", new Event("open"));
    invalidate.mockClear();
    source.emit("open", new Event("open"));
    expect(invalidate.mock.calls.map((call) => call[0])).toEqual([undefined]);
  });

  it("closes every stream on unmount", () => {
    const { view } = mount(["http://a.test", "http://b.test"]);
    view.unmount();
    expect(FakeEventSource.opened.map((source) => source.closed)).toEqual([true, true]);
  });
});
