"use client";

import { Badge } from "client-core/ui/badge";
import type { ReactElement, ReactNode } from "react";

import type { GraphNode, MapTurn, WorkflowGraph } from "@/api/studio-types";
import { type GlyphKind, NodeGlyph } from "@/components/marks/node-glyph";
import { StatePill } from "@/components/marks/state-pill";
import { CollapsibleCard } from "@/components/workflows/collapsible-card";
import { SchemaTable } from "@/components/workflows/schema-table";
import {
  executionOrder,
  levels,
  passesTo,
  rejectedTo,
  typeLabel,
  wiresInto,
} from "@/components/workflows/workflow-order";

// The card "The steps, in the order they run": one row per node from `executionOrder`, a
// vertical spine down the left threading through each glyph, top to bottom. A run or map row
// reads its own inputs, tools, briefing, carried program, and output shape; a gate row (a
// person's own gate, the orchestrator's, or a judge's) reads what it reviews, where a rejection
// reruns from, and what an approval passes on instead -- two different bodies under one shared
// head.

export interface StepSpineProps {
  graph: WorkflowGraph;
}

// The DOM id of one step's row, so the map above the spine can jump a reader to it.
export function stepRowId(nodeId: string): string {
  return `step-${nodeId}`;
}

const GATE_KINDS = new Set(["gate", "orchestrator-gate", "judge"]);

function glyphKind(kind: GraphNode["kind"]): GlyphKind {
  if (kind === "gate") return "person-gate";
  if (kind === "judge" || kind === "orchestrator-gate") return "agent-gate";
  return "step";
}

function Mono({ children }: { children: string }): ReactElement {
  return <span className="font-mono">{children}</span>;
}

function IdChip({ text }: { text: string }): ReactElement {
  return (
    <span className="rounded-md border border-line bg-well px-1.5 py-0.5 font-mono text-caption text-label-2">
      {text}
    </span>
  );
}

function QuoteBlock({ text }: { text: string }): ReactElement {
  return (
    <p className="whitespace-pre-wrap rounded-lg border border-line bg-well px-3 py-2 text-body text-label-1">
      {text}
    </p>
  );
}

function ProgramBlock({ text }: { text: string }): ReactElement {
  return (
    <pre className="overflow-x-auto whitespace-pre rounded-lg bg-label-1 px-3 py-2 font-mono text-caption text-surface">
      {text}
    </pre>
  );
}

function Section({ label, children }: { label: string; children: ReactNode }): ReactElement {
  return (
    <div className="flex flex-col gap-1 sm:flex-row sm:gap-4">
      <span className="shrink-0 pt-0.5 text-label-3 text-micro uppercase tracking-caps sm:w-28">
        {label}
      </span>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}

// The rows grouped by stage: every node of one stage may start as soon as the stage before it
// is done, and the nodes within a stage run side by side. Within a stage, rows keep the order
// `executionOrder` gives them, so the spine still reads top to bottom.
function stages(graph: WorkflowGraph): GraphNode[][] {
  const position = new Map(executionOrder(graph).map((node, index) => [node.id, index]));
  return levels(graph).map((group) =>
    [...group].sort((a, b) => (position.get(a.id) ?? 0) - (position.get(b.id) ?? 0)),
  );
}

// The word above one stage's rows: how it follows the stage before, and whether its nodes fan out.
function stageWord(index: number, size: number): string {
  const lead = index === 0 ? "First" : "Then";
  return size > 1 ? `${lead}, these ${size} run side by side` : lead;
}

// The card head's own right side caption: the stage count, then the first stage that fans out
// (named), or the fact that nothing does.
function shapeCaption(groups: GraphNode[][]): ReactElement {
  const count = `${groups.length} ${groups.length === 1 ? "stage" : "stages"}`;
  const parallel = groups.find((group) => group.length > 1);
  if (!parallel) return <>{groups.length === 1 ? count : `${count}, one after another`}</>;
  return (
    <>
      {`${count}, `}
      {parallel.map((n, index) => (
        <span key={n.id}>
          {index > 0 ? " and " : ""}
          <Mono>{n.id}</Mono>
        </span>
      ))}
      {" run side by side"}
    </>
  );
}

function KindSentence({ node, graph }: { node: GraphNode; graph: WorkflowGraph }): ReactElement {
  if (node.kind === "gate") return <>A person approves.</>;
  if (node.kind === "orchestrator-gate")
    return <>The orchestrator decides, no person approves it.</>;
  if (node.kind === "judge") {
    return (
      <>
        A step decides, <Mono>{node.work ?? ""}</Mono>, no person approves it.
      </>
    );
  }

  if (node.unknown) {
    return (
      <>
        This app has no step called <Mono>{node.work ?? ""}</Mono>.
      </>
    );
  }

  const work = <Mono>{node.work ?? ""}</Mono>;
  let lead: ReactElement;
  if (node.source !== "") {
    lead = <>Runs a program the workflow carries, {work}</>;
  } else if (node.briefing !== "") {
    lead = <>Briefs an agent, {work}</>;
  } else if (node.brought) {
    lead = <>Runs an agent the workflow carries, {work}</>;
  } else {
    lead = <>Runs a step this app provides, {work}</>;
  }

  if (node.kind !== "map") return <>{lead}.</>;

  const over = wiresInto(graph, node.id).find((wire) => wire.each)?.from ?? "";
  return (
    <>
      {lead}, once for every item of <Mono>{over}</Mono>, side by side.
    </>
  );
}

function GateDescription({ node, graph }: { node: GraphNode; graph: WorkflowGraph }): ReactElement {
  const reviews = wiresInto(graph, node.id)[0];
  const from = <Mono>{reviews?.from ?? ""}</Mono>;
  const to = <Mono>{rejectedTo(graph, node.id) ?? ""}</Mono>;

  if (node.kind === "orchestrator-gate") {
    return (
      <>
        This run's orchestrator judges what {from} produced against the charge below. Approved, it
        passes through unchanged. Rejected, the run goes back to {to} and runs again from there.
      </>
    );
  }
  if (node.kind === "judge") {
    return (
      <>
        The step <Mono>{node.work ?? ""}</Mono> judges what {from} produced. Approved, it passes
        through unchanged. Rejected, the run goes back to {to} and runs again from there.
      </>
    );
  }
  return (
    <>
      The run stops here until a person approves or rejects what {from} produced. Approved, it
      passes through unchanged. Rejected, the run goes back to {to} and runs again from there.
    </>
  );
}

function RowHead({ node, graph }: { node: GraphNode; graph: WorkflowGraph }): ReactElement {
  const hasTags = node.brought || node.is_output || node.unknown;
  return (
    <div className="flex flex-col gap-1">
      <h3 className="font-semibold text-label-1 text-subtitle">
        <Mono>{node.id}</Mono>
      </h3>
      <p className="text-body text-label-2">
        <KindSentence node={node} graph={graph} />
      </p>
      {hasTags ? (
        <div className="flex flex-wrap items-center gap-2">
          {node.brought ? <Badge tone="neutral">carried</Badge> : null}
          {node.is_output ? (
            <span className="rounded-full bg-label-1 px-2.5 py-1 font-semibold text-micro text-surface">
              The run's result
            </span>
          ) : null}
          {node.unknown ? (
            <StatePill tone="warning" size="sm">
              Missing here
            </StatePill>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function TakesLine({
  wire,
  inputSchemas,
}: {
  wire: ReturnType<typeof wiresInto>[number];
  inputSchemas: GraphNode["input_schemas"];
}): ReactElement {
  const schema = inputSchemas[wire.arg];
  const type = schema ? typeLabel(schema) : null;

  let phrase: ReactElement;
  if (wire.each) {
    phrase = (
      <>
        {type !== null ? (
          <>
            one <Mono>{type}</Mono>,{" "}
          </>
        ) : null}
        each item of
      </>
    );
  } else if (wire.fromKind === "parameter") {
    phrase = (
      <>
        {type !== null ? (
          <>
            <Mono>{type}</Mono>,{" "}
          </>
        ) : null}
        from the parameter
      </>
    );
  } else {
    phrase = (
      <>
        {type !== null ? (
          <>
            <Mono>{type}</Mono>,{" "}
          </>
        ) : null}
        from
      </>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-1.5 text-body text-label-2">
      <IdChip text={wire.arg} />
      <span>{phrase}</span>
      <IdChip text={wire.from} />
    </div>
  );
}

// One turn a map step chains after its first, read inside the same row rather than a stage of
// its own: that turn's own work name, its briefing or carried program in the same style the
// primary step uses, its tools, and the single-item shape it produces, per item, before the map
// collects the results. `turnNumber` is the turn's 1-based position in the map (the first extra
// turn is turn 2), so a map with several extra turns never shows the same label twice.
function TurnBlock({ turn, turnNumber }: { turn: MapTurn; turnNumber: number }): ReactElement {
  return (
    <Section label={`Turn ${turnNumber}, for each result`}>
      <div className="flex flex-col gap-3 rounded-lg border border-line bg-well px-3 py-2.5">
        <p className="text-body text-label-1">
          <Mono>{turn.work}</Mono>
        </p>
        {turn.tools.length > 0 ? (
          <Section label="Tools">
            <div className="flex flex-wrap gap-1.5">
              {turn.tools.map((tool) => (
                <IdChip key={tool} text={tool} />
              ))}
            </div>
          </Section>
        ) : null}
        {turn.briefing !== "" ? (
          <Section label="The briefing">
            <QuoteBlock text={turn.briefing} />
          </Section>
        ) : null}
        {turn.source !== "" ? (
          <Section label="The program">
            <ProgramBlock text={turn.source} />
          </Section>
        ) : null}
        <Section label="Produces">
          <SchemaTable schema={turn.output_schema} />
        </Section>
      </div>
    </Section>
  );
}

function StepRow({ node, graph }: { node: GraphNode; graph: WorkflowGraph }): ReactElement {
  const wires = wiresInto(graph, node.id);
  return (
    <div className="flex flex-col gap-2.5">
      <RowHead node={node} graph={graph} />
      {/* Only a step this app provides has a description of its own (its docstring). A carried
          step's is the briefing's first paragraph or a generated line, a map's is always
          generated, and an unknown step's says what the head already said -- all repeat the row. */}
      {node.kind === "run" && !node.brought && !node.unknown && node.detail !== "" ? (
        <p className="text-body text-label-2">{node.detail}</p>
      ) : null}
      <div className="flex flex-col gap-3">
        {wires.length > 0 ? (
          <Section label="Takes">
            <div className="flex flex-col gap-1.5">
              {wires.map((wire) => (
                <TakesLine key={wire.arg} wire={wire} inputSchemas={node.input_schemas} />
              ))}
            </div>
          </Section>
        ) : null}
        {node.tools.length > 0 ? (
          <Section label="Tools">
            <div className="flex flex-wrap gap-1.5">
              {node.tools.map((tool) => (
                <IdChip key={tool} text={tool} />
              ))}
            </div>
          </Section>
        ) : null}
        {node.briefing !== "" ? (
          <Section label="The briefing">
            <QuoteBlock text={node.briefing} />
          </Section>
        ) : null}
        {node.source !== "" ? (
          <Section label="The program">
            <ProgramBlock text={node.source} />
          </Section>
        ) : null}
        <Section label="Produces">
          <SchemaTable schema={node.output_schema} />
        </Section>
        {node.kind === "map" && node.turns && node.turns.length > 1
          ? node.turns
              .slice(1)
              .map((turn, index) => (
                <TurnBlock key={turn.node_id} turn={turn} turnNumber={index + 2} />
              ))
          : null}
      </div>
    </div>
  );
}

function GateRow({ node, graph }: { node: GraphNode; graph: WorkflowGraph }): ReactElement {
  const reviewsWire = wiresInto(graph, node.id)[0] ?? null;
  const rejectsTo = rejectedTo(graph, node.id);
  const passTargets = passesTo(graph, node.id);
  const reviewType = node.output_schema !== null ? typeLabel(node.output_schema) : null;

  return (
    <div className="flex flex-col gap-2.5">
      <RowHead node={node} graph={graph} />
      <p className="text-body text-label-2">
        <GateDescription node={node} graph={graph} />
      </p>
      <div className="flex flex-col gap-3">
        {reviewsWire !== null ? (
          <Section label="Reviews">
            <div className="flex flex-wrap items-center gap-1.5 text-body text-label-2">
              <span>
                {reviewType !== null ? (
                  <>
                    <Mono>{reviewType}</Mono>,{" "}
                  </>
                ) : null}
                from
              </span>
              <IdChip text={reviewsWire.from} />
            </div>
          </Section>
        ) : null}
        {node.kind === "orchestrator-gate" && node.briefing !== "" ? (
          <Section label="The charge">
            <QuoteBlock text={node.briefing} />
          </Section>
        ) : null}
        {rejectsTo !== null ? (
          <Section label="Rejected">
            <div className="flex flex-wrap items-center gap-1.5 text-body text-label-2">
              <span>reruns from</span>
              <IdChip text={rejectsTo} />
            </div>
          </Section>
        ) : null}
        <Section label="Passes on">
          {passTargets.length === 0 ? (
            <span className="text-body text-label-2">the same value, as the run's result</span>
          ) : (
            <div className="flex flex-wrap items-center gap-1.5 text-body text-label-2">
              <span>the same value, to</span>
              {passTargets.map((target, index) => (
                <span key={target} className="inline-flex items-center gap-1.5">
                  {index > 0 ? <span>and</span> : null}
                  <IdChip text={target} />
                </span>
              ))}
            </div>
          )}
        </Section>
      </div>
    </div>
  );
}

export function StepSpine({ graph }: StepSpineProps): ReactElement {
  const groups = stages(graph);
  const last = groups.at(-1)?.at(-1) ?? null;

  return (
    <CollapsibleCard
      title="The steps"
      aside={<span className="text-caption text-label-3">{shapeCaption(groups)}</span>}
    >
      <div className="flex flex-col">
        {groups.map((group, stageIndex) => (
          <div key={group[0]?.id ?? stageIndex} className="flex flex-col">
            <div className="grid grid-cols-[44px_1fr] items-center gap-3 border-ground border-t bg-well px-5 py-1.5 first:border-t-0">
              <div className="relative self-stretch">
                {stageIndex > 0 ? (
                  <span
                    aria-hidden
                    className="-inset-y-1.5 absolute left-1/2 w-0.5 -translate-x-1/2 bg-ground"
                  />
                ) : null}
              </div>
              <span className="text-label-3 text-micro uppercase tracking-caps">
                {stageWord(stageIndex, group.length)}
              </span>
            </div>
            {group.map((node) => (
              <div
                key={node.id}
                id={stepRowId(node.id)}
                className="relative grid grid-cols-[44px_1fr] items-start gap-3 border-ground border-t px-5 py-4"
              >
                <div className="-my-4 relative flex self-stretch justify-center pt-4">
                  <span
                    aria-hidden
                    className="absolute inset-y-0 left-1/2 w-0.5 -translate-x-1/2 bg-ground"
                    style={{ bottom: node === last ? "50%" : 0 }}
                  />
                  <span className="relative flex h-6 items-center">
                    <NodeGlyph kind={glyphKind(node.kind)} fill="ran" size={18} />
                  </span>
                </div>
                <div className="min-w-0">
                  {GATE_KINDS.has(node.kind) ? (
                    <GateRow node={node} graph={graph} />
                  ) : (
                    <StepRow node={node} graph={graph} />
                  )}
                </div>
              </div>
            ))}
          </div>
        ))}
      </div>
    </CollapsibleCard>
  );
}
