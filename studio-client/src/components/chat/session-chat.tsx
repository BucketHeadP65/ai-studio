"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type ReactElement, useEffect, useRef, useState } from "react";

import type { PageContext } from "@/api/page-context";
import { runKeys, sessionKeys } from "@/api/query-keys";
import { sessionsApi, transcriptToUiMessages } from "@/api/sessions";
import type { TranscriptMessage } from "@/api/sessions-types";
import { studioApi, studioErrorMessage } from "@/api/studio";
import type { ChatReply, GateMode } from "@/api/studio-types";
import { useFoldSettledSteps } from "@/lib/chat-preferences";

import { ApprovalCard } from "./approval-card";
import { Composer, useComposerDraft } from "./composer";
import { Conversation, type LiveGate, type LiveRun } from "./conversation";
import { TurnInspector } from "./turn-inspector";
import { useSessionChat } from "./use-session-chat";

import type { UIMessage } from "ai";

// One session's conversation, wherever it is opened: the chat station's own center column, or a
// workflow run's drawer. Owns the transcript read, the streaming engine, the gate turn at this
// session's own live run (when it has one), and the turn inspector, which is keyed to a message
// id and so follows a turn still streaming rather than freezing on the click. A caller only
// supplies the session id and the gate mode its own controls currently hold. `reply` and
// `onSettle` stay with the caller because what a settled reply means (a draft card, a store
// invalidation) differs by where this mounts.

interface SessionChatEngineProps {
  sessionId: string;
  initialMessages: UIMessage[];
  transcript: TranscriptMessage[];
  reply: ChatReply | null;
  answering: boolean;
  gateMode: GateMode;
  hasLiveRun: boolean;
  onChangeGates: () => void;
  liveRun: LiveRun | null;
  liveGate: LiveGate | null;
  onDecide: (approve: boolean, feedback?: string) => void;
  model: string | null;
  deciding: boolean;
  onSettle: (reply: ChatReply) => void;
  initialSend?: string;
  onInitialSent?: () => void;
  getContext?: () => PageContext | null;
  composerTarget?: string;
  executeTool?: (name: string, input: unknown) => string | Promise<string> | null;
  // Fires the instant a turn starts (an ISO timestamp) and again the instant it settles
  // (`null`), so a caller mounted elsewhere -- the rail, timing the turn in flight -- can
  // read the same turn this engine is running without owning any of its state itself.
  onTurnStateChange?: (turnStartedAt: string | null) => void;
}

function SessionChatEngine({
  sessionId,
  initialMessages,
  initialSend,
  onInitialSent,
  transcript,
  reply,
  answering,
  gateMode,
  hasLiveRun,
  onChangeGates,
  liveRun,
  liveGate,
  onDecide,
  deciding,
  onSettle,
  getContext,
  composerTarget,
  executeTool,
  onTurnStateChange,
  model,
}: SessionChatEngineProps): ReactElement {
  const [foldSettled] = useFoldSettledSteps();
  const chat = useSessionChat({ sessionId, initialMessages, onSettle, getContext, executeTool });
  // The id, not the message: the message a turn still streaming renders is a new object every
  // chunk, and the inspector shows whichever version is on screen now.
  const [inspectedId, setInspectedId] = useState<string | null>(null);
  const inspected = chat.messages.find((message) => message.id === inspectedId) ?? null;
  // Held here rather than in the composer, so the approval card taking the composer's place
  // leaves the target registered and the draft intact.
  const draft = useComposerDraft(composerTarget);
  const sentInitial = useRef(false);

  // A session born from a first message: the message was typed before the session existed, so
  // it goes out once, as soon as the engine is up, the same way a typed message would. The ref
  // guard is what actually keeps this to one send -- the empty dependency list alone still lets
  // React's strict-mode double-invoke of a fresh mount's effect fire this twice in dev.
  // `onInitialSent` tells the caller to forget the message once it is sent, so a later remount
  // of this same session (a session switch and back, leaving and returning to a route) never
  // sends it again.
  // biome-ignore lint/correctness/useExhaustiveDependencies: sent once, on mount
  useEffect(() => {
    if (sentInitial.current) return;
    if (initialSend === undefined || initialSend === "") return;
    sentInitial.current = true;
    void chat.send(initialSend);
    onInitialSent?.();
  }, []);
  // This turn's own state, and all the approval card is disabled by: the server's answering flag
  // says nothing that card does not already know (it is up because this turn waits here), and it
  // refreshes on the session list's own poll, so folding it in there can leave a card
  // unanswerable for a whole polling window after the turn stopped moving.
  const inFlight = chat.status === "submitted" || chat.status === "streaming";
  // A gate wait keeps the session "answering" server-side (the tool call that started the run is
  // still open), but the composer stays usable while it waits -- the gate turn is where it gets
  // answered, not here.
  const busy = (inFlight || answering) && liveGate === null;

  // Reports the turn's own start and settle instants as `busy` crosses each edge. Keyed on
  // `busy` alone: React only re-runs an effect when a dependency's value actually changes, so
  // this fires exactly once per transition rather than on every render while it holds steady --
  // the caller's own callback rides a ref so it is never part of that dependency list itself.
  const [turnStartedAt, setTurnStartedAt] = useState<string | null>(null);
  const onTurnStateChangeRef = useRef(onTurnStateChange);
  onTurnStateChangeRef.current = onTurnStateChange;
  useEffect(() => {
    const startedAt = busy ? new Date().toISOString() : null;
    setTurnStartedAt(startedAt);
    onTurnStateChangeRef.current?.(startedAt);
  }, [busy]);

  return (
    // `min-w-0` so the column shrinks to the space between the rails. Without it a flex item is
    // as wide as its widest content, and one long line of agent output widens the whole station.
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <Conversation
        messages={chat.messages}
        transcript={transcript}
        streaming={busy}
        reply={reply}
        sessionId={sessionId}
        gateMode={gateMode}
        hasLiveRun={hasLiveRun}
        onChangeGates={onChangeGates}
        liveRun={liveRun}
        liveGate={liveGate}
        onDecide={onDecide}
        deciding={deciding}
        onInspect={(message) => setInspectedId(message.id)}
        turnStartedAt={turnStartedAt}
        foldSettled={foldSettled}
      />
      {chat.pendingApprovals.length > 0 ? (
        <ApprovalCard
          pending={chat.pendingApprovals}
          onRespond={chat.respond}
          onReplyInstead={() => chat.denyPending("superseded")}
          busy={inFlight}
        />
      ) : (
        <Composer
          // The composer stays open while a turn runs anywhere: every send knocks on the steer
          // endpoint first, so a running turn takes the message mid-flight and an idle session gets
          // an ordinary turn -- the server decides, never this tab's possibly stale view.
          onSend={chat.send}
          busy={false}
          onStop={busy ? chat.stop : null}
          notice={chat.error}
          draft={draft}
          model={model}
        />
      )}
      {inspected !== null ? (
        <TurnInspector
          sessionId={sessionId}
          message={inspected}
          onClose={() => setInspectedId(null)}
        />
      ) : null}
    </div>
  );
}

export interface SessionChatProps {
  sessionId: string;
  reply: ChatReply | null;
  // The statusline's own gate mode, and the control for it, so a launch from the draft card agrees
  // with the control showing it. A caller with no such control (the run chat drawer, opened from
  // a workflow's own page) gets the harmless default: nothing here ever proposes a fresh launch
  // outside the chat station's own draft card.
  gateMode?: GateMode;
  onChangeGates?: () => void;
  onSettle: (reply: ChatReply) => void;
  // The first message of a session created for it, sent once the conversation is up.
  initialSend?: string;
  // Fires right after `initialSend` goes out, so the caller can forget the message -- otherwise
  // a later remount of this same session (a session switch and back, leaving and returning to a
  // route) reads the same prop and sends it again.
  onInitialSent?: () => void;
  // Read once per send by the engine underneath, so a page's context rides the wire as of
  // the moment the person hits send. Absent for a caller with no page to report (the run
  // chat drawer, the chat station's own composer).
  getContext?: () => PageContext | null;
  // Registers the composer beneath this conversation as a target under this name. Absent
  // where a name would not mean anything (the run chat drawer, the companion's own box).
  composerTarget?: string;
  // Performs the companion's own verbs against whatever this conversation is mounted beside.
  // Absent for a caller with nothing on screen for a verb to act on.
  executeTool?: (name: string, input: unknown) => string | Promise<string> | null;
  // See `SessionChatEngineProps.onTurnStateChange`. Absent for a caller with nowhere to show it.
  onTurnStateChange?: (turnStartedAt: string | null) => void;
  // The model answering here, said quietly in the composer. Absent where nobody knows it.
  model?: string | null;
}

export function SessionChat({
  sessionId,
  reply,
  gateMode = "wait",
  onChangeGates = () => {},
  onSettle,
  initialSend,
  onInitialSent,
  getContext,
  composerTarget,
  executeTool,
  onTurnStateChange,
  model = null,
}: SessionChatProps): ReactElement {
  const queryClient = useQueryClient();

  // Read afresh on every mount: the engine below takes its messages once, at
  // creation, so a reader who left for a workflow page and came back would
  // otherwise be seeded from the cached transcript and never see the turns
  // that landed meanwhile. The render gate further down waits for this read.
  const transcriptQuery = useQuery({
    queryKey: sessionKeys.transcript(sessionId),
    queryFn: () => sessionsApi.transcript(sessionId),
    staleTime: 0,
  });

  // Same query key the session list uses, so mounting alongside it costs one fetch, not two.
  const sessionsQuery = useQuery({
    queryKey: sessionKeys.list(false, ""),
    queryFn: () => sessionsApi.list({ archived: false }),
  });

  const runsQuery = useQuery({
    queryKey: runKeys.live,
    queryFn: () => sessionsApi.runs(),
  });

  const run = runsQuery.data?.find((candidate) => candidate.session_id === sessionId) ?? null;

  // Same query key the statusline uses for this session's own run, so the two never fetch twice.
  const runStateQuery = useQuery({
    queryKey: runKeys.state(run?.workflow_id ?? "", sessionId),
    queryFn: () => sessionsApi.runState(sessionId),
    enabled: run !== null,
  });

  // The run turn's own name and graph, fetched only once there is a run to name; the same query
  // key the workflow library uses for this workflow, so mounting both costs one poll, not two.
  const workflowQuery = useQuery({
    queryKey: ["studio", "workflow", run?.workflow_id],
    queryFn: () => studioApi.workflow(run?.workflow_id ?? ""),
    enabled: run !== null,
  });

  const decide = useMutation({
    mutationFn: ({ approve, feedback }: { approve: boolean; feedback?: string }) =>
      sessionsApi.decide(sessionId, approve, feedback),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: runKeys.live });
      queryClient.invalidateQueries({ queryKey: runKeys.state(run?.workflow_id ?? "", sessionId) });
    },
  });

  if (transcriptQuery.isError) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center text-body text-danger-ink">
        Could not load the transcript. {studioErrorMessage(transcriptQuery.error)}
      </div>
    );
  }

  if (!transcriptQuery.isSuccess || !transcriptQuery.isFetchedAfterMount) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center text-body text-label-3">
        Loading the conversation.
      </div>
    );
  }

  const answering =
    sessionsQuery.data?.find((session) => session.id === sessionId)?.answering ?? false;
  const initialMessages = transcriptToUiMessages(transcriptQuery.data);
  const liveRun: LiveRun | null =
    run !== null
      ? {
          // The card names the workflow that is running, never this session's own title.
          title: workflowQuery.data?.title || workflowQuery.data?.name || run.workflow_id,
          run,
          graph: workflowQuery.data?.graph,
        }
      : null;
  const liveGate: LiveGate | null =
    run?.state === "waiting" && run.gate !== null
      ? { pending: run.gate, startedAt: run.started_at, events: runStateQuery.data?.events ?? [] }
      : null;

  return (
    <>
      <SessionChatEngine
        sessionId={sessionId}
        initialSend={initialSend}
        onInitialSent={onInitialSent}
        initialMessages={initialMessages}
        transcript={transcriptQuery.data}
        reply={reply}
        answering={answering}
        gateMode={gateMode}
        hasLiveRun={run !== null}
        onChangeGates={onChangeGates}
        liveRun={liveRun}
        liveGate={liveGate}
        onDecide={(approve, feedback) => decide.mutate({ approve, feedback })}
        deciding={decide.isPending}
        onSettle={onSettle}
        getContext={getContext}
        composerTarget={composerTarget}
        executeTool={executeTool}
        onTurnStateChange={onTurnStateChange}
        model={model}
      />
    </>
  );
}
