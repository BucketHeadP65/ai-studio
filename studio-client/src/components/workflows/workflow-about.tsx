"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Badge } from "client-core/ui/badge";
import { Button } from "client-core/ui/button";
import { ConfirmDialog } from "client-core/ui/confirm-dialog";
import { Input } from "client-core/ui/input";
import { Textarea } from "client-core/ui/textarea";
import { Archive, ArchiveRestore, History, Play } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { ReactElement, ReactNode } from "react";
import { useEffect, useState } from "react";

import { runKeys } from "@/api/query-keys";
import { refusedReadiness, studioApi, studioErrorMessage } from "@/api/studio";
import type {
  GraphNode,
  Readiness,
  ReadinessField,
  Trust,
  WorkflowDetail,
  WorkflowGraph,
} from "@/api/studio-types";
import { NodeGlyph } from "@/components/marks/node-glyph";
import { ARCHIVE_CONFIRM } from "@/components/workflows/archive-copy";
import { BeforeRunDialog } from "@/components/workflows/before-run-dialog";
import { CollapsibleCard } from "@/components/workflows/collapsible-card";
import { FieldFinding, ReadinessNotice } from "@/components/workflows/readiness-findings";
import { SchemaTable } from "@/components/workflows/schema-table";
import { StepSpine, stepRowId } from "@/components/workflows/step-spine";
import { TrustChip } from "@/components/workflows/trust-chip";
import { useDeliberateLaunch } from "@/components/workflows/use-launch";
import { type Orientation, WorkflowCanvas } from "@/components/workflows/workflow-canvas";
import { levels, shapeCounts } from "@/components/workflows/workflow-order";
import { spendText } from "@/lib/cost-text";
import { runHref } from "@/lib/workflow-href";

// The whole "what it does" page body: what this workflow is (its own description, applicability,
// shape, and what it carries or leans on this app for), what it takes to start one, its steps in
// order (`StepSpine`), and what a run of it hands back. Never a run's own history or state --
// that reading stays the run inspector's, this page reads the artifact itself.

export interface WorkflowAboutProps {
  detail: WorkflowDetail;
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

// "nothing yet" while no run of this workflow has been priced, otherwise the running total
// over however many runs were.
function spendFact(detail: WorkflowDetail): string {
  const spend = detail.spend;
  if (spend === null || spend === undefined || spend.runs === 0) return "nothing yet";
  return `${spendText(spend.cost_usd, spend.unpriced)} over ${plural(spend.runs, "run")}`;
}

function runDisabledTitle(detail: WorkflowDetail): string | undefined {
  if (detail.fits) return undefined;
  const n = detail.missing.length;
  return `Cannot run, ${plural(n, "step")} ${n === 1 ? "is" : "are"} missing.`;
}

// A parameter's own signature still carries a " (optional)" suffix for a reader of the raw string.
// The type column shows the bare type, and whether an answer is owed is read off `required`, which
// the graph payload carries as its own boolean.
function paramType(signature: string): string {
  return signature.endsWith(" (optional)") ? signature.slice(0, -" (optional)".length) : signature;
}

// The `work` of every run or map node this app itself carries out -- not brought along by the
// workflow, and not unknown to this registry -- deduplicated in first-seen order.
function providedSteps(nodes: GraphNode[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const node of nodes) {
    if ((node.kind !== "run" && node.kind !== "map") || node.brought || node.unknown) continue;
    const work = node.work;
    if (!work || seen.has(work)) continue;
    seen.add(work);
    result.push(work);
  }
  return result;
}

const MAP_CARD_ID = "workflow-about-map";
const PARAMETERS_CARD_ID = "workflow-about-parameters";

// Where a node's click lands on the page: a step's own row on the spine, or the parameters
// card for a param node, which has no row.
function targetIdFor(node: GraphNode): string {
  return node.kind === "param" ? PARAMETERS_CARD_ID : stepRowId(node.id);
}

function scrollToId(id: string): void {
  const target = document.getElementById(id);
  if (target !== null && typeof target.scrollIntoView === "function") {
    target.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

// The frame the map draws in: room for one row per stage plus the parameters, within the
// viewport. The same whichever way the ranks run, since the drawing wraps to the frame's shape
// either way; a shorter frame would only shrink it.
function mapHeight(graph: WorkflowGraph): string {
  const rows = Math.max(3, levels(graph).length + 1);
  return `clamp(560px, ${rows * 200}px, 80vh)`;
}

// The same canvas the run inspector draws, without a run over it, so the shape of the workflow
// reads at a glance before the spine reads it in full. Full screen mounts a second canvas over
// the whole viewport (Esc or Close leaves it); a node click there leaves it too, then jumps.
function MapCard({
  graph,
  title,
  onJump,
}: {
  graph: WorkflowGraph;
  title: string;
  onJump: (node: GraphNode) => void;
}): ReactElement {
  const [fullScreen, setFullScreen] = useState(false);
  // The inline pick rides into full screen: both canvases share this one choice.
  const [orientation, setOrientation] = useState<Orientation>("auto");

  useEffect(() => {
    if (!fullScreen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setFullScreen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fullScreen]);

  function jump(node: GraphNode) {
    setFullScreen(false);
    onJump(node);
  }

  return (
    <>
      <CollapsibleCard
        id={MAP_CARD_ID}
        title="The map"
        aside={
          <>
            <span className="text-caption text-label-3">Click a node to read its step.</span>
            <Button
              variant="secondary"
              onClick={() => setFullScreen(true)}
              className="h-8 px-3 text-caption"
            >
              Full screen
            </Button>
          </>
        }
      >
        <div className="relative bg-ground" style={{ height: mapHeight(graph) }}>
          <WorkflowCanvas
            graph={graph}
            onNodeClick={jump}
            className="absolute inset-0"
            preferred={orientation}
            onPreferredChange={setOrientation}
          />
        </div>
      </CollapsibleCard>
      {fullScreen ? (
        // The bare drawing, edge to edge: nothing on it, Esc leaves. Tooling for this view
        // belongs in a drawer that stays hidden until asked for, not in standing chrome.
        <dialog
          open
          aria-modal="true"
          aria-label={`The map, full screen: ${title}. Esc leaves.`}
          className="fixed inset-0 z-50 m-0 h-screen w-screen max-h-none max-w-none border-0 bg-ground p-0 text-label-1"
        >
          <WorkflowCanvas
            graph={graph}
            onNodeClick={jump}
            chrome="none"
            className="absolute inset-0"
            preferred={orientation}
          />
        </dialog>
      ) : null}
    </>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }): ReactElement {
  return (
    <div className="flex flex-col gap-1">
      <span className="font-semibold text-label-3 text-micro uppercase tracking-caps">{label}</span>
      {children}
    </div>
  );
}

// The one screen a person answers the save gate from: the card (title, summary, when to use it)
// and the author's own four sentences, all edited in place through the card route. The spec itself
// is never touched here. What the card still lacks is shown from the backend's own reading, the
// refusal's when there was one, so the form never disagrees with the gate.
//
// Each of the four boxes is labelled with the question it answers, and its finding sits under it.
type SentenceField = Extract<
  ReadinessField,
  "purpose" | "produces" | "owner_participation" | "not_for"
>;

const SENTENCES = [
  ["purpose", "What it is for"],
  ["produces", "What a run hands you"],
  ["owner_participation", "What you do during a run"],
  ["not_for", "What it is not for"],
] as const satisfies readonly (readonly [SentenceField, string])[];

function CardEditor({
  detail,
  onDone,
}: {
  detail: WorkflowDetail;
  onDone: () => void;
}): ReactElement {
  const queryClient = useQueryClient();
  const [title, setTitle] = useState(detail.title);
  const [summary, setSummary] = useState(detail.summary);
  const [applicability, setApplicability] = useState(detail.applicability);
  const [purpose, setPurpose] = useState(detail.presentation?.purpose ?? "");
  const [produces, setProduces] = useState(detail.presentation?.produces ?? "");
  const [participation, setParticipation] = useState(
    detail.presentation?.owner_participation ?? "",
  );
  const [notFor, setNotFor] = useState(detail.presentation?.not_for ?? "");
  const boxes: Record<SentenceField, { value: string; set: (next: string) => void }> = {
    purpose: { value: purpose, set: setPurpose },
    produces: { value: produces, set: setProduces },
    owner_participation: { value: participation, set: setParticipation },
    not_for: { value: notFor, set: setNotFor },
  };

  const save = useMutation({
    mutationFn: () =>
      studioApi.redescribeWorkflow(detail.id, {
        title,
        summary,
        applicability,
        presentation: {
          purpose,
          produces,
          owner_participation: participation,
          not_for: notFor,
        },
      }),
    onSuccess: (card) => {
      queryClient.setQueryData<WorkflowDetail>(["studio", "workflow", detail.id], (previous) =>
        previous ? { ...previous, ...card } : previous,
      );
      queryClient.invalidateQueries({ queryKey: ["library"] });
      onDone();
    },
  });
  const reading: Readiness | null = save.isError
    ? refusedReadiness(save.error)
    : (detail.readiness ?? null);

  return (
    <div className="flex max-w-2xl flex-col gap-3">
      <Field label="Title">
        <Input
          aria-label="Title"
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          className="h-8 text-body"
        />
        <FieldFinding readiness={reading} field="title" />
      </Field>
      <Field label="Summary">
        <Textarea
          aria-label="Summary"
          value={summary}
          onChange={(event) => setSummary(event.target.value)}
          rows={2}
          className="text-body"
        />
        <FieldFinding readiness={reading} field="summary" />
      </Field>
      <Field label="Applicability">
        <Textarea
          aria-label="Applicability"
          value={applicability}
          onChange={(event) => setApplicability(event.target.value)}
          rows={3}
          className="text-body"
        />
      </Field>
      {SENTENCES.map(([field, label]) => (
        <Field key={field} label={label}>
          <Textarea
            aria-label={label}
            value={boxes[field].value}
            onChange={(event) => boxes[field].set(event.target.value)}
            rows={2}
            className="text-body"
          />
          <FieldFinding readiness={reading} field={field} />
        </Field>
      ))}
      <ReadinessNotice
        readiness={reading}
        exclude={["title", "summary", "purpose", "produces", "owner_participation", "not_for"]}
      />
      {save.isError ? (
        <p className="text-caption text-danger-ink">{studioErrorMessage(save.error)}</p>
      ) : null}
      <div className="flex gap-2">
        <Button
          onClick={() => save.mutate()}
          disabled={save.isPending}
          className="h-8 px-3 text-caption"
        >
          {save.isPending ? "Saving" : "Save"}
        </Button>
        <Button variant="secondary" onClick={onDone} className="h-8 px-3 text-caption">
          Cancel
        </Button>
      </div>
    </div>
  );
}

// The author's own four sentences, each under the question it answers. Only the ones somebody
// wrote: an empty field draws nothing, because nothing here composes prose a card does not carry.
function AuthorsWords({ detail }: { detail: WorkflowDetail }): ReactElement | null {
  const words = detail.presentation;
  if (!words) return null;
  const sections: [string, string][] = [
    ["What it is for", words.purpose],
    ["What a run hands you", words.produces],
    ["What you do during a run", words.owner_participation],
    ["What it is not for", words.not_for],
  ];
  const written = sections.filter(([, value]) => value !== "");
  if (written.length === 0) return null;
  return (
    <div className="flex flex-col gap-3">
      {written.map(([label, value]) => (
        <div key={label} className="flex flex-col gap-1">
          <span className="text-label-3 text-micro uppercase tracking-caps">{label}</span>
          <p className="text-body text-label-2">{value}</p>
        </div>
      ))}
    </div>
  );
}

function Chip({ text, tone }: { text: string; tone?: "warning" }): ReactElement {
  return (
    <span
      className={
        tone === "warning"
          ? "rounded-full border border-warn px-2.5 py-1 font-mono text-micro text-warn-ink"
          : "rounded-full border border-line px-2.5 py-1 font-mono text-label-3 text-micro"
      }
    >
      {text}
    </span>
  );
}

function Fact({ label, value }: { label: string; value: ReactElement | string }): ReactElement {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-label-3 text-micro uppercase tracking-caps">{label}</span>
      <span className="font-mono text-body text-label-1">{value}</span>
    </div>
  );
}

function ChipGroup({
  label,
  chips,
  tone,
}: {
  label: string;
  chips: string[];
  tone?: "warning";
}): ReactElement | null {
  if (chips.length === 0) return null;
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-label-3 text-micro uppercase tracking-caps">{label}</span>
      <div className="flex flex-wrap gap-1.5">
        {chips.map((chip) => (
          <Chip key={chip} text={chip} tone={tone} />
        ))}
      </div>
    </div>
  );
}

export function WorkflowAbout({ detail }: WorkflowAboutProps): ReactElement {
  const router = useRouter();
  const queryClient = useQueryClient();

  const [setupOpen, setSetupOpen] = useState(false);
  // The same one-press, never-retried start the run inspector's controls make, so a launch means
  // the same thing from either screen: one request, and a lost answer settled by reading the runs
  // rather than by sending it again.
  const launch = useDeliberateLaunch({
    sessionTitle: detail.title || detail.id,
    onStarted: () => {
      setSetupOpen(false);
      queryClient.invalidateQueries({ queryKey: runKeys.live });
      router.push(runHref(detail.id));
    },
    onReconciled: () => {
      setSetupOpen(false);
      queryClient.invalidateQueries({ queryKey: runKeys.live });
      router.push(runHref(detail.id));
    },
  });

  function onApproved(trust: Trust) {
    queryClient.setQueryData<WorkflowDetail>(["studio", "workflow", detail.id], (previous) =>
      previous ? { ...previous, trust } : previous,
    );
  }

  // Archiving takes the workflow out of the library and search and keeps its
  // runs and versions; Restore puts it back. The library has no erase, this is the owner's remove.
  const [archiveOpen, setArchiveOpen] = useState(false);
  function setArchivedAt(archivedAt: string | null) {
    queryClient.setQueryData<WorkflowDetail>(["studio", "workflow", detail.id], (previous) =>
      previous ? { ...previous, archived_at: archivedAt } : previous,
    );
    queryClient.invalidateQueries({ queryKey: ["library"] });
  }
  const archive = useMutation({
    mutationFn: () => studioApi.archiveWorkflow(detail.id),
    onSuccess: (card) => {
      setArchiveOpen(false);
      setArchivedAt(card.archived_at);
    },
  });
  const restore = useMutation({
    mutationFn: () => studioApi.restoreWorkflow(detail.id),
    onSuccess: (card) => setArchivedAt(card.archived_at),
  });

  // A jump from the map pushes a history entry with the row's own hash, so Back returns to the
  // map (or to whichever earlier jump is now on top) instead of leaving the page.
  useEffect(() => {
    const onPop = () => {
      const hash = window.location.hash.slice(1);
      scrollToId(hash !== "" ? hash : MAP_CARD_ID);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  function jumpToNode(node: GraphNode) {
    const id = targetIdFor(node);
    window.history.pushState(null, "", `#${id}`);
    scrollToId(id);
  }

  const [editing, setEditing] = useState(false);
  const shape = shapeCounts(detail.graph);
  const paramNodes = detail.graph.nodes.filter((node) => node.kind === "param");
  const outputNode = detail.graph.nodes.find((node) => node.is_output) ?? null;
  const provided = providedSteps(detail.graph.nodes);
  const runTitle = runDisabledTitle(detail);

  return (
    <>
      <div className="flex h-[70px] items-center gap-3 border-line border-b bg-surface px-7">
        <div className="min-w-0">
          <h1 className="truncate font-bold text-label-1 text-title">
            {detail.title || detail.id}
          </h1>
          <p className="truncate text-caption text-label-3">{detail.summary}</p>
        </div>
        {/* One compact tool cluster, symbols first: history, archive/restore, then play. Every
            symbol carries its name for hover and the screen reader; the play stays the one
            filled control on the page. */}
        <div className="ml-auto flex items-center gap-1.5">
          <Link
            href={runHref(detail.id)}
            title="Latest run"
            aria-label="Latest run"
            className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-label-2 hover:bg-well"
          >
            <History aria-hidden className="h-4.5 w-4.5" />
          </Link>
          {detail.archived_at !== null ? (
            <>
              <Badge tone="neutral">archived</Badge>
              <button
                type="button"
                onClick={() => restore.mutate()}
                disabled={restore.isPending}
                title="Restore from the archive"
                aria-label="Restore from the archive"
                className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-label-2 hover:bg-well disabled:opacity-50"
              >
                <ArchiveRestore aria-hidden className="h-4.5 w-4.5" />
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                onClick={() => setArchiveOpen(true)}
                title="Archive"
                aria-label="Archive"
                className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-label-2 hover:bg-well"
              >
                <Archive aria-hidden className="h-4.5 w-4.5" />
              </button>
              <button
                type="button"
                onClick={() => {
                  launch.reset();
                  setSetupOpen(true);
                }}
                disabled={launch.starting || !detail.fits}
                title={runTitle ?? "Run"}
                aria-label="Run"
                className="ml-1 inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-label-1 text-surface hover:opacity-90 disabled:opacity-40"
              >
                <Play aria-hidden className="h-4.5 w-4.5 fill-current" />
              </button>
            </>
          )}
        </div>
      </div>
      <ConfirmDialog
        open={archiveOpen}
        title={ARCHIVE_CONFIRM.title}
        message={ARCHIVE_CONFIRM.message}
        confirmLabel={ARCHIVE_CONFIRM.confirmLabel}
        confirming={archive.isPending}
        onConfirm={() => archive.mutate()}
        onCancel={() => setArchiveOpen(false)}
      />

      <BeforeRunDialog
        open={setupOpen}
        workflowId={detail.id}
        workflowTitle={detail.title || detail.id}
        detail={detail}
        submitting={launch.starting}
        error={launch.error}
        unknownReason={launch.unknownReason}
        onReconcile={launch.reconcile}
        reconciling={launch.reconciling}
        settledAsNothing={launch.settledAsNothing}
        onConfirm={(intent) => launch.start(intent)}
        onCancel={() => setSetupOpen(false)}
      />

      <div className="flex flex-col gap-4 px-7 pt-5 pb-7">
        <MapCard graph={detail.graph} title={detail.title || detail.id} onJump={jumpToNode} />

        <CollapsibleCard
          title="What it does"
          aside={
            <>
              <TrustChip workflowId={detail.id} trust={detail.trust} onApproved={onApproved} />
              {editing ? null : (
                <Button
                  variant="secondary"
                  onClick={() => setEditing(true)}
                  className="h-8 px-3 text-caption"
                >
                  Edit
                </Button>
              )}
            </>
          }
          bodyClassName="flex flex-col gap-4 px-5 py-4"
        >
          {editing ? <CardEditor detail={detail} onDone={() => setEditing(false)} /> : null}
          <p className="text-body text-label-1">{detail.description || "No description yet."}</p>
          <AuthorsWords detail={detail} />
          {editing ? null : <ReadinessNotice readiness={detail.readiness} />}
          {detail.applicability !== "" ? (
            <div className="flex flex-col gap-1">
              <span className="text-label-3 text-micro uppercase tracking-caps">Use it when</span>
              <p className="text-body text-label-2">{detail.applicability}</p>
            </div>
          ) : null}
          <div className="flex flex-wrap items-center gap-6">
            <Fact
              label="Steps"
              value={
                <span className="inline-flex items-center gap-1.5">
                  <NodeGlyph kind="step" size={14} />
                  {shape.steps}
                </span>
              }
            />
            <Fact
              label="Person gates"
              value={
                <span className="inline-flex items-center gap-1.5">
                  <NodeGlyph kind="person-gate" size={14} />
                  {shape.person_gates}
                </span>
              }
            />
            <Fact
              label="Orchestrator gates"
              value={
                <span className="inline-flex items-center gap-1.5">
                  <NodeGlyph kind="agent-gate" size={14} />
                  {shape.agent_gates}
                </span>
              }
            />
            <Fact label="Version" value={`v${detail.version}`} />
            <Fact label="Runs completed" value={String(detail.trust.runs_with_evidence)} />
            <Fact label="Spend" value={spendFact(detail)} />
          </div>
          <ChipGroup label="Steps it carries" chips={detail.brings} />
          <ChipGroup label="Steps this app provides" chips={provided} />
          <ChipGroup label="Missing here" chips={detail.missing} tone="warning" />
        </CollapsibleCard>

        <CollapsibleCard
          id={PARAMETERS_CARD_ID}
          title="What it takes"
          aside={
            <span className="text-caption text-label-3">
              {plural(paramNodes.length, "parameter")}
            </span>
          }
          bodyClassName="px-5 py-4"
        >
          {paramNodes.length === 0 ? (
            <p className="text-body text-label-3">This workflow takes no parameters.</p>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-line">
              <table className="w-full text-caption">
                <thead>
                  <tr className="border-line border-b bg-well">
                    <th className="px-3 py-1.5 text-left font-semibold text-label-3 text-micro uppercase tracking-caps">
                      Parameter
                    </th>
                    <th className="px-3 py-1.5 text-left font-semibold text-label-3 text-micro uppercase tracking-caps">
                      Type
                    </th>
                    <th className="px-3 py-1.5 text-left font-semibold text-label-3 text-micro uppercase tracking-caps" />
                    <th className="px-3 py-1.5 text-left font-semibold text-label-3 text-micro uppercase tracking-caps">
                      Meaning
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {paramNodes.map((param) => (
                    <tr key={param.id} className="border-line border-b last:border-b-0">
                      <td className="px-3 py-1.5 font-mono text-label-1">{param.id}</td>
                      <td className="px-3 py-1.5 font-mono text-label-2">
                        {paramType(param.signature)}
                      </td>
                      <td className="px-3 py-1.5 text-label-3">
                        {param.required ? "required" : "optional"}
                      </td>
                      <td className="px-3 py-1.5 text-label-2">{param.detail}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CollapsibleCard>

        <StepSpine graph={detail.graph} />

        {outputNode !== null ? (
          <CollapsibleCard
            title="What a run hands back"
            aside={
              <span className="text-caption text-label-3">
                {outputNode.kind === "param" ? (
                  <>
                    the parameter <span className="font-mono">{outputNode.id}</span>, unchanged
                  </>
                ) : (
                  <>
                    from the step <span className="font-mono">{outputNode.id}</span>
                  </>
                )}
              </span>
            }
            bodyClassName="px-5 py-4"
          >
            {outputNode.unknown ? (
              <p className="text-body text-label-3">
                This comes from <span className="font-mono">{outputNode.id}</span>, which this app
                cannot run.
              </p>
            ) : (
              <SchemaTable schema={outputNode.output_schema} />
            )}
          </CollapsibleCard>
        ) : null}
      </div>
    </>
  );
}
