import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { z } from "zod";

import { KNOWLEDGE_BASE_API_URL } from "@/api/config";
import { STUDIO_API_URL } from "@/api/studio-config";

// The dashboard's one push channel. Each backend serves GET /api/events, an SSE stream of
// topic-only change frames, and every frame becomes a react-query invalidation here. No frame
// carries data: the client refetches what it already knows how to fetch, so a backend can add a
// producer without the client learning a new shape.

const ChangeEventSchema = z.object({ topic: z.string(), at: z.string() });

// Each entry is one backend's whole stream URL, its own `topics=` filter included: without one,
// both backends hear every topic and a dotted topic invalidates the same queries twice.
const DEFAULT_BASES: readonly string[] = [
  `${KNOWLEDGE_BASE_API_URL}/api/events?topics=jobs,knowledge-base`,
  `${STUDIO_API_URL}/api/events?topics=run,session,sessions,schedules`,
];

export type EventSourceFactory = (url: string) => EventSource;

const defaultEventSource: EventSourceFactory = (url) => new EventSource(url);

// A topic's key is its root plus the rest: "run.wf.report" is ["run", "wf.report"], "jobs" is
// ["jobs"]. Only the FIRST dot splits, so a workflow id keeps its own dots in one element.
export function keyOf(topic: string): string[] {
  const dot = topic.indexOf(".");
  return dot === -1 ? [topic] : [topic.slice(0, dot), topic.slice(dot + 1)];
}

// A frame's topic, or null when the frame is not one of ours. A bad frame must never take the
// stream down, so the parse never throws.
export function topicOf(data: string): string | null {
  try {
    const parsed = ChangeEventSchema.safeParse(JSON.parse(data));
    return parsed.success ? parsed.data.topic : null;
  } catch {
    return null;
  }
}

// React Query matches by prefix in one direction: a filter key invalidates every query whose key
// STARTS with it. That reaches everything scoped to one entity, and never reaches the collection
// query that lists the entities, whose key is shorter. So a dotted topic invalidates twice, its
// own key and its root.
export function invalidateTopic(client: QueryClient, topic: string): void {
  const key = keyOf(topic);
  client.invalidateQueries({ queryKey: key });
  if (key.length > 1) client.invalidateQueries({ queryKey: key.slice(0, 1) });
}

// One stream per backend, opened on mount and closed on unmount. Each entry in `bases` is a
// whole stream URL, not an origin. `create` is an argument so a test hands in its own EventSource
// and nothing global is replaced.
export function useLiveInvalidation(
  bases: readonly string[] = DEFAULT_BASES,
  create: EventSourceFactory = defaultEventSource,
): void {
  const queryClient = useQueryClient();
  useEffect(() => {
    const sources = bases.map((base) => {
      const source = create(base);
      let opened = false;
      source.addEventListener("open", () => {
        // A reopen means the stream was down, and topics announced while it was down are lost, so
        // everything on screen is suspect exactly once.
        if (opened) queryClient.invalidateQueries();
        opened = true;
      });
      source.addEventListener("change", (event) => {
        if (!(event instanceof MessageEvent)) return;
        const topic = topicOf(String(event.data));
        if (topic !== null) invalidateTopic(queryClient, topic);
      });
      return source;
    });
    return () => {
      for (const source of sources) source.close();
    };
  }, [queryClient, bases, create]);
}
