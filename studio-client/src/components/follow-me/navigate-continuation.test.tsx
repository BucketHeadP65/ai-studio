import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type { ReactElement, ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useSessionChat } from "@/components/chat/use-session-chat";
import { server } from "@/shared/test-support/msw/server";

// The companion's navigate verb against a router that moves the page a tick after the push,
// the way Next's App Router does when the route it is sent to was never prefetched. The whole
// point of the file is the ordering: what the continuation reports about the page has to be
// the page the verb reached.

const route = vi.hoisted(() => {
  const listeners = new Set<() => void>();
  const state = {
    pathname: "/chat",
    // Whether a push ever lands. False models a route that never arrives.
    lands: true,
    read: (): string => state.pathname,
    subscribe: (listener: () => void): (() => void) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    push: (url: string): void => {
      if (!state.lands) return;
      setTimeout(() => {
        state.pathname = url.split("?")[0] ?? "";
        for (const listener of [...listeners]) listener();
      }, 0);
    },
  };
  return state;
});

const SEARCH = new URLSearchParams();

vi.mock("next/navigation", async () => {
  const { useSyncExternalStore } = await import("react");
  return {
    usePathname: (): string => useSyncExternalStore(route.subscribe, route.read, route.read),
    useSearchParams: (): URLSearchParams => SEARCH,
    useRouter: (): { push: (url: string) => void } => ({ push: route.push }),
  };
});

// Imported after the mock so the hooks under test see the mocked module.
import { usePathname } from "next/navigation";

import { FollowMeProvider, useFollowMe } from "./provider";
import { useTargetRegistration } from "./targets";
import { NAVIGATION_WAIT_MS, useVerbExecutor } from "./verbs";

const BASE = "http://studio.test";

type StreamChunk = Record<string, unknown>;

function streamResponse(chunks: StreamChunk[]) {
  const lines = chunks.map((chunk) => `data: ${JSON.stringify(chunk)}\n\n`).join("");
  return new HttpResponse(`${lines}data: [DONE]\n\n`, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

const NAVIGATE: StreamChunk[] = [
  { type: "start" },
  { type: "start-step" },
  { type: "tool-input-start", toolCallId: "c-1", toolName: "navigate" },
  {
    type: "tool-input-available",
    toolCallId: "c-1",
    toolName: "navigate",
    input: { url: "/knowledge-bases/research" },
  },
  { type: "finish-step" },
  { type: "finish" },
];

const SETTLED: StreamChunk[] = [
  { type: "start" },
  { type: "start-step" },
  { type: "text-start", id: "t1" },
  { type: "text-delta", id: "t1", delta: "here it is" },
  { type: "text-end", id: "t1" },
  { type: "finish-step" },
  { type: "finish" },
];

function recordStreams() {
  const bodies: unknown[] = [];
  let calls = 0;
  server.use(
    // Every send knocks on the steer endpoint first; an idle session answers 409 and the message
    // goes out as an ordinary turn.
    http.post(`${BASE}/api/sessions/:id/steer`, () =>
      HttpResponse.json({ detail: "nothing is in flight in this session" }, { status: 409 }),
    ),
    http.post(`${BASE}/api/sessions/m-1/messages`, async ({ request }) => {
      bodies.push(await request.json());
      calls += 1;
      return streamResponse(calls === 1 ? NAVIGATE : SETTLED);
    }),
  );
  return bodies;
}

// A page that declares a different target on each route, so what the continuation reports can
// only have come from the page it was collected on.
function Page(): null {
  const pathname = usePathname();
  useTargetRegistration(
    pathname === "/knowledge-bases/research"
      ? { name: "knowledge-base-tabs", kind: "tab", description: "The knowledge base tabs" }
      : { name: "composer", kind: "input", description: "The message box" },
    {},
  );
  return null;
}

function wrapper({ children }: { children: ReactNode }): ReactElement {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={client}>
      <FollowMeProvider>
        <Page />
        {children}
      </FollowMeProvider>
    </QueryClientProvider>
  );
}

function useCompanionChat() {
  const { collectContext } = useFollowMe();
  const executeTool = useVerbExecutor();
  return useSessionChat({
    sessionId: "m-1",
    initialMessages: [],
    onSettle: () => {},
    getContext: collectContext,
    executeTool,
  });
}

beforeEach(() => {
  route.pathname = "/chat";
  route.lands = true;
});

describe("the continuation after a navigate", () => {
  it("waits for the page the verb pushed, then reports it with that page's own targets", async () => {
    const bodies = recordStreams();

    const { result } = renderHook(useCompanionChat, { wrapper });

    await act(async () => {
      await result.current.send("show me the research knowledge base");
    });
    // The push has gone out and the route has not landed: nothing may ride back yet.
    expect(bodies).toHaveLength(1);

    await waitFor(() => expect(bodies).toHaveLength(2));

    expect(bodies[1]).toMatchObject({
      messages: [],
      context: {
        url: "/knowledge-bases/research",
        page: "knowledge-base-detail",
        ids: { knowledge_base: "research" },
        targets: [{ name: "knowledge-base-tabs" }],
      },
      tool_results: [
        { tool_call_id: "c-1", tool_name: "navigate", output: "navigated to /knowledge-bases/research" },
      ],
    });
  });

  it("continues anyway, on the page it never left, when the route never arrives", async () => {
    route.lands = false;
    const bodies = recordStreams();

    const { result } = renderHook(useCompanionChat, { wrapper });

    await act(async () => {
      await result.current.send("show me the research knowledge base");
    });
    await waitFor(() => expect(bodies).toHaveLength(2), { timeout: NAVIGATION_WAIT_MS + 2500 });

    expect(bodies[1]).toMatchObject({
      messages: [],
      context: { url: "/chat", targets: [{ name: "composer" }] },
      tool_results: [
        { tool_call_id: "c-1", tool_name: "navigate", output: "navigated to /knowledge-bases/research" },
      ],
    });
  });
});
