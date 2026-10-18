// The chat wire's optional page-context payload, mirroring harness's
// domain/page_context.py, plus the fence strip transcript bubbles apply
// before showing a user message as the person's own words.

export const PAGE_CONTEXT_OPEN = "<page-context>";
export const PAGE_CONTEXT_CLOSE = "</page-context>";

export interface PageTrailStep {
  url: string;
  at: string;
}

export type TargetKind = "tab" | "input" | "region";

export interface TargetDecl {
  name: string;
  kind: TargetKind;
  description: string;
  params?: Record<string, string>;
}

export interface PageContext {
  url: string;
  page: string;
  ids: Record<string, string>;
  tab: string;
  trail: PageTrailStep[];
  extras: Record<string, string>;
  targets: TargetDecl[];
}

// One companion tool's answer, riding back to the server on the next request: a verb's
// output, or an approval act's decision, keyed to the tool call it settles.
export interface ToolResult {
  tool_call_id: string;
  tool_name: string;
  output?: string;
  approved?: boolean;
  reason?: string;
}

// The verbs move the person around the page itself. The console acts operate the
// workflow console (plugins, gates, schedules, the model and profile pickers).
export const VERBS = ["navigate", "point", "prefill", "set_tab"] as const;
export type Verb = (typeof VERBS)[number];
export const CONSOLE_ACTS = [
  "set_model",
  "select_profile",
  "toggle_plugin",
  "run_control",
  "decide_gate",
  "schedule_workflow",
  "update_schedule",
  "delete_schedule",
  "run_schedule_now",
  "archive_workflow",
  "restore_workflow",
  "redescribe_workflow",
] as const;

export function isVerb(name: string): name is Verb {
  return (VERBS as readonly string[]).includes(name);
}

export function isConsoleAct(name: string): boolean {
  return (CONSOLE_ACTS as readonly string[]).includes(name);
}

export function isCompanionTool(name: string): boolean {
  return isVerb(name) || isConsoleAct(name);
}

export function stripPageContext(text: string): string {
  if (!text.startsWith(PAGE_CONTEXT_OPEN)) return text;
  const close = text.indexOf(PAGE_CONTEXT_CLOSE);
  if (close === -1) return text;
  return text.slice(close + PAGE_CONTEXT_CLOSE.length).replace(/^\s+/, "");
}
