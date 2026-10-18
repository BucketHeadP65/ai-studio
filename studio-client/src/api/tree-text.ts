import type { MessagePart } from "./sessions-types";

// A value as a person reads it: the same tree `core.ai.inspection.views.shown` renders on the
// backend, mirrored here for the reading log and the tool-call arguments it draws inline. A
// string that itself holds JSON unfolds first, recursively, so a tool return that is a dict
// holding a JSON string holding YAML never shows up escaped twice.

function tryParseJson(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

// A string unfolds only when it parses to an object or an array -- `json.loads("3")` is not an
// unfold, so a numeral or a boolean held as a string stays a string.
function deepUnfold(value: unknown): unknown {
  if (typeof value === "string") {
    const parsed = tryParseJson(value);
    if (Array.isArray(parsed) || isPlainObject(parsed)) {
      return deepUnfold(parsed);
    }
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item) => deepUnfold(item));
  }
  if (isPlainObject(value)) {
    const result: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value)) {
      result[key] = deepUnfold(item);
    }
    return result;
  }
  return value;
}

function scalarText(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

// Lines for one dict, already unfolded, two spaces per level.
function objectLines(value: Record<string, unknown>, indent: number): string[] {
  const pad = "  ".repeat(indent);
  const lines: string[] = [];
  for (const [key, item] of Object.entries(value)) {
    if (Array.isArray(item)) {
      lines.push(item.length === 0 ? `${pad}${key}: []` : `${pad}${key}:`);
      if (item.length > 0) lines.push(...arrayLines(item, indent + 1));
    } else if (isPlainObject(item)) {
      const entries = Object.entries(item);
      lines.push(entries.length === 0 ? `${pad}${key}: {}` : `${pad}${key}:`);
      if (entries.length > 0) lines.push(...objectLines(item, indent + 1));
    } else if (typeof item === "string" && item.includes("\n")) {
      lines.push(`${pad}${key}:`);
      const innerPad = "  ".repeat(indent + 1);
      lines.push(...item.split("\n").map((line) => `${innerPad}${line}`));
    } else {
      lines.push(`${pad}${key}: ${scalarText(item)}`);
    }
  }
  return lines;
}

// Lines for one list, already unfolded. A dict or list item sits under a bare dash, its own
// lines one level deeper, the same tree the backend renders for the evidence.
function arrayLines(value: unknown[], indent: number): string[] {
  const pad = "  ".repeat(indent);
  const innerPad = "  ".repeat(indent + 1);
  const lines: string[] = [];
  for (const item of value) {
    if (Array.isArray(item)) {
      if (item.length === 0) {
        lines.push(`${pad}- []`);
        continue;
      }
      lines.push(`${pad}-`, ...arrayLines(item, indent + 1));
    } else if (isPlainObject(item)) {
      const entries = Object.entries(item);
      if (entries.length === 0) {
        lines.push(`${pad}- {}`);
        continue;
      }
      lines.push(`${pad}-`, ...objectLines(item, indent + 1));
    } else if (typeof item === "string" && item.includes("\n")) {
      lines.push(`${pad}-`, ...item.split("\n").map((line) => `${innerPad}${line}`));
    } else {
      lines.push(`${pad}- ${scalarText(item)}`);
    }
  }
  return lines;
}

export function treeText(value: unknown): string {
  const unfolded = deepUnfold(value);
  if (Array.isArray(unfolded)) {
    return unfolded.length === 0 ? "[]" : arrayLines(unfolded, 0).join("\n");
  }
  if (isPlainObject(unfolded)) {
    const entries = Object.entries(unfolded);
    return entries.length === 0 ? "{}" : objectLines(unfolded, 0).join("\n");
  }
  return scalarText(unfolded);
}

// A tool call's arguments as a plain record, whether the wire sent them JSON-stringified or
// already parsed -- a string that fails to parse, or anything that isn't a bare object, reads
// as no arguments at all.
export function argsRecord(args: MessagePart["args"]): Record<string, unknown> {
  if (args === null || args === undefined) return {};
  if (typeof args === "string") {
    try {
      const parsed: unknown = JSON.parse(args);
      return parsed !== null && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : {};
    } catch {
      return {};
    }
  }
  return args;
}

function argValueText(value: unknown): string {
  if (typeof value === "string") return JSON.stringify(value);
  if (value === null || typeof value === "number" || typeof value === "boolean") return JSON.stringify(value);
  return "[...]";
}

// The compact one-liner for a tool call: `name(k: v, k: v)`, a nested value collapsed to
// `[...]` rather than spelled out inline.
export function callLine(name: string, args: Record<string, unknown>): string {
  const entries = Object.entries(args);
  if (entries.length === 0) return `${name}()`;
  return `${name}(${entries.map(([key, value]) => `${key}: ${argValueText(value)}`).join(", ")})`;
}

// Every phrase a blame's own words quote: the text between a matching pair of straight quotes
// (' or "), long enough to be a real quotation rather than a stray apostrophe -- at least eight
// characters.
// A quote opens after a non-word character and closes before one, so the apostrophe in "fetch's" pairs with nothing.
const QUOTED_PHRASE = /(?<![\w])'([^']{8,})'(?![\w])|(?<![\w])"([^"]{8,})"(?![\w])/g;
const ELLIPSIS = /\.{3}|\u2026/;
const MIN_PHRASE = 8;

export function quotedPhrases(why: string): string[] {
  const phrases: string[] = [];
  for (const match of why.matchAll(QUOTED_PHRASE)) {
    const phrase = match[1] ?? match[2];
    if (phrase === undefined) continue;
    // A quote the writer shortened with an ellipsis is several phrases: each side of it is looked for on its own.
    for (const piece of phrase.split(ELLIPSIS)) {
      const trimmed = piece.trim();
      if (trimmed.length >= MIN_PHRASE) phrases.push(trimmed);
    }
  }
  return phrases;
}
