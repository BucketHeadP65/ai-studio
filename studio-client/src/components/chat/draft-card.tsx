"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "client-core/ui/button";
import type { ReactElement } from "react";

import { runKeys } from "@/api/query-keys";
import { sessionsApi } from "@/api/sessions";
import { studioErrorMessage } from "@/api/studio";
import type { ChatReply, GateMode } from "@/api/studio-types";
import { StatePill } from "@/components/marks/state-pill";
import { MiniGraph } from "@/components/workflows/mini-graph";
import { ReadinessNotice } from "@/components/workflows/readiness-findings";

// The moment a reply carries a drafted graph, it gets its own turn: the shape it will run, drawn
// with nothing run yet, whether it is ready, and the two acts open on it here, saving it into
// the library and starting it in this same session with the gate mode the right rail currently
// holds. `hasLiveRun` disables Start once this session already has one going, whether it was this
// card's own act or something else that started it.
//
// When the draft carries a request, the card leads with it in the person's own words, so what the
// graph is an answer to is on screen beside the graph.
//
// Start saves the draft first, so it is disabled by the same reading the save route refuses on --
// the backend's `readiness`, never a rule of this component's own. A reading that never happened
// (nothing on screen parses as a spec) counts as not described, which is what `valid` already says.

export interface DraftCardProps {
  reply: ChatReply;
  sessionId: string | null;
  gateMode: GateMode;
  hasLiveRun: boolean;
  onChangeGates: () => void;
}

export function DraftCard({
  reply,
  sessionId,
  gateMode,
  hasLiveRun,
  onChangeGates,
}: DraftCardProps): ReactElement {
  const queryClient = useQueryClient();
  const title = reply.draft.title || "untitled draft";

  const start = useMutation({
    mutationFn: async () => {
      if (sessionId === null) {
        throw new Error("no session to run this draft in");
      }
      const saved = await sessionsApi.save(sessionId);
      await sessionsApi.run(sessionId, saved.id, {}, gateMode);
      return saved.id;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: runKeys.live });
      queryClient.invalidateQueries({ queryKey: ["chat", "library"] });
    },
  });

  const started = hasLiveRun || start.isSuccess;
  const described = reply.readiness?.described ?? false;

  return (
    <div className="mt-1 max-w-[92%] space-y-2.5 self-start rounded-lg border border-line bg-surface px-3.5 py-3 shadow-sm">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-body font-semibold text-label-1">
          {title}
        </span>
        {reply.valid ? (
          <StatePill tone="completed" size="sm">
            Ready to run
          </StatePill>
        ) : (
          <StatePill tone="warning" size="sm">
            Not ready to run
          </StatePill>
        )}
      </div>
      {reply.draft.intent ? (
        <p className="text-caption text-label-2 leading-snug">
          You asked for: {reply.draft.intent.request}
        </p>
      ) : null}
      <MiniGraph graph={reply.graph ?? undefined} fillFor={() => "not-run"} />
      <ReadinessNotice readiness={reply.readiness} />
      {start.isError ? (
        <p className="text-caption text-warn-ink">{studioErrorMessage(start.error)}</p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <Button
          onClick={() => start.mutate()}
          disabled={!reply.valid || !described || started || start.isPending}
          className="h-8 px-4 text-caption"
        >
          {start.isPending ? "Starting" : "Start this run"}
        </Button>
        <Button variant="secondary" onClick={onChangeGates} className="h-8 px-4 text-caption">
          Change the gates
        </Button>
      </div>
    </div>
  );
}
