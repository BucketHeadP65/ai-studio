import type { ReactElement } from "react";

import { type GlyphKind, NodeGlyph } from "@/components/marks/node-glyph";

// The fleet board's own key, once, under the run cards: what each shape in a mini graph means.
// Every entry stands at the glyph's own default fill (`ran`, the same default `NodeGlyph` itself
// picks for a glyph shown with no run in view), except "Not run yet," which is the `not-run` fill
// by name.

const SHAPES: { kind: GlyphKind; word: string }[] = [
  { kind: "step", word: "Step" },
  { kind: "person-gate", word: "Gate a person owns" },
  { kind: "agent-gate", word: "Gate the agent owns" },
];

export function RunLegend(): ReactElement {
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-lg border border-line bg-surface px-4.5 py-3.5">
      <span className="font-semibold text-label-3 text-micro uppercase tracking-caps">
        How the drawing reads
      </span>
      {SHAPES.map((shape) => (
        <span key={shape.word} className="flex items-center gap-1.5 text-caption text-label-2">
          <NodeGlyph kind={shape.kind} size={14} />
          {shape.word}
        </span>
      ))}
      <span className="flex items-center gap-1.5 text-caption text-label-2">
        <NodeGlyph kind="step" fill="not-run" size={14} />
        Not run yet
      </span>
      <span className="text-caption text-label-3 italic">
        Shape says what type of step it is, fill says whether it ran.
      </span>
    </div>
  );
}
