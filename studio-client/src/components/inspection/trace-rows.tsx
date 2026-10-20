import { Fragment, type ReactElement } from "react";

import { citesOf, sentence, spaced, surfaceLabel } from "@/api/inspection-words";
import type { TraceEntry } from "@/api/inspector-trace";
import { treeText } from "@/api/tree-text";

import { Folded } from "./folded";

// One row of the expanded trace, by entry kind: the label on the left, the content on the
// right. `answered` reads the model's own raw call arguments, whichever of the inspector's
// three answer shapes they are: an attribution (`symptom` + `blames`), a scan (`items`, one
// block per symptom it saw), or a refusal to decide (`reason`).

interface RawBlame {
  rootCause: string;
  surfaceKind: string;
  surfaceName: string;
  why: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function stringField(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  return typeof value === "string" ? value : "";
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === "string") : [];
}

// A blame list, from a `{blames: [...]}` field.
function rawBlames(record: Record<string, unknown>): RawBlame[] {
  if (!Array.isArray(record.blames)) return [];
  return record.blames.filter(isRecord).map((item) => ({
    rootCause: stringField(item, "root_cause"),
    surfaceKind: stringField(item, "surface_kind"),
    surfaceName: stringField(item, "surface_name"),
    why: stringField(item, "why"),
  }));
}

interface RawSeenItem {
  symptom: string;
  spans: string[];
  blames: RawBlame[];
  explanation: string;
}

function rawSeenItems(args: Record<string, unknown>): RawSeenItem[] {
  if (!Array.isArray(args.items)) return [];
  return args.items.filter(isRecord).map((item) => ({
    symptom: stringField(item, "symptom"),
    spans: stringArray(item.spans),
    blames: rawBlames(item),
    explanation: stringField(item, "explanation"),
  }));
}

// Where a blame's fix lands: the named surface, or, when the taxonomy named none, the surface
// kind alone with no trailing space.
function FixLandsOn({ blame }: { blame: RawBlame }): ReactElement {
  if (blame.surfaceName === "") {
    return <>the {surfaceLabel(blame.surfaceKind)}, nothing named to edit</>;
  }
  return (
    <>
      the {surfaceLabel(blame.surfaceKind)} {blame.surfaceName}
    </>
  );
}

// One blame's own two rows (`blame n`, `fix lands on`) and its why and cites, indented under
// it -- shared by the attribution shape and each of a scan's own items.
function BlameRows({ blame, index, spans }: { blame: RawBlame; index: number; spans: string[] }): ReactElement {
  return (
    <Fragment>
      <div className="text-label-3">blame {index + 1}</div>
      <div>{sentence(spaced(blame.rootCause))}</div>
      <div className="pl-3.5 text-label-3">fix lands on</div>
      <div>
        <FixLandsOn blame={blame} />
      </div>
      <div className="pl-3.5 text-label-3">why</div>
      <div>{blame.why}</div>
      <div className="pl-3.5 text-label-3">cites</div>
      <div>{citesOf(blame.why, spans).join(" ")}</div>
    </Fragment>
  );
}

function ExplanationRow({ explanation }: { explanation: string }): ReactElement | null {
  if (explanation === "") return null;
  return (
    <Fragment>
      <div className="text-label-3">explanation</div>
      <div>{explanation}</div>
    </Fragment>
  );
}

// One attribution: `symptom`, then each blame with its why and cites, then the explanation.
function AttributionGrid({ args, spans }: { args: Record<string, unknown>; spans: string[] }): ReactElement {
  const blames = rawBlames(args);
  return (
    <div className="grid grid-cols-[130px_minmax(0,1fr)] gap-x-3.5 gap-y-1 font-mono text-caption">
      <div className="text-label-3">symptom</div>
      <div>{spaced(stringField(args, "symptom"))}</div>
      {blames.map((blame, index) => (
        <BlameRows key={`blame-${index}`} blame={blame} index={index} spans={spans} />
      ))}
      <ExplanationRow explanation={stringField(args, "explanation")} />
    </div>
  );
}

// A scan's own answer: one block per symptom it saw, each `symptom`, its `spans` (mono,
// space-joined), then its blames and explanation the same way an attribution reads them. A
// blame's cites are checked against its own item's spans, not the finding's -- a scan's items
// each name their own set.
function ScanGrid({ items }: { items: RawSeenItem[] }): ReactElement {
  return (
    <div className="grid grid-cols-[130px_minmax(0,1fr)] gap-x-3.5 gap-y-1 font-mono text-caption">
      {items.map((item, itemIndex) => (
        <Fragment key={`item-${itemIndex}`}>
          <div className="text-label-3">symptom</div>
          <div>{spaced(item.symptom)}</div>
          <div className="text-label-3">spans</div>
          <div>{item.spans.join(" ")}</div>
          {item.blames.map((blame, blameIndex) => (
            <BlameRows key={`blame-${blameIndex}`} blame={blame} index={blameIndex} spans={item.spans} />
          ))}
          <ExplanationRow explanation={item.explanation} />
        </Fragment>
      ))}
    </div>
  );
}

// A reason accompanying an explicit dismissal, a clean scan or unresolved uncertainty.
function ReasonGrid({ reason }: { reason: string }): ReactElement {
  return (
    <div className="grid grid-cols-[130px_minmax(0,1fr)] gap-x-3.5 gap-y-1 font-mono text-caption">
      <div className="text-label-3">reason</div>
      <div>{reason}</div>
    </div>
  );
}

function AnsweredGrid({ args, spans }: { args: Record<string, unknown>; spans: string[] }): ReactElement {
  if (Array.isArray(args.items)) return <ScanGrid items={rawSeenItems(args)} />;
  if (typeof args.reason === "string") return <ReasonGrid reason={args.reason} />;
  return <AttributionGrid args={args} spans={spans} />;
}

export function RowLabel({ entry, model, afterAsked }: { entry: TraceEntry; model: string; afterAsked: boolean }): ReactElement {
  switch (entry.kind) {
    case "told":
      return <span className="text-caption text-label-2">What it was told</span>;
    case "call":
      return <span className="font-mono text-caption text-label-2">{entry.line}</span>;
    case "thinking":
      return <span className="text-caption text-label-2">Thinking</span>;
    case "refused":
      return <span className="font-semibold text-caption text-danger-ink">Refused</span>;
    case "answered":
      if (entry.outcome === "unvalidated") {
        return <span className="text-caption text-danger-ink">Output not accepted<span className="mt-0.5 block text-micro text-label-3">no validated result recorded</span></span>;
      }
      return (
        <span className="text-caption text-label-2">
          {entry.outcome === "dismissed" ? "Dismissed" : entry.outcome === "clean" ? "No issue seen" : entry.outcome === "undecided" ? "Undecided" : "Answered"}
          <span className="mt-0.5 block text-micro text-label-3">
            {entry.outcome === "undecided" ? "insufficient evidence" : entry.outcome === "finding" || entry.outcome === undefined ? "diagnosis, pending verification" : "reason recorded"}
          </span>
        </span>
      );
    case "asked":
      return <span className="text-caption text-label-2">You asked</span>;
    case "said":
      return afterAsked ? (
        <span className="text-caption text-label-2">
          <span className="font-mono">{model}</span> answered
        </span>
      ) : (
        <span className="text-caption text-label-2">Said</span>
      );
  }
}

export function RowContent({ entry, spans }: { entry: TraceEntry; spans: string[] }): ReactElement {
  switch (entry.kind) {
    case "told": {
      const lineCount = entry.text.split("\n").length;
      return <Folded text={entry.text} lines={1} mono fade words={{ open: "Fold", closed: `${lineCount} lines` }} />;
    }
    case "call": {
      if (entry.returned === null) {
        return <p className="text-caption text-label-3">No return recorded.</p>;
      }
      const lineCount = entry.returned.split("\n").length;
      return <Folded text={entry.returned} lines={1} mono fade words={{ open: "Fold", closed: `${lineCount} lines` }} />;
    }
    case "thinking":
      return <Folded text={entry.text} lines={1} fade words={{ open: "Fold", closed: "Unfold" }} />;
    case "refused":
      return (
        <div className="flex flex-col gap-1">
          <p className="text-body text-danger-ink">The answer was refused: {entry.text}</p>
          {entry.rejected === null ? null : (
            <Folded text={treeText(entry.rejected)} lines={1} mono fade words={{ open: "Fold", closed: "the answer it gave" }} />
          )}
        </div>
      );
    case "said":
      return <p className="whitespace-pre-wrap text-body text-label-1">{entry.text}</p>;
    case "asked":
      return <p className="whitespace-pre-wrap text-body text-label-1">{entry.text}</p>;
    case "answered":
      return <AnsweredGrid args={entry.args} spans={spans} />;
  }
}
