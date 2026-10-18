import { type ChangeObject, diffLines } from "diff";

import { formatElapsedMs } from "@/components/workflows/elapsed";

/**
 * The line changes between an edit's old and new text. Both sides are normalized to `\n` line
 * endings, then read as ending in one, before diffing: a CRLF file reads as the same lines as
 * its LF equivalent, and a last line that merely gained its newline is the same line, not a
 * removal and an addition.
 */
export function editChanges(oldText: string, newText: string): ChangeObject<string>[] {
  const normalized = (text: string): string => text.replace(/\r\n/g, "\n");
  const terminated = (text: string): string =>
    text === "" || text.endsWith("\n") ? text : `${text}\n`;
  return diffLines(terminated(normalized(oldText)), terminated(normalized(newText)));
}

// A tool call's own duration: one decimal place under ten seconds, since most calls finish
// there and a whole-second reading would read as frozen; `formatElapsedMs`'s coarser units past
// that. Non-finite reads empty and a negative clamps to zero, the same as `formatElapsedMs`.
export function formatToolElapsed(ms: number): string {
  if (!Number.isFinite(ms)) {
    return "";
  }
  const clamped = Math.max(0, ms);
  return clamped < 10_000 ? `${(clamped / 1000).toFixed(1)}s` : formatElapsedMs(clamped);
}

// A pure presentation table: given a tool's raw name and its call arguments, what verb line
// reads at a glance (the fork's filesystem and shell toolsets, `fetch`, and the studio
// read tools) and, once a call has a result, a one-line summary of what it returned. An
// unknown tool keeps its raw name as the verb and carries no subject.

export type ToolKind = "read" | "edit" | "search" | "execute" | "fetch" | "retrieve" | "other";

export interface ToolPresentation {
  kind: ToolKind;
  verb: string;
  subject: string | null;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function stringField(input: unknown, field: string): string | undefined {
  if (!isRecord(input)) {
    return undefined;
  }
  const value = input[field];
  return typeof value === "string" ? value : undefined;
}

function stringArrayField(input: unknown, field: string): string[] | undefined {
  if (!isRecord(input)) {
    return undefined;
  }
  const value = input[field];
  if (!Array.isArray(value)) {
    return undefined;
  }
  const strings: string[] = [];
  for (const item of value) {
    if (typeof item !== "string") {
      return undefined;
    }
    strings.push(item);
  }
  return strings;
}

function truncateCommand(command: string): string {
  return command.length > 60 ? `${command.slice(0, 60)}…` : command;
}

function fetchHostname(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

export function presentTool(name: string, input: unknown): ToolPresentation {
  switch (name) {
    case "read_file":
      return { kind: "read", verb: "Read", subject: stringField(input, "path") ?? null };
    case "write_file":
      return { kind: "edit", verb: "Wrote", subject: stringField(input, "path") ?? null };
    case "edit_file":
      return { kind: "edit", verb: "Edited", subject: stringField(input, "path") ?? null };
    case "list_directory":
      return { kind: "search", verb: "Listed", subject: stringField(input, "path") ?? null };
    case "search_files": {
      const pattern = stringField(input, "pattern");
      return {
        kind: "search",
        verb: "Searched",
        subject: pattern === undefined ? null : `"${pattern}"`,
      };
    }
    case "find_files": {
      const pattern = stringField(input, "pattern");
      return {
        kind: "search",
        verb: "Found files",
        subject: pattern === undefined ? null : `"${pattern}"`,
      };
    }
    case "file_info":
      return { kind: "read", verb: "Inspected", subject: stringField(input, "path") ?? null };
    case "create_directory":
      return {
        kind: "other",
        verb: "Created directory",
        subject: stringField(input, "path") ?? null,
      };
    case "run_command": {
      const command = stringField(input, "command");
      return {
        kind: "execute",
        verb: "Ran",
        subject: command === undefined ? null : truncateCommand(command),
      };
    }
    case "fetch": {
      const url = stringField(input, "url");
      if (url !== undefined) {
        return { kind: "fetch", verb: "Fetched", subject: fetchHostname(url) };
      }
      const id = stringField(input, "id");
      if (id !== undefined) {
        return { kind: "retrieve", verb: "Fetched", subject: id };
      }
      return { kind: "fetch", verb: "Fetched", subject: null };
    }
    case "search": {
      const query = stringField(input, "q") ?? stringField(input, "query");
      return {
        kind: "retrieve",
        verb: "Retrieved",
        subject: query === undefined ? null : `"${query}"`,
      };
    }
    case "graph_expand": {
      const id = stringField(input, "id");
      if (id !== undefined) {
        return { kind: "retrieve", verb: "Expanded", subject: id };
      }
      const ids = stringArrayField(input, "ids");
      return {
        kind: "retrieve",
        verb: "Expanded",
        subject: ids === undefined ? null : ids.join(", "),
      };
    }
    case "find_paths":
      return { kind: "retrieve", verb: "Found paths", subject: null };
    case "run_sql":
      return { kind: "retrieve", verb: "Queried the knowledge base", subject: null };
    case "describe_schema":
      return { kind: "retrieve", verb: "Described the schema", subject: null };
    case "run_code":
      return { kind: "execute", verb: "Ran code", subject: null };
    default:
      return { kind: "other", verb: name, subject: null };
  }
}

function totalLines(text: string): number {
  return text.split("\n").length;
}

function nonEmptyLines(text: string): number {
  return text.split("\n").filter((line) => line.trim() !== "").length;
}

// A count with its own noun, singular at one -- `1 line`, `1 match`, `1 result`, `1 entry`, `3
// lines`. The counting summaries below all read this way, so a lone result never reads plural.
function pluralize(count: number, singular: string, plural: string): string {
  return count === 1 ? `1 ${singular}` : `${count} ${plural}`;
}

const EXIT_CODE_PATTERN = /exit(?: code)?[: ]+(\d+)/i;

function summarizeExecute(output: unknown): string | null {
  if (typeof output !== "string") {
    return null;
  }
  const lines = output.split("\n");
  const lastNonEmpty = [...lines].reverse().find((line) => line.trim() !== "");
  const match = lastNonEmpty === undefined ? null : EXIT_CODE_PATTERN.exec(lastNonEmpty);
  const lineCount = pluralize(lines.length, "line", "lines");
  return match ? `exit ${match[1]}, ${lineCount}` : lineCount;
}

function summarizeEdit(input: unknown): string | null {
  const oldText = stringField(input, "old_text");
  const newText = stringField(input, "new_text");
  if (oldText === undefined || newText === undefined) {
    return null;
  }
  let added = 0;
  let removed = 0;
  for (const change of editChanges(oldText, newText)) {
    if (change.added) {
      added += change.count;
    } else if (change.removed) {
      removed += change.count;
    }
  }
  return `+${added} −${removed} lines`;
}

function summarizeWrite(input: unknown): string | null {
  const content = stringField(input, "content");
  return content === undefined ? null : pluralize(totalLines(content), "line", "lines");
}

function summarizeRetrieve(output: unknown): string | null {
  if (Array.isArray(output)) {
    return pluralize(output.length, "result", "results");
  }
  if (isRecord(output)) {
    for (const key of ["results", "hits", "items", "rows"]) {
      const value = output[key];
      if (Array.isArray(value)) {
        return pluralize(value.length, "result", "results");
      }
    }
  }
  return typeof output === "string" ? pluralize(totalLines(output), "line", "lines") : null;
}

function summarizeOther(output: unknown): string | null {
  if (typeof output === "string") {
    const line = output.split("\n").find((candidate) => candidate.trim() !== "");
    if (line === undefined) {
      return null;
    }
    const trimmed = line.trim();
    return trimmed.length > 80 ? `${trimmed.slice(0, 80)}…` : trimmed;
  }
  if (isRecord(output)) {
    return `${Object.keys(output).length} keys`;
  }
  return null;
}

// read_file's own header line, `[{path} | {n} lines | hash:{hash}]` -- `n` is read from here,
// never from counting the numbered content below it, so a truncated read (the tool's own
// continuation notice) still reports the file's real line count.
const READ_FILE_HEADER = /^\[.* \| (\d+) lines \| hash:/;

function summarizeReadFile(output: unknown): string | null {
  if (typeof output !== "string") {
    return null;
  }
  const header = output.split("\n", 1)[0] ?? "";
  const match = READ_FILE_HEADER.exec(header);
  return match?.[1] === undefined ? null : pluralize(Number(match[1]), "line", "lines");
}

function summarizeListDirectory(output: unknown): string | null {
  if (typeof output !== "string") {
    return null;
  }
  if (output === "(empty directory)") {
    return "empty";
  }
  const entries = output.split("\n").filter((line) => line.trim() !== "");
  return pluralize(entries.length, "entry", "entries");
}

// Shared by search_files (content matches) and find_files (path matches): one line per hit,
// or the fork's own "No matches found." sentinel for none.
function summarizeMatches(output: unknown): string | null {
  if (typeof output !== "string") {
    return null;
  }
  if (output === "No matches found.") {
    return "no matches";
  }
  return pluralize(nonEmptyLines(output), "match", "matches");
}

// file_info's own metadata block leads with `path: {path}`; the summary is that line as-is.
function summarizeFileInfo(output: unknown): string | null {
  if (typeof output !== "string") {
    return null;
  }
  const line = output.split("\n", 1)[0]?.trim();
  return line === undefined || line === "" ? null : line;
}

export function summarizeResult(name: string, input: unknown, output: unknown): string | null {
  switch (name) {
    case "read_file":
      return summarizeReadFile(output);
    case "list_directory":
      return summarizeListDirectory(output);
    case "search_files":
    case "find_files":
      return summarizeMatches(output);
    case "write_file":
      return summarizeWrite(input);
    case "edit_file":
      return summarizeEdit(input);
    case "file_info":
      return summarizeFileInfo(output);
  }
  const kind = presentTool(name, input).kind;
  switch (kind) {
    case "read":
      return typeof output === "string" ? pluralize(nonEmptyLines(output), "line", "lines") : null;
    case "edit":
      return summarizeEdit(input);
    case "search":
      return typeof output === "string"
        ? pluralize(nonEmptyLines(output), "match", "matches")
        : null;
    case "execute":
      return summarizeExecute(output);
    case "fetch":
      return typeof output === "string" ? pluralize(totalLines(output), "line", "lines") : null;
    case "retrieve":
      return summarizeRetrieve(output);
    case "other":
      return summarizeOther(output);
  }
}

const PLURAL_VERBS: Record<ToolKind, (count: number) => string> = {
  read: (count) => `Read ${count} files`,
  edit: (count) => `Edited ${count} files`,
  search: (count) => `Searched ${count} times`,
  execute: (count) => `Ran ${count} commands`,
  fetch: (count) => `Fetched ${count} pages`,
  retrieve: (count) => `Retrieved ${count} times`,
  other: (count) => `Called ${count} tools`,
};

export function pluralVerb(kind: ToolKind, count: number): string {
  return PLURAL_VERBS[kind](count);
}
