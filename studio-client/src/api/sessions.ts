import { ApiError, VoidSchema, createApiClient } from "client-core/api/client";

import type { UIMessage } from "ai";
import { z } from "zod";
import { isCompanionTool, stripPageContext } from "./page-context";
import {
  CreatedSessionSchema,
  type MessagePayload,
  MessagePayloadSchema,
  ProjectListSchema,
  type ProjectView,
  RunListSchema,
  type RunView,
  type SessionConversation,
  SessionConversationSchema,
  SessionDetailSchema,
  SessionListSchema,
  type SessionSummary,
  SessionSummarySchema,
  type TranscriptMessage,
  TranscriptSchema,
} from "./sessions-types";
import { studioApi } from "./studio";
import { STUDIO_API_URL } from "./studio-config";
import {
  type Automode,
  AutomodeSchema,
  type Decided,
  DecidedSchema,
  type GateMode,
  type JsonValue,
  type RunIntent,
  type RunStarted,
  RunStartedSchema,
  type RunState,
  RunStateSchema,
  type RunValues,
  RunValuesSchema,
  type Saved,
  SavedSchema,
  type WorkflowIntent,
} from "./studio-types";

// The sessions station's typed API surface, over the studio agent backend
// (`studio.agent`, :8770) — the same origin the workflow studio talks to.
// Same shape as `studioApi`/`harnessApi`: client-core supplies request/parse/
// error, this module injects the base URL and builds the endpoint object.

const { request } = createApiClient({ baseUrl: STUDIO_API_URL });

const SessionEnvelopeSchema = z.object({ session: SessionSummarySchema });

// What the intent route answers: the draft as it now reads, under the same keys a conversation
// read uses, so one parse serves both.
const RecordedIntentSchema = SessionConversationSchema.omit({ turns: true }).extend({
  ok: z.literal(true),
});
type RecordedIntent = z.infer<typeof RecordedIntentSchema>;

// Pause and resume answer with the same `{ok}` shape `studioApi` uses for the
// unscoped run route, plus which workflow the act landed on when it succeeded, and
// a sentence in `message` naming why when the route refused (`ok: false`).
const PausedSchema = z.object({
  ok: z.boolean(),
  paused: z.string().optional(),
  message: z.string().optional(),
});
type Paused = z.infer<typeof PausedSchema>;
const ResumedSchema = z.object({
  ok: z.boolean(),
  running: z.string().optional(),
  replayed: z.array(z.string()).optional(),
  withdrawn: z.array(z.string()).optional(),
  message: z.string().optional(),
});
type Resumed = z.infer<typeof ResumedSchema>;

// Stop answers with the same `{ok}` shape as pause, plus which workflow the
// act landed on when it succeeded, and a refusal sentence in `message`.
const StoppedSchema = z.object({
  ok: z.boolean(),
  stopped: z.string().optional(),
  message: z.string().optional(),
});
type Stopped = z.infer<typeof StoppedSchema>;

function queryString(params: Record<string, string | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) {
      search.set(key, String(value));
    }
  }
  const query = search.toString();
  return query ? `?${query}` : "";
}

export const sessionsApi = {
  // `kind` narrows by the session's own kind ("chat", "runner"), or "all" for both; the
  // backend defaults to "chat" when omitted, so a caller after every kind must say "all".
  list: (opts?: {
    archived?: boolean;
    q?: string;
    kind?: "chat" | "runner" | "master" | "all";
  }): Promise<SessionSummary[]> =>
    request<z.infer<typeof SessionListSchema>>(
      `/api/sessions${queryString({ archived: opts?.archived, q: opts?.q, kind: opts?.kind })}`,
      SessionListSchema,
    ).then((result) => result.sessions),

  // Resolves the id of the one session the follow-me companion chat talks to.
  master: (): Promise<string> =>
    request<{ session_id: string }>("/api/master", z.object({ session_id: z.string() })).then(
      (result) => result.session_id,
    ),

  detail: (id: string): Promise<z.infer<typeof SessionDetailSchema>> =>
    request<z.infer<typeof SessionDetailSchema>>(
      `/api/sessions/${encodeURIComponent(id)}`,
      SessionDetailSchema,
    ),

  transcript: (id: string): Promise<TranscriptMessage[]> =>
    request<z.infer<typeof TranscriptSchema>>(
      `/api/sessions/${encodeURIComponent(id)}/transcript`,
      TranscriptSchema,
    ).then((result) => result.messages),

  // New chat: opens a session, optionally on a named project's timeline.
  // `kind` defaults to "chat" server-side when omitted; a manual workflow run
  // passes "runner" so the session never surfaces in the chat rail's default list.
  create: (projectId?: string, kind?: "chat" | "runner"): Promise<SessionSummary> =>
    request<z.infer<typeof CreatedSessionSchema>>("/api/sessions", CreatedSessionSchema, {
      method: "POST",
      body: JSON.stringify({
        ...(projectId === undefined ? {} : { project_id: projectId }),
        ...(kind === undefined ? {} : { kind }),
      }),
    }).then((result) => result.session),

  // Saves THAT session's draft into the library. 400 with a sentence when the
  // draft does not validate, 404 for an unknown id.
  save: (id: string): Promise<Saved> =>
    request<Saved>(`/api/sessions/${encodeURIComponent(id)}/save`, SavedSchema, {
      method: "POST",
    }),

  // Hands a message to the turn a session is mid-way through; it reaches the
  // model inside that turn. 409 when nothing is in flight there (send it as an
  // ordinary turn instead), 404 for an unknown id.
  steer: (id: string, message: string): Promise<{ ok: true }> =>
    request<{ ok: true }>(`/api/sessions/${encodeURIComponent(id)}/steer`, z.object({ ok: z.literal(true) }), {
      method: "POST",
      body: JSON.stringify({ message }),
    }),

  // Stops the turn a session is mid-way through; the session answers again
  // immediately. 409 when nothing is in flight there, 404 for an unknown id.
  interrupt: (id: string): Promise<{ ok: true }> =>
    request<{ ok: true }>(
      `/api/sessions/${encodeURIComponent(id)}/interrupt`,
      z.object({ ok: z.literal(true) }),
      { method: "POST" },
    ),

  conversation: (id: string): Promise<SessionConversation> =>
    request<SessionConversation>(
      `/api/sessions/${encodeURIComponent(id)}/conversation`,
      SessionConversationSchema,
    ),

  // Records what the person says they asked for onto this session's draft, and answers the draft
  // as it now reads. The whole statement crosses each time, so an edited assumption arrives as
  // the statement it belongs to. Records only: nothing is saved into the library and no run or
  // schedule starts. 404 for an unknown id.
  recordIntent: (id: string, intent: WorkflowIntent): Promise<RecordedIntent> =>
    request<RecordedIntent>(`/api/sessions/${encodeURIComponent(id)}/intent`, RecordedIntentSchema, {
      method: "PUT",
      body: JSON.stringify(intent),
    }),

  // Drops the session's turn history server-side; the session itself, and its title,
  // survive. 404 for an unknown id.
  forget: (id: string): Promise<{ ok: true }> =>
    request<{ ok: true }>(
      `/api/sessions/${encodeURIComponent(id)}/conversation`,
      z.object({ ok: z.literal(true) }),
      { method: "DELETE" },
    ),

  runs: (): Promise<RunView[]> =>
    request<z.infer<typeof RunListSchema>>("/api/runs", RunListSchema).then(
      (result) => result.runs,
    ),

  // `gateMode` names who decides a gate this run reaches -- "wait" (a person, the default) sends
  // nothing extra, since that is every backend's own default too. `args` crosses as JSON and is
  // admitted against each parameter's own schema without coercion, so every value keeps the type
  // it was collected as. `runTitle` names this run itself; an empty one sends nothing, so an
  // unnamed run stays unnamed rather than arriving as a blank name.
  run: (
    id: string,
    workflowId: string,
    args: Record<string, JsonValue>,
    gateMode?: GateMode,
    runTitle?: string,
  ): Promise<RunStarted> =>
    request<RunStarted>(`/api/sessions/${encodeURIComponent(id)}/run`, RunStartedSchema, {
      method: "POST",
      body: JSON.stringify({
        workflow_id: workflowId,
        arguments: args,
        ...(gateMode === undefined || gateMode === "wait" ? {} : { gate_mode: gateMode }),
        ...(runTitle === undefined || runTitle === "" ? {} : { run_title: runTitle }),
      }),
    }),

  pauseRun: (id: string): Promise<Paused> =>
    request<Paused>(`/api/sessions/${encodeURIComponent(id)}/run/pause`, PausedSchema, {
      method: "POST",
    }),

  resumeRun: (id: string, specYaml?: string): Promise<Resumed> =>
    request<Resumed>(`/api/sessions/${encodeURIComponent(id)}/run/resume`, ResumedSchema, {
      method: "POST",
      body: JSON.stringify(specYaml === undefined ? {} : { spec_yaml: specYaml }),
    }),

  // Ends the run for good, as distinct from a pause.
  stopRun: (id: string): Promise<Stopped> =>
    request<Stopped>(`/api/sessions/${encodeURIComponent(id)}/run/stop`, StoppedSchema, {
      method: "POST",
    }),

  // `feedback` is read only on a rejection -- approving releases the value regardless of
  // what was typed alongside it.
  decide: (id: string, approve: boolean, feedback?: string): Promise<Decided> =>
    request<Decided>(`/api/sessions/${encodeURIComponent(id)}/decide`, DecidedSchema, {
      method: "POST",
      body: JSON.stringify(feedback === undefined ? { approve } : { approve, feedback }),
    }),

  // Flips `id`'s live run's own automatic-approval flag, refused when nothing is running there.
  setAutomode: (id: string, on: boolean): Promise<Automode> =>
    request<Automode>(`/api/sessions/${encodeURIComponent(id)}/run/automode`, AutomodeSchema, {
      method: "POST",
      body: JSON.stringify({ on }),
    }),

  runState: (id: string, since = 0): Promise<RunState> =>
    request<RunState>(
      `/api/sessions/${encodeURIComponent(id)}/run/state?since=${since}`,
      RunStateSchema,
    ),

  // What each node of the session's run produced, rendered for reading.
  runValues: (id: string): Promise<RunValues> =>
    request<RunValues>(`/api/sessions/${encodeURIComponent(id)}/run/values`, RunValuesSchema),

  rename: (id: string, title: string): Promise<SessionSummary> =>
    request<z.infer<typeof SessionEnvelopeSchema>>(
      `/api/sessions/${encodeURIComponent(id)}`,
      SessionEnvelopeSchema,
      {
        method: "PATCH",
        body: JSON.stringify({ title }),
      },
    ).then((result) => result.session),

  setArchived: (id: string, archived: boolean): Promise<SessionSummary> =>
    request<z.infer<typeof SessionEnvelopeSchema>>(
      `/api/sessions/${encodeURIComponent(id)}`,
      SessionEnvelopeSchema,
      {
        method: "PATCH",
        body: JSON.stringify({ archived }),
      },
    ).then((result) => result.session),

  remove: (id: string): Promise<void> =>
    request<void>(`/api/sessions/${encodeURIComponent(id)}`, VoidSchema, { method: "DELETE" }),

  // Turns a saved workflow, an edit to it, and the ended run that prompted the edit into THIS
  // session's own draft. The parent is read and never written, and so is the cited run. Nothing is
  // saved into the library and no run starts: what comes back is a proposal to read and save.
  adapt: (id: string, body: AdaptBody): Promise<{ ok: true }> =>
    request<{ ok: true }>(
      `/api/sessions/${encodeURIComponent(id)}/adapt`,
      z.object({ ok: z.literal(true) }),
      {
        method: "POST",
        body: JSON.stringify({
          workflow_id: body.workflowId,
          ops: body.ops,
          ...(body.sourceRunId === "" ? {} : { source_run_id: body.sourceRunId }),
          ...(body.feedback === "" ? {} : { feedback: body.feedback }),
        }),
      },
    ),

  projects: (): Promise<ProjectView[]> =>
    request<z.infer<typeof ProjectListSchema>>("/api/projects", ProjectListSchema).then(
      (result) => result.projects,
    ),
};

/** One adaptation as the browser posts it: the parent, the typed edit, and why it is being made. */
export interface AdaptBody {
  workflowId: string;
  /** The edits to apply, as data. An adaptation with none is not an adaptation and is refused. */
  ops: readonly Record<string, JsonValue>[];
  /** The ended run this version exists because of. Empty cites none. */
  sourceRunId: string;
  /** The owner's own reason, in their words. Recorded beside the run, graded by nothing. */
  feedback: string;
}

/**
 * How a start ended: in a run, or in a question nobody here can answer alone.
 *
 * `unknown` is a start request that never got an answer. The run may have been made and may not
 * have been, so the only honest next move is to read the runs and see, which is what
 * `reconcileRun` does. It is never a reason to send the start again.
 */
export type LaunchOutcome =
  | { readonly state: "started"; readonly sessionId: string }
  | { readonly state: "unknown"; readonly sessionId: string; readonly reason: string };

/** What an ambiguous failure says, when the failure itself says anything at all. */
function ambiguousReason(error: unknown): string {
  const said = error instanceof Error ? error.message.trim() : "";
  return said === "" ? "The start was sent and nothing answered." : `The start was sent and nothing answered. ${said}`;
}

/**
 * Starts one resolved launch, and answers with the session it now runs in.
 *
 * Three calls in order: a runner session of its own, its name, then the run. `sessionTitle` names
 * the container the run lives in (a session can host more than one), while the intent's own
 * `runTitle` names this run. Nothing here is called until an intent exists, so a setup a person
 * backed out of leaves no session behind.
 *
 * A refusal the backend actually answered with is thrown, because it is something to correct and
 * show. A start that got no answer at all is a different thing: whether the run was made is
 * unknown from here, so this reports the session it would be in rather than throwing, and never
 * sends the start a second time on its own.
 */
export async function launchWorkflowRun(
  intent: RunIntent,
  sessionTitle: string,
): Promise<LaunchOutcome> {
  const created = await sessionsApi.create(undefined, "runner");
  await sessionsApi.rename(created.id, sessionTitle);
  try {
    await sessionsApi.run(
      created.id,
      intent.workflowId,
      intent.arguments,
      intent.gateMode,
      intent.runTitle,
    );
  } catch (error) {
    if (error instanceof ApiError) throw error;
    return { state: "unknown", sessionId: created.id, reason: ambiguousReason(error) };
  }
  return { state: "started", sessionId: created.id };
}

/** A run found while settling an unknown start: still going, or already ended. */
export interface ReconciledRun {
  readonly sessionId: string;
  readonly workflowId: string;
  /** The archived run's own id, `null` for one still in flight, which has no record yet. */
  readonly runId: string | null;
  readonly live: boolean;
}

/** How far back the recent history is read when settling one unknown start. */
const RECONCILE_LIMIT = 20;

/**
 * Whether the run a lost start would have made actually exists, read off the runs themselves.
 *
 * Two reads, live first: the pool this process holds says what is in flight, and the recent
 * history says what has already ended. `null` is both reads answering that this session has no
 * run, which means the start never landed. Reading settles the question and starts nothing.
 */
export async function reconcileRun(sessionId: string): Promise<ReconciledRun | null> {
  const live = await sessionsApi.runs();
  const inFlight = live.find((view) => view.session_id === sessionId);
  if (inFlight !== undefined) {
    return {
      sessionId,
      workflowId: inFlight.workflow_id,
      runId: null,
      live: true,
    };
  }
  const recent = await studioApi.recentRuns(RECONCILE_LIMIT);
  const ended = recent.find((record) => record.session_id === sessionId);
  return ended === undefined
    ? null
    : { sessionId, workflowId: ended.workflow_id, runId: ended.run_id, live: false };
}

// Parses a transcript message's raw `payload` (pydantic-ai's ModelMessage JSON) against
// the tolerant schema. `null` when the payload doesn't even have the request/response
// shape (an empty fixture, a backend contract drift) -- callers treat that message as
// contributing nothing rather than throwing.
export function parseTranscriptPayload(payload: unknown): MessagePayload | null {
  const parsed = MessagePayloadSchema.safeParse(payload);
  return parsed.success ? parsed.data : null;
}

// The tool-return content each `tool-call` on a response payload settles to, keyed by
// `tool_call_id` -- a request payload's own `tool-return` parts are the only source, so this
// is collected in one pass over the whole transcript before any bubble is built.
function toolReturnsByCallId(messages: TranscriptMessage[]): Map<string, unknown> {
  const returns = new Map<string, unknown>();
  for (const message of messages) {
    if (message.kind !== "request") {
      continue;
    }
    const payload = parseTranscriptPayload(message.payload);
    if (payload === null) {
      continue;
    }
    for (const part of payload.parts) {
      if (part.part_kind === "tool-return" && part.tool_call_id !== undefined) {
        returns.set(part.tool_call_id, part.content);
      }
    }
  }
  return returns;
}

// When each `tool-return` part landed, keyed by the call it answers. A part that carries no
// timestamp leaves its call absent here rather than mapped to an empty string.
function toolReturnTimestampsByCallId(messages: TranscriptMessage[]): Map<string, string> {
  const timestamps = new Map<string, string>();
  for (const message of messages) {
    if (message.kind !== "request") {
      continue;
    }
    const payload = parseTranscriptPayload(message.payload);
    if (payload === null) {
      continue;
    }
    for (const part of payload.parts) {
      if (
        part.part_kind === "tool-return" &&
        part.tool_call_id !== undefined &&
        typeof part.timestamp === "string"
      ) {
        timestamps.set(part.tool_call_id, part.timestamp);
      }
    }
  }
  return timestamps;
}

// Every tool call's own id on a response payload, in call order, whichever tool it named -- the
// step's usage is what the model response spent making all of them at once, so the step names
// every one of them, not only the ones the bubble renders as an act or a card.
function responseToolCallIds(payload: MessagePayload): string[] {
  const ids: string[] = [];
  for (const part of payload.parts) {
    if (part.part_kind === "tool-call" && part.tool_call_id !== undefined) {
      ids.push(part.tool_call_id);
    }
  }
  return ids;
}

// One response row's own `data-step` part, built from its stored usage and timestamp, `null`
// when the row carries neither, so no figure is fabricated. `returns` rides only for a call this
// turn's own request row already answered.
function responseDataStepPart(
  payload: MessagePayload,
  toolReturnTimestamps: Map<string, string>,
): UIMessage["parts"][number] | null {
  if (payload.usage === undefined || typeof payload.timestamp !== "string") {
    return null;
  }
  const toolCallIds = responseToolCallIds(payload);
  const returns: Record<string, string> = {};
  for (const toolCallId of toolCallIds) {
    const timestamp = toolReturnTimestamps.get(toolCallId);
    if (timestamp !== undefined) {
      returns[toolCallId] = timestamp;
    }
  }
  return {
    type: "data-step",
    data: {
      tool_call_ids: toolCallIds,
      usage: payload.usage,
      timestamp: payload.timestamp,
      ...(Object.keys(returns).length === 0 ? {} : { returns }),
    },
  };
}

// What a companion call with no return in the transcript reads back as. The browser owed it an
// answer, the record holds none, and nothing here can give one now.
export const NEVER_ANSWERED = "pending";

// One turn of the stored transcript: the request row whose words opened it (`null` when the
// transcript starts mid-turn) and every row of the turn in order, that row included. A person's
// words are the only boundary. A turn the person answered mid-way (an approval, a verb's
// outcome) resumes on a new run whose first request carries the tool returns and no words, so
// a run change alone never opens a turn.
export interface TranscriptTurn {
  prompt: TranscriptMessage | null;
  rows: TranscriptMessage[];
}

export function transcriptTurns(messages: TranscriptMessage[]): TranscriptTurn[] {
  const turns: TranscriptTurn[] = [];
  let current: TranscriptTurn | null = null;
  for (const message of messages) {
    const opens = message.kind === "request" && personsWords(message) !== "";
    if (opens || current === null) {
      current = { prompt: opens ? message : null, rows: [] };
      turns.push(current);
    }
    current.rows.push(message);
  }
  return turns;
}

// The person's own words on a request row, fence stripped: its parsed user-prompt parts, or
// the row's own text column when the payload will not parse, so a lost payload never merges
// two turns into one.
function personsWords(message: TranscriptMessage): string {
  const payload = parseTranscriptPayload(message.payload);
  return stripPageContext(payload === null ? message.text : userPromptText(payload));
}

function userPromptText(payload: MessagePayload): string {
  return payload.parts
    .filter((part) => part.part_kind === "user-prompt" && typeof part.content === "string")
    .map((part) => String(part.content))
    .join("");
}

// A turn's opening row -> one user bubble, from its user-prompt parts alone, with any
// page-context fence stripped so the bubble shows the person's own words. A turn's response
// rows -> one assistant bubble, the same shape the live wire streams: a `step-start` part opens
// each response, then its text (untouched) and one `tool-{name}` part per companion tool call it
// made, each carrying its own return (or the never-answered marker when the transcript has
// none). A response with none of those parts opens no step, and a tool return or a retry-prompt
// never reaches the main thread, nor does a call to a tool that isn't the companion's own. A
// user bubble's id is `${run_id}:${idx}` of its row. An assistant bubble's is `${run_id}:${idx}`
// of the last response row that gave it a part, so the inspector can find that row and walk the
// turn it sits in.
export function transcriptToUiMessages(messages: TranscriptMessage[]): UIMessage[] {
  const toolReturns = toolReturnsByCallId(messages);
  const toolReturnTimestamps = toolReturnTimestampsByCallId(messages);
  const result: UIMessage[] = [];
  for (const turn of transcriptTurns(messages)) {
    const prompt = turn.prompt === null ? null : parseTranscriptPayload(turn.prompt.payload);
    if (turn.prompt !== null && prompt !== null) {
      const text = stripPageContext(userPromptText(prompt));
      result.push({
        id: `${turn.prompt.run_id}:${turn.prompt.idx}`,
        role: "user",
        parts: [{ type: "text", text }],
      });
    }
    const parts: UIMessage["parts"] = [];
    let lastRow: TranscriptMessage | null = null;
    for (const row of turn.rows) {
      if (row.kind !== "response") {
        continue;
      }
      const payload = parseTranscriptPayload(row.payload);
      if (payload === null) {
        continue;
      }
      const stepParts = responseStepParts(payload, toolReturns);
      if (stepParts.length === 0) {
        continue;
      }
      const dataStep = responseDataStepPart(payload, toolReturnTimestamps);
      parts.push({ type: "step-start" }, ...(dataStep === null ? [] : [dataStep]), ...stepParts);
      lastRow = row;
    }
    if (lastRow !== null) {
      result.push({ id: `${lastRow.run_id}:${lastRow.idx}`, role: "assistant", parts });
    }
  }
  return result;
}

// One response row's own parts, in order: its text (joined), then its companion tool calls.
function responseStepParts(
  payload: MessagePayload,
  toolReturns: Map<string, unknown>,
): UIMessage["parts"] {
  const texts: string[] = [];
  const toolParts: UIMessage["parts"] = [];
  for (const part of payload.parts) {
    if (part.part_kind === "text" && typeof part.content === "string") {
      texts.push(part.content);
    }
    if (
      part.part_kind === "tool-call" &&
      part.tool_name !== undefined &&
      part.tool_name !== null &&
      part.tool_call_id !== undefined &&
      isCompanionTool(part.tool_name)
    ) {
      const returned = toolReturns.get(part.tool_call_id);
      toolParts.push({
        type: `tool-${part.tool_name}`,
        toolCallId: part.tool_call_id,
        state: "output-available",
        input: part.args,
        output: returned === undefined ? NEVER_ANSWERED : returned,
      });
    }
  }
  const text = texts.join("");
  return text === "" ? toolParts : [{ type: "text", text }, ...toolParts];
}
