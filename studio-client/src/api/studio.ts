import { ApiError, createApiClient } from "@/shared/api/client";

import { z } from "zod";
import { STUDIO_API_URL } from "./studio-config";
import {
  type ArtifactDocument,
  ArtifactDocumentSchema,
  type ArtifactRef,
  type Automode,
  AutomodeSchema,
  type Approved,
  ApprovedSchema,
  type Conversation,
  ConversationSchema,
  ConversationStartedSchema,
  type Decided,
  DecidedSchema,
  type Step,
  StepSchema,
  type LibraryEntry,
  LibraryEntrySchema,
  OkSchema,
  type OwnerReviewResult,
  OwnerReviewResultSchema,
  type OwnerReviewSubmission,
  type Pending,
  PendingSchema,
  type RunEnvelope,
  RunEnvelopeSchema,
  type RunHistoryRecord,
  RunHistoryRecordSchema,
  type RunStarted,
  RunStartedSchema,
  type Readiness,
  type RunLimits,
  RunLimitsSchema,
  type RunState,
  RunStateSchema,
  type Saved,
  SavedSchema,
  SaveRefusedSchema,
  type WorkflowCard,
  WorkflowCardSchema,
  type WorkflowDetail,
  WorkflowDetailSchema,
  type WorkflowPresentationPatch,
} from "./studio-types";

// The Workflow studio's typed API surface, over the studio agent backend,
// `studio.agent` (:8770). Same shape as `knowledgeBaseApi`: the shared API client supplies
// request/parse/error, this module injects the base URL and builds the endpoint object.
// Local-only and unauthenticated, like the rest of the dashboard.

const { request } = createApiClient({ baseUrl: STUDIO_API_URL });

const LibraryListingSchema = z.array(LibraryEntrySchema);
const StepsSchema = z.array(StepSchema);

// Pause and resume answer with the same `{ok}` shape as everything else, plus which
// workflow the act landed on when it succeeded, absent when it did not.
const PausedSchema = z.object({ ok: z.boolean(), paused: z.string().optional() });
type Paused = z.infer<typeof PausedSchema>;
const ResumedSchema = z.object({ ok: z.boolean(), running: z.string().optional() });
type Resumed = z.infer<typeof ResumedSchema>;

const RunEnvelopeListSchema = z.object({ runs: z.array(RunEnvelopeSchema) });
const RunPinnedSchema = z.object({ ok: z.literal(true), pinned: z.boolean() });
type RunPinned = z.infer<typeof RunPinnedSchema>;
const RunDeletedSchema = z.object({ ok: z.literal(true) });
type RunDeleted = z.infer<typeof RunDeletedSchema>;

function queryString(params: Record<string, number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) {
      search.set(key, String(value));
    }
  }
  const query = search.toString();
  return query ? `?${query}` : "";
}

// A refusal's field-by-field reading, when the failure carried one. `null` for every other kind
// of failure (a network drop, a 500), so a form shows the sentence alone rather than claiming the
// backend said something about a field.
export function refusedReadiness(error: unknown): Readiness | null {
  if (!(error instanceof ApiError)) {
    return null;
  }
  try {
    const parsed = SaveRefusedSchema.safeParse(JSON.parse(error.message));
    return parsed.success ? (parsed.data.readiness ?? null) : null;
  } catch {
    return null;
  }
}

// The backend answers failures as JSON with a human sentence in `detail` or
// `message`. The shared API client throws the raw body as ApiError.message. This is the
// one place that unwraps it, so every view shows the sentence, never the soup.
// A FastAPI 422 carries `detail` as an ARRAY of issue objects, not a sentence —
// only a string may pass through, or React is handed an object as a child.
export function studioErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    try {
      const body = JSON.parse(error.message) as { detail?: unknown; message?: unknown };
      if (typeof body.detail === "string") {
        return body.detail;
      }
      if (typeof body.message === "string") {
        return body.message;
      }
      return error.message;
    } catch {
      return error.message;
    }
  }
  return error instanceof Error ? error.message : String(error);
}

export const studioApi = {
  // `archived` false (the default) lists exactly the live library; true lists the archived cards
  // instead.
  library: (options: { archived?: boolean } = {}): Promise<LibraryEntry[]> =>
    request<LibraryEntry[]>(
      `/api/library${options.archived ? "?archived=true" : ""}`,
      LibraryListingSchema,
    ),

  workflow: (id: string): Promise<WorkflowDetail> =>
    request<WorkflowDetail>(`/api/workflow/${encodeURIComponent(id)}`, WorkflowDetailSchema),

  // Rewrites one artifact's card: its title, summary, applicability, and the author's own
  // sentences. Never the spec -- that is an adaptation, through `save`/`adapt`, not this. A field
  // left out is left alone, one level down as well, so a form may send the one box it owns.
  // Answers with the card re-read off the library, and with the backend's own reading of what the
  // card still lacks.
  redescribeWorkflow: (
    id: string,
    card: {
      title?: string;
      summary?: string;
      applicability?: string;
      presentation?: WorkflowPresentationPatch;
    },
  ): Promise<WorkflowCard> =>
    request<WorkflowCard>(`/api/workflow/${encodeURIComponent(id)}/card`, WorkflowCardSchema, {
      method: "PATCH",
      body: JSON.stringify(card),
    }),

  steps: (): Promise<Step[]> => request<Step[]>("/api/steps", StepsSchema),

  conversation: (): Promise<Conversation> =>
    request<Conversation>("/api/conversation", ConversationSchema),

  startConversation: (): Promise<{ session_id: string }> =>
    request<{ session_id: string }>("/api/conversation", ConversationStartedSchema, {
      method: "POST",
    }),

  forget: (): Promise<{ ok: boolean }> =>
    request("/api/conversation", OkSchema, { method: "DELETE" }),

  save: (): Promise<Saved> => request<Saved>("/api/save", SavedSchema, { method: "POST" }),

  run: (workflowId: string, args: Record<string, string>): Promise<RunStarted> =>
    request<RunStarted>("/api/run", RunStartedSchema, {
      method: "POST",
      body: JSON.stringify({ workflow_id: workflowId, arguments: args }),
    }),

  runState: (since = 0): Promise<RunState> =>
    request<RunState>(`/api/run/state?since=${since}`, RunStateSchema),

  // What a run started here is held to, read by a preview before anybody starts one: every money
  // ceiling this deployment mounts, and the gate modes a launch may ask for. No brakes means none
  // is configured, never a ceiling of zero.
  runLimits: (): Promise<RunLimits> => request<RunLimits>("/api/run/limits", RunLimitsSchema),

  pending: (): Promise<Pending> => request<Pending>("/api/pending", PendingSchema),

  // `feedback` is read only on a rejection -- approving releases the value regardless of
  // what was typed alongside it.
  decide: (approve: boolean, feedback?: string): Promise<Decided> =>
    request<Decided>("/api/decide", DecidedSchema, {
      method: "POST",
      body: JSON.stringify(feedback === undefined ? { approve } : { approve, feedback }),
    }),

  // Flips the live run's own automatic-approval flag, refused when nothing is running.
  setAutomode: (on: boolean): Promise<Automode> =>
    request<Automode>("/api/run/automode", AutomodeSchema, {
      method: "POST",
      body: JSON.stringify({ on }),
    }),

  pause: (): Promise<Paused> => request<Paused>("/api/run/pause", PausedSchema, { method: "POST" }),

  resume: (): Promise<Resumed> =>
    request<Resumed>("/api/run/resume", ResumedSchema, {
      method: "POST",
      body: JSON.stringify({}),
    }),

  // Ended runs of one workflow, newest first; the live run (if any) prepended
  // with outcome "live".
  workflowHistory: (id: string, limit?: number): Promise<RunEnvelope[]> =>
    request<z.infer<typeof RunEnvelopeListSchema>>(
      `/api/workflows/${encodeURIComponent(id)}/history${queryString({ limit })}`,
      RunEnvelopeListSchema,
    ).then((result) => result.runs),

  // Ended runs across every workflow, newest first.
  recentRuns: (limit?: number): Promise<RunEnvelope[]> =>
    request<z.infer<typeof RunEnvelopeListSchema>>(
      `/api/runs/recent${queryString({ limit })}`,
      RunEnvelopeListSchema,
    ).then((result) => result.runs),

  // One archived run's full record: envelope plus its spec, rendered values,
  // and transition events.
  runRecord: (runId: string): Promise<RunHistoryRecord> =>
    request<RunHistoryRecord>(
      `/api/runs/history/${encodeURIComponent(runId)}`,
      RunHistoryRecordSchema,
    ),

  // The value one reference addresses, out of the run's own immutable record. The reference goes
  // in the body exactly as the run minted it; there is no URL form of this question, and the host
  // answers with those bytes or refuses.
  resolveArtifact: (runId: string, ref: ArtifactRef): Promise<ArtifactDocument> =>
    request<ArtifactDocument>(
      `/api/runs/history/${encodeURIComponent(runId)}/artifacts/resolve`,
      ArtifactDocumentSchema,
      { method: "POST", body: JSON.stringify({ ref }) },
    ),

  // Records one person's accept or reject decision over references this run fixed. The host
  // re-checks every reference against the run's own record before it writes anything, so a
  // reference it can no longer confirm is a refusal rather than a review of some newer value.
  // `review_id` is the caller's own key: repeating a submission whole returns the event already
  // written (`created: false`), and the same key over changed content is refused.
  recordOwnerReview: (
    runId: string,
    submission: OwnerReviewSubmission,
  ): Promise<OwnerReviewResult> =>
    request<OwnerReviewResult>(
      `/api/runs/history/${encodeURIComponent(runId)}/review`,
      OwnerReviewResultSchema,
      { method: "POST", body: JSON.stringify(submission) },
    ),

  pinRun: (runId: string, pinned: boolean): Promise<RunPinned> =>
    request<RunPinned>(`/api/runs/history/${encodeURIComponent(runId)}`, RunPinnedSchema, {
      method: "PATCH",
      body: JSON.stringify({ pinned }),
    }),

  // Explicit owner delete, pinned or not.
  deleteRun: (runId: string): Promise<RunDeleted> =>
    request<RunDeleted>(`/api/runs/history/${encodeURIComponent(runId)}`, RunDeletedSchema, {
      method: "DELETE",
    }),

  // The owner's own gesture: approve a saved workflow by name, with an optional reason.
  // Answers with the trust record the approved version now carries.
  approve: (workflowId: string, by: string, note = ""): Promise<Approved> =>
    request<Approved>(`/api/workflows/${encodeURIComponent(workflowId)}/approve`, ApprovedSchema, {
      method: "POST",
      body: JSON.stringify({ by, note }),
    }),

  // Archives a saved artifact: it leaves the library and search, its runs and versions stay.
  // Answers with the refreshed card.
  archiveWorkflow: (workflowId: string): Promise<WorkflowCard> =>
    request<WorkflowCard>(
      `/api/workflow/${encodeURIComponent(workflowId)}/archive`,
      WorkflowCardSchema,
      { method: "POST" },
    ),

  // Brings an archived artifact back into the library. Answers with the refreshed card.
  restoreWorkflow: (workflowId: string): Promise<WorkflowCard> =>
    request<WorkflowCard>(
      `/api/workflow/${encodeURIComponent(workflowId)}/restore`,
      WorkflowCardSchema,
      { method: "POST" },
    ),
};
