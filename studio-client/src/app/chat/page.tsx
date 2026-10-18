"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { type ReactElement, Suspense, useRef, useState } from "react";

import { useDraftStore } from "@/api/draft-store";
import { harnessApi } from "@/api/harness";
import { sessionKeys } from "@/api/query-keys";
import { sessionsApi } from "@/api/sessions";
import { studioErrorMessage } from "@/api/studio";
import type { GateMode } from "@/api/studio-types";
import { Composer, useComposerDraft } from "@/components/chat/composer";
import { SessionChat } from "@/components/chat/session-chat";
import { SessionList } from "@/components/chat/session-list";
import { SessionStatusline } from "@/components/chat/session-statusline";
import { Rail } from "@/components/panels/rail";
import { useStoredFlag } from "@/lib/use-stored";

// The chat station: sessions on the left, the running conversation in the center, and this
// session's own status row under the composer -- vitals, the gate choice, the plugins count, the
// live run's pause and stop, and Clear, all on one line. The viewed session id lives in the
// `?session=` search param so a reload or a shared link lands back on the same conversation;
// `useSearchParams` only seeds it once at mount, every later change flows through `setViewedId`,
// which updates both the local state driving the render and the URL. The gate mode a new run
// starts with lives here too, so the statusline's own choice and the draft card's own launch
// always agree on it.
//
// The conversation itself, `SessionChat`, owns its own transcript read, its streaming engine,
// and the gate turn at a pending gate; this station keys it to the viewed session so switching
// sessions remounts a fresh one, and hands it only what a settled reply means here -- stamping
// the draft store so the draft card outlives a reload elsewhere in the app.

function ChatStation(): ReactElement {
  const queryClient = useQueryClient();
  const router = useRouter();
  const searchParams = useSearchParams();
  const { draft, setDraft } = useDraftStore();

  const [sessionsOpen, setSessionsOpen] = useStoredFlag("rail:sessions", true);
  const [viewedId, setViewedIdState] = useState<string | null>(() => searchParams.get("session"));
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  // The message that created the viewed session, handed to the conversation to send once.
  const [firstMessage, setFirstMessage] = useState<{ sessionId: string; text: string } | null>(
    null,
  );
  const [gateMode, setGateMode] = useState<GateMode>("wait");
  const firstGateRadioRef = useRef<HTMLInputElement>(null);
  // The viewed session's own turn-in-flight clock: set the instant a send starts, cleared the
  // instant it settles, read here rather than owned by the conversation or the rail alone so
  // both sides of the station time the same turn. Reset on every session switch, so a session
  // opened right after another one never reads with the last one's leftover start time.
  const [turnStartedAt, setTurnStartedAt] = useState<string | null>(null);
  // Bumped on a successful Clear and folded into the conversation's key below: it seeds its
  // messages once, at creation, so emptying the transcript server-side only reaches the screen
  // once this remounts it.
  const [clearedAt, setClearedAt] = useState(0);
  // The first message of a session that does not exist yet is written here, so this box is the
  // composer target only while it is the one on screen.
  const firstDraft = useComposerDraft(viewedId === null ? "composer" : undefined);

  // A settled reply belongs to the session that produced it, stamped onto the
  // stored draft at settle time so a later view of a different session never
  // wears this one's draft card.
  const settledReply =
    draft !== null && viewedId !== null && draft.ownerKey === viewedId ? draft.reply : null;

  // A re-opened session has no in-memory settle, but its draft survives on the
  // server: the conversation read carries it, so the draft card outlives a reload.
  const conversationQuery = useQuery({
    queryKey: sessionKeys.conversation(viewedId),
    queryFn: () => sessionsApi.conversation(viewedId ?? ""),
    enabled: viewedId !== null && settledReply === null,
    staleTime: 30_000,
  });
  const conversation = conversationQuery.data;
  const reply =
    settledReply ??
    (conversation !== undefined && conversation.graph !== null
      ? {
          message: "",
          draft: conversation.draft,
          issues: conversation.issues,
          valid: conversation.valid,
          graph: conversation.graph,
          intent_issues: conversation.intent_issues,
        }
      : null);

  // Display only: which model the orchestrator tier runs, read off the same tiers listing the
  // control room's cards edit, so a change there is reflected here once that query invalidates.
  // No picker here, the three tier cards are the one place models are chosen.
  const tiers = useQuery({ queryKey: ["harness", "tiers"], queryFn: () => harnessApi.tiers() });
  const orchestratorModel =
    tiers.data?.tiers.find((view) => view.tier === "orchestrator")?.model.name ?? null;

  function setViewedId(id: string | null) {
    setViewedIdState(id);
    setTurnStartedAt(null);
    router.replace(id ? `/chat?session=${encodeURIComponent(id)}` : "/chat");
  }

  // A new chat is a composer with no session behind it yet: the session is created the moment
  // the first message is sent, never before, so an abandoned "new chat" leaves nothing behind.
  function handleNewChat() {
    setCreateError(null);
    setViewedId(null);
  }

  async function startWithMessage(text: string) {
    setCreateError(null);
    setCreating(true);
    try {
      const created = await sessionsApi.create();
      await queryClient.invalidateQueries({ queryKey: ["sessions", "list"] });
      setFirstMessage({ sessionId: created.id, text });
      setViewedId(created.id);
    } catch (error) {
      setCreateError(studioErrorMessage(error));
    } finally {
      setCreating(false);
    }
  }

  // The statusline is always on screen, so the draft card's Change the gates lands directly.
  function focusFirstGateRadio() {
    firstGateRadioRef.current?.focus();
  }

  return (
    <div className="-m-6 flex h-[calc(100%+3rem)] flex-col">
      {/* The station spends no chrome on a header, so its one level-one heading is for assistive
          technology alone: a screen reader still lands on a named page, and the conversation
          still fills the frame. */}
      <h1 className="sr-only">Chat</h1>
      {createError ? (
        <p
          role="alert"
          className="border-line border-b bg-surface px-4 py-1.5 text-caption text-danger-ink"
        >
          {createError}
        </p>
      ) : null}

      <div className="flex min-h-0 flex-1 bg-surface">
        <Rail
          side="left"
          title="Sessions"
          storageKey="rail:sessions"
          open={sessionsOpen}
          onToggle={setSessionsOpen}
          defaultWidth={300}
        >
          <SessionList viewedId={viewedId} onView={setViewedId} onNewChat={handleNewChat} />
        </Rail>

        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          {viewedId === null ? (
            <div className="flex min-h-0 flex-1 flex-col">
              <div className="flex flex-1 items-center justify-center text-body text-label-3">
                <p>Pick a session on the left, or write the first message of a new one.</p>
              </div>
              <Composer
                onSend={startWithMessage}
                busy={creating}
                onStop={null}
                notice={null}
                draft={firstDraft}
                model={orchestratorModel}
              />
            </div>
          ) : (
            <SessionChat
              key={`${viewedId}:${clearedAt}`}
              sessionId={viewedId}
              initialSend={firstMessage?.sessionId === viewedId ? firstMessage.text : undefined}
              onInitialSent={() => setFirstMessage(null)}
              reply={reply}
              gateMode={gateMode}
              onChangeGates={focusFirstGateRadio}
              composerTarget="composer"
              model={orchestratorModel}
              onSettle={(settled) => {
                setDraft({ ownerKey: viewedId, reply: settled });
                queryClient.invalidateQueries({ queryKey: sessionKeys.conversation(viewedId) });
              }}
              onTurnStateChange={setTurnStartedAt}
            />
          )}

          <SessionStatusline
            sessionId={viewedId}
            gateMode={gateMode}
            onGateModeChange={setGateMode}
            onCleared={() => setClearedAt((count) => count + 1)}
            firstGateRadioRef={firstGateRadioRef}
            turnStartedAt={turnStartedAt}
          />
        </div>
      </div>
    </div>
  );
}

export default function ChatPage(): ReactElement {
  return (
    <Suspense fallback={null}>
      <ChatStation />
    </Suspense>
  );
}
