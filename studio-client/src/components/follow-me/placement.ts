// Where the companion sits, how big it is there, and the two numbers its vitals row prints.
// Every size a viewer dragged is read back through the ranges here, so a hand-edited or
// stale stored value can never render a surface too small to use or wider than the page.

export type Placement = "left" | "right" | "bottom" | "floating";

export const PLACEMENTS: readonly Placement[] = ["left", "right", "bottom", "floating"];

export const PINNED_KEY = "followme:pinned";
export const PLACEMENT_KEY = "followme:placement";
export const WIDTH_KEY = "followme:width";
export const HEIGHT_KEY = "followme:height";
export const WINDOW_KEY = "followme:window";
export const NAV_KEY = "sidebar:collapsed";

export interface SizeRange {
  min: number;
  max: number;
  fallback: number;
}

// The rails' width, the bottom panel's height, and the floating window's own two axes.
export const WIDTH: SizeRange = { min: 320, max: 640, fallback: 400 };
export const HEIGHT: SizeRange = { min: 240, max: 600, fallback: 360 };
export const WINDOW_W: SizeRange = { min: 360, max: 720, fallback: 420 };
export const WINDOW_H: SizeRange = { min: 400, max: 800, fallback: 580 };

// The floating window's resting gap from the bottom right of the page column, and the step a
// keyboard takes on any resize grip.
export const WINDOW_INSET = 28;
export const RESIZE_STEP = 16;

export interface WindowRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export function clampWindowRect(rect: WindowRect): WindowRect {
  return {
    x: Math.max(0, rect.x),
    y: Math.max(0, rect.y),
    w: clamp(rect.w, WINDOW_W.min, WINDOW_W.max),
    h: clamp(rect.h, WINDOW_H.min, WINDOW_H.max),
  };
}

function numberAt(source: Record<string, unknown>, key: string): number | undefined {
  const value = source[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

// Reads a stored window rect back, clamped. Anything that is not four finite numbers is
// treated as absent, so the surface falls back to its resting corner rather than to nonsense.
export function parseWindowRect(value: unknown): WindowRect | undefined {
  if (typeof value !== "object" || value === null) return undefined;
  const source: Record<string, unknown> = { ...value };
  const x = numberAt(source, "x");
  const y = numberAt(source, "y");
  const w = numberAt(source, "w");
  const h = numberAt(source, "h");
  if (x === undefined || y === undefined || w === undefined || h === undefined) return undefined;
  return clampWindowRect({ x, y, w, h });
}

function scaled(value: number, unit: string): string {
  const text = value.toFixed(1);
  return `${text.endsWith(".0") ? text.slice(0, -2) : text}${unit}`;
}

// A token count at the scale a reader can hold in their head: exact under a thousand, then
// thousands, then millions, carrying a decimal only while the leading digit stands alone.
export function countText(count: number): string {
  if (count < 1000) return String(Math.round(count));
  const thousands = count / 1000;
  if (thousands < 10) return scaled(thousands, "k");
  const roundedThousands = Math.round(thousands);
  if (roundedThousands < 1000) return `${roundedThousands}k`;
  const millions = count / 1_000_000;
  return millions < 10 ? scaled(millions, "M") : `${Math.round(millions)}M`;
}

export function tokensText(used: number, window: number): string {
  return `${countText(used)} of ${countText(window)}`;
}

// The same fraction as a percentage, with a decimal only where an integer would round a live
// conversation down to nothing. An unresolved reading wears a tilde.
export function percentText(fraction: number, resolved: boolean): string {
  const percent = fraction * 100;
  const text = percent < 1 ? `${percent.toFixed(1)}%` : `${Math.round(percent)}%`;
  return resolved ? text : `~${text}`;
}
