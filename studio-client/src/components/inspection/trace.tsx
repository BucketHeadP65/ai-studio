"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { type KeyboardEvent, type ReactElement, type ReactNode, type RefObject, useEffect, useRef } from "react";

import type { Finding } from "@/api/inspection";
import { conversationPayloads, inspectionApi } from "@/api/inspection";
import { longDate, plural, runLocator } from "@/api/inspection-words";
import { traceCounts } from "@/api/inspector-trace";
import { inspectionKeys } from "@/api/query-keys";
import { studioErrorMessage } from "@/api/studio";
import { cn } from "@/shared/lib/cn";
import { Button } from "@/shared/ui/button";
import { Textarea } from "@/shared/ui/textarea";

import { TurnMessages } from "./turn-messages";

// What the inspector read, for one finding: its conversation drawn as the chat draws one, and
// the composer that continues it. The composer renders as soon as the finding has a question to
// continue, disabled until the conversation itself loads, so a challenge from the judgment table
// above always has somewhere to land, however long the fetch takes. A rule-decided finding has no
// conversation to read and says so instead. The section answers to `#trace`.

function SectionHeading({ children }: { children: ReactElement }): ReactElement {
  return (
    <section id="trace" className="flex flex-col gap-2">
      <h2 className="font-semibold text-subtitle">What the inspector read</h2>
      {children}
    </section>
  );
}

// The last row of the conversation's card, always present once a question exists to continue.
// It stays disabled (with `note` explaining why) until the conversation itself is there to append to.
function Composer({
  composerRef,
  draft,
  onDraft,
  onAsk,
  disabled,
  note,
  noteIsError,
}: {
  composerRef: RefObject<HTMLTextAreaElement | null>;
  draft: string;
  onDraft: (text: string) => void;
  onAsk: () => void;
  disabled: boolean;
  note: ReactNode;
  noteIsError: boolean;
}): ReactElement {
  const canAsk = !disabled && draft.trim() !== "";
  // A keyup that follows a keydown already handled is skipped, so one press asks once.
  const returnDownSeen = useRef(false);

  // Return asks, as in the chat, and Shift+Return keeps its newline. Keyup also asks when an
  // input method or an extension overlay swallowed the keydown.
  function askOnReturn(event: KeyboardEvent<HTMLTextAreaElement>, phase: "down" | "up") {
    const isReturn = event.code === "Enter" || event.code === "NumpadEnter" || event.key === "Enter";
    if (!isReturn || event.shiftKey) return;
    event.preventDefault();
    if (phase === "up" && returnDownSeen.current) {
      returnDownSeen.current = false;
      return;
    }
    returnDownSeen.current = phase === "down";
    if (canAsk) onAsk();
  }

  return (
    <div className="flex flex-col gap-2 border-line border-t bg-well px-3.5 py-3">
      <Textarea
        ref={composerRef}
        rows={2}
        aria-label="Ask the inspector about this finding"
        placeholder="Ask the inspector about this finding"
        value={draft}
        disabled={disabled}
        onChange={(event) => onDraft(event.target.value)}
        onKeyDown={(event) => askOnReturn(event, "down")}
        onKeyUp={(event) => askOnReturn(event, "up")}
      />
      <div className="flex items-center justify-between gap-4">
        <span className={cn("text-caption text-label-3", noteIsError && "text-danger-ink")}>{note}</span>
        <Button onClick={onAsk} disabled={!canAsk}>
          Ask
        </Button>
      </div>
    </div>
  );
}

function TraceConversation({
  questionId,
  locator,
  draft,
  onDraft,
  focusRequest,
}: {
  questionId: number;
  locator: string;
  draft: string;
  onDraft: (text: string) => void;
  focusRequest: number;
}): ReactElement {
  const queryClient = useQueryClient();
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  // The last `focusRequest` this effect actually honoured -- a challenge that lands while the
  // composer is still disabled waits here until the question loads, rather than being lost or
  // re-fired by an unrelated later disable/enable (the ask mutation's own pending state).
  const honoredFocusRequest = useRef(0);
  // A link to `#trace` arrives before this section exists, so the browser finds nothing to scroll
  // to. The section brings itself into view once, when its conversation first loads.
  const landedOnHash = useRef(false);

  const page = useQuery({
    queryKey: inspectionKeys.question(questionId),
    queryFn: () => inspectionApi.question(questionId),
  });
  const data = page.data;

  const asking = useMutation({
    mutationFn: (text: string) => inspectionApi.ask(questionId, text),
    onSuccess: () => {
      onDraft("");
      queryClient.invalidateQueries({ queryKey: inspectionKeys.question(questionId) });
    },
  });

  const composerDisabled = !data || page.isError || asking.isPending;

  useEffect(() => {
    if (focusRequest === 0 || focusRequest === honoredFocusRequest.current || composerDisabled) return;
    honoredFocusRequest.current = focusRequest;
    composerRef.current?.scrollIntoView?.({ behavior: "auto" });
    composerRef.current?.focus();
  }, [focusRequest, composerDisabled]);

  useEffect(() => {
    if (!data || landedOnHash.current || window.location.hash !== "#trace") return;
    landedOnHash.current = true;
    document.getElementById("trace")?.scrollIntoView?.({ behavior: "auto", block: "start" });
  }, [data]);

  let composerNote: ReactNode;
  let composerNoteIsError = false;
  if (page.isError) {
    composerNote = `Could not read what the inspector saw. ${studioErrorMessage(page.error)}`;
    composerNoteIsError = true;
  } else if (!data) {
    composerNote = "Reading what the inspector saw.";
  } else if (asking.isPending) {
    composerNote = "Asking the inspector.";
  } else if (asking.isError) {
    composerNote = studioErrorMessage(asking.error);
    composerNoteIsError = true;
  } else {
    composerNote = (
      <>
        Continues this conversation on <span className="font-mono">{data.question.model}</span>. The answer lands above, named by the
        model that spoke.
      </>
    );
  }

  let cardBody: ReactElement;
  if (page.isError) {
    cardBody = (
      <p role="alert" className="p-3 text-caption text-danger-ink">
        Could not read what the inspector saw. {studioErrorMessage(page.error)}
      </p>
    );
  } else if (!data) {
    cardBody = <p className="p-3 text-body text-label-3">Reading what the inspector saw.</p>;
  } else {
    cardBody = (
      <div className="p-3.5">
        <TurnMessages messages={data.messages} speaker={data.question.model} />
      </div>
    );
  }

  const counts = data ? traceCounts(conversationPayloads(data.messages)) : null;

  return (
    <SectionHeading>
      <>
        {data && counts ? (
          <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
            <span className="text-caption text-label-3">
              on <span className="font-mono">{data.question.model}</span>, asked {longDate(data.question.asked_at)}, {counts.reads} reads
              {counts.refusals > 0 ? ", first answer refused" : ""}
              {counts.followUps > 0 ? `, ${plural(counts.followUps, "follow-up", "follow-ups")}` : ""}
            </span>
            <Link href={`/inspection/runs/${locator}`} className="text-caption text-accent">
              Run overview
            </Link>
          </div>
        ) : null}

        <div className="rounded-lg border border-line bg-surface">
          {cardBody}
          <Composer
            composerRef={composerRef}
            draft={draft}
            onDraft={onDraft}
            onAsk={() => asking.mutate(draft)}
            disabled={composerDisabled}
            note={composerNote}
            noteIsError={composerNoteIsError}
          />
        </div>
      </>
    </SectionHeading>
  );
}

export function Trace({
  finding,
  draft,
  onDraft,
  focusRequest,
}: {
  finding: Finding;
  draft: string;
  onDraft: (text: string) => void;
  focusRequest: number;
}): ReactElement {
  if (finding.found_by === "rule") {
    return (
      <SectionHeading>
        <p className="text-body text-label-3">
          A rule decided this one. There is no conversation to read
          {finding.lead_found_by === null ? (
            "."
          ) : (
            <>
              , the blame comes from the rule <span className="font-mono">{finding.lead_found_by}</span>.
            </>
          )}
        </p>
      </SectionHeading>
    );
  }

  if (finding.question_id === null) {
    return (
      <SectionHeading>
        <p className="text-body text-label-3">
          This finding was decided on {longDate(finding.decided_at)}, before the inspector's conversations were recorded. The blames stand.
          The reads and the thinking behind them are gone, and there is no conversation to continue.
        </p>
      </SectionHeading>
    );
  }

  return (
    <TraceConversation
      questionId={finding.question_id}
      locator={runLocator(finding.source)}
      draft={draft}
      onDraft={onDraft}
      focusRequest={focusRequest}
    />
  );
}
