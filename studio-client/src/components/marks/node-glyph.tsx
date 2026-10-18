import { cn } from "client-core/lib/cn";
import type { ReactElement } from "react";

// The small mark that opens every node card's header (and every fleet-row mini
// graph): shape says what kind of node this is, fill says whether it ran, an
// icon inside the fill reinforces the fill so nothing here reads by color
// alone. kind picks the shape (a step's circle, a person gate's diamond, an
// agent gate's hexagon, an automatic gate's padlocked diamond frame); fill
// picks the paint. size is the glyph's own pixel footprint (a hexagon comes
// out slightly wider than tall, its natural proportion, everything else is
// square). label, when the glyph stands alone with no adjacent word, is its
// aria-label; omit it wherever a sibling word already carries the meaning.

export type GlyphKind = "step" | "person-gate" | "agent-gate" | "auto-gate";
export type GlyphFill = "ran" | "not-run" | "running" | "waiting" | "failed";

export interface NodeGlyphProps {
  kind: GlyphKind;
  fill?: GlyphFill;
  size?: number;
  label?: string;
  className?: string;
}

function CheckIcon({ className }: { className: string }): ReactElement {
  return (
    <path
      d="M3.4 8.4l3 3 6.2-6.6"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
    />
  );
}

function PlayIcon({ className }: { className: string }): ReactElement {
  return <path d="M3.4 1.8l9.4 6.2-9.4 6.2z" fill="currentColor" className={className} />;
}

function FailIcon({ className }: { className: string }): ReactElement {
  return (
    <path
      d="M3.6 3.6l8.8 8.8M12.4 3.6l-8.8 8.8"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.4}
      strokeLinecap="round"
      className={className}
    />
  );
}

function ShieldIcon({ className }: { className: string }): ReactElement {
  return (
    <path
      d="M8 1.8l4.8 2v4c0 3.4-2 5.4-4.8 6-2.8-.6-4.8-2.6-4.8-6v-4z"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinejoin="round"
      className={className}
    />
  );
}

function PadlockIcon({ className }: { className: string }): ReactElement {
  return (
    <g fill="none" stroke="currentColor" strokeWidth={1.4} className={className}>
      <path d="M8 2v3.6M8 2L5.6 4.4M8 2l2.4 2.4" />
      <rect x="2.6" y="7" width="10.8" height="6.6" />
    </g>
  );
}

function GridIcon({ className }: { className: string }): ReactElement {
  return (
    <g fill="none" stroke="currentColor" strokeWidth={1.7} className={className}>
      <rect x="4.4" y="4.4" width="7.2" height="7.2" rx="1" />
      <path d="M6.8 1.8v2.6M9.2 1.8v2.6M6.8 11.6v2.6M9.2 11.6v2.6M1.8 6.8h2.6M1.8 9.2h2.6M11.6 6.8h2.6M11.6 9.2h2.6" />
    </g>
  );
}

interface Rendered {
  content: ReactElement;
  pulse: boolean;
}

// A step, a node that does work: a plain filled circle, the same shape at
// rest or once it has run; running hollows it out to a breathing cobalt
// ring, failed keeps the fill but adds the wash-colored halo. A striped
// fill belongs to a pill, never a glyph.
function renderStep(fill: GlyphFill): Rendered {
  switch (fill) {
    case "not-run":
      return {
        content: (
          <circle
            cx="8"
            cy="8"
            r="6.4"
            fill="none"
            strokeWidth={1.6}
            strokeDasharray="2.2 2"
            className="stroke-line-strong"
          />
        ),
        pulse: false,
      };
    case "running":
      return {
        content: (
          <>
            <circle
              cx="8"
              cy="8"
              r="6.4"
              fill="none"
              strokeWidth={2.2}
              className="fill-surface stroke-accent"
            />
            <PlayIcon className="text-accent" />
          </>
        ),
        pulse: true,
      };
    case "waiting":
      return {
        content: (
          <>
            <circle cx="8" cy="8" r="7" className="fill-warn" />
          </>
        ),
        pulse: true,
      };
    case "failed":
      return {
        content: (
          <>
            <circle cx="8" cy="8" r="7.8" className="fill-warn-soft" />
            <circle cx="8" cy="8" r="7" className="fill-warn" />
            <FailIcon className="text-surface" />
          </>
        ),
        pulse: false,
      };
    case "ran":
      return {
        content: (
          <>
            <circle cx="8" cy="8" r="7" className="fill-label-1" />
            <CheckIcon className="text-surface" />
          </>
        ),
        pulse: false,
      };
  }
}

// A person gate, a diamond, needs a person's own hand to clear it: idle is a
// dashed outline (never reached) or a solid ink diamond (already resolved),
// waiting is a solid vermilion diamond that breathes until someone answers.
function renderPersonGate(fill: GlyphFill): Rendered {
  const diamond = (extraClass: string, dashed = false) => (
    <rect
      x="4.4"
      y="4.4"
      width="7.2"
      height="7.2"
      rx="1.2"
      transform="rotate(45 8 8)"
      fill={dashed ? "none" : undefined}
      strokeWidth={dashed ? 1.6 : undefined}
      strokeDasharray={dashed ? "2.2 2" : undefined}
      className={extraClass}
    />
  );
  switch (fill) {
    case "not-run":
      return { content: diamond("stroke-line-strong", true), pulse: false };
    case "running":
      return {
        content: (
          <>
            <rect
              x="4.4"
              y="4.4"
              width="7.2"
              height="7.2"
              rx="1.2"
              transform="rotate(45 8 8)"
              strokeWidth={2}
              className="fill-surface stroke-accent"
            />
            <ShieldIcon className="text-accent" />
          </>
        ),
        pulse: true,
      };
    case "waiting":
      return {
        content: (
          <>
            {diamond("fill-warn")}
            <ShieldIcon className="text-surface" />
          </>
        ),
        pulse: true,
      };
    case "failed":
      return {
        content: (
          <>
            <rect
              x="3.2"
              y="3.2"
              width="9.6"
              height="9.6"
              rx="1.4"
              transform="rotate(45 8 8)"
              className="fill-warn-soft"
            />
            {diamond("fill-warn")}
            <FailIcon className="text-surface" />
          </>
        ),
        pulse: false,
      };
    case "ran":
      return {
        content: (
          <>
            {diamond("fill-label-1")}
            <CheckIcon className="text-surface" />
          </>
        ),
        pulse: false,
      };
  }
}

// An automatic gate reuses the person gate's diamond frame with a solid ink
// border rather than a shape of its own (the padlock inside is what says
// "configured to pass," the diamond only says "this is a gate a run stops
// at"); its resting look stays the same whether the run has reached it yet
// or not, only running/waiting/failed tint the frame.
function renderAutoGate(fill: GlyphFill): Rendered {
  const frame = (extraClass: string, strokeWidth = 1.8) => (
    <rect
      x="4.4"
      y="4.4"
      width="7.2"
      height="7.2"
      rx="1.2"
      transform="rotate(45 8 8)"
      strokeWidth={strokeWidth}
      className={extraClass}
    />
  );
  switch (fill) {
    case "running":
      return {
        content: (
          <>
            {frame("fill-surface stroke-accent", 2)}
            <PadlockIcon className="text-label-1" />
          </>
        ),
        pulse: true,
      };
    case "waiting":
      return {
        content: (
          <>
            {frame("fill-surface stroke-warn", 2)}
            <PadlockIcon className="text-label-1" />
          </>
        ),
        pulse: true,
      };
    case "failed":
      return {
        content: (
          <>
            <rect
              x="3.2"
              y="3.2"
              width="9.6"
              height="9.6"
              rx="1.4"
              transform="rotate(45 8 8)"
              className="fill-warn-soft"
            />
            {frame("fill-surface stroke-warn")}
            <FailIcon className="text-warn" />
          </>
        ),
        pulse: false,
      };
    case "not-run":
    case "ran":
      return {
        content: (
          <>
            {frame("fill-surface stroke-label-1")}
            <PadlockIcon className="text-label-1" />
          </>
        ),
        pulse: false,
      };
  }
}

const HEX_POINTS = "10.5,0 31.5,0 42,19 31.5,38 10.5,38 0,19";
const HEX_HALO_POINTS = "7.5,-1.6 34.5,-1.6 44.4,19 34.5,39.6 7.5,39.6 -2.4,19";

// An agent gate is a hexagon, always solid ink (the orchestrator's own
// judgement never reads as "not yet decided" the way a person's does), a
// thin colored ring standing in for a pill the glyph itself has no room
// for. Not reached yet is the one fill this shape hollows out for, an
// outline hexagon rather than the ink fill a real judgement earns.
function renderAgentGate(fill: GlyphFill): Rendered {
  if (fill === "not-run") {
    return {
      content: (
        <polygon
          points={HEX_POINTS}
          fill="none"
          strokeWidth={1.8}
          strokeDasharray="4 3"
          className="stroke-line-strong"
        />
      ),
      pulse: false,
    };
  }
  const ring =
    fill === "running" ? "stroke-accent" : fill === "waiting" ? "stroke-warn" : undefined;
  return {
    content: (
      <>
        {fill === "failed" ? <polygon points={HEX_HALO_POINTS} className="fill-warn-soft" /> : null}
        <polygon
          points={HEX_POINTS}
          strokeWidth={ring ? 2.4 : 0}
          className={cn("fill-label-1", ring)}
        />
        {fill === "failed" ? (
          <g transform="translate(13 11)">
            <FailIcon className="text-surface" />
          </g>
        ) : (
          <g transform="translate(13 11)">
            <GridIcon className="text-surface" />
          </g>
        )}
      </>
    ),
    pulse: fill === "running" || fill === "waiting",
  };
}

export function NodeGlyph({
  kind,
  fill = "ran",
  size = 16,
  label,
  className,
}: NodeGlyphProps): ReactElement {
  if (kind === "agent-gate") {
    const rendered = renderAgentGate(fill);
    const height = Math.round((size * 38) / 42);
    return (
      <svg
        width={size}
        height={height}
        viewBox="0 0 42 38"
        data-glyph-kind={kind}
        data-glyph-fill={fill}
        className={cn(rendered.pulse && "motion-safe:animate-pulse", className)}
        role={label ? "img" : undefined}
        aria-label={label}
        aria-hidden={label ? undefined : true}
      >
        {rendered.content}
      </svg>
    );
  }

  const rendered =
    kind === "step"
      ? renderStep(fill)
      : kind === "person-gate"
        ? renderPersonGate(fill)
        : renderAutoGate(fill);

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      data-glyph-kind={kind}
      data-glyph-fill={fill}
      className={cn(rendered.pulse && "motion-safe:animate-pulse", className)}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      {rendered.content}
    </svg>
  );
}
