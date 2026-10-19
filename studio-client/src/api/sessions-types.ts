import { z } from "zod";

import {
  DraftSchema,
  IntentIssueSchema,
  PendingGateSchema,
  ReadinessSchema,
  SpendViewSchema,
  TurnSchema,
  WorkflowGraphSchema,
} from "./studio-types";

// The sessions API's response boundary, mirroring harness's
// features/sessions/routes.py and features/authoring/routes.py (the session-scoped
// plugins) and features/running/routes.py (session-scoped runs, and /api/runs). Every
// session view parses a response through these schemas first, so a backend contract
// drift surfaces as a named ZodError on the failing call instead of as an undefined
// deep in a component.

export const UsageTotalsSchema = z.object({
  input_tokens: z.number(),
  output_tokens: z.number(),
  cache_read_tokens: z.number(),
  cache_write_tokens: z.number(),
});

// A session's context-window vitals; `null` when the backend has not resolved one for
// the session yet.
export const ContextVitalsSchema = z.object({
  used_tokens: z.number(),
  window_tokens: z.number(),
  fraction: z.number(),
  resolved: z.boolean(),
});

export const SessionSummarySchema = z.object({
  id: z.string(),
  project_id: z.string(),
  title: z.string(),
  started_at: z.string().datetime({ offset: true }),
  last_activity_at: z.string().datetime({ offset: true }),
  archived: z.boolean(),
  answering: z.boolean(),
  turn_count: z.number(),
  usage: UsageTotalsSchema,
  kind: z.enum(["chat", "runner", "master"]),
  // A decimal string (never a float) -- the backend's own money type, parsed
  // here only to display, never to compute against.
  cost_usd: z.string(),
  // Responses in this session that no source could price. Above zero, `cost_usd` is a floor rather
  // than the cost, and the vitals say so rather than showing a figure that reads as the whole of it.
  unpriced: z.number(),
  context: ContextVitalsSchema.nullable(),
});
export type SessionSummary = z.infer<typeof SessionSummarySchema>;

export const SessionListSchema = z.object({ sessions: z.array(SessionSummarySchema) });

export const TranscriptMessageSchema = z.object({
  run_id: z.string(),
  idx: z.number(),
  kind: z.string(),
  payload: z.unknown(),
  text: z.string(),
});
export type TranscriptMessage = z.infer<typeof TranscriptMessageSchema>;

export const TranscriptSchema = z.object({ messages: z.array(TranscriptMessageSchema) });

// A transcript message's `payload` is pydantic-ai's own `ModelMessage`, to_jsonable_python'd:
// `{kind: "request"|"response", parts: [...]}`, one entry per `part_kind` (user-prompt,
// text, tool-call, tool-return, thinking, retry-prompt, and others this app never renders).
// Loose on purpose -- this is the subset the chat station renders, not the full pydantic-ai
// message grammar, so an unrendered field or an unlisted part_kind passes through rather
// than failing the parse.
export const MessagePartSchema = z.looseObject({
  part_kind: z.string(),
  content: z.unknown().optional(),
  // Null on a retry prompt pydantic-ai could not tie to one tool (validation failed before a
  // tool was chosen); other parts always carry a name when they carry one at all.
  tool_name: z.string().nullish(),
  tool_call_id: z.string().optional(),
  args: z
    .union([z.string(), z.record(z.string(), z.unknown())])
    .nullable()
    .optional(),
  // ISO timestamp of a tool-return part. A call this browser's own turn holds is answered before
  // it, and never carries one. A part read straight from the record carries it as null when
  // nothing stamped it.
  timestamp: z.string().nullish(),
});
export type MessagePart = z.infer<typeof MessagePartSchema>;

export const MessagePayloadSchema = z.looseObject({
  kind: z.string(),
  parts: z.array(MessagePartSchema).default([]),
  // A response row's own model usage and when it landed, absent on a request row.
  usage: UsageTotalsSchema.optional(),
  // A request read straight from the record carries it as null.
  timestamp: z.string().nullish(),
});
export type MessagePayload = z.infer<typeof MessagePayloadSchema>;

export const ProjectViewSchema = z.object({
  id: z.string(),
  name: z.string(),
  root_path: z.string(),
});
export type ProjectView = z.infer<typeof ProjectViewSchema>;

export const ProjectListSchema = z.object({ projects: z.array(ProjectViewSchema) });

export const SessionDetailSchema = z.object({
  session: SessionSummarySchema,
  turns: z.array(z.unknown()),
});

export const CreatedSessionSchema = z.object({ session: SessionSummarySchema });

// One session with a run in flight, as the right-hand panel renders it. `gate`
// carries the same payload `/api/pending` renders for the process's own session,
// embedded here because a session-scoped run has no session-scoped pending route.
export const RunViewSchema = z.object({
  session_id: z.string(),
  workflow_id: z.string(),
  // What this run is called, as its launch named it. An unnamed run is named by its workflow's own
  // card instead.
  run_title: z.string(),
  state: z.enum(["running", "paused", "waiting"]),
  active: z.array(z.string()),
  done: z.array(z.string()),
  failed: z.array(z.string()),
  waiting_at: z.string().nullable(),
  gate: PendingGateSchema.nullable(),
  // ISO 8601 UTC, "" when unknown.
  started_at: z.string(),
  // This run's own priced cost so far, absent or null when no ledger exists for it yet.
  spend: SpendViewSchema.nullish(),
});
export type RunView = z.infer<typeof RunViewSchema>;

export const RunListSchema = z.object({ runs: z.array(RunViewSchema) });

// A session's dialogue plus the proposal it is holding, so the draft inspector
// survives a page refresh. `issues`, `valid`, `graph` and `readiness` describe the draft
// nested under `draft`, re-derived by the backend on every read.
export const SessionConversationSchema = z.object({
  turns: z.array(TurnSchema),
  draft: DraftSchema,
  issues: z.array(z.string()).default([]),
  valid: z.boolean(),
  graph: WorkflowGraphSchema.nullable(),
  // `null` while nothing on screen parses as a spec, which is a weaker statement than a reading
  // that says the draft is not described.
  readiness: ReadinessSchema.nullish(),
  // Which of the intent's expected answers are still blank, kept apart from `readiness` (the words
  // the card owes) and from `issues` (what this run cannot execute).
  intent_issues: z.array(IntentIssueSchema).default([]),
});
export type SessionConversation = z.infer<typeof SessionConversationSchema>;
