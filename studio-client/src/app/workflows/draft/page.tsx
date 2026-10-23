"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/shared/ui/button";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { type ReactElement, Suspense, useEffect, useRef } from "react";

import { useDraftStore } from "@/api/draft-store";
import { sessionKeys } from "@/api/query-keys";
import { sessionsApi } from "@/api/sessions";
import { refusedReadiness, studioApi, studioErrorMessage } from "@/api/studio";
import type {
  Draft,
  IntentIssue,
  Readiness,
  WorkflowGraph,
  WorkflowIntent,
} from "@/api/studio-types";
import { WorkflowView } from "@/app/workflows/[id]/page";
import { useSessionChat } from "@/components/chat/use-session-chat";
import { useFollowMe } from "@/components/follow-me/provider";
import { useTargetRegistration } from "@/components/follow-me/targets";
import {
  AutomationIntent,
  AutomationRequest,
  DesignExchange,
} from "@/components/workflows/automation-intent";
import { CollapsibleCard } from "@/components/workflows/collapsible-card";
import { InspectorPage } from "@/components/workflows/inspector-page";
import { ReadinessNotice } from "@/components/workflows/readiness-findings";
import { WorkflowCanvas } from "@/components/workflows/workflow-canvas";

// The draft on the canvas, full page. With `?session=` on the URL this reads
// THAT session's own draft off `GET /api/sessions/{id}/conversation`, so a
// reload lands on the same proposal, and a session with no draft yet is met
// by one box asking what the automation should do. What comes back leads
// with the request and what it still owes, because that is what a person
// can judge, then what has been said and a box for saying what should
// change, so a proposal can be answered where it is read. The YAML and the
// execution graph are the answer to all of it and sit under Details. The
// side column stays what it was: whether the draft holds, and the one act
// available on it, saving it into the library.
//
// With no session named, this falls back to the in-memory store the
// authoring conversation last settled, which does not survive a reload.
// With `?run=` instead, this is not a draft still being authored but a run
// already launched from one -- the fleet board and a run archive link both
// point here with that param for a run whose workflow was never saved, since
// this static route is the only one a launch from a draft can ever carry.
// That renders the ordinary run inspector under workflow id "draft" rather
// than the draft-authoring view below, tolerating the saved-workflow lookup
// it makes that can never resolve.
//
// Nothing on this page starts a run or creates a schedule. Designing is
// designing, and the Save button is the only act it offers.

interface DraftSideProps {
  draft: Draft;
  valid: boolean;
  issues: string[];
  readiness: Readiness | null;
  onSave: () => void;
  saving: boolean;
  savedId: string | null;
  saveError: string | null;
}

function DraftSide({
  draft,
  valid,
  issues,
  readiness,
  onSave,
  saving,
  savedId,
  saveError,
}: DraftSideProps): ReactElement {
  const { registerExtras } = useFollowMe();
  const sideRef = useRef<HTMLDivElement>(null);

  // The draft on screen here is always the unsaved proposal, whether it came from a session's
  // conversation or the in-memory store -- the chat agent sees it without a round trip back.
  useEffect(
    () =>
      registerExtras("draft", () => ({
        draft_title: draft.title,
        draft_spec: draft.spec_yaml.slice(0, 500),
      })),
    [registerExtras, draft.title, draft.spec_yaml],
  );

  useTargetRegistration(
    {
      name: "save-controls",
      kind: "region",
      description: "The Save control for the draft on screen",
    },
    { element: () => sideRef.current },
  );

  const title = draft.title || "Untitled draft";
  const lineage = draft.lineage ?? null;
  return (
    <div ref={sideRef} className="space-y-2.5">
      <h2 className="font-semibold text-[13px] text-label-1">{title}</h2>
      {lineage === null ? null : (
        <div
          data-section="adapted-from"
          className="rounded-md border border-line bg-well px-2 py-1.5 text-[12px] text-label-2 leading-snug"
        >
          <p>
            A new version of {lineage.parent_name} v{lineage.parent_version}. The one it came from
            is unchanged.
          </p>
          {lineage.evidence === "" ? null : (
            <p className="mt-1 whitespace-pre-line text-label-3">{lineage.evidence}</p>
          )}
        </div>
      )}
      <p className="text-[12.5px] text-label-2">
        {valid ? "Valid, this app can run it." : "Not valid yet."}
      </p>
      {issues.length > 0 ? (
        <ul className="space-y-1">
          {issues.map((issue) => (
            <li
              key={issue}
              className="rounded-md border border-warn-ink/30 bg-warn-soft/60 px-2 py-1 text-[12.5px] text-warn-ink leading-snug"
            >
              {issue}
            </li>
          ))}
        </ul>
      ) : null}
      <ReadinessNotice readiness={readiness} />
      {savedId === null ? (
        <>
          <Button
            onClick={onSave}
            disabled={saving || !valid || !(readiness?.described ?? false)}
            className="h-7 px-4 text-[12.5px]"
          >
            {saving ? "Saving" : "Save"}
          </Button>
          <p className="text-[12px] text-label-3 leading-snug">
            Saving stores this draft in the library as a versioned workflow, so it can be run and
            reused. It does not run anything.
          </p>
        </>
      ) : (
        <p className="rounded-md border border-ok-ink/30 bg-ok-soft/60 px-2 py-1.5 text-[12.5px] text-ok-ink leading-snug">
          Saved as {savedId}, it is in the library now.{" "}
          <Link href={`/workflows/${encodeURIComponent(savedId)}`} className="underline">
            Open it?
          </Link>
        </p>
      )}
      {saveError ? <p className="text-[12.5px] text-warn-ink">{saveError}</p> : null}
    </div>
  );
}

function useSave(sessionId?: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => (sessionId === undefined ? studioApi.save() : sessionsApi.save(sessionId)),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["chat", "library"] });
    },
  });
}

/** The YAML and the execution graph, folded away: the answer, for whoever wants to read it. */
function Details({ graph, specYaml }: { graph: WorkflowGraph; specYaml: string }): ReactElement {
  return (
    <CollapsibleCard title="Details" defaultOpen={false} bodyClassName="flex flex-col">
      <div className="relative h-[360px] bg-ground">
        <WorkflowCanvas graph={graph} className="absolute inset-0" />
      </div>
      <pre className="max-h-80 overflow-auto border-line border-t bg-well px-4 py-3 text-[12px] text-label-2 leading-relaxed">
        {specYaml}
      </pre>
    </CollapsibleCard>
  );
}

/** One session's authoring view: the request first, the graph under Details, Save on the side. */
function SessionDraft({ sessionId }: { sessionId: string }): ReactElement {
  const queryClient = useQueryClient();
  const conversation = useQuery({
    queryKey: sessionKeys.conversation(sessionId),
    queryFn: () => sessionsApi.conversation(sessionId),
  });
  const save = useSave(sessionId);

  // Every turn this screen sends says it only designs, so the agent is handed no verb that starts
  // a run or changes a schedule. The narrowing is the server's, off what the request says.
  const chat = useSessionChat({
    sessionId,
    initialMessages: [],
    designOnly: true,
    onSettle: () => {
      queryClient.invalidateQueries({ queryKey: sessionKeys.conversation(sessionId) });
    },
  });
  const designing = chat.status === "submitted" || chat.status === "streaming";

  const record = useMutation({
    mutationFn: (intent: WorkflowIntent) => sessionsApi.recordIntent(sessionId, intent),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: sessionKeys.conversation(sessionId) });
    },
  });

  if (conversation.isError) {
    return (
      <p role="alert" className="text-body text-danger-ink">
        Could not open this session's draft. {studioErrorMessage(conversation.error)}
      </p>
    );
  }

  if (!conversation.data) {
    return <p className="text-body text-label-3">Loading the draft.</p>;
  }

  const graph: WorkflowGraph | null = conversation.data.graph;
  if (graph === null) {
    return (
      <AutomationRequest
        onSubmit={(request) => {
          void chat.send(request);
        }}
        busy={designing}
        error={chat.error}
      />
    );
  }

  const draft = conversation.data.draft;
  const issues: readonly IntentIssue[] = conversation.data.intent_issues;
  return (
    <InspectorPage
      title={draft.title || "Untitled draft"}
      graph={graph}
      highlight={undefined}
      body={
        <>
          <AutomationIntent
            intent={draft.intent}
            issues={issues}
            onRecord={(intent) => record.mutate(intent)}
            saving={record.isPending}
            error={record.isError ? studioErrorMessage(record.error) : null}
          />
          <DesignExchange
            turns={conversation.data.turns}
            onSend={(message) => {
              void chat.send(message);
            }}
            busy={designing}
            error={chat.error}
            chatHref={`/chat?session=${encodeURIComponent(sessionId)}`}
          />
          <Details graph={graph} specYaml={draft.spec_yaml} />
        </>
      }
      side={
        <DraftSide
          draft={draft}
          valid={conversation.data.valid}
          issues={conversation.data.issues}
          readiness={
            save.isError ? refusedReadiness(save.error) : (conversation.data.readiness ?? null)
          }
          onSave={() => save.mutate()}
          saving={save.isPending}
          savedId={save.data?.id ?? null}
          saveError={save.isError ? studioErrorMessage(save.error) : null}
        />
      }
    />
  );
}

function StoredDraft(): ReactElement {
  const { draft } = useDraftStore();
  const reply = draft?.reply ?? null;
  const save = useSave();

  if (!reply || reply.graph == null) {
    return (
      <p className="text-body text-label-3">
        No draft in memory. <Link href="/chat">Draft one in the chat?</Link>
      </p>
    );
  }

  return (
    <InspectorPage
      title={reply.draft.title || "Untitled draft"}
      graph={reply.graph}
      highlight={undefined}
      side={
        <DraftSide
          draft={reply.draft}
          valid={reply.valid}
          issues={reply.issues}
          readiness={save.isError ? refusedReadiness(save.error) : (reply.readiness ?? null)}
          onSave={() => save.mutate()}
          saving={save.isPending}
          savedId={save.data?.id ?? null}
          saveError={save.isError ? studioErrorMessage(save.error) : null}
        />
      }
    />
  );
}

function DraftView(): ReactElement {
  const searchParams = useSearchParams();
  if (searchParams.get("run") !== null) {
    return <WorkflowView id="draft" />;
  }
  const sessionId = searchParams.get("session");
  return sessionId !== null ? <SessionDraft sessionId={sessionId} /> : <StoredDraft />;
}

export default function DraftWorkflowPage(): ReactElement {
  return (
    <Suspense fallback={null}>
      <DraftView />
    </Suspense>
  );
}
