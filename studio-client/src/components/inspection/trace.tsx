"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, Brain, Check, ChevronDown, ChevronRight, ChevronUp, CircleHelp, Eye, FileText, List, MessageSquare, Search } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import Link from "next/link";
import { Fragment, type ReactElement, type ReactNode, type RefObject, useEffect, useRef, useState } from "react";

import type { Finding } from "@/api/inspection";
import { conversationPayloads, inspectionApi } from "@/api/inspection";
import { longDate, plural, runLocator } from "@/api/inspection-words";
import { traceCounts, traceEntries } from "@/api/inspector-trace";
import type { TraceEntry } from "@/api/inspector-trace";
import { inspectionKeys } from "@/api/query-keys";
import { studioErrorMessage } from "@/api/studio";
import { cn } from "@/shared/lib/cn";
import { Button } from "@/shared/ui/button";
import { Textarea } from "@/shared/ui/textarea";

import { RowContent, RowLabel } from "./trace-rows";

// What the inspector read, for one finding: a bar of chips, one per call the conversation
// made, folded to a table of the same calls in full, and the composer that continues that
// conversation. The composer renders as soon as the finding has a question to continue --
// disabled until the conversation itself loads -- so a challenge from the judgment table above
// always has somewhere to land, however long the fetch takes. A rule-decided finding has no
// calls to read and says so instead.

interface KeyedEntry {
  entry: TraceEntry;
  key: string;
}

interface TraceRow {
  label: "question" | "follow-up";
  items: KeyedEntry[];
}

// The entries split at each `asked`: the first row, labelled `question`, is everything before
// the first follow-up; every `asked` after that starts a new row, labelled `follow-up`.
function traceRows(entries: TraceEntry[]): TraceRow[] {
  const rows: TraceRow[] = [{ label: "question", items: [] }];
  entries.forEach((entry, index) => {
    const item: KeyedEntry = { entry, key: String(index) };
    if (entry.kind === "asked") {
      rows.push({ label: "follow-up", items: [item] });
    } else {
      rows[rows.length - 1].items.push(item);
    }
  });
  return rows;
}

// Whether an entry falls after the conversation's first follow-up question -- a `said` there
// is the model's answer to it, not a passing remark.
function afterAskedMap(entries: TraceEntry[]): Map<string, boolean> {
  const map = new Map<string, boolean>();
  let seen = false;
  entries.forEach((entry, index) => {
    map.set(String(index), seen);
    if (entry.kind === "asked") seen = true;
  });
  return map;
}

const KIND_ICON: Record<string, LucideIcon> = {
  told: FileText,
  thinking: Brain,
  refused: Ban,
  answered: Check,
  asked: CircleHelp,
  said: MessageSquare,
};
const CALL_ICON: Record<string, LucideIcon> = { outline: List, read: Eye, find: Search };

function chipIcon(entry: TraceEntry): LucideIcon {
  if (entry.kind === "call") return CALL_ICON[entry.name] ?? CircleHelp;
  if (entry.kind === "answered" && (entry.outcome === "unvalidated" || entry.outcome === "undecided")) return CircleHelp;
  return KIND_ICON[entry.kind] ?? CircleHelp;
}

function chipWord(entry: TraceEntry, afterAsked: boolean): string {
  switch (entry.kind) {
    case "told":
      return "what it was told";
    case "call":
      return entry.line;
    case "thinking":
      return "thinking";
    case "refused":
      return "refused";
    case "answered":
      if (entry.outcome === "unvalidated") return "not accepted";
      if (entry.outcome === "undecided") return "undecided";
      return "answered";
    case "asked":
      return "you asked";
    case "said":
      return afterAsked ? "answered" : "said";
  }
}

function Chip({
  item,
  focus,
  afterAsked,
  onPick,
}: {
  item: KeyedEntry;
  focus: string | null;
  afterAsked: boolean;
  onPick: (key: string) => void;
}): ReactElement {
  const Icon = chipIcon(item.entry);
  return (
    <button
      type="button"
      onClick={() => onPick(item.key)}
      className={cn(
        "inline-flex h-6 items-center gap-1.5 rounded border px-2 text-caption",
        focus === item.key ? "border-accent" : "border-line",
        item.entry.kind === "refused" ? "text-danger-ink" : "text-label-2",
        item.entry.kind === "call" && "font-mono text-label-1",
      )}
    >
      <Icon size={14} />
      {chipWord(item.entry, afterAsked)}
    </button>
  );
}

function SectionHeading({ children }: { children: ReactElement }): ReactElement {
  return (
    <section className="flex flex-col gap-2">
      <h2 className="font-semibold text-subtitle">What the inspector read</h2>
      {children}
    </section>
  );
}

// The last row of the trace card, always present once a question exists to continue --
// disabled (with `note` explaining why) until the conversation itself is there to append to.
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
  return (
    <div className="grid grid-cols-[260px_minmax(0,1fr)] gap-x-3.5 border-line border-t bg-well px-3.5 py-3">
      <div className="flex items-start gap-2 text-caption text-label-2">
        <CircleHelp size={14} className="mt-0.5 shrink-0" />
        Follow-up
      </div>
      <div className="flex flex-col gap-2">
        <Textarea
          ref={composerRef}
          rows={2}
          aria-label="Ask the inspector about this finding"
          placeholder="Ask the inspector about this finding"
          value={draft}
          disabled={disabled}
          onChange={(event) => onDraft(event.target.value)}
        />
        <div className="flex items-center justify-between gap-4">
          <span className={cn("text-caption text-label-3", noteIsError && "text-danger-ink")}>{note}</span>
          <Button onClick={onAsk} disabled={disabled || draft.trim() === ""}>
            Ask
          </Button>
        </div>
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
  spans,
}: {
  questionId: number;
  locator: string;
  draft: string;
  onDraft: (text: string) => void;
  focusRequest: number;
  spans: string[];
}): ReactElement {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(() => typeof window !== "undefined" && window.location.hash === "#trace");
  const [focus, setFocus] = useState<string | null>(null);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  // The last `focusRequest` this effect actually honoured -- a challenge that lands while the
  // composer is still disabled waits here until the question loads, rather than being lost or
  // re-fired by an unrelated later disable/enable (the ask mutation's own pending state).
  const honoredFocusRequest = useRef(0);

  const page = useQuery({
    queryKey: inspectionKeys.question(questionId),
    queryFn: () => inspectionApi.question(questionId),
  });
  const data = page.data;
  const entries = data ? traceEntries(conversationPayloads(data.messages)) : [];
  const counts = traceCounts(entries);

  const asking = useMutation({
    mutationFn: (text: string) => inspectionApi.ask(questionId, text),
    onSuccess: () => {
      onDraft("");
      queryClient.invalidateQueries({ queryKey: inspectionKeys.question(questionId) });
    },
  });

  const composerDisabled = !data || page.isError || asking.isPending;

  useEffect(() => {
    if (focus === null) return;
    document.getElementById(`trace-${focus}`)?.scrollIntoView?.({ behavior: "auto" });
  }, [focus]);

  useEffect(() => {
    if (focusRequest === 0 || focusRequest === honoredFocusRequest.current || composerDisabled) return;
    honoredFocusRequest.current = focusRequest;
    composerRef.current?.scrollIntoView?.({ behavior: "auto" });
    composerRef.current?.focus();
  }, [focusRequest, composerDisabled]);

  function pick(key: string): void {
    setOpen(true);
    setFocus(key);
  }

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
    const { question } = data;
    const rows = traceRows(entries);
    const afterAsked = afterAskedMap(entries);

    cardBody = (
      <>
        <div className="flex flex-col gap-2 p-3">
          {rows.map((row, rowIndex) => (
            <div key={rowIndex} className="flex flex-wrap items-center gap-1.5">
              <span className="w-16 shrink-0 text-caption text-label-3">{row.label}</span>
              {row.items.map((item, itemIndex) => (
                <Fragment key={item.key}>
                  {itemIndex > 0 ? <ChevronRight size={14} className="text-label-3" /> : null}
                  <Chip item={item} focus={focus} afterAsked={afterAsked.get(item.key) ?? false} onPick={pick} />
                </Fragment>
              ))}
            </div>
          ))}
        </div>

        {open ? (
          <div className="border-line border-t">
            {rows
              .flatMap((row) => row.items)
              .map((item, index) => (
                <div
                  key={item.key}
                  id={`trace-${item.key}`}
                  className={cn(
                    "grid grid-cols-[260px_minmax(0,1fr)] gap-x-3.5 px-3.5 py-2.5",
                    index > 0 && "border-line border-t",
                    focus === item.key && "bg-accent-soft",
                  )}
                >
                  <RowLabel entry={item.entry} model={question.model} afterAsked={afterAsked.get(item.key) ?? false} />
                  <RowContent entry={item.entry} spans={spans} />
                </div>
              ))}
          </div>
        ) : null}
      </>
    );
  }

  return (
    <SectionHeading>
      <>
        {data ? (
          <div className="flex flex-wrap items-center justify-between gap-3">
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
            <button
              type="button"
              onClick={() => setOpen((value) => !value)}
              className="inline-flex shrink-0 items-center gap-1 text-caption text-accent"
            >
              {open ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
              {open ? "Fold the trace" : "Expand the trace"}
            </button>
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
  spans,
}: {
  finding: Finding;
  draft: string;
  onDraft: (text: string) => void;
  focusRequest: number;
  spans: string[];
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
      spans={spans}
    />
  );
}
