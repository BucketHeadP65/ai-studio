"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { cn } from "client-core/lib/cn";
import { Button } from "client-core/ui/button";
import { ConfirmDialog } from "client-core/ui/confirm-dialog";
import { ChevronDown } from "lucide-react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { type ReactElement, Suspense, useMemo, useState } from "react";

import { runKeys, stepKeys } from "@/api/query-keys";
import { schedulesApi } from "@/api/schedules";
import { sessionsApi } from "@/api/sessions";
import { MessagePayloadSchema } from "@/api/sessions-types";
import type { MessagePayload } from "@/api/sessions-types";
import { stepChatApi } from "@/api/step-chat";
import { studioApi, studioErrorMessage } from "@/api/studio";
import type {
  ArtifactRef,
  GraphNode,
  PendingGate,
  RunEnvelope,
  StepEvent,
  WorkflowDetail,
  WorkflowGraph,
} from "@/api/studio-types";
import { NoteDraftSchema } from "@/api/types";
import type { NoteDraft } from "@/api/types";
import { TextViewer } from "@/components/chat/text-viewer";
import { ArtifactActions } from "@/components/workflows/artifact-actions";
import { ArtifactCard } from "@/components/workflows/artifact-card";
import { formatElapsedMs, useNowTick } from "@/components/workflows/elapsed";
import { GateTrail } from "@/components/workflows/gate-trail";
import { HistoryRail } from "@/components/workflows/history-rail";
import { InspectorPage } from "@/components/workflows/inspector-page";
import {
  OutcomeStrip,
  type RecordedOutput,
  ResultSummary,
} from "@/components/workflows/outcome-strip";
import { OwnerReviewForm } from "@/components/workflows/owner-review-form";
import { RunChatDrawer } from "@/components/workflows/run-chat-drawer";
import { RunControls } from "@/components/workflows/run-controls";
import { RunEvidencePanel } from "@/components/workflows/run-evidence-panel";
import { RunTimeline } from "@/components/workflows/run-timeline";
import { RunValuesPanel } from "@/components/workflows/run-values-panel";
import { SaveArtifactNote } from "@/components/workflows/save-artifact-note";
import { ScheduleForm, type ScheduleFormPayload } from "@/components/workflows/schedule-form";
import { SpendReport } from "@/components/workflows/spend-report";
import { StepChatDrawer } from "@/components/workflows/step-chat-drawer";
import type { RunHighlight } from "@/components/workflows/workflow-canvas";
import { WorkflowCanvas } from "@/components/workflows/workflow-canvas";
import { WorkflowHeader } from "@/components/workflows/workflow-header";

// A node opens its own step chat -- brief, thinking, tool acts, a live tail, and a composer
// that steers it -- when it structurally runs an agent: a `run`/`map` node with no carried
// program that either holds a briefing or calls tools, the same read `workflow-canvas.tsx`'s
// own `runnerIcon` uses for its bot case. This alone cannot decide a `judge` node: the rendered
// graph blanks a judge's own `briefing`, and its leaf may be a plain function with no
// conversation at all, so a judge (and anything else this structural read misses) only qualifies
// through an actual stored line -- see `inspectedIsAgentic` below, which folds this together
// with that check.
function isAgenticByStructure(node: GraphNode): boolean {
  if (node.kind !== "run" && node.kind !== "map") return false;
  if (node.source !== "") return false;
  return node.briefing !== "" || node.tools.length > 0;
}

// Whether a `conversations` dict (the archive's own, unparsed -- key existence does not need
// the parse) holds a line for this node id. The archive keys every line `"{nodeId}[{item}]"`.
function hasArchivedConversation(conversations: Record<string, unknown[]>, nodeId: string): boolean {
  const marker = `${nodeId}[`;
  return Object.keys(conversations).some((key) => key.startsWith(marker));
}

// The archive's `conversations` dict arrives as `Record<string, unknown[]>` (the backend's own
// message dump, unvalidated past that) -- parsed here into the shape the drawer trusts, one
// message at a time, so a row this app cannot read is dropped rather than cast past the boundary.
function parsedConversations(raw: Record<string, unknown[]>): Record<string, MessagePayload[]> {
  const parsed: Record<string, MessagePayload[]> = {};
  for (const [key, messages] of Object.entries(raw)) {
    parsed[key] = messages.flatMap((message) => {
      const candidate = MessagePayloadSchema.safeParse(message);
      return candidate.success ? [candidate.data] : [];
    });
  }
  return parsed;
}

// One saved workflow, full page: the run controls and its run history
// beside it. `runs` names the session (if any) already running this
// workflow, which paints the canvas's own progress here while the "Live" row
// is what the history rail shows selected, feeds the gate/pause state
// `RunControls` acts through next, and scopes the per-node outputs a click
// on any step opens. Selecting a past run instead repaints the outcome
// strip, the gate trail, the timeline, the canvas, and the node drawer from
// that run's own archived record — never from the live poll — leaving the
// acting controls untouched, since they always act on the live session
// regardless of which run the body is currently showing.
//
// The selected run also lives in the `?run=` search param, so a link built
// with a run id (a failure row on the fleet board, a shared URL) lands
// straight on that run's own picture rather than on Live; `useSearchParams`
// only seeds the initial state, and every later selection writes both the
// local state and the URL, the same split `?session=` on the chat page uses.

interface RunItCardProps {
  detail: WorkflowDetail;
  sessionId: string | null;
  /** The run the reader has open, which is the one Run again and Make a new version work from. */
  viewedRunId: string | null;
  onSchedule: () => void;
}

// The one card that acts on this workflow: its params, the choice of who decides this run's
// gates, Run, Schedule it, and the paused or running status line, all of it `RunControls`'
// own render. Sitting at the top of the side column keeps it reachable however far the reader
// scrolls the main column, and keeps the header above free to hold nothing but identity.
function RunItCard({
  detail,
  sessionId,
  viewedRunId,
  onSchedule,
}: RunItCardProps): ReactElement {
  return (
    <div className="overflow-hidden rounded-lg border border-line bg-surface shadow-sm">
      <div className="border-line border-b px-4 py-3">
        <h2 className="font-semibold text-label-1 text-subtitle">Run it</h2>
      </div>
      <div className="p-4">
        <RunControls
          detail={detail}
          sessionId={sessionId}
          viewedRunId={viewedRunId}
          onSchedule={onSchedule}
        />
      </div>
    </div>
  );
}

interface TimelineCardProps {
  events: StepEvent[];
  nodes: GraphNode[];
  envelope: RunEnvelope;
  onSelectNode: (nodeId: string) => void;
}

// The time chart draws its own legend and axis; this card adds the panel title and the run's
// own total duration above them, and hides itself when there is nothing to draw, the same rule
// the chart itself keeps.
function TimelineCard({
  events,
  nodes,
  envelope,
  onSelectNode,
}: TimelineCardProps): ReactElement | null {
  const live = envelope.outcome === "live";
  const now = useNowTick(1000, live);
  if (events.length === 0) return null;

  const startedMs = new Date(envelope.started_at).getTime();
  const endedMs = envelope.ended_at !== "" ? new Date(envelope.ended_at).getTime() : now;
  const duration = formatElapsedMs(endedMs - startedMs);

  return (
    <div className="shrink-0 overflow-hidden rounded-lg border border-line bg-surface shadow-sm">
      <div className="flex flex-wrap items-end justify-between gap-3 border-line border-b px-5 py-3">
        <h2 className="font-semibold text-label-1 text-subtitle">Timeline</h2>
        <span className="flex items-baseline gap-1 text-caption text-label-3">
          <span className="font-mono">{duration}</span>
          {live ? <span>so far</span> : null}
        </span>
      </div>
      <RunTimeline
        events={events}
        nodes={nodes}
        onSelectNode={onSelectNode}
        live={live}
        startedAt={envelope.started_at}
        spend={envelope.spend}
      />
    </div>
  );
}

interface CanvasCardProps {
  graph: WorkflowGraph;
  highlight: RunHighlight | undefined;
  onNodeClick: (node: GraphNode) => void;
  open: boolean;
  onToggle: () => void;
}

// The existing canvas, wrapped in a card a reader opens when the shape is what they came for --
// folded on arrival, because what a run produced and what it left behind are the answers, and the
// graph explains them. Tall enough on its own once open (`min-h-[70vh]` on the frame below) that a
// graph reads without any further adjusting. Whether it is open is the page's own state, so a
// reader who opened it keeps it open when the run in view changes under them.
function CanvasCard({
  graph,
  highlight,
  onNodeClick,
  open,
  onToggle,
}: CanvasCardProps): ReactElement {
  return (
    <div
      data-section="graph"
      className="flex shrink-0 flex-col overflow-hidden rounded-lg border border-line bg-surface shadow-sm"
    >
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="flex items-center gap-2 border-line border-b px-5 py-3 text-left"
      >
        <ChevronDown
          aria-hidden
          size={16}
          className={cn("shrink-0 text-label-3 transition-transform", !open && "-rotate-90")}
        />
        <h2 className="font-semibold text-label-1 text-subtitle">Canvas</h2>
      </button>
      {open ? (
        <div className="relative min-h-[70vh] bg-ground">
          <WorkflowCanvas
            graph={graph}
            runState={highlight}
            onNodeClick={onNodeClick}
            className="absolute inset-0"
          />
        </div>
      ) : null}
    </div>
  );
}

export interface WorkflowViewProps {
  id: string;
}

// Exported so `/workflows/draft` can render the same run inspector under workflow id "draft"
// when it carries `?run=` (a run the fleet board or a run archive link launched from the
// chat's draft) -- its own dynamic segment never matches that static route, so nothing else
// would ever open it there.
export function WorkflowView({ id }: WorkflowViewProps): ReactElement {
  const router = useRouter();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const [inspected, setInspected] = useState<GraphNode | null>(null);
  // `null` names the live row: the default, and what a run in flight is
  // selected on until the reader picks a past one instead.
  const [selectedRunId, setSelectedRunId] = useState<string | null>(() => searchParams.get("run"));
  const [deleteTarget, setDeleteTarget] = useState<RunEnvelope | null>(null);
  // The review being written, if any: the action that opened it, the run it is about, and the
  // references the host minted and re-checked when that action was pressed. `null` closes the
  // form and offers the action again, and the next press opens a fresh form over freshly resolved
  // references, which is what gives each review its own id and its own values.
  const [reviewing, setReviewing] = useState<{
    actionId: string;
    runId: string;
    refs: ArtifactRef[];
  } | null>(null);
  // The note being kept, if any: the action that opened it, the reference the host minted and
  // re-checked when that action was pressed, and the proposed note that reference addresses.
  // `null` closes the surface and offers the action again, and the next press resolves afresh.
  const [keeping, setKeeping] = useState<{
    actionId: string;
    origin: ArtifactRef;
    proposed: NoteDraft;
  } | null>(null);
  const [scheduleOpen, setScheduleOpen] = useState(false);
  const [chatOpen, setChatOpen] = useState(false);
  const [canvasOpen, setCanvasOpen] = useState(false);

  const detail = useQuery({
    queryKey: ["studio", "workflow", id],
    queryFn: () => studioApi.workflow(id),
  });
  const runs = useQuery({
    queryKey: runKeys.live,
    queryFn: () => sessionsApi.runs(),
  });
  const history = useQuery({
    queryKey: runKeys.history(id),
    queryFn: () => studioApi.workflowHistory(id),
  });

  const activeRun = runs.data?.find((run) => run.workflow_id === id) ?? null;
  const sessionId = activeRun?.session_id ?? null;
  const liveEnvelope = history.data?.find((run) => run.outcome === "live") ?? null;
  // The run this page falls back to showing when nothing is live and the reader has not picked
  // one: the newest archived record, `workflowHistory`'s own first non-live entry, so the page
  // shows a workflow's own last run instead of an empty page while its history still holds one.
  // Both live signals must be quiet: the run pool answers faster than the history route, so a
  // run that just started is live by `activeRun` before its record says so.
  const newestArchived =
    liveEnvelope === null && activeRun === null
      ? (history.data?.find((run) => run.outcome !== "live") ?? null)
      : null;
  const viewedRunId = selectedRunId ?? newestArchived?.run_id ?? null;
  const isHistory = viewedRunId !== null;
  // The gate trail's own decide controls act on the live session, so viewing an archived run
  // never opens a decision this page cannot make.
  const pendingGate: PendingGate | null =
    !isHistory && activeRun?.state === "waiting" ? activeRun.gate : null;

  function selectRun(nextRunId: string | null) {
    setSelectedRunId(nextRunId);
    const base = `/workflows/${encodeURIComponent(id)}`;
    router.replace(nextRunId !== null ? `${base}?run=${encodeURIComponent(nextRunId)}` : base);
  }

  const values = useQuery({
    queryKey: runKeys.values(id, sessionId),
    queryFn: () => sessionsApi.runValues(sessionId ?? ""),
    enabled: sessionId !== null && !isHistory,
  });
  const runState = useQuery({
    queryKey: runKeys.timeline(id, sessionId),
    queryFn: () => sessionsApi.runState(sessionId ?? ""),
    enabled: sessionId !== null && !isHistory,
  });
  const record = useQuery({
    queryKey: ["studio", "run-record", viewedRunId],
    queryFn: () => studioApi.runRecord(viewedRunId ?? ""),
    enabled: isHistory,
  });
  // Read here, not only inside the drawer, so a node click can tell whether a `judge` (whose
  // own briefing the rendered graph blanks) actually has a line before opening the step chat --
  // the same query key the drawer itself polls once open, so mounting both costs one fetch. No
  // poll of its own: this key sits under the "run" root, so every push frame a run publishes
  // already invalidates it, and polling it here ran every 2s regardless of whether the drawer
  // was even open. The drawer keeps its own poll for the one gap that invalidation cannot close
  // -- the moment a node's "started" push frame lands before its line is actually open.
  const stepLines = useQuery({
    queryKey: stepKeys.lines(sessionId ?? ""),
    queryFn: () => stepChatApi.lines(sessionId ?? ""),
    enabled: sessionId !== null && !isHistory,
    refetchInterval: false,
  });

  const pin = useMutation({
    mutationFn: (run: RunEnvelope) => studioApi.pinRun(run.run_id, !run.pinned),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: runKeys.history(id) }),
  });
  const remove = useMutation({
    mutationFn: (run: RunEnvelope) => studioApi.deleteRun(run.run_id),
    onSuccess: (_result, run) => {
      queryClient.invalidateQueries({ queryKey: runKeys.history(id) });
      if (selectedRunId === run.run_id) selectRun(null);
      setDeleteTarget(null);
    },
  });
  const pinningId = pin.isPending ? (pin.variables?.run_id ?? null) : null;
  const decide = useMutation({
    mutationFn: ({ approve, feedback }: { approve: boolean; feedback?: string }) =>
      sessionsApi.decide(sessionId ?? "", approve, feedback),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: runKeys.live });
      queryClient.invalidateQueries({ queryKey: runKeys.timeline(id, sessionId) });
    },
  });
  const createSchedule = useMutation({
    mutationFn: async (payload: ScheduleFormPayload) => {
      const created = await schedulesApi.create({
        workflow_id: payload.workflowId,
        arguments: payload.arguments,
        cadence: payload.cadence,
        gate_policy: payload.gatePolicy,
        gate_mode: payload.gateMode,
        capacity_key: payload.capacityKey,
        capacity_limit: payload.capacityLimit,
        blackout: payload.blackout,
        catch_up: payload.catchUp,
      });
      return created;
    },
    onSuccess: () => setScheduleOpen(false),
  });

  // Re-parsed only when the archived record itself changes -- not a plain call inline in the
  // drawer's props, which would re-walk every message on every render of this page.
  const recordConversations = useMemo(
    () => parsedConversations(record.data?.conversations ?? {}),
    [record.data],
  );

  const liveHighlight = activeRun
    ? {
        active: activeRun.active,
        done: activeRun.done,
        failed: activeRun.failed,
        waitingAt: activeRun.waiting_at,
        // `activeRun` (the live pool's own view) carries no rejection count;
        // the history poll's own "live" envelope, already fetched for the stat
        // strip above, is the one record of this run that does.
        rejected: liveEnvelope?.gates_rejected,
      }
    : undefined;
  const historyHighlight = record.data
    ? {
        active: [],
        done: [
          ...new Set(
            record.data.events
              .filter((event) => event.finished && !event.failed)
              .map((event) => event.node),
          ),
        ],
        failed: [
          ...new Set(record.data.events.filter((event) => event.failed).map((event) => event.node)),
        ],
        waitingAt: null,
        rejected: record.data.gates_rejected,
      }
    : undefined;

  // `detail` names the saved workflow in the library, which a run's own archived record does not
  // require to exist -- a run launched from a draft never saved carries workflow id "draft",
  // whose own `workflow` query 404s forever. The record's own graph (`RunHistoryRecordSchema`
  // stores it whole) is what renders instead, so a failed `detail` only stops the page when
  // there is no archived graph to fall back to either.
  const graph: WorkflowGraph | null = isHistory
    ? (record.data?.graph ?? detail.data?.graph ?? null)
    : (detail.data?.graph ?? null);

  // Held back while `record` is still in flight -- it alone might yet deliver the graph
  // `detail` could not, and showing the error before it settles would flash it, then flip to
  // the archived view a beat later. Only reads live when `record` is even in play (`isHistory`);
  // otherwise it stays disabled and permanently "pending", which must never hold this back.
  const recordStillLoading = isHistory && record.isPending;

  if (detail.isError && graph === null && !recordStillLoading) {
    return (
      <p
        role="alert"
        className="mx-auto mt-3 w-fit rounded-md bg-danger-soft px-3 py-1.5 text-caption text-danger-ink"
      >
        Could not open {id}. {studioErrorMessage(detail.error)}
      </p>
    );
  }

  if (graph === null) {
    return <p className="text-body text-label-3">Loading {id}.</p>;
  }

  const highlight = isHistory ? historyHighlight : liveHighlight;
  const footerEvents = isHistory ? (record.data?.events ?? []) : (runState.data?.events ?? []);
  const activeValues = isHistory ? (record.data?.values ?? null) : (values.data?.values ?? null);
  // OutcomeStrip reads the record it is showing: the archived one once a past run is
  // selected, otherwise the live envelope the history poll already carries alongside its own
  // finer state (running, paused, waiting on you) off the live pool.
  const statEnvelope = isHistory ? (record.data ?? null) : liveEnvelope;
  const liveState = isHistory ? null : (activeRun?.state ?? null);
  // The header this run's own identity gets when the saved workflow behind it cannot be
  // opened: the same title shape (`name vN`), read off the record's own graph instead.
  const title = detail.data
    ? `${detail.data.name} v${detail.data.version}`
    : `${graph.name} v${graph.version}`;

  const inspectedText =
    inspected === null
      ? null
      : (activeValues?.[inspected.id] ??
        (isHistory
          ? "No output recorded for this step."
          : sessionId === null
            ? "No run of this workflow is live here, so this step has no output to show."
            : "No output from this step yet."));
  // A node's own step chat opens when: live, it structurally runs an agent OR a `stepLines` row
  // names it (the only way a `judge` -- whose own briefing the rendered graph blanks -- ever
  // qualifies); archived, only an actual stored line does. Structure decides nothing once a run
  // has ended: the record already carries every line it opened, keyed by node id, so a
  // structurally agentic node with no such key -- a pre-change archive, or a step that failed
  // before opening one -- gets the plain viewer and its output instead, the useful thing when
  // there is no conversation to show.
  const inspectedIsAgentic =
    inspected !== null &&
    (isHistory
      ? hasArchivedConversation(record.data?.conversations ?? {}, inspected.id)
      : isAgenticByStructure(inspected) || (stepLines.data ?? []).some((line) => line.node === inspected.id));

  const openNode = (nodeId: string) => {
    const node = graph.nodes.find((candidate) => candidate.id === nodeId);
    if (node && node.kind !== "param") setInspected(node);
  };
  const inspectNode = (node: GraphNode) => {
    if (node.kind !== "param") setInspected(node);
  };

  // The author's own recorded-output facts, read straight off what this run recorded under each
  // declared path. A path nothing recorded says so: these are facts to show, never evidence, and
  // an invented value would read as one.
  const recorded: RecordedOutput[] = (detail.data?.presentation?.result_fields ?? []).map(
    (field) => ({
      label: field.label,
      value: activeValues?.[field.path] ?? "not recorded",
    }),
  );
  const artifacts = statEnvelope?.result.artifacts ?? [];
  const actions = detail.data?.presentation?.actions ?? [];
  // What people have made of this run, read off its own record. Undefined while no archived
  // record is in view, which is a different thing from a record carrying no review.
  const ownerReviews = record.data?.owner_reviews;

  // Rendered once, above the branch below: the canvas card sits in the same slot whether a run is
  // in view or not, so opening it survives the run's own record arriving under it.
  const canvas = (
    <CanvasCard
      graph={graph}
      highlight={highlight}
      onNodeClick={inspectNode}
      open={canvasOpen}
      onToggle={() => setCanvasOpen((shown) => !shown)}
    />
  );

  const body = (
    <>
      {statEnvelope !== null ? (
        <>
          {statEnvelope.run_title !== "" ? (
            <h2 className="font-semibold text-label-1 text-subtitle">{statEnvelope.run_title}</h2>
          ) : null}
          <OutcomeStrip envelope={statEnvelope} liveState={liveState} />
          <div data-section="result">
            <ResultSummary result={statEnvelope.result} recorded={recorded} />
          </div>
          {artifacts.length > 0 || actions.length > 0 ? (
            <section
              data-section="artifacts"
              className="flex shrink-0 flex-col gap-3 rounded-lg border border-line bg-surface p-4 shadow-sm"
            >
              <h2 className="font-semibold text-label-1 text-subtitle">
                What this run left behind
              </h2>
              {artifacts.map((artifact) => (
                <ArtifactCard key={artifact.id} runId={statEnvelope.run_id} artifact={artifact} />
              ))}
              <ArtifactActions
                runId={statEnvelope.run_id}
                actions={actions}
                artifacts={artifacts}
                handlers={{
                  recordOwnerReview: (request) =>
                    setReviewing({
                      actionId: request.actionId,
                      runId: statEnvelope.run_id,
                      refs: request.refs,
                    }),
                  saveNote: (request) => {
                    // The value is a proposed note or it is nothing this can act on. Reading it
                    // here means the surface only ever opens over a draft, and a value of some
                    // other shape says so at the press rather than inside the form.
                    const proposed = NoteDraftSchema.safeParse(request.document.value);
                    if (!proposed.success) {
                      throw new Error("This value is not a proposed note, so there is nothing to keep.");
                    }
                    setKeeping({
                      actionId: request.actionId,
                      origin: request.document.ref,
                      proposed: proposed.data,
                    });
                  },
                }}
                openActionId={reviewing?.actionId ?? keeping?.actionId ?? null}
              />
            </section>
          ) : null}
          <SpendReport runId={statEnvelope.run_id} />
        </>
      ) : (
        <p className="text-body text-label-3">No run yet. Run it from the panel on the right.</p>
      )}
      {canvas}
      {statEnvelope !== null ? (
        <>
          <GateTrail
            nodes={graph.nodes}
            envelope={statEnvelope}
            waitingAt={highlight?.waitingAt ?? null}
            events={footerEvents}
            pending={pendingGate}
            onDecide={(approve, feedback) => decide.mutate({ approve, feedback })}
            deciding={decide.isPending}
          />
          <TimelineCard
            events={footerEvents}
            nodes={graph.nodes}
            envelope={statEnvelope}
            onSelectNode={openNode}
          />
        </>
      ) : null}
    </>
  );

  return (
    <>
      <InspectorPage
        title={title}
        header={detail.data ? <WorkflowHeader detail={detail.data} /> : undefined}
        body={body}
        side={
          <div className="space-y-3">
            {detail.data ? (
              <RunItCard
                detail={detail.data}
                sessionId={sessionId}
                viewedRunId={viewedRunId}
                onSchedule={() => setScheduleOpen(true)}
              />
            ) : null}
            {sessionId !== null ? (
              <Button
                variant="secondary"
                onClick={() => setChatOpen(true)}
                className="h-8 w-full text-caption"
              >
                Open chat
              </Button>
            ) : null}
            {detail.data ? (
              <RunEvidencePanel
                trust={detail.data.trust}
                gateDecisions={
                  statEnvelope
                    ? {
                        approved: statEnvelope.gates_approved,
                        orchestrated: statEnvelope.gates_orchestrated,
                        auto: statEnvelope.gates_auto,
                      }
                    : undefined
                }
                ownerReviews={ownerReviews}
              />
            ) : null}
            <RunValuesPanel nodes={graph.nodes} values={activeValues} onSelectNode={openNode} />
            <HistoryRail
              runs={history.data ?? []}
              selectedRunId={viewedRunId}
              onSelect={(run) => selectRun(run.outcome === "live" ? null : run.run_id)}
              onTogglePin={(run) => pin.mutate(run)}
              pinningId={pinningId}
              onDeleteRequest={(run) => setDeleteTarget(run)}
            />
          </div>
        }
      />
      {inspected !== null && inspectedIsAgentic && (isHistory || sessionId !== null) ? (
        <StepChatDrawer
          key={inspected.id}
          sessionId={isHistory ? null : sessionId}
          recordConversations={isHistory ? recordConversations : null}
          node={inspected}
          open
          onClose={() => setInspected(null)}
        />
      ) : inspected !== null && inspectedText !== null ? (
        <TextViewer
          open
          title={`Step ${inspected.id}`}
          text={inspectedText}
          onClose={() => setInspected(null)}
        />
      ) : null}
      <ScheduleForm
        open={scheduleOpen}
        workflowId={id}
        submitting={createSchedule.isPending}
        error={createSchedule.isError ? studioErrorMessage(createSchedule.error) : null}
        onSubmit={(payload) => createSchedule.mutate(payload)}
        onCancel={() => setScheduleOpen(false)}
      />
      <RunChatDrawer
        sessionId={sessionId ?? ""}
        open={chatOpen}
        onClose={() => setChatOpen(false)}
      />
      {reviewing !== null ? (
        <OwnerReviewForm
          runId={reviewing.runId}
          refs={reviewing.refs}
          onClose={() => setReviewing(null)}
        />
      ) : null}
      {keeping !== null ? (
        <SaveArtifactNote
          origin={keeping.origin}
          proposed={keeping.proposed}
          onClose={() => setKeeping(null)}
        />
      ) : null}
      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete this run?"
        message="Its record, values, and events go with it."
        confirmLabel="Delete"
        destructive
        confirming={remove.isPending}
        onConfirm={() => {
          if (deleteTarget) remove.mutate(deleteTarget);
        }}
        onCancel={() => setDeleteTarget(null)}
      />
    </>
  );
}

export default function WorkflowPage(): ReactElement {
  const params = useParams<{ id: string }>();
  const id = decodeURIComponent(params.id);
  return (
    <Suspense fallback={null}>
      <WorkflowView id={id} />
    </Suspense>
  );
}
