import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { type ReactNode, createElement } from "react";
import { describe, expect, it } from "vitest";

import { server } from "@/test-support/msw/server";

import { useConversation } from "./use-conversation";

// The drawer's own hook. A turn is sent to a named session, and the name comes from the
// conversation read, so what happens before that read lands is the behaviour under test.

const BASE = "http://studio.test";

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(QueryClientProvider, { client }, children);
  };
}

describe("useConversation before the conversation read lands", () => {
  it("sends nothing and surfaces a refusal instead of posting to a nameless session", async () => {
    // Every request the browser attempts, handled or not: a POST to `/api/sessions//messages`
    // matches no handler, so watching the handlers alone would pass while the message was lost.
    const attempted: string[] = [];
    const watch = ({ request }: { request: Request }) => {
      attempted.push(`${request.method} ${new URL(request.url).pathname}`);
    };
    server.events.on("request:start", watch);

    let release = (): void => undefined;
    const held = new Promise<void>((resolve) => {
      release = () => resolve();
    });
    server.use(
      http.get(`${BASE}/api/conversation`, async () => {
        await held;
        return HttpResponse.json({ turns: [], session_id: "s-1" });
      }),
    );

    const { result } = renderHook(() => useConversation(), { wrapper: wrapper() });

    expect(result.current.ready).toBe(false);
    await act(async () => {
      result.current.send("craft a capability");
    });
    await waitFor(() => expect(result.current.error).not.toBeNull());

    expect(attempted.filter((call) => call.includes("/messages"))).toEqual([]);
    expect(result.current.error).toContain("still loading");

    release();
    await waitFor(() => expect(result.current.ready).toBe(true));
    server.events.removeListener("request:start", watch);
  });

  it("sends to the session the read named once it lands, starting no new conversation", async () => {
    const posted: string[] = [];
    const created: string[] = [];
    server.use(
      http.get(`${BASE}/api/conversation`, () =>
        HttpResponse.json({ turns: [], session_id: "s-1" }),
      ),
      http.post(`${BASE}/api/conversation`, () => {
        created.push("POST /api/conversation");
        return HttpResponse.json({ session_id: "should-not-be-minted" });
      }),
      http.post(`${BASE}/api/sessions/s-1/messages`, ({ request }) => {
        posted.push(new URL(request.url).pathname);
        return new HttpResponse(
          `data: {"type":"data-settle","data":{"message":"ok","draft":{"spec_yaml":"","title":"","summary":"","applicability":"","lineage":null},"issues":[],"valid":false,"graph":null}}\n\ndata: [DONE]\n\n`,
          { status: 200, headers: { "Content-Type": "text/event-stream" } },
        );
      }),
    );

    const { result } = renderHook(() => useConversation(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.ready).toBe(true));

    await act(async () => {
      result.current.send("craft a capability");
    });
    await waitFor(() => expect(posted).toEqual(["/api/sessions/s-1/messages"]));

    expect(created).toEqual([]);
    expect(result.current.error).toBeNull();
  });

  it("starts the conversation, then sends to the id it starts, when the read answered a null id", async () => {
    const created: unknown[] = [];
    const messagePosts: Array<{ path: string; body: unknown }> = [];
    server.use(
      http.get(`${BASE}/api/conversation`, () =>
        HttpResponse.json({ turns: [], session_id: null }),
      ),
      http.post(`${BASE}/api/conversation`, () => {
        created.push("POST /api/conversation");
        return HttpResponse.json({ session_id: "s-2" });
      }),
      http.post(`${BASE}/api/sessions/s-2/messages`, async ({ request }) => {
        messagePosts.push({
          path: new URL(request.url).pathname,
          body: await request.json(),
        });
        return new HttpResponse(
          `data: {"type":"data-settle","data":{"message":"ok","draft":{"spec_yaml":"","title":"","summary":"","applicability":"","lineage":null},"issues":[],"valid":false,"graph":null}}\n\ndata: [DONE]\n\n`,
          { status: 200, headers: { "Content-Type": "text/event-stream" } },
        );
      }),
    );

    const { result } = renderHook(() => useConversation(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.ready).toBe(true));

    await act(async () => {
      result.current.send("craft a capability");
    });
    await waitFor(() => expect(messagePosts).toHaveLength(1));

    expect(created).toEqual(["POST /api/conversation"]);
    expect(messagePosts).toEqual([
      {
        path: "/api/sessions/s-2/messages",
        body: {
          trigger: "submit-message",
          id: "s-2",
          messages: [
            {
              id: expect.stringMatching(
                /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
              ),
              role: "user",
              parts: [{ type: "text", text: "craft a capability" }],
            },
          ],
        },
      },
    ]);
    expect(result.current.error).toBeNull();
  });
});
